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

Section tail, lines 99-102 (the last section drops the final two lines and
runs straight into the program footer)::

    M59
    G80
    G17 G91 G28 Z0 M95
    M92
"""

from __future__ import annotations

from .model import (
    Box,
    CutPlan,
    PanelSpec,
    PartProgram,
    PassSpec,
    PostConfig,
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_WDC_SLOT,
    SheetProgram,
    ToolSpec,
    WdcSlotSpec,
    default_config,
)

__all__ = [
    "generate",
    "fmt",
    "groove_segment",
    "wdc_slot_segment",
    "default_entry_side",
]

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


def groove_segment(part: PartProgram, index: int, panel: PanelSpec):
    """Return ``((x0, y0), (x1, y1))`` for one T13 groove of ``part``.

    ``index`` 0..3 = stile-low, rail-low, stile-high, rail-high in the
    part's own orientation; the returned segment always runs low-to-high
    (the plan's ``reverse`` flag picks the other direction).  A rotated
    part's stiles run along X instead of Y — see the module docstring of
    :mod:`~faceframe_cnc.post.model`.
    """
    box = part.box
    stile, rail, over = panel.stile_inset, panel.rail_inset, panel.overrun
    if not part.rotated:
        # Stiles are the left/right edges: grooves run in Y, overrunning.
        stile_lines = (box.x0 + stile, box.x1 - stile)
        rail_lines = (box.y0 + rail, box.y1 - rail)
        if index in (0, 2):
            x = stile_lines[0 if index == 0 else 1]
            return (x, box.y0 - over), (x, box.y1 + over)
        y = rail_lines[0 if index == 1 else 1]
        return (stile_lines[0], y), (stile_lines[1], y)
    # Rotated: stiles are the bottom/top edges.
    stile_lines = (box.y0 + stile, box.y1 - stile)
    rail_lines = (box.x0 + rail, box.x1 - rail)
    if index in (0, 2):
        y = stile_lines[0 if index == 0 else 1]
        return (box.x0 - over, y), (box.x1 + over, y)
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
    a 45-degree V bit's effective radius is its depth of cut, so the deeper
    pass overruns LESS than the shallower, wider one.  The segment always
    runs low-to-high.
    """
    box = part.box
    inset = spec.inset_from_outside_edge
    if not part.rotated:
        x = box.x0 + inset if index == 0 else box.x1 - inset
        return (x, box.y0 - overrun), (x, box.y1 + overrun)
    y = box.y0 + inset if index == 0 else box.y1 - inset
    return (box.x0 - overrun, y), (box.x1 + overrun, y)


class _Emitter:
    """Accumulates lines while tracking the modal machine position."""

    def __init__(self, config: PostConfig):
        self.config = config
        self.lines: list[str] = []
        self.x = 0.0
        self.y = 0.0

    def line(self, text: str) -> None:
        self.lines.append(text)

    def blank(self) -> None:
        self.lines.append("")

    # -- motion helpers ----------------------------------------------------

    def _axis_words(self, x: float, y: float) -> str:
        words = []
        if abs(x - self.x) > 1e-9:
            words.append(f"X{fmt(x)}")
        if abs(y - self.y) > 1e-9:
            words.append(f"Y{fmt(y)}")
        self.x, self.y = x, y
        return " ".join(words)

    def preposition(self, x: float, y: float, tool: ToolSpec, first: bool) -> None:
        cfg = self.config
        if first:
            self.line(
                f"G0 G54 G90 X{fmt(x)} Y{fmt(y)} M13 S{tool.speed}"
            )
            self.line(f"G43 H{tool.number} Z{fmt(cfg.rapid_z)}")
            self.line(f"G0 Z{fmt(cfg.approach_z)}")
        else:
            self.line(f"X{fmt(x)} Y{fmt(y)} Z{fmt(cfg.rapid_z)}")
            self.line(f"Z{fmt(cfg.approach_z)}")
        self.x, self.y = x, y

    def retract(self) -> None:
        self.line(f"G0 Z{fmt(self.config.rapid_z)}")

    # -- features ----------------------------------------------------------

    def groove(
        self,
        part: PartProgram,
        index: int,
        reverse: bool,
        tool: ToolSpec,
        panel: PanelSpec,
        first: bool,
    ) -> None:
        across = part.box.height if part.rotated else part.box.width
        along = part.box.width if part.rotated else part.box.height
        if across <= 2 * panel.stile_inset or along <= 2 * panel.rail_inset:
            raise ValueError(
                f"a {part.box.width}x{part.box.height} part is too small for the "
                f"{panel.stile_inset}/{panel.rail_inset} panel groove pattern"
            )
        start, end = groove_segment(part, index, panel)
        if reverse:
            start, end = end, start
        self.preposition(start[0], start[1], tool, first)
        self.line(f"G1 Z{fmt(panel.z_cut)} F{fmt(panel.entry_feed)}")
        self.line(f"{self._axis_words(end[0], end[1])} F{fmt(panel.cut_feed)}")
        self.retract()

    def slot(
        self,
        part: PartProgram,
        index: int,
        tool: ToolSpec,
        spec: WdcSlotSpec,
        first: bool,
    ) -> None:
        """Cut one WDC stile slot: every configured depth pass, in order,
        on the one centreline."""
        cfg = self.config
        for position, z_cut in enumerate(spec.z_cuts):
            start, end = wdc_slot_segment(
                part, index, spec, cfg.wdc_slot_reach(position)
            )
            self.preposition(start[0], start[1], tool, first and position == 0)
            self.line(f"G1 Z{fmt(z_cut)} F{fmt(spec.entry_feed)}")
            self.line(f"{self._axis_words(end[0], end[1])} F{fmt(spec.cut_feed)}")
            self.retract()

    def loop(
        self,
        box: Box,
        side: str,
        tool: ToolSpec,
        spec: PassSpec,
        first: bool,
    ) -> None:
        """Cut one closed rectangle counter-clockwise, leading in on the
        midpoint of ``side``."""
        cfg = self.config
        ramp = (cfg.approach_z - spec.z_cut) * cfg.ramp_ratio
        over = tool.diameter
        lead = spec.lateral_lead

        # Entry point, travel direction along the entry edge (CCW), and the
        # outward normal used to stand the ramp off the profile line.
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
        self.preposition(pre[0], pre[1], tool, first)
        self.line(
            f"G1 {self._axis_words(entry[0], entry[1])} "
            f"Z{fmt(spec.z_cut)} F{fmt(spec.entry_feed)}"
        )
        for i, corner in enumerate(corners):
            words = self._axis_words(corner[0], corner[1])
            self.line(f"{words} F{fmt(spec.cut_feed)}" if i == 0 else words)
        self.line(self._axis_words(entry[0], entry[1]))
        self.line(
            self._axis_words(entry[0] + step[0] * over, entry[1] + step[1] * over)
        )
        out = (
            entry[0] + step[0] * (over + ramp) + normal[0] * lead,
            entry[1] + step[1] * (over + ramp) + normal[1] * lead,
        )
        self.line(f"{self._axis_words(out[0], out[1])} Z{fmt(self.config.approach_z)}")
        self.retract()


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
        ("opening", cfg.openings_pass.z_cut),
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
    raise ValueError(f"unknown section {section!r}")


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
    cfg = config or default_config()
    _check_config(cfg, program)
    parts = program.flat_parts()
    emitter = _Emitter(cfg)
    header = program.header

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
                emitter.groove(part, ref.index, ref.reverse, tool, cfg.panel, i == 0)

        elif section == SECTION_WDC_SLOT:
            for i, ref in enumerate(plan.wdc_slot):
                part = part_of(ref, "wdc_slot")
                if not 0 <= ref.index <= 1:
                    raise ValueError(
                        f"WDC slot index {ref.index} out of range 0..1 (a frame "
                        f"has two stiles)"
                    )
                emitter.slot(part, ref.index, tool, cfg.wdc_slot, i == 0)

        elif section in (SECTION_OPENINGS, SECTION_DETAIL):
            spec = cfg.openings_pass if section == SECTION_OPENINGS else cfg.detail_pass
            refs = (
                plan.openings if section == SECTION_OPENINGS else plan.detail_order()
            )
            for i, ref in enumerate(refs):
                part = part_of(ref, "opening")
                if not 0 <= ref.index < len(part.openings):
                    raise ValueError(
                        f"plan references opening {ref.index} of part {ref.part}, "
                        f"which has {len(part.openings)}"
                    )
                opening = part.openings[ref.index]
                side = ref.entry or default_entry_side(opening, "opening")
                cut = opening.grow(spec.offset)
                _require_cuttable(cut, f"opening {ref.index} of part {ref.part}")
                emitter.loop(cut, side, tool, spec, i == 0)

        elif section == SECTION_PERIMETER:
            if len(plan.perimeter) != len(cfg.perimeter_passes):
                raise ValueError(
                    f"plan has {len(plan.perimeter)} perimeter pass order(s) but the "
                    f"post is configured for {len(cfg.perimeter_passes)} depth pass(es)"
                )
            index = 0
            for spec, refs in zip(cfg.perimeter_passes, plan.perimeter):
                for ref in refs:
                    part = part_of(ref, "perimeter")
                    side = ref.entry or default_entry_side(part.box, "perimeter")
                    cut = part.box.grow(spec.offset)
                    _require_cuttable(cut, f"the footprint of part {ref.part}")
                    emitter.loop(cut, side, tool, spec, index == 0)
                    index += 1
                    if index == 1 and cfg.perimeter_marker_after_first_loop:
                        emitter.line("M59")
                        emitter.retract()

        # The last section stops after M59/G80 and runs into the epilogue.
        for text in SECTION_TAIL if not last_section else SECTION_TAIL[:2]:
            emitter.line(text)

    for text in EPILOGUE:
        emitter.line(text)

    return NEWLINE.join(emitter.lines) + NEWLINE
