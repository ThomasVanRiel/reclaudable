"""reclaudable turn driver: read newest page in a Claude-folder notebook, get Claude's
reply, append it as a new page.

  python chat.py [notebook-uuid]

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

import rmstore as R
import writeback as W
from config import CLAUDE_FOLDER, CLAUDE_CWD, MODEL_LABEL  # host config; see .env

HERE = Path(__file__).parent
STATE_DIR = HERE / "state"
RENDER_DIR = HERE / "renders"
BLANK_RM_MAX_BYTES = 1000   # .rm files smaller than this carry no strokes

# The reMarkable assistant's behaviour — edit persona.md to change it.
PERSONA = (HERE / "persona.md").read_text().strip()

# MODEL_LABEL is shown in each reply's frame (writeback adds the "model ·
# timestamp" line). It is not parsed from the result JSON — set it in .env to
# match the backend model.

# Label on pages we write to report a failed turn — distinct from a real reply,
# and written directly (bypasses the model, so no extra cost).
ERROR_LABEL = "reclaudable error"

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


def call_claude(png: Path, resume: str | None) -> dict:
    cmd = ["claude", "-p", "--output-format", "json",
           "--permission-mode", "bypassPermissions",
           "--allowedTools", "Read,WebSearch,WebFetch",
           "--append-system-prompt", PERSONA]
    if resume:
        cmd += ["--resume", resume]
    prompt = (f"New or edited handwritten page from the user. Read the image at "
              f"{png}. If it is an unfinished draft or has no request for you, "
              f"reply with exactly {WAIT_SENTINEL} and nothing else. Otherwise "
              "respond per your instructions.")
    CLAUDE_CWD.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(cmd, input=prompt, text=True, capture_output=True,
                         timeout=300, cwd=CLAUDE_CWD)
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
    from render import rm_bytes_to_png

    state = load_state(nb.uuid)
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
        rm_bytes_to_png(R.read_blob(page_hash), png)

        result = call_claude(png, state.get("session_id"))
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

        W.append_page(nb.uuid, reply, folder=CLAUDE_FOLDER,
                      visible_name=nb.visible_name, model_label=MODEL_LABEL)

        # record the answered page AND our freshly-created page so neither retriggers.
        state["session_id"] = result.get("session_id")
        state["handled"].append(key)
        _record_created_page(nb.uuid, state)
        save_state(nb.uuid, state)
        print(f"done. cost ${result.get('total_cost_usd', 0):.4f}")
        return True
    except RateLimited:
        raise   # transient: let the watcher back off and retry this page later
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
