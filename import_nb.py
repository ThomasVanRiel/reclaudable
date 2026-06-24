"""Import a Claude Code conversation into a new reMarkable notebook.

Driven by the `/reclaudable` skill (which runs on a remote machine and pipes the
payload here over SSH). Reads a JSON payload from stdin:

    {"title": "Optional title", "summary": "markdown summary of the conversation"}

and creates a new notebook in the Claude/ folder seeded with that summary, then
primes a reclaudable Claude session with the SAME summary so the first handwritten
follow-up on the tablet continues with context. The notebook arrives DORMANT — the
seeded page is marked handled so the watcher doesn't auto-reply.

  ... import_nb.py < payload.json
  ... import_nb.py --name "Title" --summary-file summary.md   # local testing

Continuity must run here (the server): reclaudable's watcher resumes the primed
session_id from this host's local Claude Code session store; a session created
anywhere else is unusable.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import traceback
import uuid

import chat
import rmstore as R
import writeback as W
from config import CLAUDE_FOLDER


def _load_payload(args: argparse.Namespace) -> tuple[str, str]:
    """Return (title, summary) from --summary-file or the stdin JSON payload."""
    if args.summary_file:
        summary = open(args.summary_file, encoding="utf-8").read().strip()
        title = args.name or ""
    else:
        try:
            payload = json.loads(sys.stdin.read())
        except (json.JSONDecodeError, ValueError) as e:
            sys.exit(f"import: invalid JSON payload on stdin ({e})")
        summary = (payload.get("summary") or "").strip()
        title = args.name or (payload.get("title") or "").strip()
    if not summary:
        sys.exit("import: empty summary — nothing to import")
    if not title:
        title = f"CC import {datetime.date.today():%Y-%m-%d}"
    return title, summary


def main() -> None:
    ap = argparse.ArgumentParser(description="import a conversation into a notebook")
    ap.add_argument("--name", help="notebook title (overrides payload title)")
    ap.add_argument("--summary-file", help="read summary from a file (local testing)")
    args = ap.parse_args()

    title, summary = _load_payload(args)

    folder = R.find_folder(CLAUDE_FOLDER)
    if folder is None:
        sys.exit(f"import: no {CLAUDE_FOLDER!r} folder on the device yet")

    header = f"Imported from Claude Code · {datetime.datetime.now():%-d %B %Y}"
    page_text = f"{header}\n\n{summary}"

    # Reserve the doc UUID and flag it 'importing' BEFORE upload, so if the watcher
    # sees the notebook before we finish (priming is a live Claude call) it skips it
    # instead of auto-replying to the seeded page. The flag is cleared at the end.
    doc_uuid = str(uuid.uuid4())
    chat.save_state(doc_uuid, {"importing": True})

    try:
        W.create_notebook(title, [page_text], folder=CLAUDE_FOLDER,
                          parent_uuid=folder.uuid, doc_uuid=doc_uuid)
    except Exception:
        (chat.STATE_DIR / f"{doc_uuid}.json").unlink(missing_ok=True)  # nothing uploaded
        print(f"import: failed to create notebook:\n{traceback.format_exc()}")
        sys.exit(1)

    # Prime the session with the same summary. Best-effort: if the backend is
    # throttled or errors, the notebook still exists; it just starts a fresh
    # conversation on the first handwritten page instead of a primed one.
    session_id = None
    try:
        session_id = chat.prime_session(summary)
    except chat.RateLimited as e:
        print(f"import: backend rate-limited — notebook created but NOT primed ({e})")
    except Exception:
        print(f"import: priming failed — notebook created but NOT primed:\n"
              f"{traceback.format_exc()}")

    # Reconcile: mark the actual uploaded page(s) handled (dormant) and store the
    # session id, dropping the 'importing' flag. If we can't read the doc back, keep
    # the flag (stay safely dormant) rather than risk an auto-reply.
    doc = R.load_doc(doc_uuid)
    pages = R.ordered_pages(doc) if doc else []
    if not pages:
        print(f"import: WARNING could not read back {doc_uuid} — left 'importing' "
              "(dormant). Re-run or clear state/<uuid>.json if this persists.")
        sys.exit(1)
    state: dict = {"handled": [f"{p}:{h}" for p, h in pages]}
    if session_id:
        state["session_id"] = session_id
    chat.save_state(doc_uuid, state)

    print(f"imported {title!r} ({doc_uuid}) into /{CLAUDE_FOLDER} — "
          f"{'session primed' if session_id else 'NOT primed'}")


if __name__ == "__main__":
    main()
