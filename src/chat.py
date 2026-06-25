"""reclaudable turn driver: read newest page in a Claude-folder notebook, get Claude's
reply, append it as a new page.

  python src/chat.py [notebook-uuid]

Per-notebook state in state/<uuid>.json:
  session_id  - Claude session to --resume (delta = only the new page)
  handled     - "pageUUID:blobHash" already processed, so we never re-answer a
                page or reply to our own output

The reply persona lives in persona.md; the reply-Claude runs in an isolated cwd
(CLAUDE_CWD) so this repo's CLAUDE.md never leaks into its context.
"""
from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path

import mailer
import rmstore as R
import writeback as W
from config import CLAUDE_BIN, CLAUDE_FOLDER, CLAUDE_CWD, EMAIL_TO, MODEL_LABEL  # host config; see .env

# Repo root (this module lives in src/); state/, renders/ and persona.md sit there.
ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
RENDER_DIR = ROOT / "renders"
SKETCH_DIR = RENDER_DIR / "sketches"   # cropped user sketches, per-notebook subdir
BLANK_RM_MAX_BYTES = 1000   # .rm files smaller than this carry no strokes

# The reMarkable assistant's behaviour — edit persona.md to change it.
PERSONA = (ROOT / "persona.md").read_text().strip()

# MODEL_LABEL is shown in each reply's frame (writeback adds the "model ·
# timestamp" line). It is not parsed from the result JSON — set it in .env to
# match the backend model.

# Label on pages we write to report a failed turn — distinct from a real reply,
# and written directly (bypasses the model, so no extra cost).
ERROR_LABEL = "reclaudable error"

# Label on the placeholder page written when the backend is rate-limited. The
# placeholder is later rewritten IN PLACE (writeback.replace_page) into the real
# reply once the limit resets — same slot, so it visibly turns into the answer.
PENDING_LABEL = "reclaudable · waiting"

# The reply-Claude emits this (and nothing else) when the page is an unfinished
# draft or carries no request — so we don't answer a page that synced mid-edit.
WAIT_SENTINEL = "<<WAIT>>"

# CLAUDE_CWD (from .env) runs the reply-Claude OUTSIDE the repo tree, so it never
# auto-loads this project's CLAUDE.md (coding instructions) as context — only
# `persona.md` via --append-system-prompt shapes replies. It must be a STABLE
# path: Claude Code keys resumable sessions by working directory.


def _state_path(u: str) -> Path:
    return STATE_DIR / f"{u}.json"


def load_state(u: str) -> dict:
    p = _state_path(u)
    s = json.loads(p.read_text()) if p.exists() else {}
    s.setdefault("handled", [])   # "pageUUID:blobHash" already processed
    return s


def save_state(u: str, s: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    _state_path(u).write_text(json.dumps(s, indent=2))


class RateLimited(Exception):
    """Backend (Claude Code on Pro) hit its usage/session limit."""


def _run_claude(prompt: str, resume: str | None = None) -> dict:
    """Run one headless `claude -p` call with the reply persona, parse its JSON
    result, and translate errors (429 -> RateLimited). Shared by the turn loop
    (call_claude) and session priming (prime_session)."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json",
           "--permission-mode", "bypassPermissions",
           "--allowedTools", "Read,WebSearch,WebFetch",
           "--append-system-prompt", PERSONA]
    if resume:
        cmd += ["--resume", resume]
    CLAUDE_CWD.mkdir(parents=True, exist_ok=True)
    # Headroom for large turns: an exhaustive emailed report is a big generation.
    out = subprocess.run(cmd, input=prompt, text=True, capture_output=True,
                         timeout=600, cwd=CLAUDE_CWD)
    # claude prints a JSON result even on error; parse it for a clean signal.
    try:
        data = json.loads(out.stdout)
    except (json.JSONDecodeError, ValueError):
        raise RuntimeError(f"claude failed: {out.stderr or out.stdout}")
    if data.get("is_error"):
        if data.get("api_error_status") == 429:
            raise RateLimited(data.get("result", "session limit"))
        raise RuntimeError(f"claude error: {data.get('result')}")
    return data


def _sketch_manifest(sketches: list[dict] | None) -> str:
    """One-line summary of sketches captured so far, for the turn prompt — so a
    later 'email me a report' turn knows which crops it can reference. Empty when
    none captured yet."""
    items = sketches or []
    if not items:
        return ""
    listed = ", ".join(f'{s["id"]}={s.get("caption") or "sketch"}' for s in items)
    return ("Sketches captured from earlier pages that you may reference in an "
            "emailed report (only when relevant) as ![caption](sketch:ID): " + listed)


def call_claude(png: Path, strokes_png: Path, resume: str | None,
                sketches: list[dict] | None = None) -> dict:
    prompt = (
        f"New or edited handwritten page from the user. Read the image at {png} — "
        f"that is the page exactly as the user sees it, including any earlier typed "
        f"reply of yours they may have annotated on top of (read those annotations "
        f"in context). A SECOND image at {strokes_png} shows the SAME page with the "
        f"typed text removed, leaving only pen strokes — the user's handwriting and "
        f"any drawing. Read it too: use it to spot and precisely locate any "
        f"sketch/drawing the user made, which can be nearly invisible in the first "
        f"image when drawn over text. Any <<SKETCH>> bbox you give is in page "
        f"fractions and both images share the same canvas. If the page is an "
        f"unfinished draft or has no request for you, reply with exactly "
        f"{WAIT_SENTINEL} and nothing else. Otherwise respond per your instructions.")
    manifest = _sketch_manifest(sketches)
    if manifest:
        prompt += "\n\n" + manifest
    return _run_claude(prompt, resume)


def _render_strokes(page_bytes: bytes, strokes_png: Path, full_png: Path,
                    nb: R.Doc) -> Path:
    """Render the ink-only view of a page (typed text removed). Falls back to the
    full render on failure so the turn still proceeds (degraded: a drawing over text
    may be missed, but nothing breaks)."""
    from render import rm_bytes_to_strokes_png
    try:
        return rm_bytes_to_strokes_png(page_bytes, strokes_png)
    except Exception as exc:
        print(f"{nb.visible_name!r}: strokes-only render failed ({exc}) — "
              "using full render for this turn.")
        return full_png


def prime_session(summary_text: str) -> str:
    """Seed a fresh reclaudable session with a summary of an imported conversation
    and return its session_id (to store in state so the first handwritten page
    resumes it). The model just acknowledges with <<WAIT>>; we keep the session_id,
    not the reply. Raises RateLimited if the backend is throttled."""
    prompt = ("You're being handed a summary of an earlier conversation to continue "
              "inside a reMarkable notebook. Read it for context; the user will "
              "continue the thread by handwriting new pages. Do not produce a page "
              f"now — reply with exactly {WAIT_SENTINEL} and nothing else.\n\n"
              "--- SUMMARY ---\n" + summary_text)
    return _run_claude(prompt).get("session_id")


def _capture_sketches(nb: R.Doc, strokes_png: Path, reply: str, state: dict,
                      page_uuid: str) -> str:
    """Crop any `<<SKETCH …>>` regions the model flagged out of `strokes_png` (the
    STROKES-ONLY page render — Claude's typed reply text removed, so an annotation
    drawn over a reply yields a clean sketch), stash them under
    renders/sketches/<uuid>/, record them in state["sketches"], and return the prose
    with the SKETCH tags stripped (so they never reach the page). A crop failure
    skips just that sketch."""
    import sketch as S

    prose, specs = S.parse_sketch(reply)
    if not specs:
        return prose
    captured = state.setdefault("sketches", [])
    next_id = max((int(s["id"]) for s in captured
                   if str(s.get("id", "")).isdigit()), default=0) + 1
    sk_dir = SKETCH_DIR / nb.uuid
    sk_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        sid = str(next_id)
        next_id += 1
        out = sk_dir / f"{sid}.png"
        try:
            S.crop_page(strokes_png, spec.bbox, out)
        except Exception as exc:
            print(f"{nb.visible_name!r}: sketch {sid} crop failed ({exc}) — skipping.")
            continue
        captured.append({"id": sid, "caption": spec.caption,
                         "file": str(out), "page_uuid": page_uuid})
        print(f"{nb.visible_name!r}: captured sketch {sid} ({spec.caption!r}).")
    return prose


def _deliver(nb: R.Doc, reply: str, state: dict) -> str:
    """If the reply carries an `<<EMAIL>>…<<END>>` block, send it and return the
    page prose (block stripped). On send failure, append a short note to the prose
    so the device still shows a reply explaining the email didn't go out. Replies
    with no email block pass through unchanged. Captured sketches the report
    references are embedded by the mailer."""
    prose, spec = mailer.parse_email(reply)
    if spec is None:
        return reply
    try:
        to = mailer.send_report(spec, to=EMAIL_TO, notebook_name=nb.visible_name,
                                sketches=state.get("sketches"))
        print(f"{nb.visible_name!r}: emailed report to {to}")
    except Exception as exc:
        detail = next((ln for ln in str(exc).splitlines() if ln.strip()),
                      exc.__class__.__name__)
        print(f"{nb.visible_name!r}: email send failed: {detail}")
        prose += ("\n\n(Note: I couldn't send the email just now — "
                  f"{detail.strip()[:160]}. The report is ready; ask again to retry.)")
    return prose


def main() -> None:
    docs = R.load_docs()
    folder = R.find_folder(CLAUDE_FOLDER, docs)
    if folder is None:
        sys.exit(f"No {CLAUDE_FOLDER!r} folder.")

    if len(sys.argv) > 1:
        nbs = [docs[sys.argv[1]]]
    else:
        nbs = [d for d in R.children_of(folder.uuid, docs)
               if d.doc_type == "DocumentType" and d.page_rm_files]
    if not nbs:
        sys.exit("No notebooks with pages in Claude folder.")

    try:
        for nb in nbs:
            process_notebook(nb)
    except RateLimited as e:
        print(f"backend rate-limited: {e} — try again after the reset.")
        sys.exit(2)


def process_notebook(nb: R.Doc) -> bool:
    """Run one turn for nb if its newest page is new input. Returns True if a reply
    was appended. Raises RateLimited if the backend is throttled."""
    from render import rm_bytes_to_png, rm_bytes_to_strokes_png

    state = load_state(nb.uuid)
    if state.get("importing"):
        # import_nb.py wrote this flag before uploading the seeded notebook and
        # clears it once the page is marked handled + the session primed. Skip until
        # then so we never auto-reply to an import mid-creation.
        print(f"{nb.visible_name!r}: import in progress — skip.")
        return False
    if state.get("pending"):
        # A rate-limited turn left a placeholder page owed a real answer. Resolve
        # that first (rewrite it in place) before looking at any newer page.
        return _retry_pending(nb, state)

    pages = R.ordered_pages(nb)                 # (pageUUID, hash) in page order
    # Skip trailing blank pages (reMarkable keeps an empty page ready to write on;
    # a blank Paper Pro .rm is ~400 bytes). Target the last page with content.
    while pages and len(R.read_blob(pages[-1][1])) < BLANK_RM_MAX_BYTES:
        print(f"{nb.visible_name!r}: page {pages[-1][0][:8]} is blank — skip.")
        pages = pages[:-1]
    if not pages:
        return False
    page_uuid, page_hash = pages[-1]            # newest page with content
    key = f"{page_uuid}:{page_hash}"
    if key in state["handled"]:
        print(f"{nb.visible_name!r}: newest page unchanged — skip.")
        return False

    print(f"{nb.visible_name!r}: responding to page {page_uuid}")
    try:
        RENDER_DIR.mkdir(exist_ok=True)
        png = RENDER_DIR / f"{nb.uuid}.png"
        strokes_png = RENDER_DIR / f"{nb.uuid}.strokes.png"
        page_bytes = R.read_blob(page_hash)
        rm_bytes_to_png(page_bytes, png)                 # full page: read annotations
        strokes_png = _render_strokes(page_bytes, strokes_png, png, nb)  # ink only

        result = call_claude(png, strokes_png, state.get("session_id"),
                             sketches=state.get("sketches"))
        reply = result.get("result", "").strip()

        # The page isn't a request yet (unfinished draft, or no ask) — don't reply.
        # Mark this hash handled so we don't re-read it until the page changes; leave
        # session_id untouched so the ephemeral skip stays out of the conversation.
        if reply.upper().startswith(WAIT_SENTINEL):
            print(f"{nb.visible_name!r}: page not a request yet — waiting for a trigger.")
            state["handled"].append(key)
            save_state(nb.uuid, state)
            return False

        print("\n----- reply -----\n" + reply + "\n-----------------")

        prose = _capture_sketches(nb, strokes_png, reply, state, page_uuid)
        prose = _deliver(nb, prose, state)
        W.append_page(nb.uuid, prose, folder=CLAUDE_FOLDER,
                      visible_name=nb.visible_name, model_label=MODEL_LABEL)

        # record the answered page AND our freshly-created page so neither retriggers.
        state["session_id"] = result.get("session_id")
        state["handled"].append(key)
        _record_created_page(nb.uuid, state)
        save_state(nb.uuid, state)
        print(f"done. cost ${result.get('total_cost_usd', 0):.4f}")
        return True
    except RateLimited as exc:
        # Backend is throttled. Surface a placeholder page so the device isn't
        # silent, remember the page we owe an answer to, then re-raise so the
        # watcher backs off. The retry path (driven by state["pending"]) rewrites
        # this placeholder in place once the limit resets.
        _surface_rate_limit(nb, state, key, exc)
        raise   # transient: let the watcher back off and retry after the reset
    except Exception as err:
        print(f"{nb.visible_name!r}: failed to answer page:\n{traceback.format_exc()}")
        _surface_error(nb, state, key, err)
        return False


def _record_created_page(uuid: str, state: dict) -> None:
    """Mark the page we just appended as handled, so the watcher never reads our
    own output back as new input. Re-reads one notebook, not the whole store."""
    fresh_doc = R.load_doc(uuid)
    fresh = R.ordered_pages(fresh_doc) if fresh_doc else []
    if fresh:
        np_uuid, np_hash = fresh[-1]
        state["handled"].append(f"{np_uuid}:{np_hash}")


def _rate_limit_message(exc: Exception) -> str:
    detail = next((ln for ln in str(exc).splitlines() if ln.strip()), "session limit")
    return ("I'm rate-limited on the backend right now, so I can't answer this page "
            "yet:\n\n"
            f"{detail.strip()[:200]}\n\n"
            "This page is a placeholder — I'll rewrite it in place with the real "
            "reply as soon as the limit resets. Nothing you wrote was lost; no need "
            "to do anything.")


def _surface_rate_limit(nb: R.Doc, state: dict, key: str, exc: Exception) -> None:
    """Write a 'waiting' placeholder page and record the page we still owe an
    answer to, so the retry path can rewrite it in place once the limit resets.
    Best-effort: if even the placeholder write fails, fall back to silent backoff
    (no pending state) rather than throwing over the original RateLimited."""
    try:
        placeholder = W.append_page(nb.uuid, _rate_limit_message(exc),
                                    folder=CLAUDE_FOLDER, visible_name=nb.visible_name,
                                    model_label=PENDING_LABEL)
        state["pending"] = {"source_key": key, "page": placeholder}
        state["handled"].append(key)
        _record_created_page(nb.uuid, state)   # placeholder is newest -> mark handled
        save_state(nb.uuid, state)
        print(f"{nb.visible_name!r}: rate-limited — wrote placeholder page {placeholder[:8]}.")
    except Exception:
        print(f"{nb.visible_name!r}: could not write rate-limit placeholder:\n"
              f"{traceback.format_exc()}")


def _retry_pending(nb: R.Doc, state: dict) -> bool:
    """Re-answer the page a rate-limited turn left pending, rewriting its
    placeholder page IN PLACE. Returns True if a real reply was written. Raises
    RateLimited (placeholder kept) so the watcher keeps backing off."""
    from render import rm_bytes_to_png

    pending = state["pending"]
    source_uuid, _, source_hash = pending["source_key"].partition(":")
    placeholder = pending["page"]
    print(f"{nb.visible_name!r}: retrying pending page {source_uuid[:8]} "
          f"-> placeholder {placeholder[:8]}")
    try:
        RENDER_DIR.mkdir(exist_ok=True)
        png = RENDER_DIR / f"{nb.uuid}.png"
        strokes_png = RENDER_DIR / f"{nb.uuid}.strokes.png"
        page_bytes = R.read_blob(source_hash)
        rm_bytes_to_png(page_bytes, png)
        strokes_png = _render_strokes(page_bytes, strokes_png, png, nb)

        result = call_claude(png, strokes_png, state.get("session_id"),
                             sketches=state.get("sketches"))
        reply = result.get("result", "").strip()

        # The rate-limit may have fired on a page that was only a mid-edit draft.
        # If the retry now says WAIT, the placeholder was spurious — remove it.
        if reply.upper().startswith(WAIT_SENTINEL):
            print(f"{nb.visible_name!r}: pending page isn't a request — removing placeholder.")
            W.remove_page(nb.uuid, placeholder, folder=CLAUDE_FOLDER,
                          visible_name=nb.visible_name)
            state.pop("pending", None)
            save_state(nb.uuid, state)
            return False

        print("\n----- reply -----\n" + reply + "\n-----------------")
        prose = _capture_sketches(nb, strokes_png, reply, state, source_uuid)
        prose = _deliver(nb, prose, state)
        W.replace_page(nb.uuid, placeholder, prose, folder=CLAUDE_FOLDER,
                       visible_name=nb.visible_name, model_label=MODEL_LABEL)
        state["session_id"] = result.get("session_id")
        state.pop("pending", None)
        _record_created_page(nb.uuid, state)   # rewritten page has a NEW hash
        save_state(nb.uuid, state)
        print(f"done (in place). cost ${result.get('total_cost_usd', 0):.4f}")
        return True
    except RateLimited:
        raise   # still throttled: keep the placeholder, retry on the next root change
    except Exception as err:
        print(f"{nb.visible_name!r}: pending retry failed:\n{traceback.format_exc()}")
        try:   # turn the placeholder into an error page rather than appending one
            W.replace_page(nb.uuid, placeholder, _error_message(err),
                           folder=CLAUDE_FOLDER, visible_name=nb.visible_name,
                           model_label=ERROR_LABEL)
            state.pop("pending", None)
            _record_created_page(nb.uuid, state)
        except Exception:
            print(f"{nb.visible_name!r}: could not write error to placeholder:\n"
                  f"{traceback.format_exc()}")
        save_state(nb.uuid, state)
        return False


def _error_message(err: Exception) -> str:
    first = next((ln for ln in str(err).splitlines() if ln.strip()), "")
    detail = (first or err.__class__.__name__).strip()[:200]
    return ("I couldn't finish answering this page — something broke on my end:\n\n"
            f"{detail}\n\n"
            "Nothing you wrote was lost. Edit or rewrite the page to try again "
            "(that re-triggers me), or check the watcher log if it keeps happening.")


def _surface_error(nb: R.Doc, state: dict, key: str, err: Exception) -> None:
    """Write a short failure notice as a new page so the error shows on the device,
    not just in the log. Mark the offending page handled so a deterministic failure
    doesn't loop — the user re-triggers by editing the page. RateLimited never gets
    here (it's re-raised for backoff/retry); this is for terminal failures."""
    state["handled"].append(key)
    try:
        W.append_page(nb.uuid, _error_message(err), folder=CLAUDE_FOLDER,
                      visible_name=nb.visible_name, model_label=ERROR_LABEL)
        _record_created_page(nb.uuid, state)
    except Exception:
        print(f"{nb.visible_name!r}: could not write error page:\n{traceback.format_exc()}")
    save_state(nb.uuid, state)


if __name__ == "__main__":
    main()
