"""rm-llm turn driver: read newest page in a Claude-folder notebook, get Claude's
reply, append it as a new page.

  python chat.py [notebook-uuid]

Per-notebook state in state/<uuid>.json:
  session_id        - Claude session to --resume (delta = only the new page)
  generated_pages   - page UUIDs we created (so we never reply to our own output)
  answered_pages    - user page hashes already answered (idempotency)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import rmstore as R
import writeback as W

HERE = Path(__file__).parent
STATE_DIR = HERE / "state"
RENDER_DIR = HERE / "renders"
CLAUDE_FOLDER = "Claude"
BLANK_RM_MAX_BYTES = 1000   # .rm files smaller than this carry no strokes

PERSONA = (
    "You are a handwriting chat/editor assistant on a reMarkable tablet. The user "
    "writes a page by hand; your reply becomes the next page. Begin every reply "
    "with 'Transcription: \"...\"' of the handwritten input so misreads get caught. "
    "Then answer. Write in PLAIN PROSE — no markdown symbols (#, *, -, backticks), "
    "since they render literally. Keep the whole reply to about one page: complete "
    "but bounded (there is no pagination)."
)


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
           "--permission-mode", "bypassPermissions", "--allowedTools", "Read",
           "--append-system-prompt", PERSONA]
    if resume:
        cmd += ["--resume", resume]
    prompt = (f"New handwritten page from the user. Read the image at {png} "
              "and respond per your instructions.")
    out = subprocess.run(cmd, input=prompt, text=True, capture_output=True,
                         timeout=300)
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
    RENDER_DIR.mkdir(exist_ok=True)
    png = RENDER_DIR / f"{nb.uuid}.png"
    rm_bytes_to_png(R.read_blob(page_hash), png)

    result = call_claude(png, state.get("session_id"))
    reply = result.get("result", "").strip()
    print("\n----- reply -----\n" + reply + "\n-----------------")

    W.append_page(nb.uuid, reply, folder=CLAUDE_FOLDER,
                  visible_name=nb.visible_name)

    # record the answered page AND our freshly-created page so neither retriggers.
    state["session_id"] = result.get("session_id")
    state["handled"].append(key)
    fresh = R.ordered_pages(R.load_docs()[nb.uuid])
    if fresh:
        np_uuid, np_hash = fresh[-1]
        state["handled"].append(f"{np_uuid}:{np_hash}")
    save_state(nb.uuid, state)
    print(f"done. cost ${result.get('total_cost_usd', 0):.4f}")
    return True


if __name__ == "__main__":
    main()
