"""Render a reMarkable v6 `.rm` page to PNG.

Pipeline: `.rm` --rmc--> SVG --cairosvg--> PNG.
(rmc's PDF output needs Inkscape, which isn't installed; SVG->PNG via cairosvg
avoids that. drawj2d would also work but needs a JRE.)

rmc is driven IN-PROCESS (not as a CLI subprocess) so that (a) it works whatever
PATH the watcher is launched with, and (b) we can patch its colour palette below.
"""
from __future__ import annotations

import io
import logging
import re
import tempfile
from pathlib import Path

import cairosvg
import rmscene.scene_items as _si
from rmc.cli import convert_rm
from rmc.exporters.writing_tools import RM_PALETTE
from rmscene import scene_stream as _ss

# rmscene logs a WARNING ("Some data has not been read … newer format") for every
# Paper Pro page, because it can't parse the newest block types — but the strokes
# we care about still render. As a subprocess this went to discarded stderr; in
# process it leaks into the watcher log. Silence it (real failures still raise).
logging.getLogger("rmscene").setLevel(logging.ERROR)

# rmc's RM_PALETTE predates the Paper Pro's expanded pen palette and raises
# KeyError on any colour it doesn't know — e.g. PenColor.HIGHLIGHT (9), emitted
# when the user highlights an annotation. Fill every missing PenColor so the
# exporter can never crash on a stroke colour. Highlighter -> yellow; anything
# else unknown -> mid grey.
for _c in _si.PenColor:
    if _c not in RM_PALETTE:
        RM_PALETTE[_c] = (251, 247, 25) if _c.name.startswith("HIGHLIGHT") \
            else (128, 128, 128)

# reMarkable Paper Pro panel width in px; height scales with the canvas.
DEFAULT_WIDTH = 1404


def rm_to_png(rm_path: str | Path, png_path: str | Path,
              width: int = DEFAULT_WIDTH) -> Path:
    rm_path, png_path = Path(rm_path), Path(png_path)
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tf:
        svg_path = Path(tf.name)
    try:
        # rmscene warns about unread (newer Paper Pro) blocks; strokes still render.
        with open(svg_path, "w") as fout:
            convert_rm(rm_path, "svg", fout)
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path),
                         output_width=width, background_color="white")
    finally:
        svg_path.unlink(missing_ok=True)
    return png_path


def rm_bytes_to_png(rm_bytes: bytes, png_path: str | Path,
                    width: int = DEFAULT_WIDTH) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as tf:
        tf.write(rm_bytes)
        rm_path = Path(tf.name)
    try:
        return rm_to_png(rm_path, png_path, width)
    finally:
        rm_path.unlink(missing_ok=True)


_SVG_OPEN_RE = re.compile(r"<svg[^>]*>")


def _rm_bytes_to_svg(rm_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as tf:
        tf.write(rm_bytes)
        rm_path = Path(tf.name)
    try:
        out = io.StringIO()
        convert_rm(rm_path, "svg", out)
        return out.getvalue()
    finally:
        rm_path.unlink(missing_ok=True)


def _strip_text_blocks(rm_bytes: bytes) -> bytes:
    """Drop the page's typed-text block (RootTextBlock), keeping only pen strokes."""
    blocks = [b for b in _ss.read_blocks(io.BytesIO(rm_bytes))
              if not isinstance(b, _ss.RootTextBlock)]
    buf = io.BytesIO()
    _ss.write_blocks(buf, blocks)
    return buf.getvalue()


def rm_bytes_to_strokes_png(rm_bytes: bytes, png_path: str | Path,
                            width: int = DEFAULT_WIDTH) -> Path:
    """Render ONLY the user's pen strokes — Claude's typed reply text is dropped —
    onto the SAME canvas as the full-page render. Sketches are pen strokes, but on
    a Claude reply page the user draws over Claude's printed text; stripping the
    text block keeps that text out of a captured sketch crop. The strokes-only SVG
    is forced to the full render's `<svg>` viewBox/size so a bbox measured against
    the full page maps 1:1 onto this image."""
    png_path = Path(png_path)
    full_open = _SVG_OPEN_RE.search(_rm_bytes_to_svg(rm_bytes))
    stroke_svg = _rm_bytes_to_svg(_strip_text_blocks(rm_bytes))
    if full_open:   # pin the stroke canvas to the full page's (alignment)
        stroke_svg = _SVG_OPEN_RE.sub(full_open.group(0), stroke_svg, count=1)
    cairosvg.svg2png(bytestring=stroke_svg.encode("utf-8"), write_to=str(png_path),
                     output_width=width, background_color="white")
    return png_path
