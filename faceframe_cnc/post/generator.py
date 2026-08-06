"""Emit ``.anc`` NC text from a :class:`~.model.SheetProgram` + plan.

The emitter is a template machine: it walks the sections named by the plan,
and for each feature stamps out the motion grammar measured from the
reference files with the feature's own coordinates substituted.  It never
decides what to cut (that is the plan) and never invents a code, feed or Z
(those are :class:`~.model.PostConfig`).

The grammar, block by block (line numbers are ``R710101N.anc``)
---------------------------------------------------------------
Program header, lines 1-11, then a blank line.

Section head, lines 13-17::

    (ROUTE TOOL #13: T13 - 3/8 PANEL CUTTER)   tool comment, verbatim
    (DIAMETER: 0.6299)                          diameter comment, verbatim
    M59
    G0 G54 G90 X0. Y0.       <- restates the CURRENT position (the end of
    T13                         the previous section; X0. Y0. for the first)

First feature of a section, lines 18-21::

    G0 G54 G90 X29.4375 Y61.8475 M13 S17500    spindle on with the rapid
    G43 H13 Z2.5                               tool length comp at rapid Z
    G0 Z2.                                     down to the ramp plane

Every later feature, lines 24-25::

    X29.0625 Y59.8925 Z2.5     (still modal G0)
    Z2.

A straight T13 groove, lines 26-28::

    G1 Z0.55 F150.        plunge straight down at the entry feed
    Y31.0175 F490.        one axis moves; cut feed
    G0 Z2.5

(The grammar is unchanged by the 2026-08-05 amendment; the two ENDPOINTS of a
stile groove are not what they were in that reference line, because they are
now clamped inside the part — see :func:`groove_segment`.)

A T17 WDC stile slot is the same three lines at T17's own feeds, twice —
once per depth pass, both on the one centreline (``RFK0101N.anc`` 21-28)::

    G1 Z0.4062 F150.      first bite
    Y37.3438 F400.
    G0 Z2.5
    X1.6614 Y0.5625 Z2.5  back to the start of the SAME centreline, but
    Z2.                   0.0937 further out: the deeper pass's V is wider,
    G1 Z0.3125 F150.      so its overrun past the part end is longer
    Y37.4375 F400.
    G0 Z2.5

A closed profile loop (T11/T12), lines 112-120::

    G1 X15. Z0.15 F150.   ramp in: 2 units of travel per unit of Z
    X28.3025 F545.        first cut move, counter-clockwise
    Y71.2125              ... three more corners, changed axis only ...
    X1.6975
    Y62.6075
    X15.                  back to the entry point
    X15.375               overshoot one tool diameter past it
    X19.075 Z2.           ramp out and lift
    G0 Z2.5

A tab lift, where a loop crosses one of its profile's holding tabs (the
2026-08-05 amendment §3b — no reference file contains one, and the four lines
are assembled entirely out of the two ramp forms above)::

    X28.3025              cut on at depth to the foot of the climb
    X27.7905 Z0.25        climb to the tab top at the modal cutting feed,
                          exactly as the lead-out ramp climbs
    X27.0405              traverse the 0.75 at full tab height
    X26.5285 Z-0.006 F150.  back down at the entry feed, as the lead-in
                          descends -- so the next at-depth move restates the
                          cutting feed, again as the loop's first one does
    X26.0165 F498.2

A release cut — one tab, in the final T12 section (the 2026-08-05 amendment
§3c).  It is the straight-cut grammar above at the release pass's own feeds, and
it is the last machining in the program::

    X26.5285 Y15.408 Z2.5   rapid to the start of the release span, which is
    Z2.                     inside kerf the through pass already cut open
    G1 Z-0.002 F50.         plunge to the release depth, through air
    X28.5045 F150.          one straight move, flush with the finished profile,
                            milling the 0.252 of tab that was left standing
    G0 Z2.5

Section tail, lines 99-102 (the last section drops the final two lines and
runs straight into the program footer)::

    M59
    G80
    G17 G91 G28 Z0 M95
    M92

Text and motion come out together
---------------------------------
:func:`emit` is the one emission path: it walks the plan once and builds a
stream of :class:`~.motion.Event` — each a rendered line plus, for the lines
that command a move, the typed :class:`~.motion.Motion` built from the same
coordinates at the same moment.  :func:`generate` is
:func:`~.motion.render` over that stream and :func:`generate_motions` is the
motions out of it, so neither can describe a program the other does not.
"""

from __future__ import annotations

import re

from .model import (
    SIDES,
    Box,
    CutPlan,
    FeatureRef,
    PanelSpec,
    PartProgram,
    PassSpec,
    PostConfig,
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_RELEASE,
    SECTION_WDC_SLOT,
    SheetProgram,
    ToolSpec,
    WdcSlotSpec,
    default_config,
)
from .motion import (
    EmittedProgram,
    Event,
    Motion,
    MotionKind,
    classify,
    render,
)
from . import tabs

__all__ = [
    "generate",
    "generate_motions",
    "emit",
    "EmittedProgram",
    "Event",
    "Motion",
    "MotionKind",
    "fmt",
    "groove_segment",
    "wdc_slot_segment",
    "default_entry_side",
    "entry_side_for",
    "pinned_entry_side",
    "loop_points",
    "loop_spans",
    "loop_extent",
    "release_path",
    "profile_cuts",
]

#: Parses the number back out of a ``ToolSpec.diameter_comment`` so it can be
#: held to the ``diameter`` beside it (2026-08-04 review, fix 10).
_DIA_COMMENT_RE = re.compile(r"^\(DIAMETER: (-?\d*\.?\d+)\)$")

NEWLINE = "\r\n"

#: The fixed program prologue below the identity comments (R710101N 6-11).
PROLOGUE = (
    "G0 G20 G91 G28 Z0 M15",
    "G90 G40 M22",
    "M88 B0",
    "M89 B0",
    "G08 P1",
    "M25",
)

#: The fixed program epilogue after the last section's ``M59``/``G80``
#: (R710101N 312-323).
EPILOGUE = (
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

#: Section tail for every section but the last (R710101N 99-102).
SECTION_TAIL = ("M59", "G80", "G17 G91 G28 Z0 M95", "M92")


def fmt(value: float) -> str:
    """Format a coordinate/feed the way the reference post does.

    Four decimals maximum, trailing zeros stripped, the decimal point
    always kept: ``2.0`` -> ``"2."``, ``0.55`` -> ``"0.55"``, ``-0.006`` ->
    ``"-0.006"``, ``490.0`` -> ``"490."``.
    """
    rounded = round(value, 4)
    if rounded == 0:
        rounded = 0.0
    text = f"{rounded:.4f}".rstrip("0")
    return text


def default_entry_side(box: Box, kind: str) -> str:
    """Which edge's midpoint the tool leads in on, as measured.

    Openings: the reference CAM leads in on the BOTTOM edge when the
    opening is wider than it is tall (R710101N 109-112, a 27x9 opening) and
    on the RIGHT edge otherwise (R710101N 121-123, a 27x27 opening).

    Perimeters: always the RIGHT edge (R710101N 233-235, R730101N 365-367,
    R720101N 233-235), including for parts that are wider than they are
    tall (R710101N 255-257 cuts a 30x12 part from its right edge).

    Two documented exceptions in the references are NOT rules and must be
    supplied as :attr:`FeatureRef.entry` overrides: the 18x30 part sitting
    at the far right of the sheet in R710101N/R730101N leads in on its LEFT
    edge, and one nested inner's 9x27 opening in R720101N (line 121-123)
    leads in on its LEFT edge.  Nothing in the files explains either, so
    neither is guessed at here.
    """
    if kind == "opening" and box.width > box.height:
        return "bottom"
    return "right"


def groove_segment(
    part: PartProgram, index: int, panel: PanelSpec, tool_radius: float
):
    """Return ``((x0, y0), (x1, y1))`` for one T13 groove of ``part``.

    ``index`` 0..3 = stile-low, rail-low, stile-high, rail-high in the
    part's own orientation; the returned segment always runs low-to-high
    (the plan's ``reverse`` flag picks the other direction).  A rotated
    part's stiles run along X instead of Y — see the module docstring of
    :mod:`~faceframe_cnc.post.model`.

    The stile grooves are CLAMPED (2026-08-05 amendment, Scott, job R0805)
    ------------------------------------------------------------------------
    The rail grooves have always stopped at the two stile centre lines, well
    inside the part.  The stile grooves ran ``panel.overrun`` — the measured
    0.375 — past both part ends, so a 0.6299 cutter removed material 0.690
    past the part.  On ``R080501N.anc`` the neighbouring WDC frame was 0.455
    away and lost two half-round bites 0.235 into its stile, and the far end
    cut 0.42 past the edge of the sheet.  So each stile groove endpoint is
    clamped to ``[part edge + tool_radius + end_inset, part edge -
    tool_radius - end_inset]`` on the groove's long axis: the SWEPT cut ends
    flush with the part edge (``end_inset`` 0.0), so the groove still runs the
    full length of the part and still breaks out through both rail ends — it
    just cannot leave the part.  :attr:`~.model.PanelSpec.end_inset` is the
    single place to change that choice.

    ``tool_radius`` is the T13 radius and is passed in rather than looked up
    so that this function has exactly one source for it — the measured tool
    table (``config.tool(SECTION_PANEL).radius``) — the same way
    :func:`wdc_slot_segment` is handed its per-pass overrun.  It is required,
    not defaulted: a caller that forgot it would silently emit the overrun
    this amendment exists to remove.
    """
    box = part.box
    stile, rail, over = panel.stile_inset, panel.rail_inset, panel.overrun
    reach = tool_radius + panel.end_inset
    if not part.rotated:
        # Stiles are the left/right edges: grooves run in Y.
        stile_lines = (box.x0 + stile, box.x1 - stile)
        rail_lines = (box.y0 + rail, box.y1 - rail)
        if index in (0, 2):
            x = stile_lines[0 if index == 0 else 1]
            return (
                (x, max(box.y0 - over, box.y0 + reach)),
                (x, min(box.y1 + over, box.y1 - reach)),
            )
        y = rail_lines[0 if index == 1 else 1]
        return (stile_lines[0], y), (stile_lines[1], y)
    # Rotated: stiles are the bottom/top edges, so those grooves run in X.
    stile_lines = (box.y0 + stile, box.y1 - stile)
    rail_lines = (box.x0 + rail, box.x1 - rail)
    if index in (0, 2):
        y = stile_lines[0 if index == 0 else 1]
        return (
            (max(box.x0 - over, box.x0 + reach), y),
            (min(box.x1 + over, box.x1 - reach), y),
        )
    x = rail_lines[0 if index == 1 else 1]
    return (x, stile_lines[0]), (x, stile_lines[1])


def wdc_slot_segment(
    part: PartProgram, index: int, spec: WdcSlotSpec, overrun: float
):
    """``((x0, y0), (x1, y1))`` for one T17 stile slot centreline of ``part``.

    ``index`` 0 is the LOW-side stile and 1 the high-side one, in sheet
    coordinates — the same low-then-high pair, and the same rotation
    reasoning, as :func:`groove_segment`'s stile grooves (indices 0 and 2),
    which is exactly the pair a WDC frame gives up to get this slot.  An
    upright part's stiles are its left and right edges, so its slots run in
    Y; a rotated part's run in X.

    The centreline is measured from the stile's OUTSIDE edge here
    (``stile_width - inset_from_inside_edge`` = 0.6614 for the measured
    2"/34 mm pair) because that is the edge the part's own box gives us.

    ``overrun`` is how far past each part end the tool CENTRE runs, which
    the caller takes per pass from :meth:`~.model.PostConfig.wdc_slot_reach`:
    a 45-degree V bit's effective radius is its depth of cut, so the DEEPER
    pass cuts wider and overruns further — ``RFK0101N.anc`` 22/27 run the
    0.3438-deep pass to Y37.3438 and the 0.4375-deep one to Y37.4375, 0.0937
    further out, on a part ending at Y37.  The segment always runs low-to-high.
    """
    box = part.box
    inset = spec.inset_from_outside_edge
    if not part.rotated:
        x = box.x0 + inset if index == 0 else box.x1 - inset
        return (x, box.y0 - overrun), (x, box.y1 + overrun)
    y = box.y0 + inset if index == 0 else box.y1 - inset
    return (box.x0 - overrun, y), (box.x1 + overrun, y)


def loop_points(box: Box, side: str, tool: ToolSpec, spec: PassSpec, config: PostConfig):
    """Every XY point one closed profile loop commands, in order.

    ``(pre, entry, corner1..4, close, overshoot, out)`` — the lead-in ramp's
    start, the point it lands on, the four corners traversed counter-clockwise,
    the return to the lead-in point, the one-tool-diameter overshoot past it and
    the lead-out ramp's end.  :meth:`_Emitter.loop` emits exactly these and
    :func:`loop_extent` measures exactly these, so the envelope test in
    :func:`entry_side_for` can never disagree with the code that is written.
    """
    ramp = (config.approach_z - spec.z_cut) * config.ramp_ratio
    over = tool.diameter
    lead = spec.lateral_lead

    if side == "bottom":
        entry = (box.mid_x, box.y0)
        step, normal = (1.0, 0.0), (0.0, -1.0)
        corners = [
            (box.x1, box.y0),
            (box.x1, box.y1),
            (box.x0, box.y1),
            (box.x0, box.y0),
        ]
    elif side == "right":
        entry = (box.x1, box.mid_y)
        step, normal = (0.0, 1.0), (1.0, 0.0)
        corners = [
            (box.x1, box.y1),
            (box.x0, box.y1),
            (box.x0, box.y0),
            (box.x1, box.y0),
        ]
    elif side == "top":
        entry = (box.mid_x, box.y1)
        step, normal = (-1.0, 0.0), (0.0, 1.0)
        corners = [
            (box.x0, box.y1),
            (box.x0, box.y0),
            (box.x1, box.y0),
            (box.x1, box.y1),
        ]
    elif side == "left":
        entry = (box.x0, box.mid_y)
        step, normal = (0.0, -1.0), (-1.0, 0.0)
        corners = [
            (box.x0, box.y0),
            (box.x1, box.y0),
            (box.x1, box.y1),
            (box.x0, box.y1),
        ]
    else:  # pragma: no cover - guarded by the plan validator
        raise ValueError(f"unknown entry side {side!r}")

    pre = (
        entry[0] - step[0] * ramp + normal[0] * lead,
        entry[1] - step[1] * ramp + normal[1] * lead,
    )
    overshoot = (entry[0] + step[0] * over, entry[1] + step[1] * over)
    out = (
        entry[0] + step[0] * (over + ramp) + normal[0] * lead,
        entry[1] + step[1] * (over + ramp) + normal[1] * lead,
    )
    return [pre, entry, *corners, entry, overshoot, out]


def loop_spans(box: Box, side: str, points) -> tuple[tuple[str, float, float], ...]:
    """Which side each at-depth move of a loop runs along, and how far.

    One ``(side, from_offset, to_offset)`` per move :meth:`_Emitter.loop`
    emits after the lead-in, in emission order — the four corners, the close
    back onto the entry point and the overshoot past it — with the offsets
    measured from that side's midpoint in its travel direction
    (:func:`~.tabs.travel_offset`).  ``points`` is :func:`loop_points`'s list.

    The loop is always counter-clockwise, so the sides come round in
    :data:`~.model.SIDES` order from the entry side; the first move runs from
    the entry point to the end of the entry side, the next three are whole
    sides, and the last two are the first half of the entry side and the
    overshoot.  Only the tab lift (2026-08-05 amendment §3b) needs this: a zone
    is a position on a side, and this is what says which MOVE has to lift over
    it.  ``to_offset`` always exceeds ``from_offset`` because the tool only ever
    travels one way along a side.
    """
    first = SIDES.index(side)
    order = [SIDES[(first + step) % 4] for step in (0, 1, 2, 3)] + [side, side]
    spans = []
    previous = points[1]
    for moving_along, point in zip(order, points[2:]):
        spans.append(
            (
                moving_along,
                tabs.travel_offset(box, moving_along, previous),
                tabs.travel_offset(box, moving_along, point),
            )
        )
        previous = point
    return tuple(spans)


def loop_extent(
    box: Box, side: str, tool: ToolSpec, spec: PassSpec, config: PostConfig
) -> Box:
    """The XY rectangle one loop's whole motion needs, ramps included."""
    points = loop_points(box, side, tool, spec, config)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return Box(min(xs), min(ys), max(xs), max(ys))


def release_path(box: Box, kind: str, radius: float) -> Box:
    """The FLUSH tool-centre path of the release cut over one profile.

    Spec §3c's subtle point, and §8's hard prohibition, in one function.  The
    T12 is 0.2 wide against the T11's 0.375 kerf, so a release cut run down the
    T11 CENTRELINE would leave a ~0.09 rib of tab standing on the finished part
    edge.  The release path therefore runs flush with the finished profile: the
    tool centre one T12 radius from the finished edge, offset **into the waste
    side**, so the cutter's own edge lies exactly on the finished line and the
    tab remnant rides away on the waste.

    Which side the waste is on is the whole difference between the two kinds:

    *   an OPENING's waste is the dropout INSIDE it, so the path is the finished
        opening shrunk by the radius — which is exactly
        :attr:`~.model.PostConfig.detail_pass`'s own path (offset -0.1 with a 0.1
        radius), i.e. the release re-traces the T12 detail kerf through the tab
        zone, as spec §3c says it must;
    *   a PERIMETER's waste is the skeleton OUTSIDE it, so the path is the
        finished footprint grown by the radius — the equivalent flush offset on
        the other side.  It is NOT the perimeter pass's path (offset 0.1875, the
        T11 radius), and the 0.0875 between them is the rib this avoids.

    ``box`` is the FINISHED profile (a part footprint or a finished opening),
    never a pass's offset rectangle, so this function owns the offset and there
    is one place the flush rule is written.
    """
    if kind == "opening":
        return box.grow(-radius)
    if kind == "perimeter":
        return box.grow(radius)
    raise ValueError(
        f"{kind!r} is not a profile the release section can cut - only an "
        f"opening (waste inside) or a perimeter (waste outside) has a finished "
        f"edge and a waste side"
    )


def profile_cuts(kind: str, config: PostConfig):
    """The ``(pass, tool)`` pairs that cut one profile, by profile kind.

    :func:`~.tabs.opening_cuts` / :func:`~.tabs.perimeter_cuts` behind one name,
    so the release section reserves room for exactly the passes
    :func:`~.tabs.place_tabs` placed the tabs for.
    """
    if kind == "opening":
        return tabs.opening_cuts(config)
    if kind == "perimeter":
        return tabs.perimeter_cuts(config)
    raise ValueError(f"{kind!r} is not a tabbed profile kind")


def entry_side_for(
    box: Box,
    kind: str,
    tool: ToolSpec,
    spec: PassSpec,
    config: PostConfig,
    override: str | None = None,
) -> str:
    """Which edge to lead in on, given that the lead-in has to FIT.

    :func:`default_entry_side` says what the reference CAM does, which for
    every perimeter is the right edge.  A perimeter's lead-in ramp is about
    four inches long, so on a SHORT part it runs four inches past the part
    along the entry edge — and for a 48x5 frame near the front of the sheet
    that ramp starts at Y-1.012, a full inch outside the sheet plus its 0.375
    trim overhang.  The verifier is right to refuse that (a rapid and a ramp
    over the fence is still over the fence); what was wrong was emitting it
    and calling a legal layout unbuildable (2026-08-04 review, fix 6).

    So the measured default is tried first and kept whenever it fits, which is
    what keeps every reference file and every sheet the optimizer produces
    today byte-identical.  Only when it does NOT fit does this fall back to the
    first of the four edges that does, in :data:`~.model.SIDES` order, and only
    when none of the four fits does it refuse — with the envelope and the part
    in the message, because at that point the sheet needs re-nesting.

    An explicit ``override`` (a :attr:`~.model.FeatureRef.entry` from a
    reconstructed file) is obeyed as given: replicating what the shop's CAM
    actually did is the point of that field, and the verifier has the last
    word on it either way.
    """
    if override is not None:
        return override
    return _fitting_side(box, kind, ((box, tool, spec),), config)


def pinned_entry_side(
    finished: Box,
    kind: str,
    cuts,
    config: PostConfig,
    override: str | None = None,
) -> str:
    """ONE lead-in edge that fits EVERY pass over one profile (spec §3b/§3c).

    :func:`entry_side_for` answers for one pass at a time, and the fallback
    above means two passes over the same profile can in principle land on two
    different edges — a deeper pass has a longer ramp, so it can fail the
    envelope test where a shallower one passed.  That was harmless until tabs:
    :func:`~.tabs.place_tabs` excludes ONE lead-in span, so a second pass
    entering somewhere else would ramp straight through a tab the first pass
    stood up (the emitter refuses that outright — see
    :func:`~.tabs.entry_conflict` and ``record_entry`` in :func:`emit`).

    So a tabbed profile gets its entry edge decided ONCE, here, against the
    worst case over ``cuts`` — the ``(pass, tool)`` pairs of
    :func:`profile_cuts` — and pinned onto every
    :attr:`~.model.FeatureRef.entry` that cuts it, release included.  The
    release cut itself constrains nothing (it plunges straight down inside the
    open kerf and never ramps along the profile), but it reads the pinned side
    to put its cuts in the loop's own travel order.

    ``finished`` is the FINISHED profile; each pass's own rectangle is
    ``finished.grow(spec.offset)``, which is why this takes the profile rather
    than a path.
    """
    if override is not None:
        return override
    trials = tuple(
        (finished.grow(spec.offset), tool, spec) for spec, tool in cuts
    )
    if not trials:
        raise ValueError(
            f"no pass cuts this {kind} profile, so nothing here can say which "
            f"edge it leads in on"
        )
    return _fitting_side(finished, kind, trials, config)


def _fitting_side(describe: Box, kind: str, trials, config: PostConfig) -> str:
    """The first edge in the fallback order whose whole motion fits the sheet.

    ``trials`` is one ``(cut rectangle, tool, pass)`` per pass that has to fit,
    and an edge is only accepted when EVERY one of them does.  One
    implementation so that the single-pass answer (:func:`entry_side_for`) and
    the pinned multi-pass one (:func:`pinned_entry_side`) cannot drift apart in
    either the fallback order or the refusal they raise.
    """
    envelope = Box(
        -config.overhang,
        -config.overhang,
        config.sheet_width + config.overhang,
        config.sheet_length + config.overhang,
    )
    preferred = default_entry_side(describe, kind)
    order = [preferred] + [side for side in SIDES if side != preferred]
    tried: list[str] = []
    for side in order:
        extents = [
            loop_extent(box, side, tool, spec, config) for box, tool, spec in trials
        ]
        if all(envelope.contains(extent, 1e-9) for extent in extents):
            return side
        union = Box(
            min(e.x0 for e in extents),
            min(e.y0 for e in extents),
            max(e.x1 for e in extents),
            max(e.y1 for e in extents),
        )
        tried.append(
            f"{side} -> x[{union.x0:.4f}, {union.x1:.4f}] "
            f"y[{union.y0:.4f}, {union.y1:.4f}]"
        )
    ramp = max(
        (config.approach_z - spec.z_cut) * config.ramp_ratio for _, _, spec in trials
    )
    diameter = max(tool.diameter for _, tool, _ in trials)
    raise ValueError(
        f"no lead-in edge fits: the {describe.width:g}x{describe.height:g} cut at "
        f"({describe.x0:.4f}, {describe.y0:.4f}) needs a {ramp:g} lead-in ramp and a "
        f"{diameter:g} overshoot, and every edge runs outside the "
        f"{config.sheet_width:g}x{config.sheet_length:g} sheet plus its "
        f"{config.overhang:g} overhang - "
        + "; ".join(tried)
        + ". Re-nest the sheet so this part is further from the edge"
    )


class _Emitter:
    """Builds the event stream while tracking the modal machine state.

    ``x``/``y``/``z`` are the position the machine has been commanded to, i.e.
    what the next line's omitted axis words mean.  ``z`` is ``None`` until a
    section's ``G43`` establishes work Z; ``section`` tags every event the
    emitter appends while it is set.
    """

    def __init__(self, config: PostConfig):
        self.config = config
        self.events: list[Event] = []
        self.x = 0.0
        self.y = 0.0
        self.z: float | None = None
        self.section: str | None = None

    def line(self, text: str) -> None:
        self.events.append(
            Event(text=text, line_index=len(self.events), section=self.section)
        )

    def blank(self) -> None:
        self.line("")

    def begin_section(self, section: str) -> None:
        """Enter ``section``, whose head has yet to be written.

        Work Z goes unknown here because the previous section's tail homed it
        (``G17 G91 G28 Z0 M95``) and the program footer's ``G91 G28 Z0`` does
        the same for the last one.
        """
        self.section = section
        self.z = None

    # -- motion helpers ----------------------------------------------------

    def _axis_words(self, x: float, y: float) -> str:
        """The X/Y words a move needs, unchanged axes suppressed.

        Reads the modal position and does not advance it: :meth:`_move` does
        that, for the one line whose text this call is part of, so the words on
        a line and the :class:`~.motion.Motion` beside it can never be
        computed against different positions.
        """
        words = []
        if abs(x - self.x) > 1e-9:
            words.append(f"X{fmt(x)}")
        if abs(y - self.y) > 1e-9:
            words.append(f"Y{fmt(y)}")
        return " ".join(words)

    def _move(
        self,
        text: str,
        kind: MotionKind,
        to_x: float,
        to_y: float,
        to_z: float | None,
        tool: ToolSpec,
        feed: float | None,
        feature: FeatureRef | None,
        pass_index: int | None,
    ) -> None:
        """Append one line and the motion it commands, then advance the state."""
        motion = Motion(
            kind=kind,
            from_x=self.x,
            from_y=self.y,
            from_z=self.z,
            to_x=to_x,
            to_y=to_y,
            to_z=to_z,
            tool=tool,
            feed=feed,
            section=self.section,
            feature=feature,
            pass_index=pass_index,
            line_index=len(self.events),
        )
        self.events.append(
            Event(
                text=text,
                line_index=len(self.events),
                section=self.section,
                motion=motion,
            )
        )
        self.x, self.y, self.z = to_x, to_y, to_z

    def preposition(
        self,
        x: float,
        y: float,
        tool: ToolSpec,
        first: bool,
        feature: FeatureRef | None = None,
        pass_index: int | None = None,
    ) -> None:
        cfg = self.config
        if first:
            self._move(
                f"G0 G54 G90 X{fmt(x)} Y{fmt(y)} M13 S{tool.speed}",
                # The spindle comes on and the tool traverses before the G43
                # below states where Z is, so this move's Z is unknown at both
                # ends -- it is the one move in the program that has no Z.
                MotionKind.RAPID,
                x,
                y,
                self.z,
                tool,
                None,
                feature,
                pass_index,
            )
            self._move(
                f"G43 H{tool.number} Z{fmt(cfg.rapid_z)}",
                MotionKind.RAPID,
                x,
                y,
                cfg.rapid_z,
                tool,
                None,
                feature,
                pass_index,
            )
        else:
            self._move(
                f"X{fmt(x)} Y{fmt(y)} Z{fmt(cfg.rapid_z)}",
                classify(True, self.z, cfg.rapid_z),
                x,
                y,
                cfg.rapid_z,
                tool,
                None,
                feature,
                pass_index,
            )
        self._move(
            f"G0 Z{fmt(cfg.approach_z)}" if first else f"Z{fmt(cfg.approach_z)}",
            classify(True, self.z, cfg.approach_z),
            x,
            y,
            cfg.approach_z,
            tool,
            None,
            feature,
            pass_index,
        )

    def retract(
        self,
        tool: ToolSpec,
        feature: FeatureRef | None = None,
        pass_index: int | None = None,
    ) -> None:
        cfg = self.config
        self._move(
            f"G0 Z{fmt(cfg.rapid_z)}",
            # Named, not classified: after the perimeter marker's M59 the tool
            # is ALREADY at the rapid plane (R710101N 230-232), so this move's
            # dZ is zero and :func:`~.motion.classify` would call it a rapid.
            # The command is a retract in both cases.
            MotionKind.RETRACT,
            self.x,
            self.y,
            cfg.rapid_z,
            tool,
            None,
            feature,
            pass_index,
        )

    # -- features ----------------------------------------------------------

    def groove(
        self,
        part: PartProgram,
        index: int,
        reverse: bool,
        tool: ToolSpec,
        panel: PanelSpec,
        first: bool,
        ref: FeatureRef | None = None,
    ) -> None:
        across = part.box.height if part.rotated else part.box.width
        along = part.box.width if part.rotated else part.box.height
        if across <= 2 * panel.stile_inset or along <= 2 * panel.rail_inset:
            raise ValueError(
                f"a {part.box.width}x{part.box.height} part is too small for the "
                f"{panel.stile_inset}/{panel.rail_inset} panel groove pattern"
            )
        start, end = groove_segment(part, index, panel, tool.radius)
        if reverse:
            start, end = end, start
        self.preposition(start[0], start[1], tool, first, ref)
        self._straight(
            end, panel.z_cut, panel.entry_feed, panel.cut_feed, tool, ref, None
        )

    def slot(
        self,
        part: PartProgram,
        index: int,
        tool: ToolSpec,
        spec: WdcSlotSpec,
        first: bool,
        ref: FeatureRef | None = None,
    ) -> None:
        """Cut one WDC stile slot: every configured depth pass, in order,
        on the one centreline."""
        cfg = self.config
        for position, z_cut in enumerate(spec.z_cuts):
            start, end = wdc_slot_segment(
                part, index, spec, cfg.wdc_slot_reach(position)
            )
            self.preposition(
                start[0], start[1], tool, first and position == 0, ref, position
            )
            self._straight(
                end, z_cut, spec.entry_feed, spec.cut_feed, tool, ref, position
            )

    def _straight(
        self,
        end: tuple[float, float],
        z_cut: float,
        entry_feed: float,
        cut_feed: float,
        tool: ToolSpec,
        ref: FeatureRef | None,
        pass_index: int | None,
    ) -> None:
        """Plunge, cut one straight line to ``end``, retract.

        The T13 panel groove and the T17 stile slot are the same three lines at
        their own feeds and depths (module docstring), so they are written once.
        """
        self._move(
            f"G1 Z{fmt(z_cut)} F{fmt(entry_feed)}",
            classify(False, self.z, z_cut),
            self.x,
            self.y,
            z_cut,
            tool,
            entry_feed,
            ref,
            pass_index,
        )
        self._move(
            f"{self._axis_words(end[0], end[1])} F{fmt(cut_feed)}",
            classify(False, self.z, z_cut),
            end[0],
            end[1],
            z_cut,
            tool,
            cut_feed,
            ref,
            pass_index,
        )
        self.retract(tool, ref, pass_index)

    def release(
        self,
        finished: Box,
        kind: str,
        entry_side: str,
        zones: tuple,
        tool: ToolSpec,
        first: bool,
        ref: FeatureRef | None = None,
    ) -> int:
        """Cut one profile's tabs away, one at a time (spec §3c).

        Returns how many release cuts were emitted, so the caller can keep
        track of which one is the section's FIRST feature (the one that carries
        the spindle start and the ``G43``).

        Per tab, the grammar is the post's own straight-cut grammar
        (:meth:`_straight` — the same four lines the T13 groove and the T17 slot
        bite use, at the release pass's own feeds):

        *   rapid to the start of the release span, which lies in kerf the
            earlier pass cut right through (:func:`~.tabs.release_span`), and
            drop to the ramp plane;
        *   ``G1 Z-0.002 F50.`` — plunge to the release depth at the release
            plunge feed, through nothing but air;
        *   one straight move along the flush path at the release cut feed,
            milling the ~0.252 of standing tab;
        *   ``G0 Z2.5`` and on to the next tab.

        Nothing about the XY path depends on the depth, which is why the dry-run
        twin (:func:`~.job.dry_run_config`) lifts this section like every other
        one and traces exactly the same air.
        """
        cfg = self.config
        spec = cfg.release
        if spec is None:
            raise ValueError(
                f"{ref} is in the release section but this post table configures "
                f"no release pass - the section has no feeds and no depth"
            )
        path = release_path(finished, kind, tool.radius)
        _require_cuttable(path, f"the release path of {ref}")
        emitted = 0
        for zone in tabs.travel_sequence(zones, entry_side):
            low, high = tabs.release_span(zone, cfg)
            limit = tabs.side_length(path, zone.side) / 2.0
            if low < -limit - 1e-9 or high > limit + 1e-9:
                raise ValueError(
                    f"the release cut for tab zone {zone} of {ref} would run "
                    f"travel offsets {low:.4f}..{high:.4f} on a side that is only "
                    f"{2.0 * limit:.4f} long - it would turn a corner, and a "
                    f"release cut is one straight move along one side"
                )
            start = tabs.zone_point(path, zone.side, low)
            end = tabs.zone_point(path, zone.side, high)
            self.preposition(start[0], start[1], tool, first and emitted == 0, ref)
            self._straight(
                end, cfg.release_z, spec.entry_feed, spec.cut_feed, tool, ref, None
            )
            emitted += 1
        return emitted

    def loop(
        self,
        box: Box,
        side: str,
        tool: ToolSpec,
        spec: PassSpec,
        first: bool,
        ref: FeatureRef | None = None,
        pass_index: int | None = None,
        zones: tuple = (),
    ) -> None:
        """Cut one closed rectangle counter-clockwise, leading in on the
        midpoint of ``side``.

        The geometry itself is :func:`loop_points`, shared with
        :func:`loop_extent` so that the envelope test which CHOOSES the entry
        side measures the same motion this writes.

        ``zones`` are the profile's holding tabs
        (:attr:`~.model.CutPlan.tabs`, the 2026-08-05 amendment).  If this pass
        cuts below the tab top the loop rises over each of them
        (:meth:`_tab_lift`) instead of cutting through; if it does not, or there
        are none, not one byte of this method's output changes.
        """
        points = loop_points(box, side, tool, spec, self.config)
        pre, entry = points[0], points[1]
        targets = [*points[2:6], points[6], points[7]]
        out = points[8]

        lifting = self._lift_plan(box, side, tool, spec, points, zones, ref)

        self.preposition(pre[0], pre[1], tool, first, ref, pass_index)
        self._move(
            f"G1 {self._axis_words(entry[0], entry[1])} "
            f"Z{fmt(spec.z_cut)} F{fmt(spec.entry_feed)}",
            classify(False, self.z, spec.z_cut),
            entry[0],
            entry[1],
            spec.z_cut,
            tool,
            spec.entry_feed,
            ref,
            pass_index,
        )
        # The cut feed is stated once and is modal for the rest of the loop, so
        # every one of these moves runs at it whether its line says so or not --
        # until a tab lift's descent restates the entry feed, which is why this
        # tracks whether the cut feed is in force rather than counting moves.
        stated = False
        for index, point in enumerate(targets):
            for zone in lifting[index]:
                stated = self._tab_lift(
                    box, zone, spec, tool, stated, ref, pass_index
                )
            words = self._axis_words(point[0], point[1])
            self._move(
                words if stated else f"{words} F{fmt(spec.cut_feed)}",
                classify(False, self.z, spec.z_cut),
                point[0],
                point[1],
                spec.z_cut,
                tool,
                spec.cut_feed,
                ref,
                pass_index,
            )
            stated = True
        self._move(
            f"{self._axis_words(out[0], out[1])} Z{fmt(self.config.approach_z)}",
            classify(False, self.z, self.config.approach_z),
            out[0],
            out[1],
            self.config.approach_z,
            tool,
            spec.cut_feed,
            ref,
            pass_index,
        )
        self.retract(tool, ref, pass_index)

    def _lift_plan(
        self,
        box: Box,
        side: str,
        tool: ToolSpec,
        spec: PassSpec,
        points,
        zones: tuple,
        ref: FeatureRef | None,
    ) -> tuple[tuple, ...]:
        """Which tab zones each of this loop's six cut moves lifts over.

        Empty tuples all round for a pass at or above the tab top (T13's 0.55
        groove and both T17 slot bites never touch a tab) or for a profile with
        no tabs, which is what keeps every pre-amendment plan emitting the bytes
        it always did.
        """
        cfg = self.config
        spans = loop_spans(box, side, points)
        if not zones or not tabs.lifts_over_tabs(spec.z_cut, cfg):
            return tuple(() for _ in spans)
        ramp = tabs.tab_ramp(spec.z_cut, cfg)
        fouled = tabs.entry_conflict(
            zones, side, tabs.entry_exclusion(((spec, tool),), cfg), ramp
        )
        if fouled is not None:
            raise ValueError(
                f"{ref} leads in on its {side} edge, whose lead-in and lead-out "
                f"ramps run through tab zone {fouled} - the pass would cut the "
                f"tab away instead of standing it up. Every pass over one "
                f"profile has to lead in on the side the tabs were placed for"
            )
        try:
            return tabs.assign_zones(zones, spans, ramp)
        except ValueError as exc:
            raise ValueError(f"{ref}: {exc}") from exc

    def _tab_lift(
        self,
        box: Box,
        zone,
        spec: PassSpec,
        tool: ToolSpec,
        stated: bool,
        ref: FeatureRef | None,
        pass_index: int | None,
    ) -> bool:
        """Rise over one tab and come back down (spec §3b), four moves.

        Feeds follow the grammar the lead-in and lead-out ramps of this very
        loop already use, so no F value appears that the section did not already
        contain: the CLIMB and the run along the top stay at the modal cutting
        feed exactly as the lead-out ramp does (R710101N 119: ``X19.075 Z2.``,
        no F word), and the DESCENT back to depth states the pass's entry feed
        exactly as the lead-in does (line 112: ``G1 X15. Z0.15 F150.``).  Returns
        whether the cutting feed is still in force — it is not, after a descent,
        so the caller restates it on the next at-depth move, which is again what
        the loop's own first cut move does.
        """
        cfg = self.config
        top = cfg.tabs.top_z
        ramp = tabs.tab_ramp(spec.z_cut, cfg)
        low, high = zone.span()
        foot = tabs.zone_point(box, zone.side, low - ramp)
        crest_in = tabs.zone_point(box, zone.side, low)
        crest_out = tabs.zone_point(box, zone.side, high)
        landing = tabs.zone_point(box, zone.side, high + ramp)

        # 1. carry on cutting at depth as far as the foot of the climb
        words = self._axis_words(*foot)
        self._move(
            words if stated else f"{words} F{fmt(spec.cut_feed)}",
            classify(False, self.z, spec.z_cut),
            foot[0],
            foot[1],
            spec.z_cut,
            tool,
            spec.cut_feed,
            ref,
            pass_index,
        )
        # 2. climb to the tab top
        self._move(
            f"{self._axis_words(*crest_in)} Z{fmt(top)}",
            classify(False, self.z, top),
            crest_in[0],
            crest_in[1],
            top,
            tool,
            spec.cut_feed,
            ref,
            pass_index,
        )
        # 3. traverse the full-height length; Z is unchanged, so no Z word
        self._move(
            self._axis_words(*crest_out),
            classify(False, self.z, top),
            crest_out[0],
            crest_out[1],
            top,
            tool,
            spec.cut_feed,
            ref,
            pass_index,
        )
        # 4. back down to depth at the entry feed, as the lead-in descends
        self._move(
            f"{self._axis_words(*landing)} Z{fmt(spec.z_cut)} "
            f"F{fmt(spec.entry_feed)}",
            classify(False, self.z, spec.z_cut),
            landing[0],
            landing[1],
            spec.z_cut,
            tool,
            spec.entry_feed,
            ref,
            pass_index,
        )
        return False


def _check_config(cfg: PostConfig, program: SheetProgram) -> None:
    """Refuse a post table that would drive the machine out of its limits.

    Spec section 8 makes the Z window machine protection, not a preference,
    so this fires before a single line is written rather than leaving it to
    the verifier to catch afterwards.
    """
    if (
        abs(program.sheet_width - cfg.sheet_width) > 1e-9
        or abs(program.sheet_length - cfg.sheet_length) > 1e-9
    ):
        raise ValueError(
            f"the sheet is {program.sheet_width}x{program.sheet_length} but the post "
            f"is configured for {cfg.sheet_width}x{cfg.sheet_length}"
        )
    depths = [
        ("panel groove", cfg.panel.z_cut),
        *[
            (f"opening pass {i + 1}", p.z_cut)
            for i, p in enumerate(cfg.openings_passes)
        ],
        ("detail", cfg.detail_pass.z_cut),
        *[
            (f"WDC slot pass {i + 1}", z)
            for i, z in enumerate(cfg.wdc_slot.z_cuts)
        ],
        *[(f"perimeter pass {i + 1}", p.z_cut) for i, p in enumerate(cfg.perimeter_passes)],
    ]
    for what, z in depths:
        if z < cfg.z_min - 1e-9:
            raise ValueError(
                f"the {what} depth Z{z} is below the Z{cfg.z_min} floor - "
                f"spoilboard strike"
            )
    for what, z in (("ramp plane", cfg.approach_z), ("rapid plane", cfg.rapid_z)):
        if z > cfg.z_max + 1e-9:
            raise ValueError(f"the {what} Z{z} is above the Z{cfg.z_max} ceiling")
        # 2026-08-04 review, fix 1: both planes are reached by RAPIDS, so both
        # have to clear the stock.  With approach_z 0.6 against a 0.75 stock top
        # the post used to emit `G0 Z0.6` prepositions -- a rapid plunge 0.15
        # into the part, at every feature, and the verifier had nothing to say
        # about it because none of its rules looked at a G0.
        if z < cfg.stock_top_z - 1e-9:
            raise ValueError(
                f"the {what} Z{z} is below the Z{cfg.stock_top_z} top of the stock - "
                f"the machine RAPIDS to it, so it would rapid-plunge "
                f"{cfg.stock_top_z - z:g} into the part before any feed move starts"
            )
    if cfg.rapid_z < cfg.approach_z - 1e-9:
        raise ValueError(
            f"the rapid plane Z{cfg.rapid_z} is below the ramp plane "
            f"Z{cfg.approach_z} - the retract between features would descend"
        )

    # The release pass (2026-08-05 amendment §3c) is the one section whose path
    # is derived from ANOTHER section's numbers, so the relationship is checked
    # here rather than assumed at emission time.
    if cfg.release is not None:
        release_tool = cfg.tools.get(SECTION_RELEASE)
        if release_tool is None:
            raise ValueError(
                "the post table configures a tab-release pass but names no tool "
                "for the 'release' section - the release cut is a T12 pass and "
                "has to be announced as one"
            )
        if cfg.tabs.length <= 0:
            raise ValueError(
                f"the tab length is {cfg.tabs.length} - there is nothing for the "
                f"release pass to cut away"
            )
        for what, feed in (
            ("cut", cfg.release.cut_feed),
            ("plunge", cfg.release.entry_feed),
        ):
            if feed <= 0:
                raise ValueError(
                    f"the release pass's {what} feed is {feed} - a feed of zero "
                    f"stalls the tool in the cut"
                )
        if cfg.release.overlap < 0:
            raise ValueError(
                f"the release overlap is {cfg.release.overlap}, i.e. the cut would "
                f"stop SHORT of the tab it exists to remove"
            )
        # The release cut reserves room for the worst ramp the Z floor admits
        # (:func:`~.tabs.release_ramp`), so the floor has to be below the tab top
        # or that reservation is negative and the cut is shorter than the tab.
        if cfg.z_min > cfg.tabs.top_z - 1e-9:
            raise ValueError(
                f"the Z{cfg.z_min} floor is at or above the Z{cfg.tabs.top_z} tab "
                f"top, so no pass can cut below a tab - there would be nothing for "
                f"the release pass to remove and nothing holding anything"
            )
        # Spec §3c: an opening's release path IS the T12 detail path (re-trace
        # it through the tab zone).  That is only true while the detail pass's
        # offset is one release-tool radius inside the finished edge, which is
        # what makes the detail kerf flush in the first place.  If a future post
        # table breaks that, the release would cut a rib rather than remove one,
        # and spec §8 forbids exactly that.
        flush = cfg.detail_pass.offset + release_tool.radius
        if abs(flush) > 1e-9:
            raise ValueError(
                f"the T12 detail pass runs its centre {-cfg.detail_pass.offset:g} "
                f"inside the finished opening edge but the release tool's radius is "
                f"{release_tool.radius:g} - the detail kerf is then not flush with "
                f"the finished line, so re-tracing it at release ({flush:+g} out) "
                f"would leave a rib on the finished edge"
            )

    # Fix 10: the verifier arms its cone rule by matching the diameter the FILE
    # declares to the float in this table, so a comment that has drifted away
    # from its own number silently swaps the v-slot check for a stream of
    # misleading foreign-cut refusals.  The two are one measurement; hold them
    # to each other here, where the table is in hand.
    for section, tool in sorted(cfg.tools.items()):
        match = _DIA_COMMENT_RE.match(tool.diameter_comment)
        if match is None:
            raise ValueError(
                f"the {section!r} tool T{tool.number} declares "
                f"{tool.diameter_comment!r}, which is not a (DIAMETER: n) comment - "
                f"the verifier identifies a tool by the diameter the program states"
            )
        if abs(float(match.group(1)) - tool.diameter) > 1e-9:
            raise ValueError(
                f"the {section!r} tool T{tool.number} announces "
                f"{tool.diameter_comment} but its diameter is {tool.diameter:g} - "
                f"the comment is what the machine operator and the verifier both "
                f"read, so the two may not disagree"
            )

    # The V-slot geometry everything downstream uses -- overrun, swept
    # width, the optimizer's end clearance -- is the cone's "radius equals
    # depth" rule, which stops being true once the bit is buried past its
    # own shoulder.  Refuse rather than silently model a flat-bottomed cut.
    if cfg.wdc_slot.overruns is not None and len(cfg.wdc_slot.overruns) != len(
        cfg.wdc_slot.z_cuts
    ):
        raise ValueError(
            f"the post table pins {len(cfg.wdc_slot.overruns)} WDC slot overrun(s) "
            f"for {len(cfg.wdc_slot.z_cuts)} depth pass(es)"
        )
    v_tool = cfg.tools.get(SECTION_WDC_SLOT)
    if v_tool is not None and cfg.wdc_slot.overruns is None:
        for position, z_cut in enumerate(cfg.wdc_slot.z_cuts, start=1):
            depth = cfg.stock_top_z - z_cut
            if depth * cfg.wdc_slot.flank_slope > v_tool.radius - 1e-9:
                raise ValueError(
                    f"WDC slot pass {position} cuts {depth:g} deep, at or past the "
                    f"{v_tool.radius:g} radius of the {v_tool.diameter:g} T"
                    f"{v_tool.number} bit - the 45-degree cone model does not "
                    f"describe that cut"
                )


def _require_cuttable(box: Box, what: str) -> None:
    if box.width <= 0 or box.height <= 0:
        raise ValueError(
            f"{what} collapses to {box.width}x{box.height} once the tool offset is "
            f"applied - the feature is too small for this tool"
        )


def _section_features(plan: CutPlan, section: str) -> list:
    if section == SECTION_PANEL:
        return plan.panel
    if section == SECTION_WDC_SLOT:
        return plan.wdc_slot
    if section == SECTION_OPENINGS:
        return plan.openings
    if section == SECTION_DETAIL:
        return plan.detail_order()
    if section == SECTION_PERIMETER:
        return [ref for pass_refs in plan.perimeter for ref in pass_refs]
    if section == SECTION_RELEASE:
        return plan.release
    raise ValueError(f"unknown section {section!r}")


def emit(
    program: SheetProgram,
    plan: CutPlan,
    config: PostConfig | None = None,
) -> EmittedProgram:
    """Walk ``plan`` once and return the text together with its event stream.

    The single emission path (see the module docstring): :func:`generate` and
    :func:`generate_motions` are both views of what this returns.

    Raises ``ValueError`` on a plan that references a part, opening or
    groove that does not exist — a plan is never allowed to fall through to
    a silently skipped cut.
    """
    cfg = config or default_config()
    _check_config(cfg, program)
    parts = program.flat_parts()
    emitter = _Emitter(cfg)
    header = program.header

    #: Every profile whose tabs were actually handed to a loop.  A plan that
    #: names tabs on a profile the emitter never cuts believes the part is held
    #: when it is not, so that is a refusal at the bottom of this function
    #: rather than a silent no-op (2026-08-05 amendment §3b).
    tabbed: set[tuple[int, str, int]] = set()

    #: Which edge each profile's loops actually led in on.  Tab placement
    #: excludes ONE lead-in span (:func:`~.tabs.entry_exclusion`), so every pass
    #: over a tabbed profile has to lead in on the same edge or the zones are
    #: clear of one pass's ramp and not another's; the release section then reads
    #: this to put its cuts in the loop's own travel order.
    entry_of: dict[tuple[int, str, int], str] = {}

    def zones_of(ref) -> tuple:
        if plan.tabs and ref.profile in plan.tabs:
            tabbed.add(ref.profile)
        return plan.zones_for(ref)

    def record_entry(ref, side: str) -> None:
        previous = entry_of.setdefault(ref.profile, side)
        if previous != side and plan.zones_for(ref):
            raise ValueError(
                f"{ref} is cut from its {side} edge by one pass and its "
                f"{previous} edge by another, and it carries holding tabs - the "
                f"tabs were placed clear of ONE lead-in span, so the other pass "
                f"would ramp straight through one of them. Pin one entry side per "
                f"tabbed profile"
            )

    def part_of(ref, kind: str) -> PartProgram:
        if ref.kind != kind:
            raise ValueError(
                f"the {kind} section was handed a {ref.kind!r} feature reference"
            )
        if not 0 <= ref.part < len(parts):
            raise ValueError(f"plan references part {ref.part}, sheet has {len(parts)}")
        return parts[ref.part]

    # --- header -----------------------------------------------------------
    emitter.line("%")
    emitter.line(f"O{header.o_number:04d} ({header.name})")
    emitter.line(f"(CREATED ON {header.created})")
    emitter.line(header.material_comment)
    emitter.line(header.load_comment)
    for extra in cfg.banner_lines:
        emitter.line(extra)
    for text in PROLOGUE:
        emitter.line(text)

    sections = [s for s in plan.sections if _section_features(plan, s)]
    for position, section in enumerate(sections):
        last_section = position == len(sections) - 1
        if section not in cfg.tools:
            raise ValueError(
                f"the plan has {section!r} cuts but the post table has no tool "
                f"for that section"
            )
        tool = cfg.tool(section)
        emitter.begin_section(section)
        emitter.blank()
        emitter.line(tool.header_comment)
        emitter.line(tool.diameter_comment)
        emitter.line("M59")
        emitter.line(f"G0 G54 G90 X{fmt(emitter.x)} Y{fmt(emitter.y)}")
        emitter.line(f"T{tool.number}")

        if section == SECTION_PANEL:
            for i, ref in enumerate(plan.panel):
                part = part_of(ref, "groove")
                if not 0 <= ref.index <= 3:
                    raise ValueError(f"groove index {ref.index} out of range 0..3")
                emitter.groove(
                    part, ref.index, ref.reverse, tool, cfg.panel, i == 0, ref
                )

        elif section == SECTION_WDC_SLOT:
            for i, ref in enumerate(plan.wdc_slot):
                part = part_of(ref, "wdc_slot")
                if not 0 <= ref.index <= 1:
                    raise ValueError(
                        f"WDC slot index {ref.index} out of range 0..1 (a frame "
                        f"has two stiles)"
                    )
                emitter.slot(part, ref.index, tool, cfg.wdc_slot, i == 0, ref)

        elif section in (SECTION_OPENINGS, SECTION_DETAIL):
            # The T11 opening section runs every configured depth pass over one
            # opening before moving to the next (the 2026-08-05 max-bite ladder,
            # :func:`~.from_layout.generated_opening_passes`) — the same grammar
            # as the two bites of one T17 slot, and for the same reason: they are
            # one rectangle at two depths, so the tool stays where it is.  The
            # T12 detail section has one pass and always did.
            specs = (
                cfg.openings_passes
                if section == SECTION_OPENINGS
                else (cfg.detail_pass,)
            )
            if not specs:
                raise ValueError(
                    "the post table configures no opening depth pass, so no "
                    "opening would be routed at all"
                )
            refs = (
                plan.openings if section == SECTION_OPENINGS else plan.detail_order()
            )
            emitted_loops = 0
            for ref in refs:
                part = part_of(ref, "opening")
                if not 0 <= ref.index < len(part.openings):
                    raise ValueError(
                        f"plan references opening {ref.index} of part {ref.part}, "
                        f"which has {len(part.openings)}"
                    )
                opening = part.openings[ref.index]
                for pass_index, spec in enumerate(specs):
                    cut = opening.grow(spec.offset)
                    _require_cuttable(cut, f"opening {ref.index} of part {ref.part}")
                    try:
                        side = entry_side_for(
                            cut, "opening", tool, spec, cfg, override=ref.entry
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"opening {ref.index} of {part.part_number} @"
                            f"({part.box.x0:.4f},{part.box.y0:.4f}) cannot be cut on "
                            f"this sheet: {exc}"
                        ) from exc
                    record_entry(ref, side)
                    emitter.loop(
                        cut,
                        side,
                        tool,
                        spec,
                        emitted_loops == 0,
                        ref,
                        # A single-rung ladder tags its motions with no pass
                        # index, exactly as this section always did, so a
                        # reference program's motion stream is unchanged.
                        pass_index if len(specs) > 1 else None,
                        zones=zones_of(ref),
                    )
                    emitted_loops += 1

        elif section == SECTION_PERIMETER:
            if len(plan.perimeter) != len(cfg.perimeter_passes):
                raise ValueError(
                    f"plan has {len(plan.perimeter)} perimeter pass order(s) but the "
                    f"post is configured for {len(cfg.perimeter_passes)} depth pass(es)"
                )
            index = 0
            for pass_index, (spec, refs) in enumerate(
                zip(cfg.perimeter_passes, plan.perimeter)
            ):
                for ref in refs:
                    part = part_of(ref, "perimeter")
                    cut = part.box.grow(spec.offset)
                    _require_cuttable(cut, f"the footprint of part {ref.part}")
                    try:
                        side = entry_side_for(
                            cut, "perimeter", tool, spec, cfg, override=ref.entry
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"{part.part_number} @({part.box.x0:.4f},"
                            f"{part.box.y0:.4f}) cannot be cut on this sheet: {exc}"
                        ) from exc
                    record_entry(ref, side)
                    emitter.loop(
                        cut,
                        side,
                        tool,
                        spec,
                        index == 0,
                        ref,
                        pass_index,
                        zones=zones_of(ref),
                    )
                    index += 1
                    if index == 1 and cfg.perimeter_marker_after_first_loop:
                        emitter.line("M59")
                        # Tagged with the loop it follows: the tool has not moved
                        # since, and this retract is that loop's second one.
                        emitter.retract(tool, ref, pass_index)

        elif section == SECTION_RELEASE:
            # Spec §3c: the last machining in the program.  Nothing on this
            # sheet is fully separated until these cuts run, and after them
            # everything is free, exactly once.
            emitted = 0
            for ref in plan.release:
                if ref.kind not in ("opening", "perimeter"):
                    raise ValueError(
                        f"the release section was handed a {ref.kind!r} feature "
                        f"reference - only an opening or a perimeter is held by "
                        f"tabs"
                    )
                part = part_of(ref, ref.kind)
                zones = zones_of(ref)
                if not zones:
                    raise ValueError(
                        f"{ref} is in the release section but the plan puts no "
                        f"holding tabs on it - there would be nothing to release, "
                        f"which means the profile was already cut free"
                    )
                if ref.kind == "opening":
                    if not 0 <= ref.index < len(part.openings):
                        raise ValueError(
                            f"plan releases opening {ref.index} of part {ref.part}, "
                            f"which has {len(part.openings)}"
                        )
                    finished = part.openings[ref.index]
                else:
                    finished = part.box
                side = entry_of.get(ref.profile)
                if side is None:
                    raise ValueError(
                        f"{ref} is in the release section but this program cuts no "
                        f"loop for that profile - the release would be milling "
                        f"material nothing else has been near"
                    )
                emitted += emitter.release(
                    finished, ref.kind, side, zones, tool, emitted == 0, ref
                )

        # The last section stops after M59/G80 and runs into the epilogue.
        for text in SECTION_TAIL if not last_section else SECTION_TAIL[:2]:
            emitter.line(text)

    if plan.tabs:
        orphans = sorted(key for key in plan.tabs if key not in tabbed)
        if orphans:
            raise ValueError(
                f"the plan puts holding tabs on profile(s) {orphans} that this "
                f"program never cuts a loop for - the tabs would not be emitted "
                f"and the parts would not be held"
            )

    emitter.section = None
    for text in EPILOGUE:
        emitter.line(text)

    events = tuple(emitter.events)
    return EmittedProgram(text=render(events, NEWLINE), events=events)


def generate(
    program: SheetProgram,
    plan: CutPlan,
    config: PostConfig | None = None,
) -> str:
    """Render ``program`` as ``.anc`` text (CRLF, ``%``-wrapped).

    Raises ``ValueError`` on a plan that references a part, opening or
    groove that does not exist — a plan is never allowed to fall through to
    a silently skipped cut.
    """
    return emit(program, plan, config).text


def generate_motions(
    program: SheetProgram,
    plan: CutPlan,
    config: PostConfig | None = None,
) -> list[Motion]:
    """Every move ``program`` commands, tagged with section, feature and pass.

    The same walk that writes the text (:func:`emit`), so
    :attr:`~.motion.Motion.line_index` indexes the lines of
    :func:`generate`'s output for the same three arguments.
    """
    return list(emit(program, plan, config).motions)
