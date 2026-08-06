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
    each stile edge running the full part length, 0.9375 in from each rail
    running between the two stile centre lines.  The stile pair's measured
    0.375 overrun past the part ends is CLAMPED away since the 2026-08-05
    amendment (:func:`~.generator.groove_segment`): the cut ends flush with
    the part edge, which is why this module adds no groove spacing rule (spec
    §8 — Scott chose the clamp over a nesting clearance, and the clamp makes
    the clearance unnecessary by construction).
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

``openings`` (T11, the max-bite ladder down to Z0.15) and ``detail`` (T12 through)
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

    **2026-08-05 amendment (Scott): the T11 roughing pass is a LADDER.**  The
    measured pass takes 0.60 of material in one bite (Z0.75 surface down to
    Z0.15), which is more than the 0.4 the 3/8 compression bit is now allowed
    (:data:`T11_MAX_BITE`), so a generated sheet cuts every opening in two equal
    0.3 bites — Z0.45 then Z0.15 — at the measured 0.1975 offset and the
    measured feeds (:func:`generated_opening_passes`).  Both bites of one opening
    are emitted back to back, exactly as the two bites of one T17 slot are: they
    are the same rectangle at two depths, so splitting them would only add a
    reposition, and neither bite frees anything (the T12 detail pass is what cuts
    through), so nothing about the sheet's stability depends on interleaving
    them.  The T12 detail pass is a different tool and is untouched.

``perimeter`` (T11) — the max-bite ladder, ending on the through pass
    The last configured pass (Z-0.006, through) runs **all nested inner
    frames first, across the whole sheet**, then the non-nested parts and
    the hosts — same ``(-depth, index)`` key as the openings.  Any pass
    before it runs EVERY part in canonical order.
    :class:`~.model.CutPlan` carries one ordered list per depth pass, so
    this is pure sequencing, and the sequence is built from however many
    passes the post table in hand configures
    (:func:`generated_post_passes`).

    **2026-08-05 amendment (Scott, job R0805): the onion skin goes.**  The
    2026-08-03 onion-skin order put a Z0.06 skin pass first so that 0.06" of
    material still held every part while the rest of the sheet was cut; job
    R0805 broke two frames anyway (a freed opening dropout beside a thin MDF
    ring), the answer to that is tabs (spec §3, milestones 2b/3), and Scott's
    decision on 2026-08-05 was that once parts are tab-held the skin has no
    holding job left — "don't need it anymore".

    **2026-08-05, second amendment the same day (Scott): 0.4 of material per
    T11 pass, to reduce the load on the 3/8 compression bit.**  Dropping the
    skin left the through pass taking the whole 0.756 in one go, which is what
    Scott saw and ruled on: *"when the 3/8 comp (T11) is being used, only let it
    take a maximum of 0.4 inch of material per pass. That will help reduce the
    load on it."*  0.756 "basically cuts in half", so a generated perimeter runs
    **two equal 0.378 bites** — an intermediate pass at Z0.372 in canonical
    order, then the through pass at Z-0.006 inners-first.  That is not the old
    onion skin back under another name: the skin was a 0.06 holding rib at the
    bottom of the cut, this is a depth-of-cut ladder whose rungs are
    :data:`T11_MAX_BITE` apart, the holding is done by the tabs either way, and
    the intermediate pass runs at the measured pass-1 offset (0.1895, leaving
    0.002 of spring stock for the through pass to shave) and the measured
    perimeter feeds.  :func:`generated_post_passes` builds the ladder and
    :func:`post_config_for` is the one place it is turned on.

    The measured table in :func:`~.model.default_config` keeps BOTH passes:
    the reference ``.anc`` files were cut with the two-pass dialect and
    :mod:`~.reconstruct` and :func:`~.verifier.verify` must go on reading
    and judging them exactly as before.  This module's sequencing works for
    either.

``release`` (T12) — the last section, since the 2026-08-05 amendment
    One straight cut per holding tab, flush with the finished profile, at the
    ratified release feeds (spec §3c).  Order: every OPENING's tabs first, then
    every perimeter's, inners before hosts within each — so the last motions in
    the program free an outermost part, and nothing on the sheet is fully
    separated until this section runs.  :func:`hold_profile` decides where the
    tabs are; this module only sequences them, and the geometry of a release cut
    belongs to :func:`~.generator.release_path` and :func:`~.tabs.release_span`.

    The section exists only when the post table configures a release pass
    (:attr:`~.model.PostConfig.release`, which :func:`post_config_for` sets and
    the measured table leaves ``None``) — and so, therefore, do the tabs: a tab
    with nothing to release it is a part that never comes off the sheet.

Lead-in edges: PINNED, one per profile (2026-08-05 amendment)
------------------------------------------------------------
:func:`~.generator.default_entry_side` still says which edge is preferred
(openings wider than tall lead in on the bottom edge, everything else on the
right), but a tabbed profile can no longer let each pass answer for itself:
tab placement excludes exactly one lead-in span, so every pass over one profile
has to enter on the same edge.  :func:`hold_profile` therefore pins ONE edge per
profile against the worst case over all of that profile's passes
(:func:`~.generator.pinned_entry_side`) and every :class:`~.model.FeatureRef`
this module builds carries it explicitly — openings, detail (which repeats the
opening refs), perimeter and release alike.  For every sheet the shop actually
cuts that is the same edge the per-pass rule chose, so nothing moved; what
changed is that it can no longer differ by pass.

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

import math
from dataclasses import replace

from ..geometry import (
    WDC_SLOT_DEPTH,
    WDC_SLOT_END_REACH,
    WDC_SLOT_INSET_FROM_INSIDE_EDGE,
    FrameType,
    infer_frame_type,
)
from .generator import pinned_entry_side, profile_cuts, wdc_slot_segment
from .model import (
    Box,
    CutPlan,
    DEFAULT_SECTIONS,
    FeatureRef,
    PanelSpec,
    PartProgram,
    PassSpec,
    PostConfig,
    ProgramHeader,
    ReleaseSpec,
    SECTION_OPENINGS,
    SECTION_PERIMETER,
    SheetProgram,
    T17,
    default_config,
    program_from_placements,
)
from .tabs import TabZone, place_tabs

__all__ = [
    "SheetPlanError",
    "WdcNotSupportedError",
    "T17",
    "T11_MAX_BITE",
    "WDC_SLOT_DEPTH",
    "WDC_SLOT_END_REACH",
    "WDC_SLOT_INSET_FROM_INSIDE_EDGE",
    "wdc_slot_lines",
    "panel_groove_indices",
    "part_depths",
    "cut_plan_for",
    "sheet_program_from_layout",
    "plan_sheet",
    "bite_ladder",
    "max_bite_for",
    "generated_post_passes",
    "generated_opening_passes",
    "generated_tools",
    "post_config_for",
    "hold_profile",
    "is_wdc",
    "wdc_slot_z",
]

EPS = 1e-9

#: How much material a T11 pass may remove on a GENERATED sheet, in inches of
#: depth.  **RATIFIED POLICY — Scott, 2026-08-05**, in his own words: *"When the
#: 3/8 comp (T11) is being used, only let it take a maximum of 0.4 inch of
#: material per pass. That will help reduce the load on it."*  He had just seen a
#: perimeter take the full 0.756 in a single pass (the onion skin having been
#: dropped earlier the same day) and noted that 0.4 "basically cuts that in
#: half".
#:
#: Not a measurement.  Every Z level, offset and feed in this post came off the
#: reference ``.anc`` files (rule zero); this number came off the owner, like
#: :class:`~.model.TabSpec` and :class:`~.model.ReleaseSpec`, and like them it
#: lives in exactly one place so a shop that re-times the bit has one line to
#: change.  It is applied to the two T11 sections of a GENERATED sheet's post
#: table by :func:`generated_tools`, read back off that table by
#: :func:`max_bite_for`, and turned into a pass ladder by
#: :func:`generated_post_passes` / :func:`generated_opening_passes`.  The
#: measured table declares no limit, so the reference programs are read and
#: judged exactly as they were cut.
T11_MAX_BITE = 0.4


class SheetPlanError(ValueError):
    """This sheet cannot be turned into a program, with a reason fit for the UI.

    The message is the whole reason and is what a UI shows; the two optional
    attributes say WHICH part the refusal is about, so a view can point at it
    on the sheet instead of leaving the operator to find it in the prose:

    ``part_number``
        the part this post is refusing to cut, or ``None``;
    ``box``
        that part's footprint in sheet coordinates, or ``None``.

    Both default to ``None`` and both are keyword-only, so every existing
    ``raise SheetPlanError(message)`` keeps working unchanged and the message
    text is untouched — the raise sites that happen to know a part fill them
    in, and the ones that do not (an empty sheet, a mis-sized post table) do
    not have to pretend.
    """

    def __init__(
        self,
        *args,
        part_number: str | None = None,
        box: Box | None = None,
    ):
        super().__init__(*args)
        self.part_number = part_number
        self.box = box


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
                    raise WdcNotSupportedError(
                        _wdc_edge_refusal(part, swept, program),
                        part_number=part.part_number,
                        box=part.box,
                    )
                for other, bands in solids:
                    if other is part:
                        continue
                    for band in bands:
                        if band.overlaps(swept, EPS):
                            # The part named is the one whose CUT is refused:
                            # the slot belongs to the WDC frame, and it is the
                            # frame a view has to point at.  The neighbour it
                            # would have carved is in the message.
                            raise WdcNotSupportedError(
                                _wdc_neighbour_refusal(part, other),
                                part_number=part.part_number,
                                box=part.box,
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
        # Placement dimensions are AS PLACED (already swapped when rotated),
        # so this is the footprint without any further reasoning.
        where = Box.from_size(
            placement.x, placement.y, placement.width, placement.height
        )
        spec = ordered.get(placement.part_number)
        if spec is None:
            raise SheetPlanError(
                f"{placement.part_number} is on the sheet but not in the order — "
                f"refusing to cut a part with no order line",
                part_number=placement.part_number,
                box=where,
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
                f"frame dimensions must never be altered",
                part_number=placement.part_number,
                box=where,
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


def hold_profile(
    part: PartProgram,
    kind: str,
    finished: Box,
    config: PostConfig,
) -> tuple[str, tuple[TabZone, ...]]:
    """``(entry side, tab zones)`` for one profile — the 2026-08-05 amendment.

    The two decisions that have to be made TOGETHER, made in one place:

    *   which edge every pass over this profile leads in on
        (:func:`~.generator.pinned_entry_side`, worst case over
        :func:`~.generator.profile_cuts`).  One edge per profile, pinned onto
        every :attr:`~.model.FeatureRef.entry` that cuts it, because tab
        placement excludes exactly ONE lead-in span and a second pass entering
        elsewhere would ramp through a tab (spec §3b);
    *   where the tabs are (:func:`~.tabs.place_tabs`), which is answered
        against that edge and against those same passes — so a pass at or above
        the tab top gets no vote and an AIR-CUT table (every depth mirrored
        above the stock, :func:`~.job.dry_run_config`) places nothing.

    No tabs at all when the post table configures no release pass
    (:attr:`~.model.PostConfig.release` — which the MEASURED table leaves
    ``None``).  That is not a shortcut, it is the safety rule: a tab nothing
    releases is a part that never comes off the sheet, so the two are one
    decision and :func:`post_config_for` makes it.  The entry side is still
    pinned, because pinning it costs nothing and is what the amendment asks for.

    Raises :class:`SheetPlanError`, naming the part, when no edge fits the sheet
    for all of this profile's passes: at that point the sheet needs re-nesting
    and the operator has to be told which frame is the problem.
    """
    try:
        side = pinned_entry_side(finished, kind, profile_cuts(kind, config), config)
    except ValueError as exc:
        where = "footprint" if kind == "perimeter" else "opening"
        raise SheetPlanError(
            f"{part.part_number} @({part.box.x0:.4f},{part.box.y0:.4f}) cannot be "
            f"cut on this sheet: its {where} {exc}",
            part_number=part.part_number,
            box=part.box,
        ) from exc
    if config.release is None:
        return side, ()
    return side, place_tabs(finished, side, profile_cuts(kind, config), config)


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

    #: Where the holding tabs are, per profile, and the one edge every pass over
    #: that profile leads in on (:func:`hold_profile`).
    zones: dict[tuple[int, str, int], tuple[TabZone, ...]] = {}
    release: list[FeatureRef] = []

    openings: list[FeatureRef] = []
    for index in inners_first:
        part = parts[index]
        for opening in range(len(part.openings)):
            side, tabs_here = hold_profile(
                part, "opening", part.openings[opening], cfg
            )
            ref = FeatureRef(index, "opening", opening, entry=side)
            openings.append(ref)
            if tabs_here:
                zones[ref.profile] = tabs_here
                # Spec §3c: opening tabs first, and the openings are already in
                # inners-before-hosts order, so this list inherits it.
                release.append(ref)
    if not openings:
        raise SheetPlanError(
            "no part on this sheet has a routed opening — the geometry engine "
            "produced nothing to cut"
        )

    if not cfg.perimeter_passes:
        raise SheetPlanError(
            "the post table configures no perimeter depth pass, so nothing would "
            "cut any part free"
        )
    # One ordered list per configured depth pass (module docstring): the LAST
    # pass is the one that cuts a part's outline right through, so it runs inners
    # first; anything before it only scores, so canonical order will do.  Two
    # passes give the 2026-08-03 onion-skin sequence on the measured table and
    # the 2026-08-05 max-bite ladder on a generated one, one gives a table with
    # neither, and the shape of the loop is why none of them is written down
    # twice.
    footprint: dict[int, FeatureRef] = {}
    for index in canonical:
        side, tabs_here = hold_profile(parts[index], "perimeter", parts[index].box, cfg)
        footprint[index] = FeatureRef(index, "perimeter", entry=side)
        if tabs_here:
            zones[footprint[index].profile] = tabs_here
    perimeter = [
        [footprint[index] for index in canonical] for _ in cfg.perimeter_passes[:-1]
    ]
    perimeter.append([footprint[index] for index in inners_first])
    # Spec §3c: the perimeter tabs come after every opening tab, inners before
    # hosts — the same order the freeing pass itself runs in, so the last
    # motions in the whole program free an outermost part.
    release.extend(
        footprint[index] for index in inners_first if footprint[index].profile in zones
    )

    return CutPlan(
        panel=panel,
        wdc_slot=slots,
        openings=openings,
        perimeter=perimeter,
        detail=None,
        sections=sections,
        tabs=zones or None,
        release=release,
    )


def _check_groove_fit(part: PartProgram, panel: PanelSpec) -> None:
    across = part.box.height if part.rotated else part.box.width
    along = part.box.width if part.rotated else part.box.height
    if across <= 2 * panel.stile_inset + EPS or along <= 2 * panel.rail_inset + EPS:
        raise SheetPlanError(
            f"{part.part_number} is {part.box.width:g}x{part.box.height:g}, too "
            f"small for the measured {panel.stile_inset:g}/{panel.rail_inset:g} "
            f"panel-groove pattern",
            part_number=part.part_number,
            box=part.box,
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
            f"the T17 slot centrelines are not on stiles",
            part_number=part.part_number,
            box=part.box,
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


def bite_ladder(
    final: PassSpec, template: PassSpec, max_bite: float | None, stock_top_z: float
) -> tuple[PassSpec, ...]:
    """``final`` split into equal bites no deeper than ``max_bite``.

    The arithmetic of Scott's 2026-08-05 rule, written once and used by both
    ladders (:func:`generated_post_passes`, :func:`generated_opening_passes`):

    *   the cut is ``stock_top_z - final.z_cut`` deep (Z0 is the spoilboard, so
        the depth of cut is measured down from the surface — see
        :mod:`~.model`'s Z table);
    *   ``n = ceil(depth / max_bite)`` bites, each ``depth / n``.  EQUAL bites,
        not "as much as allowed then the remainder": Scott's own words for the
        0.756 perimeter were that 0.4 "basically cuts that in half", i.e. two
        passes of 0.378 rather than 0.4 + 0.356.  Equal bites also mean the
        machine sees the same load twice instead of a heavy pass followed by a
        light one;
    *   the LAST rung is ``final`` itself, unchanged — same Z, same offset, same
        feeds, same lateral lead — because that is the measured pass that cuts
        the feature to size and nothing about it may move;
    *   every rung above it is ``template`` at the ladder's Z.  ``template``
        carries the offset and the feeds a non-final pass runs at, which are
        measured values the CALLER picks out of its own table (the perimeter's
        pass-1 spec, the opening pass itself); this function never invents one.

    ``max_bite`` of ``None`` — a table that declares no limit, which is every
    measured table — returns ``(final,)``: one pass, exactly as before, which is
    what keeps the reference dialect and its round-trips untouched.
    """
    depth = stock_top_z - final.z_cut
    if max_bite is None or max_bite <= 0 or depth <= max_bite + EPS:
        return (final,)
    count = math.ceil(depth / max_bite - EPS)
    bite = depth / count
    return (
        *(
            replace(template, z_cut=round(stock_top_z - bite * step, 9))
            for step in range(1, count)
        ),
        final,
    )


def max_bite_for(cfg: PostConfig, section: str) -> float | None:
    """The deepest bite the tool in ``section`` may take, or ``None``.

    One accessor so that every ladder in this module asks the same question of
    the same place — the TOOL's own :attr:`~.model.ToolSpec.max_bite`, which is
    where a rule about a bit belongs (:class:`~.model.ToolSpec`) and what the
    verifier reads back off the finished config.
    """
    tool = cfg.tools.get(section)
    return None if tool is None else tool.max_bite


def generated_post_passes(cfg: PostConfig) -> tuple[PassSpec, ...]:
    """The perimeter depth passes a GENERATED sheet is cut with.

    Both halves of the 2026-08-05 amendment, in the order Scott made them:

    1.  **the onion skin goes.**  The measured table carries two perimeter
        passes — the Z0.06 onion skin that held every part while the sheet was
        finished, then the Z-0.006 pass that cuts through — because that is what
        the reference programs do.  A generated sheet no longer runs the skin:
        the parts are held by tabs (spec §3), so the skin's holding job is over.
        Everything below is built on the LAST configured pass, the through one.
    2.  **0.4 of material per pass** (:data:`T11_MAX_BITE`, Scott: "that will
        help reduce the load on it").  The through pass alone would cut 0.756
        deep, so :func:`bite_ladder` splits it into two equal 0.378 bites: an
        intermediate pass at **Z0.372**, then the measured through pass at
        Z-0.006, untouched.

    Where the intermediate pass's offset and feeds come from, and why
    ----------------------------------------------------------------
    From the measured table's own FIRST perimeter pass — offset 0.1895 (0.1875
    tool radius plus 0.002 of spring stock) and the measured 150/498.2 feeds.
    Nothing here is invented: the shop's own two-pass wall treatment is a
    roughing lap 0.002 proud of the finished line followed by a through lap
    tangent to it, and that is exactly the relationship a max-bite ladder wants
    — the deep bite takes the wall down to 0.002 over size and the through pass
    finish-shaves it at full depth.  It also restores the verifier's backstop for
    a too-tight part gap: the intermediate lap sweeps 0.377 past the part edge
    (0.1895 + 0.1875), so two parts 0.375 apart are refused again, which the
    single tangent through pass could not see.

    A table whose T11 declares no ``max_bite`` (:func:`~.model.default_config`,
    and any base a caller hands in unchanged) gets the single through pass this
    function returned before the ladder existed.

    This is all a SCHEDULING policy, not a change to the measured table: the
    reference files keep their two-pass dialect forever and
    :func:`~.model.default_config` keeps describing it, which is what lets
    :mod:`~.reconstruct` read them and :func:`~.verifier.verify` judge them
    exactly as it did before the amendment.
    """
    through = cfg.perimeter_passes[-1]
    # The roughing template is the measured pass 1 when the table has one; on a
    # table that carries the through pass alone there is nothing else measured to
    # reach for, so the ladder repeats the through pass's own offset and feeds.
    template = cfg.perimeter_passes[0] if len(cfg.perimeter_passes) > 1 else through
    return bite_ladder(
        through, template, max_bite_for(cfg, SECTION_PERIMETER), cfg.stock_top_z
    )


def generated_opening_passes(cfg: PostConfig) -> tuple[PassSpec, ...]:
    """The T11 opening roughing passes a GENERATED sheet is cut with.

    The same rule as :func:`generated_post_passes`, on the other T11 operation,
    and this one is a straight application of it with nothing to undo first: the
    measured opening pass takes 0.60 in one bite (Z0.75 down to Z0.15), which is
    over the ratified 0.4, so it becomes **two equal 0.3 bites — Z0.45 then
    Z0.15**.

    Both rungs run at the measured opening pass's OWN offset (-0.1975: the 0.1875
    tool radius plus the 0.010 of finish stock T12 takes) and its own measured
    feeds, so the template and the final pass are the same spec at two depths.
    There is no equivalent of the perimeter's 0.002 spring stock here because the
    T12 detail pass is what finishes an opening to size, and it is a different
    tool with no bite limit of its own.

    FLAGGED FOR RATIFICATION (2026-08-05): Scott's instruction named the
    perimeter, because the perimeter is what he had just watched cut 0.756 in one
    go.  The rule he stated is about the TOOL — "when the 3/8 comp is being
    used" — and the opening pass is the same bit taking 0.60, so it is inside the
    rule as worded and is split here.  If he meant the perimeter only, the change
    is to declare the limit on a perimeter-only T11 rather than on the tool.
    """
    final = cfg.openings_passes[-1]
    return bite_ladder(
        final, final, max_bite_for(cfg, SECTION_OPENINGS), cfg.stock_top_z
    )


def generated_tools(cfg: PostConfig) -> dict:
    """``cfg.tools`` with the ratified T11 bite limit declared on it.

    RATIFIED POLICY, **Scott, 2026-08-05** — see :data:`T11_MAX_BITE`.  It is
    stamped on the tools of the two T11 sections here, and only here, so that
    every reader of a generated sheet's post table (the ladders above, the
    emitter's own config check, and the verifier re-deriving the ladder from the
    text) reads ONE number from one place.  A table whose section already
    declares a limit keeps it: a caller who has re-tuned the bit is not
    overruled.

    The measured table is not touched: :func:`~.model.default_config` declares
    no limit anywhere, which is why a reference program is judged by exactly the
    rules it was cut under.
    """
    tools = dict(cfg.tools)
    for section in (SECTION_OPENINGS, SECTION_PERIMETER):
        tool = tools.get(section)
        if tool is not None and tool.max_bite is None:
            tools[section] = replace(tool, max_bite=T11_MAX_BITE)
    return tools


def post_config_for(
    nesting_config, base: PostConfig | None = None
) -> PostConfig:
    """The post table a GENERATED sheet is cut with.

    Every generated-sheet policy is decided here, and this is the only place any
    of them is decided, because :func:`faceframe_cnc.post.job.build_job` and
    :meth:`faceframe_cnc.gui.session.Session.simulation_inputs` — every route
    from an optimizer layout to emitted code — come through it:

    *   the sheet size matches the optimizer's.  The emitter refuses a program
        whose sheet differs from its configured one (a mismatch means the
        coordinates were computed for another sheet), so the two settings are
        tied together here rather than left to callers;
    *   the 3/8 compression bit takes at most :data:`T11_MAX_BITE` of material
        per pass (:func:`generated_tools` — Scott, 2026-08-05, "reduce the load
        on it").  The limit is declared on the tool FIRST, so that the two
        ladders below and everything downstream read one number from the finished
        table rather than from a constant of their own;
    *   the perimeter runs that ladder, ending on the measured through pass —
        two 0.378 bites, Z0.372 then Z-0.006 (:func:`generated_post_passes`,
        which also drops the 2026-08-03 onion skin);
    *   the openings run the same ladder — two 0.3 bites, Z0.45 then Z0.15
        (:func:`generated_opening_passes`);
    *   the sheet is TAB-HELD and released by a final slow T12 pass
        (:class:`~.model.ReleaseSpec`).  This and the dropped onion skin are one
        decision: the skin's holding job went to the tabs, so the pass that goes
        and the section that arrives are turned off and on in the same line of
        code.  The measured table (:func:`~.model.default_config`) carries none
        of it, which is what lets the reference programs go on reconstructing and
        verifying exactly as before.

    ``base`` is where a caller's own post table joins in (``JobOptions``); every
    rule applies to it, since what it is being used for is cutting a generated
    sheet.
    """
    cfg = base or default_config()
    # The bite limit lands on the tools before either ladder is built: both read
    # it back off the config in hand (:func:`max_bite_for`), so there is one
    # source for the number and no way for a ladder to be built against a limit
    # the finished table does not declare.
    cfg = replace(cfg, tools=generated_tools(cfg))
    cfg = replace(
        cfg,
        openings_passes=generated_opening_passes(cfg),
        perimeter_passes=generated_post_passes(cfg),
        release=cfg.release if cfg.release is not None else ReleaseSpec(),
    )
    if nesting_config is None:
        return cfg
    return replace(
        cfg,
        sheet_width=float(nesting_config.sheet_width),
        sheet_length=float(nesting_config.sheet_height),
    )
