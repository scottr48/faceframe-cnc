"""Every decision the 3D view makes, decided without Qt.

Milestone 3 is a 3D window, and a 3D window is the last place a rule should
live.  So this module answers, in plain Python, everything
:mod:`~faceframe_cnc.gui.sim3d.scene` and
:mod:`~faceframe_cnc.gui.sim3d.window` need:

*   what the CURRENT TOOL field says (:func:`tool_display`) and what the rest
    of the readout strip says (:class:`Readouts`);
*   what material is GONE at a cursor, as typed geometry records
    (:func:`reveals`) — the reveal model the scene turns into entities;
*   where the machine's two subtle danger envelopes lie (:func:`overlays`),
    and where a :class:`~faceframe_cnc.sim.FindingSet`'s findings land on the
    sheet (:class:`DangerModel`) — the marks the scene draws in red;
*   how long a commanded move takes on screen at a speed multiplier
    (:func:`step_duration`) and where the bit tip is part way through one
    (:func:`point_at`);
*   what the cut list holds (:func:`cut_rows`), what the bit looks like
    (:func:`bit_profile`) and where the camera starts (:func:`camera_pose`).

No Qt import, and no clock: ``tests/test_sim3d.py`` walks this file's syntax
tree to prove both, for the same reason :mod:`faceframe_cnc.sim` is held to
it — a view whose decisions need a GL context cannot be tested, and one that
reads the wall clock cannot be replayed.  The QTimer in
:mod:`~faceframe_cnc.gui.sim3d.window` is the only clock in the feature, and
it does nothing but hand this module a number of seconds.

Geometry is taken, never re-derived
-----------------------------------
Groove and slot centrelines come from
:func:`~faceframe_cnc.post.generator.groove_segment` and
:func:`~faceframe_cnc.post.generator.wdc_slot_segment`, the same public
helpers the emitter cuts with, and opening/perimeter rectangles are built the
way :func:`~faceframe_cnc.post.generator.emit` builds them
(``box.grow(spec.offset)``).  A reveal is therefore what the machine removed
or it is a bug in the post, never a disagreement between two derivations.
Every number is read out of :class:`~faceframe_cnc.post.model.PostConfig`,
:class:`~faceframe_cnc.post.model.ToolSpec` and
:class:`~faceframe_cnc.post.model.WdcSlotSpec` at call time; the module-level
constants here are all VISUAL-ONLY and say so.

An overlay is not a verdict
---------------------------
The two envelope families :func:`overlays` returns are INFORMATIONAL
geometry: where the WDC cone's swept material really ends, and how far a
profile loop's ramps really reach, both measured with the emitter's own
helpers.  They are drawn when the operator asks for them and they are never
red.  The only thing in this whole feature that turns something red is a
:class:`~faceframe_cnc.post.verifier.Violation`, located by
:mod:`faceframe_cnc.sim.findings` — see :class:`DangerModel`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ...post.from_layout import is_wdc, part_depths, wdc_slot_sweep
from ...post.generator import (
    entry_side_for,
    groove_segment,
    loop_extent,
    wdc_slot_segment,
)
from ...post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_WDC_SLOT,
    Box,
    CutPlan,
    PostConfig,
    SheetProgram,
    ToolSpec,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...post.motion import Motion
    from ...sim import FindingSet, MaterialState, SimController, SimTimeline
    from ...sim.timeline import CutOccurrence

__all__ = [
    "tool_display",
    "section_display",
    "feed_text",
    "z_text",
    "cut_counter_text",
    "speed_text",
    "Readouts",
    "RevealKind",
    "Reveal",
    "reveal_key",
    "reveals",
    "OverlayKind",
    "Overlay",
    "deepest_slot_pass",
    "wdc_slot_positions",
    "wdc_cone_overlays",
    "lead_in_overlays",
    "sheet_fence",
    "sheet_fence_overlay",
    "overlays",
    "cut_reveal_key",
    "FlaggedCut",
    "FlaggedMotion",
    "flagged_cuts",
    "flagged_motions",
    "DangerModel",
    "banner_text",
    "finding_rows",
    "BANNER_VERDICT",
    "step_duration",
    "motion_duration",
    "point_at",
    "tip_at",
    "CutRow",
    "cut_rows",
    "current_row",
    "BitProfile",
    "bit_profile",
    "camera_pose",
    "SPEED_CHOICES",
    "DEFAULT_SPEED_INDEX",
    "DEFAULT_SPEED",
    "RAPID_DISPLAY_IPM",
]

# --------------------------------------------------------------------------
# Visual-only constants.  Nothing here is a machine fact; every machine fact
# is read from the post table at call time.
# --------------------------------------------------------------------------

#: VISUAL ONLY.  A ``G0`` has no commanded feed and no reference file states
#: the machine's rapid rate, so a rapid is animated at this many inches per
#: minute: fast enough to read as a traverse, slow enough that the operator
#: sees where the tool went.  Also used for any cutting move whose feed is
#: missing, which the emitter never produces.
RAPID_DISPLAY_IPM = 1200.0

#: VISUAL ONLY.  Playback multipliers the speed slider offers, one per tick.
#: The owner asked for a wide range over real time; 1.0 is real time.
SPEED_CHOICES = (0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 10.0, 15.0, 20.0)

#: VISUAL ONLY.  Default tick: a full sheet at real time is a twenty-minute
#: watch, so playback opens faster than the machine runs.
DEFAULT_SPEED_INDEX = 4

#: The multiplier at :data:`DEFAULT_SPEED_INDEX`.
DEFAULT_SPEED = SPEED_CHOICES[DEFAULT_SPEED_INDEX]

#: VISUAL ONLY.  Where the camera sits, as a direction from the middle of the
#: sheet (front-left and above, so X reads left-to-right and Y runs away from
#: the operator) and a distance in multiples of the sheet's long side.  The
#: factor is not a taste: it is the distance at which all four corners of the
#: sheet sit inside :data:`CAMERA_FOV_DEGREES` from that direction, which
#: ``tests/test_sim3d.py`` checks by projecting them.
CAMERA_DIRECTION = (-0.55, -0.80, 0.62)
CAMERA_DISTANCE_FACTOR = 1.35

#: VISUAL ONLY.  Vertical field of view the opening pose is computed against;
#: :func:`~faceframe_cnc.gui.sim3d.window.create_qt3d_viewport` sets the
#: camera lens with this same number, so the framing that is tested is the
#: framing that is shown.
CAMERA_FOV_DEGREES = 45.0

#: VISUAL ONLY.  The viewport shape the framing is promised at.  A perspective
#: lens is stated as a VERTICAL angle and widened by the aspect, so the whole
#: sheet fits across as long as the viewport is at least this wide for its
#: height — which is why the container has a landscape minimum size.
CAMERA_MIN_ASPECT = 4.0 / 3.0

#: The separator in the current-tool field.  The tool's own name follows it,
#: parsed from :attr:`~faceframe_cnc.post.model.ToolSpec.header_comment`.
TOOL_SEPARATOR = " — "

#: Shown when there is no tool in the spindle, which only an empty program
#: manages.
NO_TOOL_TEXT = "no tool"

#: The section header comment's shape, e.g.
#: ``(ROUTE TOOL #13: T13 - 3/8 PANEL CUTTER)``.  The name the operator knows
#: the bit by is group 2; nothing in this package spells a tool name out.
_TOOL_COMMENT_RE = re.compile(r"^\(\s*(?:ROUTE\s+)?TOOL\s*#(\d+)\s*:\s*(.+?)\s*\)$")

#: What each section is doing, in shop words.  Keyed by the section constants
#: themselves so a renamed section breaks the import rather than the display.
SECTION_WORDS = {
    SECTION_PANEL: "panel groove",
    SECTION_WDC_SLOT: "WDC stile slot",
    SECTION_OPENINGS: "openings",
    SECTION_DETAIL: "opening detail",
    SECTION_PERIMETER: "perimeter",
}


# --------------------------------------------------------------------------
# Readouts
# --------------------------------------------------------------------------


def tool_display(tool: ToolSpec | None) -> str:
    """The current-tool field's text: ``"T13 - 3/8 PANEL CUTTER"``, em dash.

    Derived from :attr:`~faceframe_cnc.post.model.ToolSpec.header_comment`,
    which is the line the machine operator reads in the ``.anc`` file itself
    (``(ROUTE TOOL #13: T13 - 3/8 PANEL CUTTER)``): the wrapper is stripped,
    the leading ``T13`` token and any punctuation after it dropped because
    the field states the number itself, and runs of the comment's verbatim
    double spaces collapsed.

    A comment that does not have that shape — or has nothing but the tool
    number in it — falls back to the number and the measured diameter, which
    are facts the :class:`~faceframe_cnc.post.model.ToolSpec` always carries.
    Nothing in this package contains a tool's name as a literal, so a table
    that swaps a bit re-labels the field with no code change.
    """
    if tool is None:
        return NO_TOOL_TEXT
    match = _TOOL_COMMENT_RE.match(tool.header_comment.strip())
    body = ""
    if match is not None:
        body = match.group(2).strip()
        prefix = f"T{tool.number}"
        if body.upper().startswith(prefix.upper()):
            body = body[len(prefix) :]
        body = " ".join(body.strip().lstrip("-").split())
    if not body:
        return f"T{tool.number}{TOOL_SEPARATOR}{tool.diameter:g} dia"
    return f"T{tool.number}{TOOL_SEPARATOR}{body}"


def section_display(section: str | None) -> str:
    """What the section being cut is, in words; the raw name if unmapped."""
    if section is None:
        return "-"
    return SECTION_WORDS.get(section, section)


def feed_text(feed: float | None) -> str:
    """``"545 in/min"``, or ``"rapid"`` for a ``G0``, which has no feed."""
    if feed is None:
        return "rapid"
    return f"{feed:g} in/min"


def z_text(z: float | None) -> str:
    """The tool's Z, or the fact that the program has not established one yet.

    Work Z is unknown before a section's ``G43``
    (:class:`~faceframe_cnc.post.motion.Motion`), and saying so is better than
    showing a number the program never commanded.
    """
    if z is None:
        return "Z not set"
    return f"Z {z:g}"


def cut_counter_text(cut_index: int, cut_total: int) -> str:
    """``"cut 34 of 512"``; at the end, that the program is finished.

    :attr:`~faceframe_cnc.sim.SimController.cut_index` is one past the last
    cut when the program is done, the same convention the step cursor uses.
    """
    if cut_total == 0:
        return "no cuts"
    if cut_index >= cut_total:
        return f"cut {cut_total} of {cut_total} - complete"
    return f"cut {cut_index + 1} of {cut_total}"


def speed_text(multiplier: float) -> str:
    """``"4x"`` / ``"0.25x"`` - the multiplier the operator is watching at."""
    return f"{multiplier:g}x"


@dataclass(frozen=True)
class Readouts:
    """Every string in the readout strip, taken off one cursor position.

    Built in one call so the strip can never show a feed from one cursor and
    a tool from another.
    """

    tool: str
    feed: str
    z: str
    section: str
    counter: str
    cut_label: str
    speed: str

    @classmethod
    def from_controller(
        cls, controller: "SimController", multiplier: float = DEFAULT_SPEED
    ) -> "Readouts":
        cut = controller.current_cut
        return cls(
            tool=tool_display(controller.tool),
            feed=feed_text(controller.feed),
            z=z_text(controller.position[2]),
            section=section_display(controller.section),
            counter=cut_counter_text(controller.cut_index, controller.cut_total),
            cut_label="program complete" if cut is None else cut.label,
            speed=speed_text(multiplier),
        )


# --------------------------------------------------------------------------
# The reveal model: what material is gone
# --------------------------------------------------------------------------


def reveal_key(
    kind: "RevealKind", part_index: int, feature_index: int, pass_index: int | None
) -> str:
    """The identity of one revealed feature, as a string.

    One function so that everything naming a reveal names it the same way:
    :attr:`Reveal.key` (what the scene caches an entity under) and
    :func:`flagged_cuts` (which of those entities a verifier finding
    condemns) cannot drift apart into two spellings of one feature.
    """
    return (
        f"{kind.value}:{part_index}:{feature_index}:"
        f"{'-' if pass_index is None else pass_index}"
    )


class RevealKind(StrEnum):
    """What a piece of removed material IS, which decides how it is drawn."""

    #: A T13 panel groove: centreline, tool width, 0.20 deep.
    GROOVE = "groove"
    #: One bite of a T17 stile slot: centreline, 45-degree flanks.
    SLOT = "slot"
    #: A T11-roughed opening: a pocket with finish stock still on its walls.
    OPENING = "opening"
    #: The T12 finishing pass on an opening: the rim, cut to the line.
    DETAIL = "detail"
    #: Perimeter pass 0: the part is scored to size and still held by the
    #: onion skin.
    SKIN = "skin"
    #: The last perimeter pass: the part is loose on the spoilboard.
    FREED = "freed"


@dataclass(frozen=True)
class Reveal:
    """One piece of material the program has removed, as CUT geometry.

    "Cut geometry" means what is gone, not where the tool centre went:

    *   :attr:`segment` kinds (:attr:`RevealKind.GROOVE`,
        :attr:`RevealKind.SLOT`) carry the centreline plus :attr:`width`, and
        :attr:`swept_box` is the footprint the cutter actually swept — the
        centreline grown by half a width in every direction, because a round
        cutter reaches its own radius past the end of its commanded line
        (which is exactly why a WDC slot pass's material ends ``2 * reach``
        past the part, :meth:`~faceframe_cnc.post.model.PostConfig.wdc_slot_reach`);
    *   :attr:`box` kinds carry a rectangle: for
        :attr:`RevealKind.OPENING`/:attr:`RevealKind.DETAIL` the HOLE that
        pass leaves (tool centre path plus tool radius, so the T11 rough is
        the finished opening less its finish stock and the T12 pass is the
        finished opening itself); for :attr:`RevealKind.SKIN` the perimeter
        kerf's centre path with :attr:`width` the kerf; for
        :attr:`RevealKind.FREED` the finished part outline, because at that
        point the part IS that outline and the view lifts it.

    :attr:`depth` is depth of cut below the top of the stock and
    :attr:`z_cut` the machine Z of the cut floor.  A dry-run table
    (:func:`~faceframe_cnc.post.job.dry_run_config`) cuts above the stock, so
    :attr:`depth` can be zero or negative; the scene clamps what it draws and
    this record keeps the truth.

    :attr:`key` identifies the reveal across cursor positions, so a scene can
    cache one entity per feature and re-show it when the operator scrubs
    forward again instead of rebuilding the tree.
    """

    kind: RevealKind
    part_index: int
    #: The part sits inside another frame's opening (spec 4b nesting).
    nested: bool
    #: The part has frames nested in its own openings.
    host: bool
    feature_index: int
    pass_index: int | None
    z_cut: float
    depth: float
    width: float
    segment: tuple[tuple[float, float], tuple[float, float]] | None = None
    box: Box | None = None

    @property
    def key(self) -> str:
        return reveal_key(
            self.kind, self.part_index, self.feature_index, self.pass_index
        )

    @property
    def axis(self) -> str:
        """``"x"`` or ``"y"``: which way a segment reveal runs.

        Both centreline helpers return axis-aligned segments, so this is the
        long axis and the width runs across the other one.
        """
        if self.segment is None:
            raise ValueError(f"{self.kind.value} reveals carry a box, not a segment")
        (x0, y0), (x1, y1) = self.segment
        return "x" if abs(x1 - x0) >= abs(y1 - y0) else "y"

    @property
    def swept_box(self) -> Box:
        """The XY footprint of the material a segment reveal removed."""
        if self.segment is None:
            raise ValueError(f"{self.kind.value} reveals carry a box, not a segment")
        (x0, y0), (x1, y1) = self.segment
        half = self.width / 2.0
        return Box(
            min(x0, x1) - half,
            min(y0, y1) - half,
            max(x0, x1) + half,
            max(y0, y1) + half,
        )


def reveals(
    state: "MaterialState", program: SheetProgram, config: PostConfig
) -> tuple[Reveal, ...]:
    """Every :class:`Reveal` visible at ``state``, in a deterministic order.

    A fold over the material state, part by part and within a part in a fixed
    kind order, so two calls with equal states return equal tuples — which is
    what lets the scene diff its entity set instead of rebuilding it.

    The state only ever holds FINISHED cuts
    (:class:`~faceframe_cnc.sim.MaterialState`), so a groove being cut is not
    in here: half a groove is not a fact this model has, and a view that
    guessed at one would be inventing material.
    """
    parts = program.flat_parts()
    depths = part_depths(program)
    out: list[Reveal] = []

    for index, part in enumerate(parts):
        entry = state[index]
        nested = depths[index] > 0
        host = bool(part.children)

        def make(kind: RevealKind, feature: int, **kwargs) -> Reveal:
            return Reveal(
                kind=kind,
                part_index=index,
                nested=nested,
                host=host,
                feature_index=feature,
                **kwargs,
            )

        for groove in sorted(entry.grooves_cut):
            panel = config.panel
            tool = config.tool(SECTION_PANEL)
            out.append(
                make(
                    RevealKind.GROOVE,
                    groove,
                    pass_index=None,
                    z_cut=panel.z_cut,
                    depth=config.stock_top_z - panel.z_cut,
                    width=tool.diameter,
                    segment=groove_segment(part, groove, panel),
                )
            )

        for stile, position in sorted(entry.slots_cut):
            spec = config.wdc_slot
            # 45-degree flanks: the cut's half width at the surface IS the
            # reach the post table computes for that pass, so the deeper bite
            # is the wider one and neither number is chosen here.
            reach = config.wdc_slot_reach(position)
            z_cut = spec.z_cuts[position]
            out.append(
                make(
                    RevealKind.SLOT,
                    stile,
                    pass_index=position,
                    z_cut=z_cut,
                    depth=config.stock_top_z - z_cut,
                    width=2.0 * reach,
                    segment=wdc_slot_segment(part, stile, spec, reach),
                )
            )

        rounds = (
            (RevealKind.OPENING, SECTION_OPENINGS, config.openings_pass, entry.openings_cut),
            (RevealKind.DETAIL, SECTION_DETAIL, config.detail_pass, entry.openings_detailed),
        )
        for kind, section, spec, done in rounds:
            tool = config.tool(section)
            for opening in sorted(done):
                # The emitter cuts opening.grow(spec.offset) as the TOOL
                # CENTRE path; the hole it leaves is that path plus the tool's
                # radius, which for T11 is the finished opening less its 0.010
                # of finish stock and for T12 the finished opening itself.
                out.append(
                    make(
                        kind,
                        opening,
                        pass_index=None,
                        z_cut=spec.z_cut,
                        depth=config.stock_top_z - spec.z_cut,
                        width=tool.diameter,
                        box=part.openings[opening].grow(spec.offset + tool.radius),
                    )
                )

        if entry.skinned:
            spec = config.perimeter_passes[0]
            tool = config.tool(SECTION_PERIMETER)
            out.append(
                make(
                    RevealKind.SKIN,
                    0,
                    pass_index=0,
                    z_cut=spec.z_cut,
                    depth=config.stock_top_z - spec.z_cut,
                    width=tool.diameter,
                    box=part.box.grow(spec.offset),
                )
            )

        if entry.freed:
            last = len(config.perimeter_passes) - 1
            spec = config.perimeter_passes[last]
            tool = config.tool(SECTION_PERIMETER)
            out.append(
                make(
                    RevealKind.FREED,
                    0,
                    pass_index=last,
                    z_cut=spec.z_cut,
                    depth=config.stock_top_z - spec.z_cut,
                    width=tool.diameter,
                    box=part.box,
                )
            )

    return tuple(out)


def freed_parts(items: tuple[Reveal, ...]) -> frozenset[int]:
    """Which parts are loose, off a reveal list."""
    return frozenset(r.part_index for r in items if r.kind is RevealKind.FREED)


# --------------------------------------------------------------------------
# The danger envelopes: informational geometry, never a verdict
# --------------------------------------------------------------------------


class OverlayKind(StrEnum):
    """The three envelopes this app's two subtle failure modes live in."""

    #: Where a WDC stile slot's DEEPEST pass really removes material: the
    #: cone's footprint, which ends ``2 * reach`` past each end of the stile.
    #: The thing a neighbouring frame gets carved by
    #: (:mod:`faceframe_cnc.post.from_layout`).
    CONE_REACH = "cone-reach"
    #: Everything one profile loop's motion touches, ramps and overshoot
    #: included — four inches longer than the part on the entry edge, which is
    #: how a short part's lead-in ends up over the fence.
    LEAD_IN = "lead-in"
    #: The legal fence: the sheet plus its trim overhang.  Not a cut — the
    #: boundary the envelopes above are judged against.
    FENCE = "fence"


@dataclass(frozen=True)
class Overlay:
    """One danger envelope, as an XY rectangle in sheet coordinates.

    :attr:`box` is the whole footprint the thing needs.  For
    :attr:`OverlayKind.CONE_REACH` that is the swept material of the deepest
    slot pass (:func:`~faceframe_cnc.post.from_layout.wdc_slot_sweep`); for
    :attr:`OverlayKind.LEAD_IN` it is
    :func:`~faceframe_cnc.post.generator.loop_extent` of the loop the emitter
    writes, on the entry side the emitter chooses; for
    :attr:`OverlayKind.FENCE` it is the sheet grown by
    :attr:`~faceframe_cnc.post.model.PostConfig.overhang`.

    :attr:`z_cut` is the machine Z the envelope belongs to and :attr:`depth`
    the depth of cut there, so a view can sink the overlay to where the
    material actually goes.  The fence has neither and reports the stock top
    with zero depth: it is a boundary, not a cut.

    :attr:`segment` carries the commanded centreline where there is one (the
    slot's), because the difference between where the tool centre goes and
    how far the material goes IS the failure mode being shown.
    """

    kind: OverlayKind
    box: Box
    z_cut: float
    depth: float
    part_index: int | None = None
    part_number: str = ""
    section: str | None = None
    feature_index: int = 0
    pass_index: int | None = None
    #: Which edge the loop leads in on, for :attr:`OverlayKind.LEAD_IN`.
    side: str | None = None
    segment: tuple[tuple[float, float], tuple[float, float]] | None = None

    @property
    def key(self) -> str:
        return (
            f"{self.kind.value}:{'-' if self.part_index is None else self.part_index}:"
            f"{self.section or '-'}:{self.feature_index}:"
            f"{'-' if self.pass_index is None else self.pass_index}"
        )


def deepest_slot_pass(config: PostConfig) -> int:
    """Which WDC slot pass cuts deepest, i.e. reaches furthest sideways.

    Read off :attr:`~faceframe_cnc.post.model.WdcSlotSpec.z_cuts` rather than
    assumed to be the last one: Z0 is the bottom of the stock, so the deepest
    pass is the one with the LOWEST machine Z, and a table that listed its
    passes in another order would still be drawn honestly.
    """
    z_cuts = config.wdc_slot.z_cuts
    return min(range(len(z_cuts)), key=lambda index: z_cuts[index])


def wdc_slot_positions(
    program: SheetProgram, plan: CutPlan | None = None
) -> tuple[tuple[int, int], ...]:
    """``(part index, stile)`` for every T17 slot on this sheet.

    Off the PLAN when there is one, because that is what the emitter cuts.
    A sheet the planner refused has no plan at all
    (:class:`~faceframe_cnc.post.from_layout.SheetPlanError`) and is exactly
    the sheet whose cone reach somebody needs to see, so without one this
    falls back to :func:`~faceframe_cnc.post.from_layout.is_wdc` over the
    parts — the same predicate
    :func:`~faceframe_cnc.post.from_layout.cut_plan_for` uses to decide which
    frames get slots, and both of a frame's stiles, as it always plans them.
    """
    if plan is not None:
        return tuple((ref.part, ref.index) for ref in plan.wdc_slot)
    return tuple(
        (index, stile)
        for index, part in enumerate(program.flat_parts())
        if is_wdc(part.part_number)
        for stile in (0, 1)
    )


def wdc_cone_overlays(
    program: SheetProgram, plan: CutPlan | None, config: PostConfig
) -> tuple[Overlay, ...]:
    """One cone-reach envelope per T17 slot on the sheet.

    The deep pass's own geometry:
    :func:`~faceframe_cnc.post.generator.wdc_slot_segment` run out by
    ``reach = config.wdc_slot_reach(deepest)`` — which is how far past the
    stile end the emitter puts the tool CENTRE — and then grown by that same
    ``reach`` in every direction, because a 45-degree cone breaks the surface
    its depth of cut either side of the centre.  The envelope therefore ends
    ``2 * reach`` past each end of the stile, which is the 0.875 the planner,
    the optimizer and the verifier all keep clear.
    """
    parts = program.flat_parts()
    spec = config.wdc_slot
    position = deepest_slot_pass(config)
    reach = config.wdc_slot_reach(position)
    z_cut = spec.z_cuts[position]
    out: list[Overlay] = []
    for part_index, stile in wdc_slot_positions(program, plan):
        part = parts[part_index]
        out.append(
            Overlay(
                kind=OverlayKind.CONE_REACH,
                box=wdc_slot_sweep(part, stile, position, config),
                z_cut=z_cut,
                depth=config.stock_top_z - z_cut,
                part_index=part_index,
                part_number=part.part_number,
                section=SECTION_WDC_SLOT,
                feature_index=stile,
                pass_index=position,
                segment=wdc_slot_segment(part, stile, spec, reach),
            )
        )
    return tuple(out)


def lead_in_overlays(
    program: SheetProgram, plan: CutPlan, config: PostConfig
) -> tuple[Overlay, ...]:
    """One motion envelope per closed loop the plan cuts, ramps included.

    Every loop in the program, in the plan's own order: the openings, the
    T12 detail pass over them, and each perimeter depth pass.  The cut
    rectangle is the one the emitter builds (``box.grow(spec.offset)``), the
    entry side is :func:`~faceframe_cnc.post.generator.entry_side_for` with
    the ref's own override — the same call with the same arguments the
    emitter made — and the extent is
    :func:`~faceframe_cnc.post.generator.loop_extent` of exactly the points
    it wrote.

    ``entry_side_for`` can refuse (no edge's ramp fits the sheet), but not
    here: this program has already been emitted with these arguments, so the
    answer is the one it got.
    """
    parts = program.flat_parts()
    out: list[Overlay] = []

    rounds: list[tuple[str, object, list, int | None]] = [
        (SECTION_OPENINGS, config.openings_pass, list(plan.openings), None),
        (SECTION_DETAIL, config.detail_pass, list(plan.detail_order()), None),
    ]
    for pass_index, refs in enumerate(plan.perimeter):
        rounds.append(
            (
                SECTION_PERIMETER,
                config.perimeter_passes[pass_index],
                list(refs),
                pass_index,
            )
        )

    for section, spec, refs, pass_index in rounds:
        tool = config.tool(section)
        perimeter = section == SECTION_PERIMETER
        kind = "perimeter" if perimeter else "opening"
        for ref in refs:
            part = parts[ref.part]
            base = part.box if perimeter else part.openings[ref.index]
            cut = base.grow(spec.offset)
            side = entry_side_for(
                cut, kind, tool, spec, config, override=ref.entry
            )
            out.append(
                Overlay(
                    kind=OverlayKind.LEAD_IN,
                    box=loop_extent(cut, side, tool, spec, config),
                    z_cut=spec.z_cut,
                    depth=config.stock_top_z - spec.z_cut,
                    part_index=ref.part,
                    part_number=part.part_number,
                    section=section,
                    feature_index=ref.index,
                    pass_index=pass_index,
                    side=side,
                )
            )
    return tuple(out)


def sheet_fence(config: PostConfig) -> Box:
    """The sheet plus its trim overhang: where a move is allowed to be.

    The verifier's ``bounds`` rule and
    :func:`~faceframe_cnc.post.generator.entry_side_for`'s envelope test are
    both this rectangle; drawing it is how a lead-in that leaves it becomes
    obvious rather than arithmetic.
    """
    return Box(
        -config.overhang,
        -config.overhang,
        config.sheet_width + config.overhang,
        config.sheet_length + config.overhang,
    )


def sheet_fence_overlay(config: PostConfig) -> Overlay:
    """:func:`sheet_fence` as an :class:`Overlay` record."""
    return Overlay(
        kind=OverlayKind.FENCE,
        box=sheet_fence(config),
        z_cut=config.stock_top_z,
        depth=0.0,
        section=None,
    )


def overlays(
    program: SheetProgram, plan: CutPlan, config: PostConfig
) -> tuple[Overlay, ...]:
    """Every danger envelope on this sheet, in a deterministic order.

    Cone reaches first, then the loop envelopes in plan order, then the
    fence.  All of it is available whether or not anything is wrong: an
    operator investigating a refused nest wants to SEE the reach that took
    the room away.
    """
    return (
        wdc_cone_overlays(program, plan, config)
        + lead_in_overlays(program, plan, config)
        + (sheet_fence_overlay(config),)
    )


# --------------------------------------------------------------------------
# Findings made drawable
# --------------------------------------------------------------------------


def cut_reveal_key(cut: "CutOccurrence", config: PostConfig) -> str | None:
    """Which revealed feature a cut occurrence leaves behind, by key.

    ``None`` where the occurrence leaves no feature entity of its own: the
    final perimeter pass reveals the PART (it comes loose,
    :attr:`RevealKind.FREED`), not a channel or a pocket, so a finding on it
    is drawn on the part rather than on a feature.  Any middle perimeter pass
    a future table added would be in the same position, which is why this
    tests the pass index instead of listing kinds.
    """
    section = cut.section
    if section == SECTION_PANEL:
        return reveal_key(RevealKind.GROOVE, cut.part_index, cut.feature.index, None)
    if section == SECTION_WDC_SLOT:
        return reveal_key(
            RevealKind.SLOT, cut.part_index, cut.feature.index, cut.pass_index
        )
    if section == SECTION_OPENINGS:
        return reveal_key(RevealKind.OPENING, cut.part_index, cut.feature.index, None)
    if section == SECTION_DETAIL:
        return reveal_key(RevealKind.DETAIL, cut.part_index, cut.feature.index, None)
    if section == SECTION_PERIMETER and cut.pass_index == 0:
        return reveal_key(RevealKind.SKIN, cut.part_index, 0, 0)
    return None


@dataclass(frozen=True)
class FlaggedCut:
    """One cut occurrence the verifier condemned, and what to redden for it.

    :attr:`reveal_key` is the feature entity to tint, or ``None`` when this
    cut leaves none (see :func:`cut_reveal_key`).  :attr:`codes` are the
    verifier's own codes, in the order it reported them, so a tooltip can say
    WHY without this module wording anything.
    """

    cut_index: int
    part_index: int
    section: str
    label: str
    reveal_key: str | None
    codes: tuple[str, ...]


@dataclass(frozen=True)
class FlaggedMotion:
    """One commanded move a finding names, as the path to draw a mark along.

    :attr:`z` is the deepest Z the move is commanded to, which is where the
    damage is; an unknown Z (only around a section's first spindle-on rapid)
    is displayed at the rapid plane, the same display choice
    :func:`point_at` makes.
    """

    step_index: int
    cut_index: int | None
    part_index: int | None
    segment: tuple[tuple[float, float], tuple[float, float]]
    z: float
    codes: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"mark:{self.step_index}"


def flagged_cuts(
    timeline: "SimTimeline", findings: "FindingSet"
) -> tuple[FlaggedCut, ...]:
    """Every condemned cut occurrence, in program order.

    Straight off :attr:`~faceframe_cnc.sim.FindingSet.flagged_cuts`: this
    adds no cut and drops none.
    """
    out: list[FlaggedCut] = []
    for index in sorted(findings.flagged_cuts):
        cut = timeline.cuts[index]
        out.append(
            FlaggedCut(
                cut_index=index,
                part_index=cut.part_index,
                section=cut.section,
                label=cut.label,
                reveal_key=cut_reveal_key(cut, timeline.config),
                codes=tuple(f.code for f in findings.for_cut(index)),
            )
        )
    return tuple(out)


def flagged_motions(
    timeline: "SimTimeline", findings: "FindingSet"
) -> tuple[FlaggedMotion, ...]:
    """Every condemned MOVE, in program order: one mark each.

    Several findings can land on one line (the verifier reports one per rule
    it broke), and one line is one move, so the marks are keyed by step and
    carry all of that step's codes.
    """
    out: list[FlaggedMotion] = []
    for step_index in sorted(findings.flagged_steps):
        motion = timeline.steps[step_index]
        zs = [z for z in (motion.from_z, motion.to_z) if z is not None]
        out.append(
            FlaggedMotion(
                step_index=step_index,
                cut_index=timeline.cut_of_step[step_index],
                part_index=timeline.cut_at_step(step_index).part_index,
                segment=(
                    (motion.from_x, motion.from_y),
                    (motion.to_x, motion.to_y),
                ),
                z=min(zs) if zs else timeline.config.rapid_z,
                codes=tuple(f.code for f in findings.for_step(step_index)),
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class DangerModel:
    """Everything the scene draws beyond the material, decided without Qt.

    Two independent halves, and the difference between them is the whole
    point:

    :attr:`overlays`
        the informational envelopes (:func:`overlays`).  Always built, always
        neutral, shown only when a toggle asks for them.
    :attr:`flagged` / :attr:`marks` / :attr:`flagged_steps`
        the verifier's findings, located.  EMPTY unless a
        :class:`~faceframe_cnc.sim.FindingSet` was handed in, and every entry
        traceable to one :class:`~faceframe_cnc.post.verifier.Violation`.

    So a scene given no findings — or an empty set — has nothing red in it at
    all, which is what lets a clean sheet look clean.
    """

    overlays: tuple[Overlay, ...] = ()
    flagged: tuple[FlaggedCut, ...] = ()
    marks: tuple[FlaggedMotion, ...] = ()
    flagged_steps: frozenset[int] = frozenset()
    flagged_parts: frozenset[int] = frozenset()

    @classmethod
    def build(
        cls, timeline: "SimTimeline", findings: "FindingSet | None" = None
    ) -> "DangerModel":
        """The envelopes of ``timeline``'s sheet, plus ``findings`` if given."""
        envelopes = overlays(timeline.program, timeline.plan, timeline.config)
        if findings is None:
            return cls(overlays=envelopes)
        return cls(
            overlays=envelopes,
            flagged=flagged_cuts(timeline, findings),
            marks=flagged_motions(timeline, findings),
            flagged_steps=findings.flagged_steps,
            flagged_parts=findings.flagged_parts,
        )

    @classmethod
    def for_sheet(cls, program: SheetProgram, config: PostConfig) -> "DangerModel":
        """The envelopes a sheet with NO PLAN can still show, and no findings.

        A refused sheet never became a program, so there is nothing to verify
        and no loop to measure a lead-in against — but the two things a
        refusal is usually about survive: where each WDC cone's material
        really ends, and where the fence is.  That is the whole reason the
        refusal view is a 3D view and not a message box.
        """
        return cls(
            overlays=wdc_cone_overlays(program, None, config)
            + (sheet_fence_overlay(config),)
        )

    @classmethod
    def empty(cls) -> "DangerModel":
        """No envelopes and no findings at all."""
        return cls()

    def of_kind(self, kind: OverlayKind) -> tuple[Overlay, ...]:
        return tuple(item for item in self.overlays if item.kind is kind)

    def is_flagged_step(self, step_index: int | None) -> bool:
        """Whether the move at ``step_index`` is one a finding names.

        ``None`` (the end of the program, where no move is in progress) is
        never flagged: there is no move to be wrong.
        """
        return step_index is not None and step_index in self.flagged_steps


#: The verdict half of the banner.  A verifier finding is not a warning to
#: click through: the file is not fit to run until it is gone.
BANNER_VERDICT = "the machine must not run this sheet"


def banner_text(findings: "FindingSet | None") -> str:
    """The banner across the top of the window; ``""`` for a clean program.

    The count is what the operator needs first (one finding is a mistake, a
    dozen is a sheet that needs re-nesting), and the verdict is the same
    refusal the Generate button gives: this is not advice.
    """
    count = 0 if findings is None else findings.count
    if count == 0:
        return ""
    word = "finding" if count == 1 else "findings"
    return f"{count} verifier {word} on this program - {BANNER_VERDICT}."


def finding_rows(findings: "FindingSet | None") -> tuple[str, ...]:
    """The findings panel's rows: each finding's own text, verbatim.

    :attr:`~faceframe_cnc.sim.Finding.display` is the verifier's ``__str__``
    and nothing is done to it here — not truncated, not re-ordered, not
    re-worded.  The panel is the authority speaking.
    """
    if findings is None:
        return ()
    return tuple(finding.display for finding in findings.all)


# --------------------------------------------------------------------------
# Animation maths
# --------------------------------------------------------------------------


def step_duration(
    path_length: float, feed: float | None, multiplier: float = 1.0
) -> float:
    """Seconds one commanded move takes on screen.

    At 1x a cutting move takes exactly as long as the machine takes:
    ``path_length / (feed / 60)``, the feed being inches per MINUTE.  A move
    with no commanded feed (a ``G0``; F is not a word on a rapid) is animated
    at :data:`RAPID_DISPLAY_IPM`, which is a display choice and not a machine
    number.  The multiplier divides the result, so 4x is four times faster.

    ``multiplier`` must be positive: a zero or negative playback rate is not
    a slower simulation, it is a stopped or reversed one, and the transport
    controls own those.
    """
    if multiplier <= 0:
        raise ValueError(
            f"a playback multiplier must be positive, not {multiplier!r} - "
            f"pausing and stepping backwards are transport gestures, not speeds"
        )
    rate = feed if feed is not None and feed > 0 else RAPID_DISPLAY_IPM
    return (path_length / (rate / 60.0)) / multiplier


def motion_duration(
    motion: "Motion", path_length: float, multiplier: float = 1.0
) -> float:
    """:func:`step_duration` for a motion and its
    :attr:`~faceframe_cnc.sim.SimTimeline.path_lengths` entry."""
    return step_duration(path_length, motion.feed, multiplier)


def point_at(
    motion: "Motion", t: float, rapid_z: float
) -> tuple[float, float, float]:
    """Tool centre a fraction ``t`` along ``motion``, clamped to its ends.

    ``t=0`` is the move's start and ``t=1`` its end, EXACTLY: both ends are
    returned as the numbers on the line rather than as
    ``from + (to - from) * t``, which for ``t=1`` is off by a float's last
    bit.  The tool has to finish a move where the program sent it — that is
    the whole claim a cut simulation makes — and a view that interpolated past
    either end would put it somewhere the program never sent it at all, so ``t``
    is clamped.

    DISPLAY CHOICE: an unknown Z (``None``, which happens only around a
    section's first spindle-on rapid, before the ``G43`` establishes work Z)
    is drawn at ``rapid_z``.  The machine really is at a clearance height
    there — it has just homed Z — but the number is a machine position this
    post never states, so this is the view choosing where to put the bit and
    not the program saying.
    """
    z0 = rapid_z if motion.from_z is None else motion.from_z
    z1 = rapid_z if motion.to_z is None else motion.to_z
    if t <= 0.0:
        return (motion.from_x, motion.from_y, z0)
    if t >= 1.0:
        return (motion.to_x, motion.to_y, z1)
    fraction = float(t)
    return (
        motion.from_x + (motion.to_x - motion.from_x) * fraction,
        motion.from_y + (motion.to_y - motion.from_y) * fraction,
        z0 + (z1 - z0) * fraction,
    )


def tip_at(
    controller: "SimController", fraction: float, config: PostConfig
) -> tuple[float, float, float]:
    """Where to draw the bit tip: part way through the move in progress.

    At the end of the program there is no move in progress, so the tip stays
    where the last one left it (with the same unknown-Z display choice as
    :func:`point_at`).
    """
    motion = controller.current_motion
    if motion is None:
        x, y, z = controller.position
        return (x, y, config.rapid_z if z is None else z)
    return point_at(motion, fraction, config.rapid_z)


# --------------------------------------------------------------------------
# The cut list
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CutRow:
    """One row of the cut list: an occurrence, named the way the plan named it."""

    index: int
    label: str
    section: str
    part_index: int


def cut_rows(timeline: "SimTimeline") -> tuple[CutRow, ...]:
    """The cut list: one row per cut occurrence, in program order.

    Straight off :attr:`~faceframe_cnc.sim.SimTimeline.cuts`; the label is the
    one :func:`~faceframe_cnc.sim.cut_label` built from the plan, the program
    and the post table, so the list and the ``.anc`` file name the same cut
    the same way.
    """
    return tuple(
        CutRow(
            index=cut.index,
            label=cut.label,
            section=cut.section,
            part_index=cut.part_index,
        )
        for cut in timeline.cuts
    )


def current_row(controller: "SimController") -> int:
    """Which cut-list row to highlight, ``-1`` for a program with no cuts.

    The cursor reads one PAST the last cut when the program is finished, and
    the list has no such row, so the last cut stays highlighted — the
    operator's eye should end on the cut that just finished.
    """
    total = controller.cut_total
    if total == 0:
        return -1
    return min(controller.cut_index, total - 1)


# --------------------------------------------------------------------------
# The bit, and where the camera starts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BitProfile:
    """The shape of the bit in the spindle, for the scene to build a mesh of.

    ``shape`` is ``"cone"`` for a V bit and ``"cylinder"`` for everything
    else.  ``radius`` is always the tool's own; ``length`` is the flute the
    view draws — for a cone it is geometry (a 45-degree flank climbs one inch
    per inch, so the cone from tip to shoulder is exactly as long as it is
    wide at the shoulder), for a cylinder it is a visual multiple of the
    diameter, which makes the three round bits distinguishable at a glance
    without inventing a tool length no reference file states.
    """

    shape: str
    radius: float
    length: float
    tool_number: int


#: VISUAL ONLY.  A cylindrical bit is drawn this many diameters long.  The
#: three round tools have three different diameters, so this alone makes the
#: T13/T11/T12 bits three different sizes on screen.
BIT_LENGTH_RATIO = 2.5


def bit_profile(tool: ToolSpec, config: PostConfig) -> BitProfile:
    """What the bit in the spindle looks like.

    The V bit is identified by the post table (it is the tool the WDC slot
    section is cut with), never by its number or its name, and its cone is
    sized from :attr:`~faceframe_cnc.post.model.WdcSlotSpec.flank_slope` —
    the same rule that decides how wide each slot pass cuts.
    """
    v_tool = config.tools.get(SECTION_WDC_SLOT)
    if v_tool is not None and v_tool.number == tool.number:
        slope = config.wdc_slot.flank_slope
        length = tool.radius / slope if slope > 0 else tool.radius
        return BitProfile("cone", tool.radius, length, tool.number)
    return BitProfile(
        "cylinder", tool.radius, tool.diameter * BIT_LENGTH_RATIO, tool.number
    )


def camera_pose(
    config: PostConfig,
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """``(eye, centre, up)`` for a view of the whole sheet, in inches.

    Isometric-ish from front-left above, aimed at the middle of the stock's
    top face, with +Z up — scene units are inches and the sheet's own
    coordinate system is the scene's (:mod:`faceframe_cnc.post.model`).  The
    distance frames the WHOLE sheet at :data:`CAMERA_FOV_DEGREES`, corners
    included, and the "reset view" action is this function called again.
    """
    centre = (config.sheet_width / 2.0, config.sheet_length / 2.0, config.stock_top_z)
    span = max(config.sheet_width, config.sheet_length)
    distance = span * CAMERA_DISTANCE_FACTOR
    dx, dy, dz = CAMERA_DIRECTION
    scale = (dx * dx + dy * dy + dz * dz) ** 0.5
    eye = (
        centre[0] + dx / scale * distance,
        centre[1] + dy / scale * distance,
        centre[2] + dz / scale * distance,
    )
    return eye, centre, (0.0, 0.0, 1.0)
