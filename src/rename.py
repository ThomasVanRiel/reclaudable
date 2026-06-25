"""The "rename this notebook" channel: detect a default/auto-generated notebook
name and parse the reply-Claude's `<<RENAME>> … <<END>>` block into a clean title.

A notebook created on the device gets a throwaway name — a timestamp like
`2026-06-23_092942`, an incrementing `Notebook 9`, or a blank/`Untitled`
placeholder. When `is_default_name` flags such a name, the turn driver tells the
reply-Claude (which alone has the conversation context) it may propose a title via
ONE `<<RENAME>>Short Title<<END>>` block. `parse_rename` splits that block out of
the page prose — the same shape as `mailer.parse_email` / `draw.parse_draw` — and
`writeback` patches the new name into the notebook's `.metadata` in the same
bundle round-trip. Code decides WHEN to rename (the default-name gate); the model
only decides WHAT the title is.
"""
from __future__ import annotations

import re

# Auto-generated names eligible for renaming: a device timestamp
# (YYYY-MM-DD_HHMMSS), an incrementing `Notebook N`, or `Untitled`.
DEFAULT_NAME_RE = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}_\d{6}|Notebook\s+\d+|Untitled)\s*$", re.IGNORECASE)

_RENAME_RE = re.compile(r"<<\s*RENAME([^>]*)>>(.*?)<<\s*END\s*>>",
                        re.IGNORECASE | re.DOTALL)
# Fallback if the model forgets <<END>>: take everything after <<RENAME…>>.
_RENAME_OPEN_RE = re.compile(r"<<\s*RENAME([^>]*)>>(.*)\Z", re.IGNORECASE | re.DOTALL)

MAX_TITLE_LEN = 60


def is_default_name(name: str | None) -> bool:
    """True if `name` is a throwaway auto-generated name (so it's worth renaming):
    None, blank/whitespace, or a `DEFAULT_NAME_RE` match. This is the gate — code
    only ever proposes a rename for names this returns True for."""
    if name is None or not name.strip():
        return True
    return bool(DEFAULT_NAME_RE.match(name))


def sanitize_title(raw: str) -> str | None:
    """Clean a model-proposed title: collapse internal whitespace/newlines to single
    spaces and strip. Reject (return None) if it ends up empty, or is itself a
    default-style name; trim to MAX_TITLE_LEN otherwise."""
    title = " ".join(raw.split())
    if not title or is_default_name(title):
        return None
    return title[:MAX_TITLE_LEN].strip()


def parse_rename(reply_text: str) -> tuple[str, str | None]:
    """Split a reply into (prose, title|None). The first `<<RENAME>>…<<END>>` block
    is parsed into a sanitized title; all rename regions are stripped from the prose
    (so the block never lands on the page)."""
    m = _RENAME_RE.search(reply_text) or _RENAME_OPEN_RE.search(reply_text)
    if not m:
        return reply_text, None
    prose = _RENAME_RE.sub("", reply_text)
    prose = _RENAME_OPEN_RE.sub("", prose).strip()
    return prose, sanitize_title(m.group(2))
