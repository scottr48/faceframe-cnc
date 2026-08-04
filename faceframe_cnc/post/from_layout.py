"""Turn an optimizer :class:`~faceframe_cnc.nesting.SheetLayout` into the
:class:`~.model.SheetProgram` + :class:`~.model.CutPlan` pair the emitter
consumes.

Phase 1 proved the emitter by round-tripping the production files; this
module is the other input path — a sheet the OPTIMIZER invented rather than
one the shop's CAM already cut.  It adds no geometry and no post table: the
grooves, offsets, depths and feeds all come from
:class:`~.model.PostConfig`; the only thing decided here is *sequence*, plus
the one hard refusal (WDC, below).

What the sequence is, and why
-----------------------------
``panel`` (T13)
    Every standard frame gets its four measured grooves — 0.5625 in from
    each stile edge running the full part length with the 0.375 overrun,
    0.9375 in from each rail running between the two stile centre lines.
    Order: parts in canonical (depth-first, host before its passengers)
    order, and within a part **stiles then rails** (groove indices 0, 2, 1,
    3).  The reference files' groove order is arbitrary — R720101N
    interleaves four parts' grooves with no discernible rule — so any
    deterministic order is faithful, and a predictable one diffs better.

    A WDC frame gets only the rail pair here; its stiles take the T17 slot
    instead (:func:`panel_groove_indices`).

``wdc_slot`` (T17) — only when the sheet holds a WDC frame
    Two 45-degree slots per WDC frame, one down each 2" stile, each cut in
    two depth passes on one centreline (Z0.4062 then Z0.3125).  Order:
    parts in canonical order, and within a part the LOW-side stile then the
    high-side one in sheet coordinates — the same low-then-high pair the
    T13 stile grooves would have used, so a reader comparing a WDC sheet
    with a plain one sees the two sections line up.  Both passes of one
    slot are emitted back to back: they share a centreline, so splitting
    them would only add a reposition.

    The section sits between T13 and the first T11 because the 2026-08-03
    amendment says so ("the T13 and T17 groove routing runs FIRST"), and
    because that is where T17 sits in ``RFK0101N.anc``.

``openings`` (T11 at Z0.15) and ``detail`` (T12 through)
    EVERY opening on the sheet, including the openings of frames nested
    inside another frame — those are routed while the inner's slab is still
    part of its host's interior waste, exactly as in R720101N.  Order:
    **deepest nesting first**, then canonical order.  R720101N cuts one
    host's opening before its inner's and the other's after, so the file
    licenses either; deepest-first is the one the 2026-08-03 onion-skin
    amendment's reasoning ("parts move less when everything stays anchored
    as long as possible") points at, because it finishes routing the inner
    before the T12 pass frees the host's slab.

    ``detail`` is left as ``None``, i.e. "repeat the opening order" — which
    is what all four reference files do, cut for cut.

``perimeter`` (T11, two depth passes) — the 2026-08-03 onion-skin amendment
    Pass 1 (Z0.06, the skin that still holds every part to the sheet) runs
    EVERY part in canonical order.  Pass 2 (Z-0.006, through) runs **all
    nested inner frames first, across the whole sheet**, then the
    non-nested parts and the hosts — same ``(-depth, index)`` key as the
    openings.  :class:`~.model.CutPlan` already carries one ordered list
    per depth pass, so this is pure sequencing.

Lead-in edges are left to :func:`~.generator.default_entry_side` (openings
wider than tall lead in on the bottom edge, everything else on the right).
The two per-feature overrides that exist in the references are replication
quirks with no stated rule behind them and are deliberately NOT carried
into generated sheets.

WDC: cut, but only with room for the cone
-----------------------------------------
The T17 slot is now emitted (Milestone 5), so a WDC sheet is no longer
refused for lacking a tool.  What IS still refused is a WDC frame with
something inside the reach of its own slot.

The deeper of the two passes cuts 0.4375 into the stock, and a 45-degree
bit's cut is as wide as it is deep: that pass runs its centre 0.4375 past
each stile end and its cone breaks the surface a further 0.4375 out, so the
material it sweeps ends **0.875 past the part**.  At the 0.455 part gap a
neighbour beyond a stile end would be carved up to ~0.42 deep.  The owner
has not approved nicking a neighbouring frame, so:

*   the optimizer keeps the full 0.875 clear beyond every WDC stile end,
    against neighbours and against the sheet edge alike
    (:class:`faceframe_cnc.nesting.NestingConfig`), and
    ``validate_layouts`` enforces the same rule independently;
*   :func:`plan_sheet` re-checks it here from the part geometry and raises
    :class:`WdcNotSupportedError` when a hand-built layout violates it;
*   :func:`faceframe_cnc.post.verifier.verify` models the swept cone a
    third time, from the finished NC text.

Three independent checks for one rule is deliberate: it is the only rule in
this post where a legal-looking layout damages a part the operator was not
expecting to lose.
"""

from __future__ import annotations

from dataclasses import replace

from ..geometry import (
    WDC_SLOT_DEPTH,
    WDC_SLOT_END_REACH,
    WDC_SLOT_INSET_FROM_INSIDE_EDGE,
    FrameType,
    infer_frame_type,
)
from .generator import wdc_slot_segment
from .model import (
    Box,
    CutPlan,
    DEFAULT_SECTIONS,
    FeatureRef,
    PanelSpec,
    PartProgram,
    PostConfig,
    ProgramHeader,
    SheetProgram,
    T17,
    default_config,
    program_from_placements,
)

__all__ = [
    "SheetPlanError",
    "WdcNotSupportedError",
    "T17",
    "WDC_SLOT_DEPTH",
    "WDC_SLOT_END_REACH",
    "WDC_SLOT_INSET_FROM_INSIDE_EDGE",
    "wdc_slot_lines",
    "panel_groove_indices",
    "part_depths",
    "cut_plan_for",
    "sheet_program_from_layout",
    "plan_sheet",
    "post_config_for",
    "is_wdc",
    "wdc_slot_z",
]

EPS = 1e-9


class SheetPlanError(ValueError):
    """This sheet cannot be turned into a program, with a reason fit for the UI."""


class WdcNotSupportedError(SheetPlanError):
    """A WDC frame on this sheet cannot be given its T17 slot safely.

    Kept under its Milestone-5-phase-2 name (callers key their "wdc"
    refusal category off it) but no longer means "the tool is unknown": T17
    is cut now, and this is raised only when something sits inside the
    slot's 0.875 end reach.
    """


# --------------------------------------------------------------------------
# T17 / WDC
# --------------------------------------------------------------------------

#: Owner-confirmed machine Z of the two passes, in order.  The post table
#: (:class:`~.model.WdcSlotSpec`) is the authority; this alias exists
#: because callers and tests written against the extension point used it.
WDC_SLOT_PASS_DEPTHS = default_config().wdc_slot.z_cuts

#: The amendment's earlier 15/16" centreline was a tape measurement and is
#: explicitly superseded by the 34 mm one.  Kept only so a reader who
#: remembers the old number can see which won.
WDC_SLOT_INSET_SUPERSEDED = 0.9375

#: Stile width of a WDC frame.  The post table
#: (:attr:`~.model.WdcSlotSpec.stile_width`) is what the emitter reads;
#: :mod:`faceframe_cnc.geometry` is where the frame engine gets it from.
WDC_STILE_WIDTH = default_config().wdc_slot.stile_width


def wdc_slot_lines(
    part: PartProgram,
    overrun: float | None = None,
    config: PostConfig | None = None,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """The two T17 slot centrelines of a WDC part, in sheet coordinates.

    Returned low-to-high like :func:`~.generator.groove_segment`, and
    oriented by the part's rotation the same way: an upright part's stiles
    are its left/right edges, so the slots run in Y.

    ``overrun`` past each end defaults to the full-depth value: a 45-degree
    V bit's effective radius equals its depth of cut, so the pass reaching
    :data:`WDC_SLOT_DEPTH` overruns by that much.  A shallower pass passes
    its own value in — :func:`cut_plan_for` and the emitter both take it
    from :meth:`~.model.PostConfig.wdc_slot_reach` per pass.

    The geometry itself is :func:`~.generator.wdc_slot_segment`, so this
    convenience wrapper and the emitted code can never disagree.
    """
    spec = (config or default_config()).wdc_slot
    if overrun is None:
        overrun = WDC_SLOT_DEPTH
    return [wdc_slot_segment(part, index, spec, overrun) for index in (0, 1)]


def wdc_slot_z(config: PostConfig | None = None) -> float:
    """Machine Z of the bottom of the 45-degree slot (0.3125 by default)."""
    cfg = config or default_config()
    return cfg.stock_top_z - WDC_SLOT_DEPTH


def is_wdc(part_number: str) -> bool:
    return infer_frame_type(part_number) is FrameType.WDC


def panel_groove_indices(part_number: str) -> tuple[int, ...]:
    """Which T13 grooves a part gets, stiles first then rails.

    ``0, 2`` are the two stile grooves and ``1, 3`` the two rail grooves in
    :func:`~.generator.groove_segment`'s numbering.  A WDC frame gets only
    the rail pair — its stiles take the T17 slot instead (2026-08-03
    amendment) — which is why the rule lives in one place.
    """
    if is_wdc(part_number):
        return (1, 3)
    return (0, 2, 1, 3)


def wdc_slot_sweep(
    part: PartProgram, index: int, position: int, config: PostConfig
) -> Box:
    """Where WDC slot pass ``position`` removes material at the stock SURFACE.

    The 45-degree cone is widest where it breaks the surface, so the box
    returned here — the centreline grown by that pass's surface radius on
    every side — is what the cut actually costs the sheet.  Along the slot
    it reaches ``2 * radius`` past the part end (the centre's overrun plus
    the cone's own half width), which for the deep pass is
    :data:`~faceframe_cnc.geometry.WDC_SLOT_END_REACH`.
    """
    reach = config.wdc_slot_reach(position)
    (x0, y0), (x1, y1) = wdc_slot_segment(part, index, config.wdc_slot, reach)
    return Box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)).grow(reach)


def _check_wdc_clearance(program: SheetProgram, config: PostConfig) -> None:
    """Refuse a sheet where a WDC slot would cut something it must not.

    Checked against every OTHER part's solid (footprint minus its openings)
    so that a WDC nested in a host is judged against the host's rails and
    stiles, not against the opening void it legitimately sits in — and
    against the sheet itself, since a cone that runs off the edge is a cut
    into the spoilboard fence, not into trim.
    """
    parts = program.flat_parts()
    solids = [(part, part.solid_boxes()) for part in parts]
    sheet = Box(0.0, 0.0, program.sheet_width, program.sheet_length)

    for part in parts:
        if not is_wdc(part.part_number):
            continue
        for index in (0, 1):
            for position in range(len(config.wdc_slot.z_cuts)):
                swept = wdc_slot_sweep(part, index, position, config)
                if not sheet.contains(swept, EPS):
                    raise WdcNotSupportedError(_wdc_edge_refusal(part, swept, program))
                for other, bands in solids:
                    if other is part:
                        continue
                    for band in bands:
                        if band.overlaps(swept, EPS):
                            raise WdcNotSupportedError(
                                _wdc_neighbour_refusal(part, other)
                            )


def _wdc_reason() -> str:
    return (
        f"A WDC frame's 2\" stiles take a 45-degree T17 slot "
        f"({WDC_SLOT_DEPTH:g} deep, centreline "
        f"{WDC_SLOT_INSET_FROM_INSIDE_EDGE:g} from the stile's inside edge) "
        f"instead of the standard T13 stile grooves. The deep pass is as wide "
        f"as it is deep, so the material it removes ends "
        f"{WDC_SLOT_END_REACH:g} past each end of the stile"
    )


def _wdc_neighbour_refusal(part: PartProgram, other: PartProgram) -> str:
    return (
        f"refusing to generate NC for this sheet: the T17 stile slot of "
        f"{part.part_number} @({part.box.x0:.4f},{part.box.y0:.4f}) would cut "
        f"into {other.part_number} @({other.box.x0:.4f},{other.box.y0:.4f}). "
        f"{_wdc_reason()}, and that neighbour is closer than that. Re-nest the "
        f"sheet so nothing sits within {WDC_SLOT_END_REACH:g} of a WDC stile "
        f"end - the optimizer leaves that room automatically; a hand-placed "
        f"part can take it away. Nicking a finished frame is not something "
        f"this post will do silently."
    )


def _wdc_edge_refusal(part: PartProgram, swept: Box, program: SheetProgram) -> str:
    return (
        f"refusing to generate NC for this sheet: the T17 stile slot of "
        f"{part.part_number} @({part.box.x0:.4f},{part.box.y0:.4f}) sweeps "
        f"x[{swept.x0:.4f}, {swept.x1:.4f}] y[{swept.y0:.4f}, {swept.y1:.4f}], "
        f"off the {program.sheet_width:g}x{program.sheet_length:g} sheet. "
        f"{_wdc_reason()}, so a WDC frame needs that much room between its "
        f"stile ends and the edge of the sheet."
    )


# --------------------------------------------------------------------------
# Layout -> program
# --------------------------------------------------------------------------


def _walk(placements):
    for placement in placements:
        yield placement
        yield from _walk(placement.children)


def _check_against_order(placements, specs) -> None:
    """Placed dimensions must be the ordered ones, rotation aside (spec 4a).

    The optimizer's own validator says the same thing; repeating it on the
    way into the post means a hand-edited layout that slipped past cannot
    quietly cut a resized frame.
    """
    if not specs:
        return
    ordered = {spec.part_number: spec for spec in specs}
    for placement in _walk(placements):
        spec = ordered.get(placement.part_number)
        if spec is None:
            raise SheetPlanError(
                f"{placement.part_number} is on the sheet but not in the order — "
                f"refusing to cut a part with no order line"
            )
        same = (
            abs(placement.width - spec.width) <= 1e-6
            and abs(placement.height - spec.height) <= 1e-6
        )
        swapped = (
            abs(placement.width - spec.height) <= 1e-6
            and abs(placement.height - spec.width) <= 1e-6
        )
        if not (same or swapped):
            raise SheetPlanError(
                f"{placement.part_number} is placed {placement.width:g}x"
                f"{placement.height:g} but ordered {spec.width:g}x{spec.height:g} — "
                f"frame dimensions must never be altered"
            )


def sheet_program_from_layout(
    layout,
    header: ProgramHeader,
    specs=None,
    nesting_config=None,
) -> SheetProgram:
    """Build the :class:`~.model.SheetProgram` for one optimizer sheet.

    ``layout`` is a :class:`~faceframe_cnc.nesting.SheetLayout` (or anything
    with a ``placements`` list), ``specs`` the ordered
    :class:`~faceframe_cnc.nesting.PartSpec` lines, and ``nesting_config`` a
    :class:`~faceframe_cnc.nesting.NestingConfig` supplying the sheet size.
    """
    placements = getattr(layout, "placements", layout)
    if not placements:
        raise SheetPlanError("the sheet is empty — nothing to cut")

    _check_against_order(placements, specs)

    width = 49.0
    length = 97.0
    if nesting_config is not None:
        width = float(nesting_config.sheet_width)
        length = float(nesting_config.sheet_height)

    try:
        return program_from_placements(
            placements, header, sheet_width=width, sheet_length=length
        )
    except ValueError as exc:  # geometry engine rejected a frame
        raise SheetPlanError(str(exc)) from exc


# --------------------------------------------------------------------------
# Program -> plan
# --------------------------------------------------------------------------


def part_depths(program: SheetProgram) -> list[int]:
    """Nesting depth of each part, indexed like :meth:`SheetProgram.flat_parts`.

    0 for a part sitting on the sheet, 1 for a frame nested in one of its
    openings, 2 for a frame nested in THAT frame, and so on.
    """
    depths: dict[int, int] = {}

    def walk(items, depth: int) -> None:
        for part in items:
            depths[id(part)] = depth
            walk(part.children, depth + 1)

    walk(program.parts, 0)
    return [depths[id(part)] for part in program.flat_parts()]


def _inners_first(depths: list[int]) -> list[int]:
    """Part indices, deepest nesting first, canonical order within a depth."""
    return sorted(range(len(depths)), key=lambda i: (-depths[i], i))


def cut_plan_for(
    program: SheetProgram,
    sections: tuple[str, ...] = DEFAULT_SECTIONS,
    config: PostConfig | None = None,
) -> CutPlan:
    """The deterministic cut sequence for a generated sheet (module docstring)."""
    cfg = config or default_config()
    parts = program.flat_parts()
    if not parts:
        raise SheetPlanError("the sheet is empty — nothing to cut")
    depths = part_depths(program)
    canonical = list(range(len(parts)))
    inners_first = _inners_first(depths)

    panel: list[FeatureRef] = []
    slots: list[FeatureRef] = []
    for index in canonical:
        part = parts[index]
        _check_groove_fit(part, cfg.panel)
        for groove in panel_groove_indices(part.part_number):
            panel.append(FeatureRef(index, "groove", groove))
        if is_wdc(part.part_number):
            _check_slot_fit(part, cfg)
            slots.extend(FeatureRef(index, "wdc_slot", stile) for stile in (0, 1))
    if slots:
        _check_wdc_clearance(program, cfg)

    openings: list[FeatureRef] = []
    for index in inners_first:
        for opening in range(len(parts[index].openings)):
            openings.append(FeatureRef(index, "opening", opening))
    if not openings:
        raise SheetPlanError(
            "no part on this sheet has a routed opening — the geometry engine "
            "produced nothing to cut"
        )

    perimeter = [
        [FeatureRef(index, "perimeter") for index in canonical],
        [FeatureRef(index, "perimeter") for index in inners_first],
    ]
    if len(perimeter) != len(cfg.perimeter_passes):
        raise SheetPlanError(
            f"the onion-skin sequence needs exactly {len(perimeter)} perimeter "
            f"depth passes but the post table has {len(cfg.perimeter_passes)}"
        )

    return CutPlan(
        panel=panel,
        wdc_slot=slots,
        openings=openings,
        perimeter=perimeter,
        detail=None,
        sections=sections,
    )


def _check_groove_fit(part: PartProgram, panel: PanelSpec) -> None:
    across = part.box.height if part.rotated else part.box.width
    along = part.box.width if part.rotated else part.box.height
    if across <= 2 * panel.stile_inset + EPS or along <= 2 * panel.rail_inset + EPS:
        raise SheetPlanError(
            f"{part.part_number} is {part.box.width:g}x{part.box.height:g}, too "
            f"small for the measured {panel.stile_inset:g}/{panel.rail_inset:g} "
            f"panel-groove pattern"
        )


def _check_slot_fit(part: PartProgram, config: PostConfig) -> None:
    """The two slots must land on the part's own two stiles.

    A WDC frame narrower than two stiles plus a gap would put both slots in
    the same place, or outside the frame; the geometry engine would have
    rejected such a part already, but this post never emits a cut it has
    not checked belongs to a real member.
    """
    spec = config.wdc_slot
    across = part.box.height if part.rotated else part.box.width
    if across <= 2 * spec.stile_width + EPS:
        raise SheetPlanError(
            f"{part.part_number} is {part.box.width:g}x{part.box.height:g}: its "
            f"two {spec.stile_width:g}\" stiles leave no frame between them, so "
            f"the T17 slot centrelines are not on stiles"
        )


def plan_sheet(
    layout,
    header: ProgramHeader,
    specs=None,
    nesting_config=None,
    post_config: PostConfig | None = None,
    sections: tuple[str, ...] = DEFAULT_SECTIONS,
) -> tuple[SheetProgram, CutPlan]:
    """``(SheetProgram, CutPlan)`` for one optimizer sheet.

    Raises :class:`WdcNotSupportedError` (a :class:`SheetPlanError`) when a
    WDC frame's T17 slot would reach into a neighbouring part or off the
    sheet, and :class:`SheetPlanError` for anything else this post cannot
    honestly cut.
    """
    program = sheet_program_from_layout(layout, header, specs, nesting_config)
    plan = cut_plan_for(program, sections=sections, config=post_config)
    return program, plan


def post_config_for(
    nesting_config, base: PostConfig | None = None
) -> PostConfig:
    """A post table whose sheet size matches the optimizer's.

    The emitter refuses a program whose sheet differs from its configured
    one (a mismatch means the coordinates were computed for another sheet),
    so the two settings are tied together here rather than left to callers.
    """
    cfg = base or default_config()
    if nesting_config is None:
        return cfg
    return replace(
        cfg,
        sheet_width=float(nesting_config.sheet_width),
        sheet_length=float(nesting_config.sheet_height),
    )
