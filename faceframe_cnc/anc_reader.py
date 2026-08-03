"""Minimal, stdlib-only reader for through-cut opening rectangles in .anc files.

This is a verification tool for Milestone 1c: cross-checking the geometry
engine (``faceframe_cnc.geometry``) against a real production NC file's
actual cut coordinates (``docs/CLAUDE_CODE_PROMPT_Faceframe_Optimizer.md``
section 6 and milestone 1). It is deliberately NOT a general G-code
simulator — it parses just enough modal G-code to recover the axis-aligned
tool-center extents of each closed rectangular cut loop in a tool section.

Observed structure of a T11 through-cut opening loop (see R730101N.anc):
a rapid (G0) pre-position, spindle-on, tool-length-comp and rapid-to-Z2
lines, then a G1 "plunge" move that changes one axis while driving Z down
to cutting depth, three or four more G1 moves that trace the rectangle's
edges back to (approximately) the start corner, a couple of small G1
overtravel moves along the same edge (lead-out), and a final G1 move that
lifts Z back up while still moving along that edge. All of the G1-mode
points — including the plunge and lead-out ones — fall within the true
rectangle's X/Y extents in the reference file, because the lead-in/out
moves are collinear with an edge that is already at that edge's coordinate.
So collecting the min/max X and Y over every point visited while in G1
(modal feed) mode, per contiguous G1 run, recovers each loop's tool-center
bounding box exactly.

Only G0/G1 (motion mode) and X/Y/Z (which persist modally) are tracked.
Other G-codes on the same line (G54, G90, G17, G28, G40, G43, G80, G91),
and M/S/F/H/T/B/P words, are parsed and discarded — they don't affect the
tool-center path this module recovers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "Rect",
    "find_tool_sections",
    "extract_rectangles",
]

_TOOL_HEADER_RE = re.compile(r"^\(ROUTE TOOL #(\d+)")
_COMMENT_RE = re.compile(r"\([^)]*\)")
_WORD_RE = re.compile(r"([A-Za-z])(-?\d*\.?\d+)")


@dataclass(frozen=True)
class Rect:
    """Axis-aligned tool-center extents of one closed rectangular cut loop."""

    min_x: float
    max_x: float
    min_y: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y


def _strip_comment(line: str) -> str:
    return _COMMENT_RE.sub("", line).strip()


def find_tool_sections(lines: list[str], tool_number: int) -> list[tuple[int, int]]:
    """Return [(start, end), ...] line-index ranges (end exclusive) for every
    section in ``lines`` whose "(ROUTE TOOL #N ...)" header announces
    ``tool_number``, in file order. ``start`` is the header line's index;
    ``end`` is the index of the next "(ROUTE TOOL" header, or len(lines).
    """
    headers = [i for i, l in enumerate(lines) if l.lstrip().startswith("(ROUTE TOOL")]
    sections = []
    for pos, h in enumerate(headers):
        m = _TOOL_HEADER_RE.match(lines[h].lstrip())
        if not m:
            continue
        end = headers[pos + 1] if pos + 1 < len(headers) else len(lines)
        if int(m.group(1)) == tool_number:
            sections.append((h, end))
    return sections


def extract_rectangles(path: str, tool_number: int = 11, section_index: int = 0) -> list[Rect]:
    """Extract closed rectangular tool-center cut loops from one tool section.

    ``section_index`` selects which occurrence of a "(ROUTE TOOL #tool_number
    ...)" section to read when the tool appears more than once in the file
    (e.g. T11 runs as both the openings-through-cut pass and, later, the
    perimeter pass). Index 0 is the first occurrence in file order.

    Returns one Rect per closed loop, in cut order.
    """
    with open(path, "r", newline="") as f:
        text = f.read()
    lines = text.splitlines()

    sections = find_tool_sections(lines, tool_number)
    if not sections:
        raise ValueError(f"no (ROUTE TOOL #{tool_number} ...) section found in {path!r}")
    if section_index >= len(sections):
        raise ValueError(
            f"only {len(sections)} T{tool_number} section(s) found in {path!r}, "
            f"section_index={section_index} requested"
        )
    start, end = sections[section_index]

    modal_g: int | None = None  # 0 = rapid (G0), 1 = feed (G1)
    x: float | None = None
    y: float | None = None
    loop_points: list[tuple[float, float]] = []
    rects: list[Rect] = []

    def flush() -> None:
        nonlocal loop_points
        if loop_points:
            xs = [p[0] for p in loop_points]
            ys = [p[1] for p in loop_points]
            rects.append(Rect(min(xs), max(xs), min(ys), max(ys)))
        loop_points = []

    for raw in lines[start:end]:
        code = _strip_comment(raw)
        if not code:
            continue
        words = _WORD_RE.findall(code)
        if not words:
            continue

        line_g: int | None = None
        moved = False
        for letter, num in words:
            letter = letter.upper()
            if letter == "G" and num in ("0", "1"):
                line_g = int(num)
            elif letter == "X":
                x = float(num)
                moved = True
            elif letter == "Y":
                y = float(num)
                moved = True
            elif letter == "Z":
                # Z persists modally too but this module only needs X/Y
                # extents, so it isn't tracked beyond triggering `moved`.
                moved = True
            # F, S, M, H, T, B, P and non-motion G-words don't affect the
            # tool-center path and are intentionally ignored.

        if line_g is not None:
            if line_g == 0 and modal_g == 1:
                flush()
            modal_g = line_g

        if moved and modal_g == 1 and x is not None and y is not None:
            loop_points.append((x, y))

    flush()
    return rects
