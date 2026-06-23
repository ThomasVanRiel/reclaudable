"""Append a reply page to a conversation notebook and upload it via rmapi.

Strategy (honours append-to-same-notebook + stable UUID):
  rmapi get <notebook>          -> bundle .zip (.content/.metadata/<uuid>/<page>.rm)
  render reply text -> reply.rm  (rmc markdown->rm)
  add page file + patch .content (new cPages entry, pageCount+1, uuids counter+1)
  rmapi rm <notebook> ; rmapi put <zip> /<folder>   (re-adds same UUID, +1 page)

rmapi runs through bin/rmapi (RMAPI_HOST -> self-hosted rmfakecloud). The store is
content-addressed, so the pre-write root in backups/ is a full rollback point.
"""
from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from render import rm_to_png  # noqa: F401  (handy for debugging)

HERE = Path(__file__).parent
RMAPI = str(HERE / "bin" / "rmapi")
CLAUDE_FOLDER = "Claude"


def _rmapi(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([RMAPI, "-ni", *args], text=True,
                          capture_output=True, check=check)


def _next_idx(idx: str) -> str:
    """Fractional index sorting strictly after `idx` (good enough for appends)."""
    if not idx:
        return "ba"
    if idx[-1] < "z":
        return idx[:-1] + chr(ord(idx[-1]) + 1)
    return idx + "n"   # can't bump last char; append → sorts after idx


# Text-block geometry (reMarkable canvas is centred: x in [-702, +702] for the
# 1404px page). Defaults put the column from 117px (left) to 1326px (right),
# i.e. left margin 117px, right margin 78px — you never annotates the left and
# expands pages as needed. rmc's default was pos_x=-468/width=936 (234px both).
POS_X = -585.0
WIDTH = 1209.0
# Hard-wrap width, in chars. The device ALSO auto-wraps typed text to the
# WIDTH=1209px box (~48 device-font chars), so keep this at/under ~48 or lines
# double-wrap into ragged rows. We deliberately wrap a bit narrow: you likes
# the right margin it leaves for annotating replies by hand. (76 was wrong — it
# was tuned to rmc's small 7pt reading-render, not the larger device font.)
WRAP_WIDTH = 46

# Horizontal rule for the reply frame. One WRAP_WIDTH-char token, so _wrap leaves
# it on a single line. If the `─` glyph is ever missing from the device's
# typed-text font, switch to "-" * WRAP_WIDTH.
BANNER = "─" * WRAP_WIDTH


def _frame_reply(reply_text: str, model_label: str,
                 when: datetime.datetime | None = None) -> str:
    """Wrap the model's reply in a deterministic frame:

        BANNER / paraphrase / BANNER / "model · timestamp" / blank / body

    The model emits `Read as: <paraphrase>` + a blank line + the response (the
    persona enforces this); we split on the first blank line and compose the
    frame here so the timestamp is real wall-clock and the layout is identical
    every turn. If there's no `Read as` prefix, fall back to metadata + body with
    no top box."""
    when = when or datetime.datetime.now()
    meta = f"{model_label} · {when:%A %-d %B at %H:%M}"
    body = reply_text.strip()
    if body[:7].lower().startswith("read as"):
        head, sep, rest = body.partition("\n\n")
        if sep:
            paraphrase, body = head.strip(), rest.strip()
            return "\n".join([BANNER, paraphrase, BANNER, meta, "", body])
    return "\n".join([meta, "", body])


def _wrap(text: str) -> str:
    """Word-wrap each paragraph to WRAP_WIDTH. We hard-wrap (rather than let the
    device wrap) to keep the column narrow — see WRAP_WIDTH — and because the rmc
    reading-path render does not auto-wrap."""
    import textwrap
    out: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
        else:
            out.extend(textwrap.wrap(para, width=WRAP_WIDTH) or [""])
    return "\n".join(out)


def _text_to_rm(text: str, out_rm: Path) -> None:
    """Render plain reply text to a .rm v6 page via rmscene, with custom margins.
    Strips md markers (rendered literally) and hard-wraps long lines."""
    from rmscene import scene_stream as ss

    clean = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)      # headings
    clean = re.sub(r"^\s*[-*]\s+", "• ", clean, flags=re.M)     # bullets
    clean = clean.replace("**", "").replace("`", "")
    clean = _wrap(clean)

    blocks = list(ss.simple_text_document(clean))
    for b in blocks:
        if isinstance(b, ss.RootTextBlock):
            b.value.pos_x = POS_X
            b.value.width = WIDTH
    with open(out_rm, "wb") as f:
        ss.write_blocks(f, blocks)


def append_page(notebook_uuid: str, reply_text: str,
                folder: str = CLAUDE_FOLDER, visible_name: str | None = None,
                model_label: str = "Claude", dry_run: bool = False) -> str:
    """Append reply_text as a new page. Returns the new page UUID."""
    work = Path(tempfile.mkdtemp(prefix="rmwb-"))
    try:
        # 1. download the current bundle
        dl = work / "dl"
        dl.mkdir()
        # rmapi get writes <visibleName>.zip into cwd
        cp = subprocess.run([RMAPI, "-ni", "get", f"/{folder}/{visible_name}"],
                            cwd=dl, text=True, capture_output=True)
        if cp.returncode != 0:
            raise RuntimeError(f"rmapi get failed: {cp.stderr or cp.stdout}")
        zips = list(dl.glob("*.zip"))
        if not zips:
            raise RuntimeError("rmapi get produced no zip")
        src_zip = zips[0]

        # 2. unpack
        unp = work / "unp"
        with zipfile.ZipFile(src_zip) as z:
            z.extractall(unp)

        content_path = next(unp.glob("*.content"))
        doc_uuid = content_path.stem
        content = json.loads(content_path.read_text())

        # 3. render reply -> new page .rm inside the doc's page dir
        new_page = str(uuid.uuid4())
        page_dir = unp / doc_uuid
        page_dir.mkdir(exist_ok=True)
        framed = _frame_reply(reply_text, model_label)
        _text_to_rm(framed, page_dir / f"{new_page}.rm")

        # 4. patch .content
        pages = content["cPages"]["pages"]
        # Sort after EVERY page (the .content array is not necessarily in idx
        # order; the device reorders), so the reply always lands last.
        last_idx = max((p["idx"]["value"] for p in pages), default="ba")
        template = pages[-1].get("template", {"timestamp": "1:1", "value": "Blank"}) \
            if pages else {"timestamp": "1:1", "value": "Blank"}
        pages.append({
            "id": new_page,
            "idx": {"timestamp": "1:2", "value": _next_idx(last_idx)},
            "modifed": str(int(time.time() * 1000)),
            "template": template,
        })
        uuids = content["cPages"].get("uuids")
        if uuids:
            uuids[0]["second"] = uuids[0].get("second", 1) + 1
        content["pageCount"] = content.get("pageCount", len(pages) - 1) + 1
        content_path.write_text(json.dumps(content))

        # 5. re-zip with the SAME visibleName (so put keeps the name; UUID comes
        #    from the internal files)
        out_zip = work / f"{visible_name}.zip"
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(unp.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(unp).as_posix())

        if dry_run:
            keep = HERE / "renders" / f"{visible_name}.modified.zip"
            keep.parent.mkdir(exist_ok=True)
            shutil.copy(out_zip, keep)
            print(f"[dry-run] modified bundle: {keep} (page {new_page})")
            return new_page

        # 6. replace: rm old, put new
        rm = _rmapi("rm", f"/{folder}/{visible_name}", check=False)
        if rm.returncode != 0:
            raise RuntimeError(f"rmapi rm failed: {rm.stderr or rm.stdout}")
        put = _rmapi("put", str(out_zip), f"/{folder}", check=False)
        if put.returncode != 0:
            raise RuntimeError(
                f"rmapi put failed (notebook was removed! restore from backup): "
                f"{put.stderr or put.stdout}")
        print(f"uploaded: +1 page ({new_page}) to /{folder}/{visible_name}")
        return new_page
    finally:
        shutil.rmtree(work, ignore_errors=True)
