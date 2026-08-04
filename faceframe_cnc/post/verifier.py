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
``feed``          every F word is a feed the post table gives the tool in
                  the spindle, for the kind of move it is on: an entry
                  (plunge or ramp) move runs at that operation's
                  ``entry_feed`` and a cutting move at the ``cut_feed`` of an
                  operation that tool performs.  Modal, as the control is
                  (see below).
``spindle-speed`` every S word is the RPM the post table gives the tool in
                  the spindle, every ``M13`` states one, and every tool
                  section states one somewhere.
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
``v-slot``        the 45-degree T17 slot, judged on the cone it actually
                  sweeps rather than on its centreline (see below).  V-bit
                  moves are excluded from ``foreign-cut`` so that one rule
                  owns them.
``geometry``      every cut happens at a Z the post knows (panel groove,
                  WDC slot pass, opening, detail or a configured perimeter
                  pass) and every closed loop closes.
``missing-cut``   a cut the sheet's layout calls for is not in the file.
``extra-cut``     a cut is in the file that the layout does not call for.
                  Both of these are checked ONLY when the caller hands
                  :func:`verify` an :class:`ExpectedWork` manifest (see
                  below); with ``expected=None`` the file is judged entirely
                  on its own, exactly as it always was.

Foreign cuts and MISSING cuts (2026-08-04 review)
-------------------------------------------------
Every check above answers "does this file do something it must not?".  None
of them can answer "does this file do everything it must?", because a
re-parse only ever sees what is there.  Three ways a program can be
catastrophically wrong and still pass every rule above, all real:

*   drop each part's full-depth perimeter pass but keep the onion-skin
    pass.  Every part is still recovered (from the shallow loop), every
    coordinate is still legal, and the machine leaves the whole sheet
    attached at 0.06 of skin;
*   drop one opening's T11 and T12 loops.  The verifier then reads that
    area as solid frame — and agrees with itself about it;
*   drop a WDC's T17 slots, or a frame's T13 groove.  Nothing is out of
    bounds, so nothing complains.

The fix is that the caller who KNOWS what the sheet holds says so.
:func:`expected_work` turns an optimizer :class:`SheetLayout` into an
:class:`ExpectedWork` manifest — one entry per cut the sheet owes — and
:func:`verify` then requires that the file's recovered cuts and the
manifest match, one for one, in both directions.
:func:`faceframe_cnc.post.job.build_job` passes one for the production text
and one for the dry-run text (a rehearsal missing a cut is still wrong).

The manifest is derived from the LAYOUT, not from the emitter: placements,
:func:`faceframe_cnc.geometry.compute_geometry`, and the measured tables in
:mod:`~faceframe_cnc.post.model`.  It deliberately does NOT import
:mod:`~faceframe_cnc.post.from_layout` (the planner) or
:mod:`~faceframe_cnc.post.generator` (the emitter), and re-derives the
groove and slot centrelines, the opening transform for a rotated placement
and the per-pass offsets as a second copy — for the same reason the header
templates above are a second copy.  A manifest built from the plan would
agree with the emitter by construction and could never catch the emitter
dropping a cut, which is the entire point of this check.

The V-slot rule
---------------
A 45-degree V bit does not cut a slot as wide as the tool: it cuts a cone,
so at depth ``d`` the material it removes is ``d`` wide either side of the
centreline and the cut spreads ``d`` past each END of the commanded move.
That makes the swept region of a slot pass the centreline dilated by ``d``
— which for the deep pass reaches 0.875 past the part it belongs to, twice
the ordinary trim overhang and nearly twice the part gap.

This check re-derives that region from the text: it finds the moves made
with the V bit (by the diameter the program itself declares), works out
each one's depth of cut from the commanded Z, and rejects the file if the
swept region touches the solid of any part other than the one the cut runs
down the middle of, or leaves the sheet plus its 0.375 overhang.  The
dilation is applied to the move's bounding box, which over-states the two
rounded ends by a corner each — deliberately, since erring outward is the
safe direction for a check whose job is to catch a cut nobody approved.

Cuts that never reach the stock (a dry-run file) have no cone and are
skipped, exactly as they are by ``foreign-cut``.

Feeds and spindle speeds (2026-08-04, owner-approved follow-up)
--------------------------------------------------------------
Until this change the verifier read no ``F`` and no ``S`` word at all, so a
program that cut every part in exactly the right place at 900 ipm with the
spindle at 12000 rpm verified clean: right geometry, wrong machining, burnt
bit and a scorched frame.  Recorded as a follow-up when the manifest went
in, approved by the owner on 2026-08-04, and closed here.

The grammar these rules encode is the one the reference files actually use,
read off R710101N / R720101N / R730101N (and RFK0101N for T17):

*   **one S word per tool section**, on the section's first preposition —
    the line that starts the spindle (``M13``).  Its value is the tool's
    measured RPM: T13 17500, T11 16700, T12 17000, T17 16000.  R710101N has
    four sections and four S words, the two T11 sections both stating
    16700.  A section that starts the spindle without saying how fast is the
    hazard here: the control simply keeps whatever the previous section left
    in it, so a 0.2 downshear would spin up at the panel cutter's speed.
*   **two F words per feature**: the plunge or lead-in ramp states the
    operation's ``entry_feed`` and the first cutting move states its
    ``cut_feed``.  Every later move of the feature — the remaining three
    corners of a loop, the return to the lead-in point, the overshoot and
    the lead-out ramp — states no F at all and runs on the modal one.
*   the same tool legitimately uses **more than one cutting feed in one
    program**: T11 cuts openings at F545 and perimeters at F498.2 (both
    depth passes), so the rule per tool is a SET of table feeds, not a
    single number.  T11's entry feed happens to be 150 for both.

So the check re-derives, from the SAME :class:`~.model.PostConfig` it is
handed, which tool numbers perform which operations and what each
operation's two feeds are, and then judges each feed move by its own
geometry: a move that descends is an entry move and must run at an entry
feed of that tool; anything else is a cutting move and must run at one of
its cut feeds.  Nothing is hardcoded — a dry-run table, a future feed table
or a shop that re-times a tool judges itself, and the reference files are
the ground truth the rules were read off rather than an assumption they are
held to.

Modality is tracked the way the control tracks it: an F persists until
another F replaces it, across ``G0`` moves and section boundaries alike.
That is the point of the rule — the bug worth catching is not a visibly
wrong number, it is a MISSING one, where a cutting move silently inherits
the plunge feed (or the previous feature's) and nobody can see it in the
text.  A violation is therefore attributed to the line where the wrong
value takes effect on a cutting move, not to the line that stated it, and
says which line it was inherited from.

Two deliberate exemptions:

*   **rapids carry no feed check.**  A ``G0`` ignores the feed word, so an F
    on one is not itself a fault; it does change the modal value, and the
    check picks that up at the next move that FEEDS on it, which is where it
    can do harm.  No reference file puts an F on a ``G0``.
*   **height is not an exemption.**  The rule judges every feed move
    whatever Z it is at, including moves entirely above the stock, for one
    decisive reason: the dry-run twin (:func:`~.job.dry_run_config`) lifts
    every cut ABOVE the stock and changes nothing else, so a rule that only
    bit on moves reaching the material would switch itself off for exactly
    the rehearsal the operator watches to confirm a program's feeds.  In the
    production references there is no feed move fully clear of the stock
    anyway: even the lead-out ramp starts at the cut depth.

What the feed rule does NOT catch, deliberately: T11 cutting a PERIMETER at
F545, the openings' feed.  The commanded Z would identify which operation a
move belongs to and so pin the feed to one number instead of the tool's set,
but that would rebuild the Z-to-operation map inside a feed rule — and that
map is ``geometry``'s and the manifest's job.  A file with a wrong Z would
then report a cascade of feed findings about the same lines it already
reports one clear depth finding about.  Recorded here rather than fixed:
one tool, one set of its own feeds, is the rule the owner approved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..geometry import FrameType, compute_geometry, infer_frame_type
from .model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_WDC_SLOT,
    Box,
    PostConfig,
    default_config,
)

__all__ = [
    "Violation",
    "ExpectedCut",
    "ExpectedWork",
    "expected_work",
    "verify",
    "verify_file",
]

TOL = 1e-6

#: Decimals a coordinate reaches the file with.  The post prints four and
#: strips trailing zeros, so a program can only ever state a coordinate to
#: this precision — and a manifest coordinate is therefore compared at it,
#: with :data:`TOL` on top for float noise.  This is not a looser tolerance
#: than the rest of the module: it is the same 0.0001 grid the reference
#: files are drawn on (see :data:`~faceframe_cnc.post.model.EPS`).
PLACES = 4

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
    """One commanded move, with the modal state that was in force for it.

    ``radius`` is the tool's, ``tool`` its number, and ``feed``/``feed_line``
    the F word the control would have used and the line it last came from —
    which is the move's own line when the move states its own F, and an
    earlier one when it inherits (see the module docstring's feed section).
    ``feed`` is ``None`` only before the program's first F word.
    """

    rapid: bool
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    radius: float
    line: int
    tool: int = 0
    feed: float | None = None
    feed_line: int = 0


def verify_file(
    path: str,
    config: PostConfig | None = None,
    expected: "ExpectedWork | None" = None,
) -> list[Violation]:
    with open(path, "r", newline="") as handle:
        return verify(handle.read(), config, expected)


def verify(
    text: str,
    config: PostConfig | None = None,
    expected: "ExpectedWork | None" = None,
) -> list[Violation]:
    """Re-parse ``text`` and return every violation found (empty = good).

    ``expected`` is optional and changes nothing about the checks above it:
    with ``None`` (the default, and the only possibility for a file the shop
    already cut, which has no layout behind it) the program is judged purely
    on its own contents.  Given an :class:`ExpectedWork` manifest — see
    :func:`expected_work` and the module docstring — the file must ALSO
    contain every cut the sheet owes and nothing else.
    """
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
    # Feeds and speeds are judged against the SAME table, and independently
    # of the expected-work manifest: a reference file with no layout behind
    # it still has to be cutting at the right feed (2026-08-04 follow-up).
    problems.extend(_check_feeds(moves, cfg))
    problems.extend(_check_speeds(lines, cfg))
    if cfg.dry_run:
        problems.extend(_check_air_cut(moves, cfg))

    parts, found, geometry_problems = _recover_parts(moves, cfg)
    problems.extend(geometry_problems)
    problems.extend(_check_part_bounds(parts, cfg))
    problems.extend(_check_foreign_cuts(moves, parts, cfg))
    problems.extend(_check_v_slot_cuts(moves, parts, cfg))
    if expected is not None:
        problems.extend(_check_expected_work(found, expected))

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
    tool_number = 0
    # Modal feed, exactly as the control holds it: set by any F word on any
    # line (a G0 ignores it for its own motion but still latches it) and
    # never cleared, not by a retract and not by a section change.
    modal_feed: float | None = None
    modal_feed_line = 0
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
                tool_number = tool
            elif letter == "F":
                modal_feed = float(value)
                modal_feed_line = index
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
                _Move(
                    modal_rapid,
                    x,
                    y,
                    z,
                    new_x,
                    new_y,
                    new_z,
                    radius,
                    index,
                    tool_number,
                    modal_feed,
                    modal_feed_line,
                )
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
# feeds and spindle speeds (2026-08-04, owner-approved follow-up)
# --------------------------------------------------------------------------
#
# See the module docstring's "Feeds and spindle speeds" section for the
# grammar these two rules encode and for the two deliberate exemptions
# (rapids, and NOT exempting moves above the stock).


#: What each post-table section's operation is called in a refusal, without
#: its tool number — that is prefixed from the table, so a shop that moves an
#: operation to another tool gets a message that still reads true.
_OPERATION_NAMES = {
    SECTION_PANEL: "panel groove",
    SECTION_WDC_SLOT: "45-degree stile slot",
    SECTION_OPENINGS: "opening through-cut",
    SECTION_DETAIL: "opening finish pass",
    SECTION_PERIMETER: "perimeter pass",
}


def _operations(cfg: PostConfig) -> list[tuple[int, str, object]]:
    """``[(tool number, what it is, the spec carrying its two feeds)]``.

    Every operation the post can emit, taken from the config it was handed
    and nowhere else.  :class:`~.model.PanelSpec`,
    :class:`~.model.WdcSlotSpec` and :class:`~.model.PassSpec` all carry
    ``entry_feed`` and ``cut_feed``, which is what lets one walk cover the
    lot; a section with no tool in the table contributes nothing, so a
    cut-down table (one with no T17, say) simply has no T17 rule rather than
    an empty one that would accept anything.
    """
    out: list[tuple[int, str, object]] = []

    def add(section: str, spec, suffix: str = "") -> None:
        tool = cfg.tools.get(section)
        if tool is None:
            return
        out.append(
            (tool.number, f"T{tool.number} {_OPERATION_NAMES[section]}{suffix}", spec)
        )

    add(SECTION_PANEL, cfg.panel)
    add(SECTION_WDC_SLOT, cfg.wdc_slot)
    add(SECTION_OPENINGS, cfg.openings_pass)
    add(SECTION_DETAIL, cfg.detail_pass)
    total = len(cfg.perimeter_passes)
    for position, spec in enumerate(cfg.perimeter_passes):
        add(SECTION_PERIMETER, spec, f" {position + 1} of {total}")
    return out


#: ``{feed -> the operations that run at it}``.  The operations ride along so
#: a refusal can say what the table MEANS and not just what number it holds.
_FeedUses = dict[float, list[str]]


def _tool_feeds(cfg: PostConfig) -> dict[int, tuple[_FeedUses, _FeedUses]]:
    """Per tool number, ``(entry feeds, cutting feeds)`` -> which operations.

    A SET of feeds per tool per kind of move, because one tool honestly runs
    at more than one: T11 cuts openings at F545 and perimeters at F498.2 in
    the same program.  The operations behind each number are kept so a
    refusal can say what the table means, not just what it says.
    """
    table: dict[int, tuple[_FeedUses, _FeedUses]] = {}
    for number, what, spec in _operations(cfg):
        entry, cut = table.setdefault(number, ({}, {}))
        entry.setdefault(round(float(spec.entry_feed), PLACES), []).append(what)
        cut.setdefault(round(float(spec.cut_feed), PLACES), []).append(what)
    return table


def _fword(value: float) -> str:
    """A feed the way the program states it (four decimals, zeros stripped).

    Second copy of :func:`~.generator.fmt`, for the same reason every other
    template in this module is one — and so a refusal quotes the number in
    the form the operator will find in the file: ``F490.``, not ``F490.0``.
    """
    return f"{round(value, PLACES):.4f}".rstrip("0")


def _feed_choices(feeds: _FeedUses) -> str:
    """``F490. (T13 panel groove)`` / ``F498.2 (...) or F545. (...)``."""
    if not feeds:
        return "nothing - the post table gives it no feed at all"
    return " or ".join(
        f"F{_fword(value)} ({', '.join(feeds[value])})" for value in sorted(feeds)
    )


def _check_feeds(moves: list[_Move], cfg: PostConfig) -> list[Violation]:
    """Every feed move runs at a feed ``cfg`` gives its tool for that move.

    The move's own geometry decides which of the two feeds applies: a move
    that DESCENDS is the plunge or lead-in ramp of a feature and owes an
    ``entry_feed``; anything else — a lateral cut, the overshoot, the
    climbing lead-out that is still in the material when it starts — runs on
    the ``cut_feed`` the first cutting move of the feature set, and owes one
    of the tool's.  Rapids are skipped: a ``G0`` ignores F.

    A tool the table does not describe is skipped here and reported once by
    :func:`_check_speeds` instead, because "I cannot judge this tool" is one
    finding about the tool, not one per move it makes.
    """
    problems: list[Violation] = []
    table = _tool_feeds(cfg)
    for move in moves:
        if move.rapid:
            continue
        feeds = table.get(move.tool)
        if feeds is None:
            continue
        descending = move.z1 < move.z0 - TOL
        allowed = feeds[0] if descending else feeds[1]
        what = "a plunge/ramp into the cut" if descending else "a cutting move"
        kind = "entry" if descending else "cutting"
        if move.feed is None:
            problems.append(
                Violation(
                    "feed",
                    f"{what} with T{move.tool} in the spindle runs before the "
                    f"program states any F word, so it would run at whatever feed "
                    f"the last program left in the control - the post table's "
                    f"{kind} feed for T{move.tool} is {_feed_choices(allowed)}",
                    move.line,
                )
            )
            continue
        found = round(move.feed, PLACES)
        if any(abs(found - value) <= TOL for value in allowed):
            continue
        origin = (
            ""
            if move.feed_line == move.line
            else f", inherited from the F word on line {move.feed_line}"
        )
        problems.append(
            Violation(
                "feed",
                f"{what} with T{move.tool} in the spindle runs at "
                f"F{_fword(move.feed)}{origin} - the post table's {kind} feed for "
                f"T{move.tool} is {_feed_choices(allowed)}",
                move.line,
            )
        )
    return problems


def _check_speeds(lines: list[str], cfg: PostConfig) -> list[Violation]:
    """Every S word is the RPM the post table gives the tool in the spindle.

    Its own small walk of the text rather than a hitch-hike on
    :func:`_simulate`: the rule is purely about words (which tool is called,
    what speed is commanded, where the spindle is started) and owes nothing
    to where the machine is, so keeping it separate keeps both readable.

    Three findings, all of them things every reference file satisfies: an S
    that is not the table's speed for the tool; an ``M13`` that starts the
    spindle without saying how fast, which leaves the previous section's RPM
    in it; and a tool section that states no speed anywhere.  Plus the honest
    admission that a tool the table does not describe cannot be judged at all
    — which is also what turns off :func:`_check_feeds` for it.
    """
    problems: list[Violation] = []
    speeds: dict[int, set[int]] = {}
    for tool in cfg.tools.values():
        speeds.setdefault(tool.number, set()).add(int(tool.speed))

    head = 0  # line of the section head currently open, 0 for none
    tool_number = 0
    stated = 0  # S words seen since the section head

    def close_section() -> None:
        if head and tool_number and not stated:
            wanted = speeds.get(tool_number)
            problems.append(
                Violation(
                    "spindle-speed",
                    f"the T{tool_number} section states no spindle speed, so the "
                    f"spindle would keep whatever the previous section left it at - "
                    f"the post table runs T{tool_number} at "
                    f"{_speed_choices(wanted)}",
                    head,
                )
            )

    for index, raw in enumerate(lines, start=1):
        if _TOOL_RE.match(raw):
            close_section()
            head, tool_number, stated = index, 0, 0
            continue
        code = _COMMENT_RE.sub("", raw).strip()
        if not code or code == "%":
            continue
        words = _WORD_RE.findall(code)
        commanded: int | None = None
        starts_spindle = False
        for letter, value in words:
            letter = letter.upper()
            if letter == "T":
                tool_number = int(float(value))
                if tool_number not in speeds:
                    problems.append(
                        Violation(
                            "spindle-speed",
                            f"T{tool_number} is not a tool in the post table, so "
                            f"nothing here can say what speed or feed it should "
                            f"run at",
                            index,
                        )
                    )
            elif letter == "M" and int(float(value)) == 13:
                starts_spindle = True
            elif letter == "S":
                commanded = int(float(value))
        if (commanded is not None or starts_spindle) and not tool_number:
            # Structurally impossible in a file that passes ``section`` (which
            # requires the Tn call before a section's first feature), so this
            # is a guard rather than a rule: without a tool there is no table
            # row to judge against, and saying "T0" would be nonsense.
            stated += 1
            problems.append(
                Violation(
                    "spindle-speed",
                    "the spindle is commanded before any tool is called, so nothing "
                    "here says whose speed it should be",
                    index,
                )
            )
        elif commanded is not None:
            stated += 1
            wanted = speeds.get(tool_number)
            if wanted is None:
                pass  # already reported against the T word above
            elif commanded not in wanted:
                problems.append(
                    Violation(
                        "spindle-speed",
                        f"S{commanded} with T{tool_number} in the spindle - the "
                        f"post table runs T{tool_number} at {_speed_choices(wanted)}",
                        index,
                    )
                )
        elif starts_spindle:
            wanted = _speed_choices(speeds.get(tool_number))
            problems.append(
                Violation(
                    "spindle-speed",
                    f"the spindle is started (M13) with no S word, so it would run "
                    f"at whatever speed the previous section left it at - the post "
                    f"table runs T{tool_number} at {wanted}",
                    index,
                )
            )
    close_section()
    return problems


def _speed_choices(wanted: set[int] | None) -> str:
    if not wanted:
        return "no speed at all (it is not in the tool table)"
    return " or ".join(f"S{value}" for value in sorted(wanted))


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


@dataclass(frozen=True)
class _FoundCut:
    """One cutting run the file actually makes, kept WITH its depth.

    ``kind`` is the section the Z word puts it in (``"groove"``, ``"slot"``,
    ``"opening"``, ``"detail"``, ``"perimeter"``) and ``path`` is the extent
    of the TOOL CENTRE — the closed rectangle for a profile loop, the
    degenerate box of the segment for a straight cut.  Tool-centre space,
    not feature space, because that is what the file literally states: the
    offsets are then only applied on the manifest side, once.
    """

    kind: str
    z: float
    path: Box
    line: int


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
) -> tuple[list[_RecoveredPart], list[_FoundCut], list[Violation]]:
    """Rebuild part footprints and openings from the cut coordinates alone.

    Two things come back, and the difference matters (2026-08-04 review).
    The FOOTPRINTS are deduplicated across depths — a part's two perimeter
    passes recover the same rectangle, and the foreign-cut and v-slot rules
    want one part, not two.  The INVENTORY (:class:`_FoundCut`, second
    return value) is not: it is every cutting run the file makes, kept with
    the Z it was made at, because "the second pass is missing" is invisible
    to anything that has already collapsed the two passes into one
    rectangle.  That collapse is exactly what hid a dropped through pass
    before this was split in two.
    """
    problems: list[Violation] = []
    # Straight cuts (the T13 groove, the T17 slot passes) carry no footprint
    # to recover, so they contribute to the inventory only; where they are
    # ALLOWED to reach is _check_v_slot_cuts / _check_foreign_cuts below.
    known = {round(cfg.panel.z_cut, 9): ("groove", 0.0)}
    for z_cut in cfg.wdc_slot.z_cuts:
        known[round(z_cut, 9)] = ("slot", 0.0)
    known[round(cfg.openings_pass.z_cut, 9)] = ("opening", cfg.openings_pass.offset)
    known[round(cfg.detail_pass.z_cut, 9)] = ("detail", cfg.detail_pass.offset)
    for spec in cfg.perimeter_passes:
        known[round(spec.z_cut, 9)] = ("perimeter", spec.offset)

    found: list[_FoundCut] = []
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
        if kind in ("groove", "slot"):
            found.append(_FoundCut(kind, depth, _straight_extent(run), run[0].line))
            continue
        box = _closed_loop_box(run)
        if box is None:
            problems.append(
                Violation("geometry", "a profile loop does not close", run[0].line)
            )
            continue
        found.append(_FoundCut(kind, depth, box, run[0].line))
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
    return parts, found, problems


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


def _straight_extent(run: list[_Move]) -> Box:
    """The extent a straight cut (T13 groove, T17 slot pass) swept.

    The measured grammar for both is "plunge, then move one axis" — a plunge
    move that only changes Z and a cut move that only changes X or Y — so
    the extent of the run's endpoints IS the segment, and a degenerate box
    holds it without needing a second geometry type.  A run with more moves
    in it than that is not a straight cut this post writes, and comes back
    as a box that matches no manifest entry, which is the right answer.
    """
    xs = [move.x1 for move in run]
    ys = [move.y1 for move in run]
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
        if _v_bit_radius(move, cfg) is not None:
            continue  # the cone rule owns these (see _check_v_slot_cuts)
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


def _v_bit_radius(move: _Move, cfg: PostConfig) -> float | None:
    """The V bit's own radius when ``move`` is being made with it, else None.

    Identified by the diameter the PROGRAM declares for the tool it called,
    which is the only thing in the text that says which cutter is in the
    spindle.  No other tool in the post table shares 0.96.
    """
    tool = cfg.tools.get(SECTION_WDC_SLOT)
    if tool is None:
        return None
    return tool.radius if abs(2.0 * move.radius - tool.diameter) <= TOL else None


def _check_v_slot_cuts(
    moves: list[_Move], parts: list[_RecoveredPart], cfg: PostConfig
) -> list[Violation]:
    """The 45-degree slot, judged on the cone it sweeps (module docstring)."""
    problems: list[Violation] = []
    solids = [(part, part.solids()) for part in parts]
    low_x, low_y = -cfg.overhang, -cfg.overhang
    high_x = cfg.sheet_width + cfg.overhang
    high_y = cfg.sheet_length + cfg.overhang

    for move in moves:
        if move.rapid:
            continue
        tool_radius = _v_bit_radius(move, cfg)
        if tool_radius is None:
            continue
        depth = cfg.stock_top_z - min(move.z0, move.z1)
        if depth <= TOL:
            continue  # above the stock: an air cut removes nothing
        # 45 degrees per side, so the cut is as wide as it is deep - until
        # the bit is buried to its shoulder, where it can get no wider.
        reach = min(depth * cfg.wdc_slot.flank_slope, tool_radius)
        centre = Box(
            min(move.x0, move.x1),
            min(move.y0, move.y1),
            max(move.x0, move.x1),
            max(move.y0, move.y1),
        )
        swept = centre.grow(reach)

        if (
            swept.x0 < low_x - TOL
            or swept.y0 < low_y - TOL
            or swept.x1 > high_x + TOL
            or swept.y1 > high_y + TOL
        ):
            problems.append(
                Violation(
                    "v-slot",
                    f"the 45-degree slot at Z{min(move.z0, move.z1)} cuts "
                    f"{reach} wide either side of its path, sweeping "
                    f"x[{swept.x0:.4f}, {swept.x1:.4f}] "
                    f"y[{swept.y0:.4f}, {swept.y1:.4f}] - outside the "
                    f"{cfg.sheet_width}x{cfg.sheet_length} sheet plus its "
                    f"{cfg.overhang} overhang",
                    move.line,
                )
            )
            continue

        own = _smallest_containing(parts, centre.mid_x, centre.mid_y)
        for part, bands in solids:
            if part is own:
                continue
            for solid in bands:
                if solid.overlaps(swept, TOL):
                    problems.append(
                        Violation(
                            "v-slot",
                            f"the 45-degree slot at Z{min(move.z0, move.z1)} "
                            f"sweeps x[{swept.x0:.4f}, {swept.x1:.4f}] "
                            f"y[{swept.y0:.4f}, {swept.y1:.4f}] and cuts up to "
                            f"{depth} deep into the part at {part.box}",
                            move.line,
                        )
                    )
                    break
            else:
                continue
            break
    return problems


def _smallest_containing(parts: list[_RecoveredPart], x: float, y: float):
    """The smallest recovered footprint holding ``(x, y)``, or None.

    A slot runs down the middle of a stile, so the part it belongs to is
    simply the part its midpoint is inside — no tolerance games with how far
    the ends over-run.  Part footprints never overlap except when one is
    nested in another's opening, which is what "smallest" settles.
    """
    best = None
    for part in parts:
        box = part.box
        if box.x0 - TOL <= x <= box.x1 + TOL and box.y0 - TOL <= y <= box.y1 + TOL:
            if best is None or (box.width * box.height) < (
                best.box.width * best.box.height
            ):
                best = part
    return best


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


# --------------------------------------------------------------------------
# the expected-work manifest (2026-08-04 review)
# --------------------------------------------------------------------------
#
# Everything below answers the question the rest of the file cannot: is any
# cut MISSING?  See the module docstring for why that needs a second input,
# and why that input is the layout rather than the plan the emitter used.


#: What each recovered/expected kind is called in a message.  The kind
#: itself is the SECTION the cut's Z word puts it in, which is all the file
#: says about it.
_KIND_NAMES = {
    "groove": "T13 panel groove",
    "slot": "T17 45-degree stile slot pass",
    "opening": "T11 opening through-cut",
    "detail": "T12 opening finish pass",
    "perimeter": "perimeter loop",
}

_GROOVE_NAMES = {
    0: "stile groove (low side)",
    1: "rail groove (low side)",
    2: "stile groove (high side)",
    3: "rail groove (high side)",
}


@dataclass(frozen=True)
class ExpectedCut:
    """One cut a sheet's program owes, described so a refusal reads plainly.

    ``path`` is the TOOL CENTRE geometry — the closed rectangle for a
    profile loop, a degenerate box for a straight cut — because that is what
    the program states and so what a re-parse can compare it against.
    ``what`` names the feature and ``consequence`` says what the operator
    gets if it is not there; both end up verbatim in the refusal the GUI and
    the PDF cut sheet show.
    """

    kind: str
    z: float
    path: Box
    part_number: str
    part_box: Box
    what: str
    consequence: str

    def describe(self) -> str:
        return (
            f"{self.part_number} @({self.part_box.x0:.4f},{self.part_box.y0:.4f}): "
            f"{self.what} is not in the program - nothing cuts "
            f"x[{self.path.x0:.4f}, {self.path.x1:.4f}] "
            f"y[{self.path.y0:.4f}, {self.path.y1:.4f}] at Z{_zword(self.z)}. "
            f"{self.consequence}"
        )


@dataclass(frozen=True)
class ExpectedWork:
    """Every cut one sheet owes — and, by being complete, nothing else.

    Both directions are load bearing: an entry with no cut in the file is a
    ``missing-cut``, and a cut in the file with no entry here is an
    ``extra-cut``.  That is only sound because the manifest is exhaustive,
    which is why :func:`expected_work` builds it from the whole layout and
    never from a subset of it.
    """

    cuts: tuple[ExpectedCut, ...] = ()

    def __len__(self) -> int:
        return len(self.cuts)

    def counts(self) -> dict[str, int]:
        """How many cuts of each kind, for a caller that wants to say so."""
        out: dict[str, int] = {}
        for cut in self.cuts:
            out[cut.kind] = out.get(cut.kind, 0) + 1
        return out

    def describe(self) -> list[str]:
        """One line per owed cut, in part order (for tests and diagnostics)."""
        return [f"{cut.part_number}: {cut.what}" for cut in self.cuts]


def expected_work(layout, config: PostConfig | None = None) -> ExpectedWork:
    """The manifest of cuts an optimizer sheet owes.

    ``layout`` is a :class:`faceframe_cnc.nesting.SheetLayout` (or any object
    with a ``placements`` list, or the list itself) and ``config`` the post
    table the program was emitted with — the Z levels and offsets have to be
    the SAME table, so a dry-run program is judged against
    :func:`~.job.dry_run_config`'s lifted twin, not against the production
    one.  The sheet size is not used: where a cut may reach is
    ``bounds``/``part-bounds``/``v-slot``'s business, and this is only about
    WHICH cuts exist.

    Every part on the sheet — including frames nested inside another frame's
    opening, which are cut while their slab is still host waste — owes:

    *   its T13 panel grooves, all four, or the RAIL pair only for a WDC
        frame whose stiles take the T17 slot instead (2026-08-03 amendment);
    *   both T17 slot passes down each of a WDC frame's two stiles;
    *   for every opening the frame engine computes: the T11 through-cut
        (tool centre 0.1975 inside the finished opening edge) and the T12
        finish pass;
    *   one perimeter loop per configured depth pass — the onion skin AND
        the full-depth pass that frees the part (2026-08-03 amendment).

    Raises ``ValueError`` when the sheet is empty or the frame engine cannot
    compute a part's geometry: there is then no honest statement of what the
    program owes, and refusing to make one is better than making a short one
    that a file could satisfy.
    """
    cfg = config or default_config()
    placements = getattr(layout, "placements", layout)
    cuts: list[ExpectedCut] = []
    for placement in _walk_placements(placements):
        cuts.extend(_expected_for_placement(placement, cfg))
    if not cuts:
        raise ValueError(
            "this sheet holds no placement, so there is no work to expect of its "
            "program"
        )
    return ExpectedWork(tuple(cuts))


def _walk_placements(placements):
    """Every placement on the sheet, hosts before their passengers."""
    for placement in placements:
        yield placement
        yield from _walk_placements(getattr(placement, "children", ()) or ())


def _expected_for_placement(placement, cfg: PostConfig) -> list[ExpectedCut]:
    box = Box.from_size(
        float(placement.x),
        float(placement.y),
        float(placement.width),
        float(placement.height),
    )
    rotated = bool(getattr(placement, "rotated", False))
    name = placement.part_number
    cuts: list[ExpectedCut] = []

    def add(kind: str, z: float, path: Box, what: str, consequence: str) -> None:
        cuts.append(
            ExpectedCut(
                kind=kind,
                z=z,
                path=path,
                part_number=name,
                part_box=box,
                what=what,
                consequence=consequence,
            )
        )

    # -- T13 panel grooves -------------------------------------------------
    for index in _groove_indices(name):
        add(
            "groove",
            cfg.panel.z_cut,
            _groove_extent(box, rotated, index, cfg.panel),
            f"the T13 {_GROOVE_NAMES[index]}",
            "The frame would come off the machine without the groove its "
            "cabinet seats in.",
        )

    # -- T17 WDC stile slots ----------------------------------------------
    if infer_frame_type(name) is FrameType.WDC:
        passes = len(cfg.wdc_slot.z_cuts)
        for index in (0, 1):
            side = "low" if index == 0 else "high"
            for position, z_cut in enumerate(cfg.wdc_slot.z_cuts):
                add(
                    "slot",
                    z_cut,
                    _slot_extent(
                        box,
                        rotated,
                        index,
                        cfg.wdc_slot,
                        cfg.wdc_slot_reach(position),
                    ),
                    f"the T17 slot down the {side}-side stile, pass "
                    f"{position + 1} of {passes} (centreline "
                    f"{cfg.wdc_slot.inset_from_inside_edge:g} from the stile's "
                    f"inside edge)",
                    "A WDC frame that has not had both passes of both slots "
                    "cannot meet its diagonal-corner cabinet.",
                )

    # -- T11 / T12 openings ------------------------------------------------
    openings = _sheet_openings(placement, box, rotated)
    for label, opening in openings:
        add(
            "opening",
            cfg.openings_pass.z_cut,
            opening.grow(cfg.openings_pass.offset),
            f"the T11 through-cut of the {label!r} opening",
            "The frame would be cut free with solid material where that "
            "opening belongs.",
        )
        add(
            "detail",
            cfg.detail_pass.z_cut,
            opening.grow(cfg.detail_pass.offset),
            f"the T12 finish pass of the {label!r} opening",
            "The opening's slug would never be cut free and the opening "
            "would stay undersize by the finish stock T12 exists to take.",
        )

    # -- T11 perimeter passes ---------------------------------------------
    total = len(cfg.perimeter_passes)
    for position, spec in enumerate(cfg.perimeter_passes):
        if total > 1 and position == 0:
            role = "the onion-skin perimeter pass"
            consequence = (
                "The onion skin is what holds every part while the through "
                "pass runs; without it the first part cut free is loose under "
                "the spindle."
            )
        elif position == total - 1:
            role = "the full-depth perimeter pass"
            consequence = (
                "The part would come off the machine still attached to the "
                "sheet by the onion skin - nothing would have cut it free."
            )
        else:
            role = f"perimeter pass {position + 1}"
            consequence = "The part would not be taken to its next depth."
        add(
            "perimeter",
            spec.z_cut,
            box.grow(spec.offset),
            f"{role} ({position + 1} of {total}, tool centre {spec.offset:g} "
            f"outside the part edge)",
            consequence,
        )

    return cuts


def _sheet_openings(placement, box: Box, rotated: bool) -> list[tuple[str, Box]]:
    """This placement's routed openings, labelled, in SHEET coordinates.

    A deliberate second copy of the packer's rotation convention (a rotated
    placement is the frame turned 90 degrees COUNTER-clockwise, so a
    frame-local opening at ``(x, y, w, h)`` lands at
    ``(X + (W - y - h), Y + x, h, w)``): written out here rather than taken
    from :func:`~.model.program_from_placements`, because a manifest that
    borrowed the emitter's transform could not catch the emitter using the
    wrong one.  The OPENINGS themselves come from
    :func:`faceframe_cnc.geometry.compute_geometry`, which is the one
    authority on frame geometry in this program and is upstream of both.
    """
    ordered_w = box.height if rotated else box.width
    ordered_h = box.width if rotated else box.height
    geom = compute_geometry(placement.part_number, ordered_w, ordered_h)
    if geom.errors:
        raise ValueError(
            f"cannot say what {placement.part_number} owes its program: "
            f"{geom.errors[0]}"
        )
    out: list[tuple[str, Box]] = []
    for opening in geom.openings:
        if rotated:
            out.append(
                (
                    opening.label,
                    Box.from_size(
                        box.x0 + (box.width - opening.y - opening.height),
                        box.y0 + opening.x,
                        opening.height,
                        opening.width,
                    ),
                )
            )
        else:
            out.append(
                (
                    opening.label,
                    Box.from_size(
                        box.x0 + opening.x,
                        box.y0 + opening.y,
                        opening.width,
                        opening.height,
                    ),
                )
            )
    return out


def _groove_indices(part_number: str) -> tuple[int, ...]:
    """Which T13 grooves a part owes: all four, or the rail pair only.

    A WDC frame's 2" stiles take the T17 slot INSTEAD of a stile groove
    (2026-08-03 amendment), so it owes two grooves, not four.  Read off the
    amendment here rather than off
    :func:`~.from_layout.panel_groove_indices`: if that rule is ever changed
    on one side only, this check is what says so.
    """
    if infer_frame_type(part_number) is FrameType.WDC:
        return (1, 3)
    return (0, 2, 1, 3)


def _groove_extent(box: Box, rotated: bool, index: int, panel) -> Box:
    """Where one T13 groove runs, as a degenerate box (measured insets).

    ``index`` 0..3 = stile-low, rail-low, stile-high, rail-high in the
    part's own orientation: the stile grooves sit ``stile_inset`` in from the
    two stile edges and run the full part length plus ``overrun`` at both
    ends; the rail grooves sit ``rail_inset`` in from the rail edges and run
    between the two stile centre lines.  A rotated part's stiles are its
    bottom and top edges.  Second copy of
    :func:`~.generator.groove_segment`, from the same measurements in
    :class:`~.model.PanelSpec`.
    """
    stile, rail, over = panel.stile_inset, panel.rail_inset, panel.overrun
    if not rotated:
        stile_lines = (box.x0 + stile, box.x1 - stile)
        rail_lines = (box.y0 + rail, box.y1 - rail)
        if index in (0, 2):
            x = stile_lines[0 if index == 0 else 1]
            return Box(x, box.y0 - over, x, box.y1 + over)
        y = rail_lines[0 if index == 1 else 1]
        return Box(stile_lines[0], y, stile_lines[1], y)
    stile_lines = (box.y0 + stile, box.y1 - stile)
    rail_lines = (box.x0 + rail, box.x1 - rail)
    if index in (0, 2):
        y = stile_lines[0 if index == 0 else 1]
        return Box(box.x0 - over, y, box.x1 + over, y)
    x = rail_lines[0 if index == 1 else 1]
    return Box(x, stile_lines[0], x, stile_lines[1])


def _slot_extent(box: Box, rotated: bool, index: int, spec, overrun: float) -> Box:
    """Where one T17 slot pass runs its tool centre, as a degenerate box.

    ``index`` 0 is the low-side stile and 1 the high-side one in sheet
    coordinates.  The centreline is measured from the stile's OUTSIDE edge
    (``stile_width - inset_from_inside_edge``, 0.6614 for the measured
    2"/34 mm pair) because that is the edge the placement gives us, and each
    pass runs ``overrun`` past both ends of the part — its own effective
    radius at its own depth, from
    :meth:`~.model.PostConfig.wdc_slot_reach`, which is why the two passes
    of one slot are NOT the same segment.  Second copy of
    :func:`~.generator.wdc_slot_segment`.
    """
    inset = spec.inset_from_outside_edge
    if not rotated:
        x = box.x0 + inset if index == 0 else box.x1 - inset
        return Box(x, box.y0 - overrun, x, box.y1 + overrun)
    y = box.y0 + inset if index == 0 else box.y1 - inset
    return Box(box.x0 - overrun, y, box.x1 + overrun, y)


def _zword(z: float) -> str:
    """A Z the way the program states it (four decimals, zeros stripped)."""
    return f"{round(z, PLACES):g}"


def _same_path(left: Box, right: Box) -> bool:
    """Do two tool-centre paths describe the same cut?

    Compared on the 0.0001 grid the post prints (:data:`PLACES`) with
    :data:`TOL` on top, so that the manifest's exact arithmetic and the
    file's four printed decimals agree without either being rounded
    generously: both sides go through the same ``round``.
    """
    a, b = left.rounded(PLACES), right.rounded(PLACES)
    return (
        abs(a.x0 - b.x0) <= TOL
        and abs(a.y0 - b.y0) <= TOL
        and abs(a.x1 - b.x1) <= TOL
        and abs(a.y1 - b.y1) <= TOL
    )


def _check_expected_work(
    found: list[_FoundCut], expected: ExpectedWork
) -> list[Violation]:
    """Match the file's cuts against the manifest, both ways round.

    One-for-one: each manifest entry consumes at most one recovered cut, so
    a program that emits one pass twice instead of two passes once fails as
    both a missing cut and an extra one rather than passing on a count.
    """
    problems: list[Violation] = []
    used = [False] * len(found)
    for cut in expected.cuts:
        position = _find_cut(found, used, cut)
        if position is None:
            problems.append(Violation("missing-cut", cut.describe()))
        else:
            used[position] = True
    for position, item in enumerate(found):
        if used[position]:
            continue
        problems.append(
            Violation(
                "extra-cut",
                f"a {_KIND_NAMES.get(item.kind, item.kind)} at Z{_zword(item.z)} "
                f"runs x[{item.path.x0:.4f}, {item.path.x1:.4f}] "
                f"y[{item.path.y0:.4f}, {item.path.y1:.4f}], which is not a cut "
                f"this sheet's layout calls for",
                item.line,
            )
        )
    return problems


def _find_cut(found: list[_FoundCut], used: list[bool], cut: ExpectedCut):
    for position, item in enumerate(found):
        if used[position] or item.kind != cut.kind:
            continue
        if abs(round(item.z, PLACES) - round(cut.z, PLACES)) > TOL:
            continue
        if _same_path(item.path, cut.path):
            return position
    return None
