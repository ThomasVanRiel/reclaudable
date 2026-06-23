"""Render a reMarkable v6 `.rm` page to PNG.

Pipeline: `.rm` --rmc--> SVG --cairosvg--> PNG.
(rmc's PDF output needs Inkscape, which isn't installed; SVG->PNG via cairosvg
avoids that. drawj2d would also work but needs a JRE.)
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import cairosvg

# reMarkable Paper Pro panel width in px; height scales with the canvas.
DEFAULT_WIDTH = 1404


def _rmc_bin() -> str:
    """Locate the `rmc` console script. Prefer the one in the running
    interpreter's venv (sibling of sys.executable) so rendering works even when
    .venv/bin isn't on PATH — e.g. the watcher launches via .venv/bin/python3
    directly, which sets the interpreter but NOT PATH. Fall back to PATH."""
    cand = Path(sys.executable).parent / "rmc"
    return str(cand) if cand.exists() else "rmc"


def rm_to_png(rm_path: str | Path, png_path: str | Path,
              width: int = DEFAULT_WIDTH) -> Path:
    rm_path, png_path = Path(rm_path), Path(png_path)
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tf:
        svg_path = Path(tf.name)
    try:
        # rmscene warns about unread (newer Paper Pro) blocks; strokes still render.
        subprocess.run([_rmc_bin(), "-t", "svg", str(rm_path), "-o", str(svg_path)],
                       check=True, capture_output=True)
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
