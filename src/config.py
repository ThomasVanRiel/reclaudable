"""Central configuration for reclaudable.

Host- and deployment-specific values (rmfakecloud username, container name,
folder, model label, …) live in a `.env` file at the repo root — copy
`.env.example` to `.env` and edit. Everything has a default, so the code still
imports without a `.env`, but the defaults are author-specific; set your own.

The loader is deliberately dependency-free (no python-dotenv): a `.env` line is
`KEY=value`, `#` starts a comment, surrounding quotes are stripped, and a value
already present in the real environment wins (so `FOO=bar python …` overrides).
The same `.env` is read by the `bin/rmapi` shell wrapper.
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo root (this module lives in src/); host config files like .env sit at the root.
ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --- rmfakecloud storage (sync15 reader) ---
RM_USER = _get("RECLAUDABLE_RM_USER", "user")
CONTAINER = _get("RECLAUDABLE_CONTAINER", "rmfakecloud")
SYNC = _get("RECLAUDABLE_SYNC_DIR", f"/data/users/{RM_USER}/sync")

# --- conversation behaviour ---
CLAUDE_FOLDER = _get("RECLAUDABLE_FOLDER", "Claude")
MODEL_LABEL = _get("RECLAUDABLE_MODEL_LABEL", "Claude Opus 4.8")
# Stable cwd for the headless reply-Claude (sessions are keyed by working dir).
CLAUDE_CWD = Path(_get("RECLAUDABLE_CLAUDE_CWD",
                       str(Path.home() / ".reclaudable" / "claude-cwd")))
# The `claude` CLI. Default resolves via PATH (fine for the watcher, launched from
# a login shell). Set an absolute path for non-interactive SSH (e.g. the import
# skill), whose minimal PATH omits ~/.local/bin — same reason RMAPI_BIN is absolute.
CLAUDE_BIN = _get("RECLAUDABLE_CLAUDE_BIN", "claude")

# --- rmapi write-back (also read directly by bin/rmapi) ---
RMAPI_HOST = _get("RECLAUDABLE_RMAPI_HOST", "https://example.com")
RMAPI_BIN = _get("RECLAUDABLE_RMAPI_BIN",
                 str(Path.home() / "source" / "rmapi" / "rmapi"))

# --- email a report (see mailer.py) ---
# SMTP for delivering a compiled report when the user asks to "email me ...".
# These mirror rmfakecloud's RM_SMTP_* values — copy them into reclaudable's own
# .env once (kept separate so the two stay decoupled). SMTP_FROM defaults to the
# login; EMAIL_TO (the fixed recipient — "email me") defaults to the from address.
SMTP_SERVER = _get("RECLAUDABLE_SMTP_SERVER", "smtp.gmail.com:465")
SMTP_USERNAME = _get("RECLAUDABLE_SMTP_USERNAME", "")
SMTP_PASSWORD = _get("RECLAUDABLE_SMTP_PASSWORD", "")
SMTP_FROM = _get("RECLAUDABLE_SMTP_FROM", "") or SMTP_USERNAME
EMAIL_TO = _get("RECLAUDABLE_EMAIL_TO", "") or SMTP_FROM
