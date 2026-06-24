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

import datetime
import logging
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import config

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"<<\s*EMAIL([^>]*)>>(.*?)<<\s*END\s*>>",
                       re.IGNORECASE | re.DOTALL)
# Fallback if the model forgets <<END>>: take everything after <<EMAIL…>>.
_EMAIL_OPEN_RE = re.compile(r"<<\s*EMAIL([^>]*)>>(.*)\Z", re.IGNORECASE | re.DOTALL)
_SUBJECT_RE = re.compile(r'subject\s*=\s*"([^"]*)"', re.IGNORECASE)


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


def build_message(spec: EmailSpec, *, to: str, notebook_name: str | None = None,
                  when: datetime.datetime | None = None) -> EmailMessage:
    """Assemble the multipart/mixed message (plain + html alternatives, plus a
    report.md attachment). Does not send — used by send_report and the dry-run."""
    when = when or datetime.datetime.now()
    subject = spec.subject or (
        f"{notebook_name or 'reclaudable'} — report ({when:%Y-%m-%d})")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg.set_content(spec.body_md)                              # text/plain
    msg.add_alternative(_markdown_to_html(spec.body_md), subtype="html")
    msg.add_attachment(spec.body_md.encode("utf-8"), maintype="text",
                       subtype="markdown", filename="report.md")
    return msg


def send_report(spec: EmailSpec, *, to: str | None = None,
                notebook_name: str | None = None) -> str:
    """Build and send the report email. Returns the recipient address on success;
    raises on misconfiguration or SMTP failure (the caller surfaces that on-page)."""
    to = to or config.EMAIL_TO
    if not (config.SMTP_USERNAME and config.SMTP_PASSWORD and config.SMTP_FROM):
        raise RuntimeError("SMTP not configured — set RECLAUDABLE_SMTP_* in .env")
    if not to:
        raise RuntimeError("no recipient — set RECLAUDABLE_EMAIL_TO in .env")

    msg = build_message(spec, to=to, notebook_name=notebook_name)
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

## A list
- one
- two

## A table
| key | value |
| --- | ----- |
| a   | 1     |
"""


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="reclaudable mailer self-test")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the assembled MIME message, don't send")
    ap.add_argument("--send", action="store_true",
                    help="actually send the canned report to EMAIL_TO")
    args = ap.parse_args()

    spec = EmailSpec(body_md=_SELFTEST_MD, subject="reclaudable self-test")
    if args.send:
        print(f"sent to {send_report(spec, notebook_name='selftest')}")
    else:   # default: dry-run
        print(build_message(spec, to=config.EMAIL_TO or "you@example.com",
                            notebook_name="selftest"))


if __name__ == "__main__":
    main()
