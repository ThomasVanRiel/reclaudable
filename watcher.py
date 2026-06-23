"""reclaudable watcher: poll the sync store and answer new pages in the Claude folder.

The current root hash (last line of .root.history) changes on every device sync.
When it changes we reload the store and run a turn on each Claude-folder notebook.
Our own uploads also bump the root, but process_notebook() records the pages it
creates in state, so it skips them — no self-reply loop.

  python watcher.py [--interval SECONDS]

Backend throttling (Pro session limit) is handled by backing off, then retrying.
"""
from __future__ import annotations

import argparse
import datetime
import sys
import time
import traceback
from pathlib import Path

import rmstore as R
from chat import CLAUDE_FOLDER, RateLimited, process_notebook

HERE = Path(__file__).parent
DEFAULT_INTERVAL = 5      # seconds between root-hash polls
RATE_LIMIT_BACKOFF = 600  # seconds to wait after hitting the backend limit


class _TimestampedTee:
    """Mirror stdout/stderr to a durable, timestamped log file (line-buffered)."""

    def __init__(self, console, fh):
        self.console, self.fh, self.buf = console, fh, ""

    def write(self, s: str) -> int:
        self.console.write(s)
        self.console.flush()
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.fh.write(f"{ts}  {line}\n")
            self.fh.flush()
        return len(s)

    def flush(self) -> None:
        self.console.flush()
        self.fh.flush()


def _setup_log() -> Path:
    logdir = HERE / "logs"
    logdir.mkdir(exist_ok=True)
    path = logdir / "watcher.log"
    fh = open(path, "a", buffering=1)
    sys.stdout = _TimestampedTee(sys.__stdout__, fh)
    sys.stderr = _TimestampedTee(sys.__stderr__, fh)
    return path


def run_once() -> None:
    docs = R.load_docs()
    folder = R.find_folder(CLAUDE_FOLDER, docs)
    if folder is None:
        print(f"({CLAUDE_FOLDER!r} folder not present yet)")
        return
    for nb in R.children_of(folder.uuid, docs):
        if nb.doc_type == "DocumentType" and nb.page_rm_files:
            try:
                process_notebook(nb)
            except RateLimited:
                raise
            except Exception:
                print(f"error on {nb.visible_name!r}:\n{traceback.format_exc()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()

    log_path = _setup_log()
    print(f"watching {CLAUDE_FOLDER!r} (poll {args.interval}s). "
          f"log: {log_path}. Ctrl-C to stop.")
    last_root = None
    while True:
        try:
            root = R.current_root()
        except Exception:
            print(f"cannot read root:\n{traceback.format_exc()}")
            time.sleep(args.interval)
            continue

        if root != last_root:
            print(f"root changed -> {root[:12]}; checking notebooks…")
            try:
                run_once()
                last_root = root
            except RateLimited as e:
                print(f"rate-limited: {e}; backing off {RATE_LIMIT_BACKOFF}s.")
                time.sleep(RATE_LIMIT_BACKOFF)
                # leave last_root unset so we retry this root after backoff
                continue
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
