"""The "email a report" channel: parse the reply-Claude's `<<EMAIL>> … <<END>>`
block and deliver it as a real email.

A reply may carry ONE `<<EMAIL subject="…">> … <<END>>` block (opt-in; the persona
only emits it when the user asks to email/send a report). The block body is a full
rich-markdown document compiled from the whole conversation — the "final artifact"
that otherwise never leaves the device. `parse_email` splits that block out of the
page prose (the same shape as `draw.parse_draw`), and `send_report` turns the
markdown into a multipart email and sends it via SMTP.

Email clients don't render raw markdown, so the message is multipart/mixed:
  - text/plain  : the markdown source (readable as-is)
  - text/html   : the markdown rendered to HTML (so it displays cleanly)
  - attachment  : report.md (the source, for archiving / re-editing)

SMTP credentials live in reclaudable's own .env (RECLAUDABLE_SMTP_*); copy them
once from rmfakecloud's RM_SMTP_* values. RM_SMTP_SERVER is "host:port"; port 465
means implicit TLS (SMTP_SSL), anything else (e.g. 587) uses STARTTLS.
"""
from __future__ import annotations

import base64
import datetime
import logging
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

import config

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"<<\s*EMAIL([^>]*)>>(.*?)<<\s*END\s*>>",
                       re.IGNORECASE | re.DOTALL)
# Fallback if the model forgets <<END>>: take everything after <<EMAIL…>>.
_EMAIL_OPEN_RE = re.compile(r"<<\s*EMAIL([^>]*)>>(.*)\Z", re.IGNORECASE | re.DOTALL)
_SUBJECT_RE = re.compile(r'subject\s*=\s*"([^"]*)"', re.IGNORECASE)

# A captured sketch the model chose to place in the report: `![caption](sketch:ID)`.
# We substitute the cropped image — a self-contained data URI in the markdown source
# (so the .md renders standalone) and a cid: reference in the HTML alternative.
_SKETCH_MARK_RE = re.compile(r"!\[([^\]]*)\]\(\s*sketch:([A-Za-z0-9_-]+)\s*\)")


@dataclass
class EmailSpec:
    body_md: str
    subject: str | None = None


def parse_email(reply_text: str) -> tuple[str, EmailSpec | None]:
    """Split a reply into (prose, EmailSpec|None). The first `<<EMAIL>>…<<END>>`
    block is parsed into an EmailSpec; all email regions are stripped from the
    prose (so the block never lands on the page)."""
    m = _EMAIL_RE.search(reply_text) or _EMAIL_OPEN_RE.search(reply_text)
    if not m:
        return reply_text, None
    prose = _EMAIL_RE.sub("", reply_text)
    prose = _EMAIL_OPEN_RE.sub("", prose).strip()
    sm = _SUBJECT_RE.search(m.group(1) or "")
    body = m.group(2).strip()
    if not body:
        return prose, None
    return prose, EmailSpec(body_md=body, subject=sm.group(1) if sm else None)


def _markdown_to_html(md: str) -> str:
    """Render markdown to a standalone HTML document. Falls back to a <pre> block
    if the `markdown` lib isn't installed, so a missing dep never blocks delivery."""
    try:
        import markdown as _md
        body = _md.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:
        from html import escape
        body = f"<pre>{escape(md)}</pre>"
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
            f"<body>{body}</body></html>")


def _load_sketches(sketches) -> dict[str, dict]:
    """Normalize the caller's sketch asset list into {id: {caption, png_bytes}}.
    Each asset is a mapping with `id`, `caption`, and `path` (a readable PNG).
    Unreadable/missing files are dropped with a warning (never block delivery)."""
    out: dict[str, dict] = {}
    for a in sketches or []:
        sid, path = str(a.get("id")), (a.get("path") or a.get("file"))
        try:
            data = Path(path).read_bytes()
        except Exception as e:
            log.warning("sketch %s: cannot read %r (%s) — skipping", sid, path, e)
            continue
        out[sid] = {"caption": a.get("caption") or "", "png_bytes": data}
    return out


def _resolve_sketches(body_md: str, assets: dict[str, dict]) -> tuple[str, str, list[str]]:
    """Rewrite every `![caption](sketch:ID)` marker in `body_md` for both renderings.
    Returns (markdown_source, markdown_for_html, used_ids):
      - markdown_source : marker -> self-contained `data:image/png;base64,…` image
                          (for text/plain and the report.md attachment).
      - markdown_for_html: marker -> `cid:sketch-ID` image (resolved by add_related).
    A marker whose ID has no asset is replaced by its plain alt text (logged)."""
    used: list[str] = []

    def src_sub(m: re.Match) -> str:
        alt, sid = m.group(1), m.group(2)
        a = assets.get(sid)
        if a is None:
            log.warning("sketch marker references unknown id %r — dropping image", sid)
            return alt
        b64 = base64.b64encode(a["png_bytes"]).decode("ascii")
        return f"![{alt}](data:image/png;base64,{b64})"

    def html_sub(m: re.Match) -> str:
        alt, sid = m.group(1), m.group(2)
        if sid not in assets:
            return alt
        if sid not in used:
            used.append(sid)
        return f"![{alt}](cid:sketch-{sid})"

    md_source = _SKETCH_MARK_RE.sub(src_sub, body_md)
    md_html = _SKETCH_MARK_RE.sub(html_sub, body_md)
    return md_source, md_html, used


def build_message(spec: EmailSpec, *, to: str, notebook_name: str | None = None,
                  when: datetime.datetime | None = None,
                  sketches=None) -> EmailMessage:
    """Assemble the multipart/mixed message (plain + html alternatives, plus a
    report.md attachment). Does not send — used by send_report and the dry-run.

    `sketches` is an optional list of asset mappings ({id, caption, path}); any that
    the report references via a `![caption](sketch:ID)` marker are rendered inline
    (data URI in the markdown, cid: in the HTML) and attached as `sketch-<id>.png`.
    Unreferenced sketches are NOT included — relevance is the model's call."""
    when = when or datetime.datetime.now()
    subject = spec.subject or (
        f"{notebook_name or 'reclaudable'} — report ({when:%Y-%m-%d})")

    assets = _load_sketches(sketches)
    md_source, md_html, used = _resolve_sketches(spec.body_md, assets)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg.set_content(md_source)                                 # text/plain
    msg.add_alternative(_markdown_to_html(md_html), subtype="html")
    # Inline images live in a multipart/related wrapping the HTML alternative.
    html_part = msg.get_payload()[1]
    for sid in used:
        html_part.add_related(assets[sid]["png_bytes"], maintype="image",
                              subtype="png", cid=f"<sketch-{sid}>")
    msg.add_attachment(md_source.encode("utf-8"), maintype="text",
                       subtype="markdown", filename="report.md")
    for sid in used:                                           # clean original PNGs
        msg.add_attachment(assets[sid]["png_bytes"], maintype="image",
                          subtype="png", filename=f"sketch-{sid}.png")
    return msg


def send_report(spec: EmailSpec, *, to: str | None = None,
                notebook_name: str | None = None, sketches=None) -> str:
    """Build and send the report email. Returns the recipient address on success;
    raises on misconfiguration or SMTP failure (the caller surfaces that on-page)."""
    to = to or config.EMAIL_TO
    if not (config.SMTP_USERNAME and config.SMTP_PASSWORD and config.SMTP_FROM):
        raise RuntimeError("SMTP not configured — set RECLAUDABLE_SMTP_* in .env")
    if not to:
        raise RuntimeError("no recipient — set RECLAUDABLE_EMAIL_TO in .env")

    msg = build_message(spec, to=to, notebook_name=notebook_name, sketches=sketches)
    host, _, port_s = config.SMTP_SERVER.partition(":")
    port = int(port_s) if port_s else 465
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls()
            s.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            s.send_message(msg)
    log.info("emailed report %r to %s", msg["Subject"], to)
    return to


_SELFTEST_MD = """# Test report

This is a **reclaudable** self-test report.

Here is the sketch the user drew:

![auth flow](sketch:1)

## A list
- one
- two

## A table
| key | value |
| --- | ----- |
| a   | 1     |
"""

# A 1x1 PNG, base64-encoded — stands in for a real crop in the dry-run.
_SELFTEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg==")


def main() -> None:
    import argparse
    import tempfile
    ap = argparse.ArgumentParser(description="reclaudable mailer self-test")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the assembled MIME message, don't send")
    ap.add_argument("--send", action="store_true",
                    help="actually send the canned report to EMAIL_TO")
    args = ap.parse_args()

    spec = EmailSpec(body_md=_SELFTEST_MD, subject="reclaudable self-test")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tf.write(_SELFTEST_PNG)
        sketches = [{"id": "1", "caption": "auth flow", "path": tf.name}]
    if args.send:
        print(f"sent to {send_report(spec, notebook_name='selftest', sketches=sketches)}")
    else:   # default: dry-run
        print(build_message(spec, to=config.EMAIL_TO or "you@example.com",
                            notebook_name="selftest", sketches=sketches))


if __name__ == "__main__":
    main()
