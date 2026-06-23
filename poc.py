"""Read-only proof of concept for rm-llm.

Given a notebook (by UUID, or auto-discovered inside the `claude` folder):
render its newest page -> PNG -> send to a resumed headless Claude session ->
print the reply. NO write-back yet.

Per-notebook continuity is kept by resuming the Claude session id stored in
state/<uuid>.json, so each turn only sends the NEW page (a delta).

Usage:
  python poc.py                 # newest page of newest notebook in `claude` folder
  python poc.py <notebook-uuid> # specific notebook
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import rmstore as R
from render import rm_bytes_to_png

HERE = Path(__file__).parent
STATE_DIR = HERE / "state"
RENDER_DIR = HERE / "renders"
CLAUDE_FOLDER = "claude"

PERSONA = (
    "You are a handwriting chat/editor assistant on a reMarkable tablet. "
    "The user writes a page by hand; you reply on a fresh page. "
    "FIRST line of every reply: a transcription of the handwritten input "
    "(so misreads get caught). Then your answer. Keep the whole reply to about "
    "one page: complete but bounded."
)


def _state_path(uuid: str) -> Path:
    return STATE_DIR / f"{uuid}.json"


def load_state(uuid: str) -> dict:
    p = _state_path(uuid)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(uuid: str, state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    _state_path(uuid).write_text(json.dumps(state, indent=2))


def pick_notebook(docs: dict[str, R.Doc]) -> R.Doc:
    """Newest notebook inside the `claude` folder (by page count as a proxy)."""
    folder = R.find_folder(CLAUDE_FOLDER, docs)
    if folder is None:
        sys.exit(
            f"No folder named {CLAUDE_FOLDER!r} on the device yet. Create it on "
            "the reMarkable, add a notebook inside, sync, then re-run. "
            "(Or pass a notebook UUID explicitly to test against any notebook.)"
        )
    kids = [d for d in R.children_of(folder.uuid, docs)
            if d.doc_type == "DocumentType" and d.page_rm_files]
    if not kids:
        sys.exit(f"Folder {CLAUDE_FOLDER!r} has no notebooks with pages yet.")
    return kids[0]


def call_claude(png_path: Path, resume_session: str | None) -> dict:
    """Send the page image to headless Claude; return parsed JSON result."""
    prompt = (
        f"New handwritten page from the user. Read the image at {png_path} "
        "and respond per your instructions."
    )
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "Read",
        "--append-system-prompt", PERSONA,
    ]
    if resume_session:
        cmd += ["--resume", resume_session]
    out = subprocess.run(cmd, input=prompt, text=True,
                         capture_output=True, timeout=300)
    if out.returncode != 0:
        sys.exit(f"claude failed:\n{out.stderr or out.stdout}")
    return json.loads(out.stdout)


def main() -> None:
    docs = R.load_docs()
    if len(sys.argv) > 1:
        uuid = sys.argv[1]
        if uuid not in docs:
            sys.exit(f"notebook {uuid} not found in store")
        nb = docs[uuid]
    else:
        nb = pick_notebook(docs)

    if not nb.page_rm_files:
        sys.exit(f"notebook {nb.uuid!r} has no .rm pages")

    page_name, page_hash = nb.page_rm_files[-1]
    print(f"notebook : {nb.visible_name!r} ({nb.uuid})")
    print(f"page     : {page_name}  ({len(nb.page_rm_files)} total)")

    RENDER_DIR.mkdir(exist_ok=True)
    png = RENDER_DIR / f"{nb.uuid}.png"
    rm_bytes_to_png(R.read_blob(page_hash), png)
    print(f"rendered : {png}")

    state = load_state(nb.uuid)
    result = call_claude(png, state.get("session_id"))

    state["session_id"] = result.get("session_id")
    state["last_page_hash"] = page_hash
    save_state(nb.uuid, state)

    print("\n----- Claude reply -----")
    print(result.get("result", "").strip())
    print("------------------------")
    print(f"session: {state['session_id']}  "
          f"cost: ${result.get('total_cost_usd', 0):.4f}")


if __name__ == "__main__":
    main()
