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

from config import CLAUDE_FOLDER  # host config; see .env
from render import rm_to_png  # noqa: F401  (handy for debugging)

# Repo root (this module lives in src/); bin/ and renders/ sit there.
ROOT = Path(__file__).resolve().parent.parent
RMAPI = str(ROOT / "bin" / "rmapi")


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
# i.e. left margin 117px, right margin 78px — the left is never annotated and
# pages are expanded as needed. rmc's default was pos_x=-468/width=936 (234px both).
POS_X = -585.0
WIDTH = 1209.0
# Hard-wrap width, in chars. The device ALSO auto-wraps typed text to the
# WIDTH=1209px box; that box renders as ~100mm on-device and holds ~57 narrow
# prose chars across it, so keep this at/under ~57 or lines double-wrap into
# ragged rows. NOTE this is decoupled from BANNER_WIDTH on purpose: the `─` glyph
# is em-width, so far fewer of THEM fill the same box (see BANNER_WIDTH). At 46
# the column came out ~80mm of the ~100mm box; 57 fills it while leaving a hair of
# right margin kept for hand-annotating replies. (76 was wrong — it was
# tuned to rmc's small 7pt reading-render, not the larger device font.)
WRAP_WIDTH = 57

# Horizontal rule for the reply frame. The em-width `─` glyph fills the WIDTH=1209px
# box at ~46 chars (the box's full ~100mm), which is WIDER than WRAP_WIDTH counts of
# a narrow prose glyph — hence its own constant. It's one space-free token, so _wrap
# never breaks it (textwrap won't split a word shorter than WRAP_WIDTH). If the `─`
# glyph is ever missing from the device's typed-text font, switch to "-" * a count
# tuned to fill the box with that narrower glyph.
BANNER_WIDTH = 46
BANNER = "─" * BANNER_WIDTH

# Drawing placement (see draw.py). When a reply carries a `<<DRAW>>` figure, we put
# it BELOW the text: text flows from pos_y down, and we drop the figure at
# y = pos_y + n_lines*LINE_H + GAP so it can't collide. LINE_H is canvas units per
# text line — biased HIGH on purpose: the device font is larger than rmc's reading
# render (~70 u/line there), and overshooting just pushes the figure further down a
# page that scrolls anyway. Tune LINE_H against the DEVICE if a gap looks wrong.
LINE_H = 110.0
GAP = 70.0


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


def _text_to_rm(text: str, out_rm: Path, draw_spec=None) -> None:
    """Render plain reply text to a .rm v6 page via rmscene, with custom margins.
    Strips md markers (rendered literally) and hard-wraps long lines. If draw_spec
    is given, its strokes are appended as pen Line items below the text."""
    from rmscene import scene_stream as ss

    clean = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)      # headings
    clean = re.sub(r"^\s*[-*]\s+", "• ", clean, flags=re.M)     # bullets
    clean = clean.replace("**", "").replace("`", "")
    clean = _wrap(clean)

    blocks = list(ss.simple_text_document(clean))
    pos_y = 234.0
    for b in blocks:
        if isinstance(b, ss.RootTextBlock):
            b.value.pos_x = POS_X
            b.value.width = WIDTH
            pos_y = b.value.pos_y
    if draw_spec is not None:
        import draw
        n_lines = clean.count("\n") + 1
        blocks += draw.spec_to_line_blocks(
            draw_spec, col_x0=POS_X, col_w=WIDTH,
            y_top=pos_y + n_lines * LINE_H + GAP)
    with open(out_rm, "wb") as f:
        ss.write_blocks(f, blocks)


def _render_reply(reply_text: str, model_label: str, out_rm: Path) -> None:
    """Render a full reply (prose + optional `<<DRAW>>` figure) to a .rm page.
    Pull the drawing block out BEFORE framing/wrapping (so its DSL lines aren't
    word-wrapped), then render text and strokes together. Shared by append_page
    and replace_page so a figure renders the same whether appended or rewritten
    in place over a rate-limit placeholder."""
    import draw
    prose, spec = draw.parse_draw(reply_text)
    _text_to_rm(_frame_reply(prose, model_label), out_rm, draw_spec=spec)


def _with_bundle(folder: str, visible_name: str | None,
                 mutate, dry_run: bool = False, new_name: str | None = None) -> str:
    """Round-trip a notebook bundle and apply an in-place edit.

    Downloads (`rmapi get`) → unpacks → loads `.content` → calls
    `mutate(unp, content, doc_uuid)` (which edits files under `unp/` and/or the
    `content` dict in place and returns the affected page UUID) → re-zips →
    `rmapi rm`+`put`. The whole store is content-addressed, so this same cycle can
    append, overwrite, or remove any page — the only difference is `mutate`.

    `new_name`, if given, renames the notebook in the SAME round-trip: the unpacked
    `.metadata`'s `visibleName` is patched (the bundle already contains it, so this
    costs no extra `get`/`put`, and the doc UUID is unchanged so state stays valid).

    Returns the page UUID `mutate` reports. With `dry_run`, writes the modified zip
    to `renders/` and skips the device mutation."""
    work = Path(tempfile.mkdtemp(prefix="rmwb-"))
    try:
        # 1. download the current bundle (rmapi get writes <visibleName>.zip into cwd)
        dl = work / "dl"
        dl.mkdir()
        cp = subprocess.run([RMAPI, "-ni", "get", f"/{folder}/{visible_name}"],
                            cwd=dl, text=True, capture_output=True)
        if cp.returncode != 0:
            raise RuntimeError(f"rmapi get failed: {cp.stderr or cp.stdout}")
        zips = list(dl.glob("*.zip"))
        if not zips:
            raise RuntimeError("rmapi get produced no zip")

        # 2. unpack
        unp = work / "unp"
        with zipfile.ZipFile(zips[0]) as z:
            z.extractall(unp)

        content_path = next(unp.glob("*.content"))
        doc_uuid = content_path.stem
        content = json.loads(content_path.read_text())

        # 3. apply the page-level edit, then persist the (possibly changed) content
        page = mutate(unp, content, doc_uuid)
        content_path.write_text(json.dumps(content))

        # 3b. optionally rename: patch visibleName in the unpacked .metadata so the
        #     renamed bundle goes up in this same put (no second round-trip).
        if new_name:
            meta_path = unp / f"{doc_uuid}.metadata"
            meta = json.loads(meta_path.read_text())
            meta["visibleName"] = new_name
            meta["lastModified"] = str(int(time.time() * 1000))
            meta["metadatamodified"] = True
            meta_path.write_text(json.dumps(meta))

        # 4. re-zip under the (possibly new) visibleName (the UUID comes from the
        #    internal files; the displayed name comes from the .metadata)
        upload_name = new_name or visible_name
        out_zip = work / f"{upload_name}.zip"
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(unp.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(unp).as_posix())

        if dry_run:
            keep = ROOT / "renders" / f"{upload_name}.modified.zip"
            keep.parent.mkdir(exist_ok=True)
            shutil.copy(out_zip, keep)
            print(f"[dry-run] modified bundle: {keep} (page {page})")
            return page

        # 5. replace: rm old (by its current on-device name), put new
        rm = _rmapi("rm", f"/{folder}/{visible_name}", check=False)
        if rm.returncode != 0:
            raise RuntimeError(f"rmapi rm failed: {rm.stderr or rm.stdout}")
        put = _rmapi("put", str(out_zip), f"/{folder}", check=False)
        if put.returncode != 0:
            raise RuntimeError(
                f"rmapi put failed (notebook was removed! restore from backup): "
                f"{put.stderr or put.stdout}")
        if new_name:
            print(f"uploaded: page {page} -> /{folder}/{upload_name} "
                  f"(renamed from {visible_name!r})")
        else:
            print(f"uploaded: page {page} -> /{folder}/{upload_name}")
        return page
    finally:
        shutil.rmtree(work, ignore_errors=True)


def append_page(notebook_uuid: str, reply_text: str,
                folder: str = CLAUDE_FOLDER, visible_name: str | None = None,
                model_label: str = "Claude", dry_run: bool = False,
                rename: str | None = None) -> str:
    """Append reply_text as a new page. Returns the new page UUID. `rename`, if
    given, also renames the notebook to that title in the same round-trip."""
    new_page = str(uuid.uuid4())

    def mutate(unp: Path, content: dict, doc_uuid: str) -> str:
        # render reply -> new page .rm inside the doc's page dir
        page_dir = unp / doc_uuid
        page_dir.mkdir(exist_ok=True)
        _render_reply(reply_text, model_label, page_dir / f"{new_page}.rm")

        # patch .content. Sort after EVERY page (the .content array is not
        # necessarily in idx order; the device reorders), so the reply lands last.
        pages = content["cPages"]["pages"]
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
        return new_page

    return _with_bundle(folder, visible_name, mutate, dry_run, new_name=rename)


def replace_page(notebook_uuid: str, page_uuid: str, reply_text: str,
                 folder: str = CLAUDE_FOLDER, visible_name: str | None = None,
                 model_label: str = "Claude", dry_run: bool = False,
                 rename: str | None = None) -> str:
    """Overwrite an existing page's strokes in place with reply_text — same slot,
    same position. Used to turn a rate-limit placeholder into the real reply.
    Returns page_uuid. `rename`, if given, also renames the notebook in the same
    round-trip."""
    def mutate(unp: Path, content: dict, doc_uuid: str) -> str:
        rm_path = unp / doc_uuid / f"{page_uuid}.rm"
        if not rm_path.exists():
            raise RuntimeError(f"replace_page: page {page_uuid} not in notebook")
        _render_reply(reply_text, model_label, rm_path)
        for p in content["cPages"]["pages"]:   # bump modifed; leave order untouched
            if p.get("id") == page_uuid:
                p["modifed"] = str(int(time.time() * 1000))
        return page_uuid

    return _with_bundle(folder, visible_name, mutate, dry_run, new_name=rename)


def create_notebook(visible_name: str, pages_text: list[str], *,
                    folder: str = CLAUDE_FOLDER, parent_uuid: str = "",
                    doc_uuid: str | None = None, page_ids: list[str] | None = None,
                    dry_run: bool = False) -> tuple[str, list[str]]:
    """Build a brand-new notebook bundle (fresh UUID) and upload it to /folder.

    Unlike append_page/replace_page (which round-trip an existing doc via
    `_with_bundle`), this constructs `.content`/`.metadata`/page `.rm` files from
    scratch and `rmapi put`s them — no `get` first. `parent_uuid` is the folder's
    UUID (resolve with rmstore.find_folder). `doc_uuid` may be supplied so the
    caller can write state under it before upload (`page_ids` likewise); otherwise
    they're generated. Returns (doc_uuid, [page_uuids])."""
    doc_uuid = doc_uuid or str(uuid.uuid4())
    if page_ids is not None and len(page_ids) != len(pages_text):
        raise ValueError("page_ids must match pages_text length")
    work = Path(tempfile.mkdtemp(prefix="rmnb-"))
    try:
        root = work / "bundle"
        page_dir = root / doc_uuid
        page_dir.mkdir(parents=True)

        ids = list(page_ids) if page_ids is not None else [
            str(uuid.uuid4()) for _ in pages_text]
        pages_meta: list[dict] = []
        idx = ""
        for pid, text in zip(ids, pages_text):
            _text_to_rm(text, page_dir / f"{pid}.rm")
            idx = _next_idx(idx)   # first page -> "ba", then "bb", …
            pages_meta.append({
                "id": pid,
                "idx": {"timestamp": "1:2", "value": idx},
                "template": {"timestamp": "1:1", "value": "Blank"},
            })

        content = {
            "formatVersion": 2,
            "fileType": "notebook",
            "orientation": "portrait",
            "pageCount": len(ids),
            "coverPageNumber": -1,
            "lineHeight": -1,
            "textScale": 1,
            "textAlignment": "justify",
            "fontName": "",
            "zoomMode": "bestFit",
            "tags": [],
            "pageTags": [],
            "documentMetadata": {},
            "extraMetadata": {},
            "cPages": {
                "lastOpened": {"timestamp": "1:1", "value": ids[0]},
                "original": {"timestamp": "0:0", "value": -1},
                "pages": pages_meta,
                "uuids": [{"first": str(uuid.uuid4()), "second": len(ids)}],
            },
        }
        (root / f"{doc_uuid}.content").write_text(json.dumps(content))

        now_ms = str(int(time.time() * 1000))
        metadata = {
            "createdTime": now_ms,
            "lastModified": now_ms,
            "lastOpened": now_ms,
            "lastOpenedPage": 0,
            "parent": parent_uuid,
            "pinned": False,
            "type": "DocumentType",
            "visibleName": visible_name,
            "deleted": False,
            "metadatamodified": False,
            "modified": False,
            "synced": False,
            "version": 0,
            "source": "",
        }
        (root / f"{doc_uuid}.metadata").write_text(json.dumps(metadata))

        out_zip = work / f"{visible_name}.zip"
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(root).as_posix())

        if dry_run:
            keep = ROOT / "renders" / f"{visible_name}.new.zip"
            keep.parent.mkdir(exist_ok=True)
            shutil.copy(out_zip, keep)
            print(f"[dry-run] new notebook bundle: {keep} (doc {doc_uuid})")
            return doc_uuid, ids

        put = _rmapi("put", str(out_zip), f"/{folder}", check=False)
        if put.returncode != 0:
            raise RuntimeError(f"rmapi put failed: {put.stderr or put.stdout}")
        print(f"created notebook {visible_name!r} ({doc_uuid}) in /{folder}")
        return doc_uuid, page_ids
    finally:
        shutil.rmtree(work, ignore_errors=True)


def remove_page(notebook_uuid: str, page_uuid: str,
                folder: str = CLAUDE_FOLDER, visible_name: str | None = None,
                dry_run: bool = False) -> str:
    """Delete a page in place (the .rm file + its cPages entry). Used when a
    rate-limit retry comes back <<WAIT>> — the page was never a real request.
    Returns page_uuid."""
    def mutate(unp: Path, content: dict, doc_uuid: str) -> str:
        (unp / doc_uuid / f"{page_uuid}.rm").unlink(missing_ok=True)
        pages = content["cPages"]["pages"]
        pages[:] = [p for p in pages if p.get("id") != page_uuid]
        uuids = content["cPages"].get("uuids")
        if uuids and uuids[0].get("second", 1) > 1:
            uuids[0]["second"] -= 1
        content["pageCount"] = max(content.get("pageCount", len(pages) + 1) - 1, 0)
        return page_uuid

    return _with_bundle(folder, visible_name, mutate, dry_run)
