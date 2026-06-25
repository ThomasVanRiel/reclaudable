"""Capture the user's hand-drawn sketches off a page and crop them out of the
rendered page image.

A reply may carry one or more `<<SKETCH …>>` tags (emitted automatically by the
reply-Claude whenever the user's page contains a drawing — see persona.md). Each
tag is a body-less marker holding a normalized bounding box and a short caption:

    <<SKETCH bbox="x0,y0,x1,y1" caption="auth flow">>

`bbox` is fractions of the page (0–1, top-left origin), so we never have to tell
the model the pixel dimensions. `parse_sketch` splits those tags out of the page
prose (same shape as `draw.parse_draw` / `mailer.parse_email` — the tag never lands
on the page), and `crop_page` cuts the region out of the page PNG that `render`
already produced this turn. The crops are stashed per notebook and, later, embedded
in an emailed report (see mailer.py) when the model references them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_SKETCH_RE = re.compile(r"<<\s*SKETCH([^>]*)>>", re.IGNORECASE)
_BBOX_RE = re.compile(
    r'bbox\s*=\s*"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,'
    r'\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*"', re.IGNORECASE)
_CAPTION_RE = re.compile(r'caption\s*=\s*"([^"]*)"', re.IGNORECASE)


@dataclass
class SketchSpec:
    bbox: tuple[float, float, float, float]   # normalized x0,y0,x1,y1 (0–1)
    caption: str = ""


def parse_sketch(reply_text: str) -> tuple[str, list[SketchSpec]]:
    """Split a reply into (prose, [SketchSpec]). Every `<<SKETCH …>>` tag is parsed
    into a SketchSpec and stripped from the prose (so it never reaches the page).
    Malformed tags (bad/missing bbox) are dropped with a log warning — one slip
    never breaks the reply."""
    specs: list[SketchSpec] = []
    for m in _SKETCH_RE.finditer(reply_text):
        attrs = m.group(1) or ""
        bm = _BBOX_RE.search(attrs)
        if not bm:
            log.warning("sketch: skipping tag with no valid bbox: %r", m.group(0))
            continue
        x0, y0, x1, y1 = (float(g) for g in bm.groups())
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        cm = _CAPTION_RE.search(attrs)
        specs.append(SketchSpec(bbox=(x0, y0, x1, y1),
                                caption=(cm.group(1).strip() if cm else "")))
    prose = _SKETCH_RE.sub("", reply_text).strip()
    return prose, specs


def crop_page(png_path, bbox: tuple[float, float, float, float], out_path,
              pad: float = 0.02):
    """Crop the normalized `bbox` region out of the page PNG at `png_path` and save
    it to `out_path`. `pad` (fraction of the page) is added on every side and the
    box is clamped to the image, so a slightly-off model bbox still keeps the whole
    drawing. Returns out_path."""
    from PIL import Image

    with Image.open(png_path) as im:
        w, h = im.size
        x0, y0, x1, y1 = bbox
        left = max(0, int(round((x0 - pad) * w)))
        top = max(0, int(round((y0 - pad) * h)))
        right = min(w, int(round((x1 + pad) * w)))
        bottom = min(h, int(round((y1 + pad) * h)))
        if right <= left or bottom <= top:
            raise ValueError(f"empty crop box {bbox} for {w}x{h} image")
        crop = im.crop((left, top, right, bottom))
        crop.save(out_path)
    return out_path
