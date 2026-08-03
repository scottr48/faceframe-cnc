"""Independent verifier for a finished ``.anc`` program.

Nothing in this module is imported by :mod:`~faceframe_cnc.post.generator`
and this module imports no emission code — it re-reads a program the way
the MACHINE would and re-derives everything it needs from the text itself,
exactly as :func:`faceframe_cnc.nesting.validate_layouts` re-derives a
layout rather than trusting the packer.  The templates below are a
deliberate second copy: if someone edits a template in the generator, this
file disagrees and the check fails, which is the point.

What is checked
---------------
``line-endings``  every line ends CRLF; no stray CR or LF; file ends CRLF.
``wrapper``       first and last line are ``%``.
``header``        the identity block and the fixed prologue match the
                  template.  Only the ``(CREATED ON ...)`` text and the
                  O-number digits may differ, plus optional extra comment
                  lines in the banner position (spec section 6).
``footer``        the closing block matches the template byte for byte.
``section``       every tool section opens ``(ROUTE TOOL #n: ...)`` /
                  ``(DIAMETER: d)`` / ``M59`` / restated position / ``Tn``
                  and closes ``M59`` / ``G80``.
``code``          every G/M word appears in the reference files.
``z-limit``       every commanded Z lies in [z_min, z_max] (spec section 8
                  machine protection; factory defaults are the deepest cut
                  and the rapid plane measured in the references).
``dry-run``       only when the config says the program is an air cut
                  (``PostConfig.dry_run``): no FEED move may reach the top
                  of the stock.  The rapid ``G28 Z0`` homing moves in the
                  fixed header and footer are exempt, which is why this is
                  a separate check and not simply a raised ``z_min``.
``bounds``        every commanded X/Y lies within the sheet plus the
                  measured 0.375 trim overhang.
``part-bounds``   every recovered part footprint lies on the sheet.
``foreign-cut``   no cutting move's tool CENTRE enters another part's solid
                  (footprint minus its openings, so a nested inner cut free
                  inside its host's opening is legal), and no THROUGH cut's
                  swept tool width does either.  The swept rule is limited
                  to through cuts because the references do not respect it
                  for shallow ones: R710101N line 44-47 runs a T13 groove
                  0.375 past a part edge, which puts 0.235 of the 0.6299
                  tool body over the neighbour's stile at 0.20 depth.
``geometry``      every cut happens at a Z the post knows (panel groove,
                  opening, detail or a configured perimeter pass) and every
                  closed loop closes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Box, PostConfig, default_config

__all__ = ["Violation", "verify", "verify_file"]

TOL = 1e-6

#: Independent copy of the fixed program lines (see the module docstring).
_HEADER_TAIL = (
    "G0 G20 G91 G28 Z0 M15",
    "G90 G40 M22",
    "M88 B0",
    "M89 B0",
    "G08 P1",
    "M25",
)
_FOOTER = (
    "M59",
    "G80",
    "M22",
    "G91 G28 Z0 M15",
    "G90 H0 M25",
    "M88 B0",
    "M89 B0",
    "G91 G28",
    "G90 X24. Y96.",
    "M59",
    "M07",
    "G08 P0",
    "M30",
    "%",
)
_SECTION_TAIL = ("M59", "G80", "G17 G91 G28 Z0 M95", "M92")

#: Every G/M code that appears in R710101N / R720101N / R730101N.  A
#: generated file may not contain any other code (spec section 9: never
#: invent G/M codes).
_ALLOWED_G = {0, 1, 8, 17, 20, 28, 40, 43, 54, 80, 90, 91}
_ALLOWED_M = {7, 13, 15, 22, 25, 30, 59, 88, 89, 92, 95}

_WORD_RE = re.compile(r"([A-Za-z])(-?\d*\.?\d+)")
_O_RE = re.compile(r"^O\d{4} \(.+\)$")
_CREATED_RE = re.compile(r"^\(CREATED ON .*\)$")
_TOOL_RE = re.compile(r"^\(ROUTE TOOL #(\d+): .+\)$")
_DIA_RE = re.compile(r"^\(DIAMETER: (\d*\.?\d+)\)$")
_COMMENT_RE = re.compile(r"\([^)]*\)")


@dataclass(frozen=True)
class Violation:
    """One problem found in a program.  ``line`` is 1-based, 0 if global."""

    code: str
    message: str
    line: int = 0

    def __str__(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        return f"[{self.code}] {self.message}{where}"


@dataclass
class _Move:
    rapid: bool
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    radius: float
    line: int


def verify_file(path: str, config: PostConfig | None = None) -> list[Violation]:
    with open(path, "r", newline="") as handle:
        return verify(handle.read(), config)


def verify(text: str, config: PostConfig | None = None) -> list[Violation]:
    """Re-parse ``text`` and return every violation found (empty = good)."""
    cfg = config or default_config()
    problems: list[Violation] = []

    problems.extend(_check_line_endings(text))
    lines = text.split("\r\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        return problems + [Violation("wrapper", "the program is empty")]

    problems.extend(_check_wrapper(lines))
    problems.extend(_check_header(lines))
    problems.extend(_check_footer(lines))
    problems.extend(_check_sections(lines))

    moves, motion_problems = _simulate(lines, cfg)
    problems.extend(motion_problems)
    problems.extend(_check_limits(moves, cfg))
    if cfg.dry_run:
        problems.extend(_check_air_cut(moves, cfg))

    parts, geometry_problems = _recover_parts(moves, cfg)
    problems.extend(geometry_problems)
    problems.extend(_check_part_bounds(parts, cfg))
    problems.extend(_check_foreign_cuts(moves, parts, cfg))

    problems.sort(key=lambda v: (v.line, v.code))
    return problems


# --------------------------------------------------------------------------
# text-level checks
# --------------------------------------------------------------------------


def _check_line_endings(text: str) -> list[Violation]:
    problems: list[Violation] = []
    if not text.endswith("\r\n"):
        problems.append(Violation("line-endings", "the file does not end with CRLF"))
    body = text.replace("\r\n", "")
    if "\n" in body:
        problems.append(Violation("line-endings", "a line ends with a bare LF"))
    if "\r" in body:
        problems.append(Violation("line-endings", "a line ends with a bare CR"))
    return problems


def _check_wrapper(lines: list[str]) -> list[Violation]:
    problems: list[Violation] = []
    if lines[0] != "%":
        problems.append(Violation("wrapper", "the program does not open with '%'", 1))
    if lines[-1] != "%":
        problems.append(
            Violation("wrapper", "the program does not close with '%'", len(lines))
        )
    return problems


def _check_header(lines: list[str]) -> list[Violation]:
    problems: list[Violation] = []
    if len(lines) < 12:
        return [Violation("header", "the program is too short to hold a header")]
    if not _O_RE.match(lines[1]):
        problems.append(
            Violation("header", f"bad O-number line: {lines[1]!r}", 2)
        )
    if not _CREATED_RE.match(lines[2]):
        problems.append(
            Violation("header", f"bad (CREATED ON ...) line: {lines[2]!r}", 3)
        )
    if lines[3] != "(MATERIAL: MDF 3/4 )":
        problems.append(Violation("header", f"bad material line: {lines[3]!r}", 4))
    if lines[4] != "(LOAD: Material face DOWN)":
        problems.append(Violation("header", f"bad load line: {lines[4]!r}", 5))

    index = 5
    while index < len(lines) and lines[index].startswith("("):
        index += 1  # optional generated-by banner comments
    for offset, expected in enumerate(_HEADER_TAIL):
        pos = index + offset
        if pos >= len(lines) or lines[pos] != expected:
            got = lines[pos] if pos < len(lines) else "<eof>"
            problems.append(
                Violation(
                    "header",
                    f"prologue line {offset + 1} should be {expected!r}, got {got!r}",
                    pos + 1,
                )
            )
    return problems


def _check_footer(lines: list[str]) -> list[Violation]:
    tail = lines[-len(_FOOTER):]
    if list(tail) != list(_FOOTER):
        first = len(lines) - len(_FOOTER)
        for offset, expected in enumerate(_FOOTER):
            if offset >= len(tail) or tail[offset] != expected:
                got = tail[offset] if offset < len(tail) else "<eof>"
                return [
                    Violation(
                        "footer",
                        f"footer line {offset + 1} should be {expected!r}, got {got!r}",
                        first + offset + 1,
                    )
                ]
    return []


def _check_sections(lines: list[str]) -> list[Violation]:
    problems: list[Violation] = []
    heads = [i for i, line in enumerate(lines) if line.startswith("(ROUTE TOOL")]
    if not heads:
        return [Violation("section", "the program contains no tool section")]
    for pos, head in enumerate(heads):
        match = _TOOL_RE.match(lines[head])
        if not match:
            problems.append(
                Violation("section", f"bad tool header: {lines[head]!r}", head + 1)
            )
            continue
        number = int(match.group(1))
        if head == 0 or lines[head - 1] != "":
            problems.append(
                Violation(
                    "section",
                    "a tool section must be preceded by one blank line",
                    head + 1,
                )
            )
        if not _DIA_RE.match(lines[head + 1]):
            problems.append(
                Violation("section", f"bad diameter line: {lines[head + 1]!r}", head + 2)
            )
        if lines[head + 2] != "M59":
            problems.append(
                Violation("section", "a tool section must open with M59", head + 3)
            )
        if not lines[head + 3].startswith("G0 G54 G90 X"):
            problems.append(
                Violation(
                    "section",
                    "a tool section must restate the current position before the "
                    "tool call",
                    head + 4,
                )
            )
        if lines[head + 4] != f"T{number}":
            problems.append(
                Violation(
                    "section",
                    f"tool section #{number} calls {lines[head + 4]!r}",
                    head + 5,
                )
            )
        end = heads[pos + 1] - 1 if pos + 1 < len(heads) else len(lines) - len(_FOOTER) + 2
        tail = lines[end - 4 : end] if pos + 1 < len(heads) else lines[end - 2 : end]
        expected = list(_SECTION_TAIL) if pos + 1 < len(heads) else ["M59", "G80"]
        if list(tail) != expected:
            problems.append(
                Violation(
                    "section",
                    f"tool section #{number} does not close with {expected}",
                    end,
                )
            )
    return problems


# --------------------------------------------------------------------------
# motion
# --------------------------------------------------------------------------


def _simulate(lines: list[str], cfg: PostConfig) -> tuple[list[_Move], list[Violation]]:
    """Walk the whole program as the control would and return every move."""
    problems: list[Violation] = []
    moves: list[_Move] = []
    modal_rapid = True
    x = y = z = 0.0
    radius = 0.0
    declared: dict[int, float] = {}
    pending_diameter: float | None = None

    for index, raw in enumerate(lines, start=1):
        dia = _DIA_RE.match(raw)
        if dia:
            pending_diameter = float(dia.group(1))
            continue
        code = _COMMENT_RE.sub("", raw).strip()
        if not code or code == "%":
            continue  # blank, comment-only, or a program wrapper line
        words = _WORD_RE.findall(code)
        if not words and code:
            problems.append(Violation("code", f"unreadable line {code!r}", index))
            continue

        new_x, new_y, new_z = x, y, z
        moved = False
        line_rapid = None
        for letter, value in words:
            letter = letter.upper()
            if letter == "G":
                number = int(float(value))
                if number not in _ALLOWED_G:
                    problems.append(
                        Violation("code", f"G{value} is not used by the post", index)
                    )
                if number in (0, 1):
                    line_rapid = number == 0
            elif letter == "M":
                number = int(float(value))
                if number not in _ALLOWED_M:
                    problems.append(
                        Violation("code", f"M{value} is not used by the post", index)
                    )
            elif letter == "T":
                tool = int(float(value))
                if pending_diameter is not None:
                    declared[tool] = pending_diameter
                    pending_diameter = None
                if tool not in declared:
                    problems.append(
                        Violation(
                            "section",
                            f"T{tool} is called without a (DIAMETER: ...) comment",
                            index,
                        )
                    )
                radius = declared.get(tool, 0.0) / 2.0
            elif letter == "X":
                new_x = float(value)
                moved = True
            elif letter == "Y":
                new_y = float(value)
                moved = True
            elif letter == "Z":
                new_z = float(value)
                moved = True
        if line_rapid is not None:
            modal_rapid = line_rapid
        if moved:
            moves.append(
                _Move(modal_rapid, x, y, z, new_x, new_y, new_z, radius, index)
            )
            x, y, z = new_x, new_y, new_z
    return moves, problems


def _check_limits(moves: list[_Move], cfg: PostConfig) -> list[Violation]:
    problems: list[Violation] = []
    for move in moves:
        for z in (move.z0, move.z1):
            if z < cfg.z_min - TOL:
                problems.append(
                    Violation(
                        "z-limit",
                        f"Z{z} is below the {cfg.z_min} floor - spoilboard strike",
                        move.line,
                    )
                )
                break
            if z > cfg.z_max + TOL:
                problems.append(
                    Violation(
                        "z-limit",
                        f"Z{z} is above the {cfg.z_max} ceiling - overtravel",
                        move.line,
                    )
                )
                break
        low_x = -cfg.overhang
        low_y = -cfg.overhang
        high_x = cfg.sheet_width + cfg.overhang
        high_y = cfg.sheet_length + cfg.overhang
        for px, py in ((move.x0, move.y0), (move.x1, move.y1)):
            if not (low_x - TOL <= px <= high_x + TOL) or not (
                low_y - TOL <= py <= high_y + TOL
            ):
                problems.append(
                    Violation(
                        "bounds",
                        f"({px}, {py}) is outside the {cfg.sheet_width}x"
                        f"{cfg.sheet_length} sheet plus its {cfg.overhang} overhang",
                        move.line,
                    )
                )
                break
    return problems


def _check_air_cut(moves: list[_Move], cfg: PostConfig) -> list[Violation]:
    """Dry run: no feed move may reach the stock (see the module docstring)."""
    problems: list[Violation] = []
    for move in moves:
        if move.rapid:
            continue
        low = min(move.z0, move.z1)
        if low < cfg.stock_top_z - TOL:
            problems.append(
                Violation(
                    "dry-run",
                    f"a feed move reaches Z{low}, at or below the Z{cfg.stock_top_z} "
                    f"top of the stock - this file is marked as an air cut",
                    move.line,
                )
            )
    return problems


# --------------------------------------------------------------------------
# geometry recovered from the cuts themselves
# --------------------------------------------------------------------------


@dataclass
class _RecoveredPart:
    box: Box
    openings: list[Box]

    def solids(self) -> list[Box]:
        bands = [self.box]
        for opening in self.openings:
            nxt: list[Box] = []
            for band in bands:
                nxt.extend(_subtract(band, opening))
            bands = nxt
        return bands


def _subtract(box: Box, hole: Box) -> list[Box]:
    if not box.overlaps(hole):
        return [box]
    out: list[Box] = []
    if hole.y0 > box.y0 + TOL:
        out.append(Box(box.x0, box.y0, box.x1, hole.y0))
    if hole.y1 < box.y1 - TOL:
        out.append(Box(box.x0, hole.y1, box.x1, box.y1))
    y0 = max(box.y0, hole.y0)
    y1 = min(box.y1, hole.y1)
    if y1 > y0 + TOL:
        if hole.x0 > box.x0 + TOL:
            out.append(Box(box.x0, y0, hole.x0, y1))
        if hole.x1 < box.x1 - TOL:
            out.append(Box(hole.x1, y0, box.x1, y1))
    return out


def _cut_runs(moves: list[_Move]) -> list[list[_Move]]:
    runs: list[list[_Move]] = []
    current: list[_Move] = []
    for move in moves:
        if move.rapid:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(move)
    if current:
        runs.append(current)
    return runs


def _recover_parts(
    moves: list[_Move], cfg: PostConfig
) -> tuple[list[_RecoveredPart], list[Violation]]:
    """Rebuild part footprints and openings from the cut coordinates alone."""
    problems: list[Violation] = []
    known = {round(cfg.panel.z_cut, 9): ("panel", 0.0)}
    known[round(cfg.openings_pass.z_cut, 9)] = ("opening", cfg.openings_pass.offset)
    known[round(cfg.detail_pass.z_cut, 9)] = ("opening", cfg.detail_pass.offset)
    for spec in cfg.perimeter_passes:
        known[round(spec.z_cut, 9)] = ("perimeter", spec.offset)

    boxes: dict[str, Box] = {}
    opening_boxes: dict[str, Box] = {}
    for run in _cut_runs(moves):
        depth = min(move.z1 for move in run)
        key = round(depth, 9)
        if key not in known:
            problems.append(
                Violation(
                    "geometry",
                    f"a cut runs at Z{depth}, which is not a depth this post uses "
                    f"{sorted(known)}",
                    run[0].line,
                )
            )
            continue
        kind, offset = known[key]
        if kind == "panel":
            continue
        box = _closed_loop_box(run)
        if box is None:
            problems.append(
                Violation("geometry", "a profile loop does not close", run[0].line)
            )
            continue
        recovered = box.grow(-offset).rounded()
        target = boxes if kind == "perimeter" else opening_boxes
        target[repr(recovered)] = recovered

    parts = [_RecoveredPart(box=box, openings=[]) for box in boxes.values()]
    for opening in opening_boxes.values():
        owner = None
        for part in parts:
            if part.box.contains(opening, TOL):
                if owner is None or (part.box.width * part.box.height) < (
                    owner.box.width * owner.box.height
                ):
                    owner = part
        if owner is not None:
            owner.openings.append(opening)
    return parts, problems


def _closed_loop_box(run: list[_Move]) -> Box | None:
    """The rectangle a closed profile loop cut.

    The loop's motion is: ramp in to a point on an edge, four corners, back
    to that point, then an overshoot and a ramp out that both leave the
    rectangle.  So the rectangle is the extent of the path up to the move
    that returns to the lead-in point.
    """
    start = (round(run[0].x1, 6), round(run[0].y1, 6))
    close = None
    for index, move in enumerate(run[1:], start=1):
        if (round(move.x1, 6), round(move.y1, 6)) == start:
            close = index
            break
    if close is None or close < 4:
        return None
    xs = [run[0].x1] + [run[i].x1 for i in range(1, close + 1)]
    ys = [run[0].y1] + [run[i].y1 for i in range(1, close + 1)]
    return Box(min(xs), min(ys), max(xs), max(ys))


def _check_part_bounds(parts: list[_RecoveredPart], cfg: PostConfig) -> list[Violation]:
    problems: list[Violation] = []
    for part in parts:
        box = part.box
        if (
            box.x0 < -TOL
            or box.y0 < -TOL
            or box.x1 > cfg.sheet_width + TOL
            or box.y1 > cfg.sheet_length + TOL
        ):
            problems.append(
                Violation(
                    "part-bounds",
                    f"part footprint {box} does not fit the "
                    f"{cfg.sheet_width}x{cfg.sheet_length} sheet",
                )
            )
    return problems


def _check_foreign_cuts(
    moves: list[_Move], parts: list[_RecoveredPart], cfg: PostConfig
) -> list[Violation]:
    """No cut may run through a part it is not cutting.

    A move is judged against every part except the one whose own feature it
    is cutting, which is the part whose footprint (grown by the trim
    overhang) contains the whole move.
    """
    problems: list[Violation] = []
    solids = [(part, part.solids()) for part in parts]
    through_z = cfg.stock_top_z - cfg.material_thickness

    for move in moves:
        if move.rapid:
            continue
        if min(move.z0, move.z1) >= cfg.stock_top_z - TOL:
            continue  # never enters the stock
        own = _owner_of(move, parts, cfg)
        through = min(move.z0, move.z1) <= through_z + TOL
        band = 0.0
        centre = Box(
            min(move.x0, move.x1),
            min(move.y0, move.y1),
            max(move.x0, move.x1),
            max(move.y0, move.y1),
        )
        swept = centre.grow(move.radius) if through else centre.grow(band)
        for part, bands in solids:
            if part is own:
                continue
            for solid in bands:
                if solid.overlaps(swept, TOL):
                    problems.append(
                        Violation(
                            "foreign-cut",
                            f"a cutting move at Z{min(move.z0, move.z1)} enters the "
                            f"solid of the part at {part.box}",
                            move.line,
                        )
                    )
                    break
            else:
                continue
            break
    return problems


def _owner_of(move: _Move, parts: list[_RecoveredPart], cfg: PostConfig):
    """The part this move is cutting: the smallest footprint (grown by the
    trim overhang) that contains the whole move."""
    span = Box(
        min(move.x0, move.x1),
        min(move.y0, move.y1),
        max(move.x0, move.x1),
        max(move.y0, move.y1),
    )
    best = None
    for part in parts:
        if part.box.grow(cfg.overhang).contains(span, TOL):
            if best is None or (part.box.width * part.box.height) < (
                best.box.width * best.box.height
            ):
                best = part
    return best
