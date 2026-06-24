"""Parse the reply-Claude's drawing DSL into `.rm` v6 pen strokes.

A reply may contain ONE `<<DRAW>> … <<END>>` block (opt-in; the persona only emits
it when the user asks for a diagram/sketch). The block holds simple primitive
commands on a logical grid; `parse_draw` turns them into a `DrawSpec` (a flat list
of polyline `Stroke`s in DSL coordinates), and `spec_to_line_blocks` maps those
into the page's canvas coordinates and builds rmscene `SceneLineItemBlock`s that
attach to the text page's existing layer.

DSL (one command per line, inside `<<DRAW w=1000 h=600>> … <<END>>`):
  line   x1,y1 x2,y2 [#color]            polyline if more than two points given
  arrow  x1,y1 x2,y2 [#color]            shaft + arrowhead at the last point
  box    x1,y1 x2,y2 ["label"] [#color]  rectangle (corners), optional centred label
  rect   … (alias of box)
  ellipse cx,cy r=R [#color]             also rx=,ry= ; `circle` is an alias
  polyline x1,y1 x2,y2 … [#color]        open path through all points (`path` alias)
  dot    x,y [#color]                    a small filled mark
  text   x,y "label" [size=N] [#color]   free-standing label (top-left anchored)

Coordinates: x grows right, y grows DOWN, origin top-left; default grid 1000×(auto).
Colours: black (default), gray/grey, blue, red, green, yellow. Unknown commands or
malformed lines are skipped (logged) so a model slip never breaks the whole reply.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from rmscene import scene_stream as ss
from rmscene.scene_items import Line, Pen, PenColor, Point

import hershey

log = logging.getLogger(__name__)

_DRAW_RE = re.compile(r"<<\s*DRAW([^>]*)>>(.*?)<<\s*END\s*>>", re.IGNORECASE | re.DOTALL)
# Fallback if the model forgets <<END>>: take everything after <<DRAW…>>.
_DRAW_OPEN_RE = re.compile(r"<<\s*DRAW([^>]*)>>(.*)\Z", re.IGNORECASE | re.DOTALL)
_NUM = r"-?\d+(?:\.\d+)?"
_PT_RE = re.compile(rf"^({_NUM}),({_NUM})$")
_ATTR_RE = re.compile(rf"(\w+)\s*=\s*({_NUM})")
_QUOTED_RE = re.compile(r'"([^"]*)"')

_COLORS = {
    "black": PenColor.BLACK, "gray": PenColor.GRAY, "grey": PenColor.GRAY,
    "blue": PenColor.BLUE, "red": PenColor.RED, "green": PenColor.GREEN,
    "yellow": PenColor.YELLOW,
}

SHAPE_THICKNESS = 2.0    # thickness_scale for shape outlines
LABEL_THICKNESS = 1.5    # thinner, for glyph strokes
_ELLIPSE_SEGMENTS = 48
_LAYER_ID = ss.CrdtId(0, 11)   # "Layer 1" created by ss.simple_text_document
_FIRST_CRDT = 256              # text uses (1,1)/(1,15)/(1,16); start strokes well above


@dataclass
class Stroke:
    points: list[tuple[float, float]]   # DSL coords
    color: PenColor = PenColor.BLACK
    thickness: float = SHAPE_THICKNESS


@dataclass
class DrawSpec:
    w: float = 1000.0
    h: float = 600.0
    strokes: list[Stroke] = field(default_factory=list)

    def add(self, points, color=PenColor.BLACK, thickness=SHAPE_THICKNESS):
        if len(points) >= 2:
            self.strokes.append(Stroke(points, color, thickness))


def parse_draw(reply_text: str) -> tuple[str, DrawSpec | None]:
    """Split a reply into (prose, DrawSpec|None). The first `<<DRAW>>…<<END>>`
    block is parsed into strokes; all draw regions are stripped from the prose."""
    m = _DRAW_RE.search(reply_text) or _DRAW_OPEN_RE.search(reply_text)
    if not m:
        return reply_text, None
    prose = _DRAW_RE.sub("", reply_text)
    prose = _DRAW_OPEN_RE.sub("", prose).strip()
    spec = _parse_block(m.group(1), m.group(2))
    return prose, (spec if spec.strokes else None)


def _parse_block(attr_str: str, body: str) -> DrawSpec:
    attrs = {k.lower(): float(v) for k, v in _ATTR_RE.findall(attr_str or "")}
    spec = DrawSpec(w=attrs.get("w", 1000.0), h=attrs.get("h", 0.0))
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        try:
            _parse_command(line, spec)
        except Exception as e:                      # one bad line must not kill the rest
            log.warning("draw: skipping line %r (%s)", line, e)
    if not spec.h:                                  # auto height from content bbox
        ys = [y for s in spec.strokes for _, y in s.points]
        spec.h = max(ys) if ys else 600.0
    return spec


def _parse_command(line: str, spec: DrawSpec) -> None:
    label_m = _QUOTED_RE.search(line)
    label = label_m.group(1) if label_m else None
    bare = _QUOTED_RE.sub("", line)
    toks = bare.split()
    cmd = toks[0].lower()
    color = PenColor.BLACK
    attrs: dict[str, float] = {}
    pts: list[tuple[float, float]] = []
    for t in toks[1:]:
        if t.startswith("#"):
            color = _COLORS.get(t[1:].lower(), PenColor.BLACK)
        elif "=" in t:
            k, _, v = t.partition("=")
            try:
                attrs[k.lower()] = float(v)
            except ValueError:
                pass
        else:
            pm = _PT_RE.match(t)
            if pm:
                pts.append((float(pm.group(1)), float(pm.group(2))))

    if cmd in ("line", "polyline", "path"):
        if cmd == "line" and len(pts) > 2:
            pts = [pts[0], pts[-1]]
        spec.add(pts, color)
    elif cmd == "arrow":
        _arrow(spec, pts, color)
    elif cmd in ("box", "rect"):
        _box(spec, pts, color, label)
    elif cmd in ("ellipse", "circle"):
        _ellipse(spec, pts, attrs, color)
    elif cmd == "dot":
        _dot(spec, pts, color)
    elif cmd == "text":
        _text(spec, pts, label, attrs, color)
    else:
        log.warning("draw: unknown command %r", cmd)


def _arrow(spec: DrawSpec, pts, color) -> None:
    if len(pts) < 2:
        return
    a, b = pts[0], pts[-1]
    spec.add([a, b], color)
    dx, dy = b[0] - a[0], b[1] - a[1]
    ang = math.atan2(dy, dx)
    hl = max(8.0, 0.025 * spec.w)
    for da in (math.radians(150), math.radians(-150)):
        spec.add([b, (b[0] + hl * math.cos(ang + da),
                      b[1] + hl * math.sin(ang + da))], color)


def _box(spec: DrawSpec, pts, color, label) -> None:
    if len(pts) < 2:
        return
    (x0, y0), (x1, y1) = pts[0], pts[1]
    spec.add([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], color)
    if label:
        _centered_label(spec, label, (x0 + x1) / 2, (y0 + y1) / 2,
                        abs(x1 - x0), abs(y1 - y0), color)


def _ellipse(spec: DrawSpec, pts, attrs, color) -> None:
    if not pts:
        return
    cx, cy = pts[0]
    rx = attrs.get("rx", attrs.get("r", 50.0))
    ry = attrs.get("ry", attrs.get("r", rx))
    ring = [(cx + rx * math.cos(2 * math.pi * i / _ELLIPSE_SEGMENTS),
             cy + ry * math.sin(2 * math.pi * i / _ELLIPSE_SEGMENTS))
            for i in range(_ELLIPSE_SEGMENTS + 1)]
    spec.add(ring, color)


def _dot(spec: DrawSpec, pts, color) -> None:
    if not pts:
        return
    cx, cy = pts[0]
    r = max(2.0, 0.004 * spec.w)
    spec.add([(cx - r, cy), (cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
             color, thickness=SHAPE_THICKNESS * 1.5)


def _text(spec: DrawSpec, pts, label, attrs, color) -> None:
    if not pts or not label:
        return
    x, y = pts[0]
    size = attrs.get("size", 0.035 * spec.w)
    for poly in hershey.text_to_polylines(label, x, y, size):
        spec.add(poly, color, thickness=LABEL_THICKNESS)


def _centered_label(spec, label, cx, cy, box_w, box_h, color) -> None:
    unit = hershey.measure(label, 1.0) or 1.0
    size = min(0.55 * box_h, 0.8 * box_w / unit)
    if size <= 0:
        return
    width = hershey.measure(label, size)
    for poly in hershey.text_to_polylines(label, cx - width / 2, cy - size / 2, size):
        spec.add(poly, color, thickness=LABEL_THICKNESS)


def spec_to_line_blocks(spec: DrawSpec, *, col_x0: float, col_w: float,
                        y_top: float) -> list[ss.SceneLineItemBlock]:
    """Map a DrawSpec (DSL coords) into canvas coords and build the line blocks.

    The drawing is fit to the text column [`col_x0`, `col_x0`+`col_w`] with a
    uniform scale (preserving aspect), its top placed at canvas y `y_top` (which
    the caller sets below the text). Blocks attach to the page's existing layer."""
    scale = col_w / spec.w if spec.w else 1.0

    def to_canvas(px, py):
        return (col_x0 + px * scale, y_top + py * scale)

    blocks: list[ss.SceneLineItemBlock] = []
    prev = ss.CrdtId(0, 0)
    nid = _FIRST_CRDT
    for s in spec.strokes:
        pts = [Point(x=cx, y=cy, speed=0, direction=0,
                     width=int(round(s.thickness * 4)), pressure=0)
               for cx, cy in (to_canvas(px, py) for px, py in s.points)]
        ln = Line(color=s.color, tool=Pen.FINELINER_2, points=pts,
                  thickness_scale=s.thickness, starting_length=0.0)
        item = ss.CrdtSequenceItem(item_id=ss.CrdtId(1, nid), left_id=prev,
                                   right_id=ss.CrdtId(0, 0), deleted_length=0, value=ln)
        blocks.append(ss.SceneLineItemBlock(parent_id=_LAYER_ID, item=item))
        prev = ss.CrdtId(1, nid)
        nid += 1
    return blocks
