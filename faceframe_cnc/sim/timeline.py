"""The emitted program as a timeline of steps and cut occurrences.

Milestone 1 gave the post a typed motion stream
(:mod:`faceframe_cnc.post.motion`); this module is the reading of that stream
an operator recognises.  Two units, and the difference between them matters:

``steps``
    one commanded move each, in program order — what a 3D view animates and
    what a verifier finding names, through
    :attr:`~faceframe_cnc.post.motion.Motion.line_index`.

``cuts``
    one CUT OCCURRENCE each: a contiguous run of steps serving the same
    (section, feature, depth pass).  This is the unit the operator steps by
    ("cut 34 of 512") and the unit a cut list enumerates.

An occurrence is not a plan entry.  The plan names a perimeter once, and an
opening once, and the post cuts each of them once per configured depth pass, so
one :class:`~faceframe_cnc.post.model.FeatureRef` can yield more than one
occurrence; one WDC slot entry always yields two, the shallow bite and the
deep one on the same centreline.  All three counts are properties of the post
table (:attr:`~faceframe_cnc.post.model.PostConfig.perimeter_passes`,
:attr:`~faceframe_cnc.post.model.PostConfig.openings_passes`,
:attr:`~faceframe_cnc.post.model.WdcSlotSpec.z_cuts`), which is why the
occurrence carries the pass index the emitter tagged its motions with rather
than inventing its own numbering — and why nothing here counts passes for
itself.

How many passes there are depends on which sheet is being watched.  A reference
program (R710101N and its siblings) was cut with the measured table — one T11
opening pass at Z0.15, then perimeter pass 0 the Z0.06 onion skin and pass 1
through at Z-0.006 — and simulates as one opening occurrence and two perimeter
occurrences per part.  A GENERATED sheet is cut with the 2026-08-05 max-bite
ladders instead (Scott: at most 0.4 of material per T11 pass, to reduce the load
on the 3/8 comp), so it simulates as TWO opening occurrences per opening (Z0.45
then Z0.15) and two perimeter occurrences per part (Z0.372 then through at
Z-0.006) — the onion skin having gone the same day, its holding job handed to the
tabs.  The labels say which rung and what it leaves
(:func:`pass_phase`), so the two two-pass dialects never read alike.

Contiguity is the whole grouping rule
-------------------------------------
The emitted grammar is preposition rapids -> plunge -> feed moves -> retract
per feature (:mod:`faceframe_cnc.post.generator`), so a feature's motions are
already adjacent, and the run ends where the next feature's prepositioning
begins.  Nothing is regrouped by geometry.  Two consequences worth stating:

*   the bare ``M59`` marker after the first perimeter loop carries no motion
    and its trailing ``G0 Z2.5`` is tagged with the loop it follows
    (R710101N 230-232), so that zero-length retract belongs to the same
    occurrence as the loop — the run is not broken by a line that moves
    nothing;
*   a plan that listed the identical feature reference twice in a row would
    produce ONE occurrence.  No reference file and no planner order does
    that, and a genuine repeat is a plan bug, not a display case;
*   one profile's whole RELEASE is one occurrence, however many tabs it has
    (2026-08-05 amendment §3c).  Its several plunge-and-cut blocks are one
    contiguous run of one feature at one depth, so the general rule already says
    so — and it is the grouping the freed-at-release semantics needs, because an
    occurrence takes effect when its LAST step has executed and the piece really
    does drop when the last of its tabs goes, not when the first one does.

Geometry, never time
--------------------
:attr:`SimTimeline.xy_lengths` and :attr:`SimTimeline.path_lengths` are how
far each step travels.  A view that wants to animate at feed rate divides
one by the other's feed itself; nothing here reads a clock, and nothing here
knows what a second is.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field, replace
from math import hypot

from ..post.from_layout import part_depths
from ..post.generator import emit
from ..post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_RELEASE,
    SECTION_WDC_SLOT,
    CutPlan,
    FeatureRef,
    PartProgram,
    PostConfig,
    SheetProgram,
    default_config,
)
from ..post.motion import EmittedProgram, Motion
from .state import MaterialState

__all__ = [
    "CutOccurrence",
    "SectionSpan",
    "SimTimeline",
    "cut_label",
    "GROOVE_NAMES",
    "SLOT_NAMES",
]

#: Z comparison tolerance, the emitter's own (:data:`~.post.motion.Z_EPS`).
EPS = 1e-9

#: What :attr:`~faceframe_cnc.post.model.FeatureRef.index` names for a T13
#: groove, verbatim from :func:`~faceframe_cnc.post.generator.groove_segment`:
#: "0..3 = stile-low, rail-low, stile-high, rail-high in the part's own
#: orientation".  A WDC frame cuts only the rail pair (1 and 3) because its
#: stiles take the T17 slot instead, so the "of 4" in a groove label counts
#: the pattern's positions, not the part's own grooves.
GROOVE_NAMES = (
    "stile, low side",
    "rail, low side",
    "stile, high side",
    "rail, high side",
)

#: What :attr:`~faceframe_cnc.post.model.FeatureRef.index` names for a T17
#: slot: "0 or 1 = the low-side then high-side stile in sheet coordinates"
#: (:class:`~faceframe_cnc.post.model.FeatureRef`).
SLOT_NAMES = ("low-side stile", "high-side stile")


def _ordinal(index: int, count: int) -> str:
    """``"3 of 4"``, or bare ``"1"`` when there is only one of the thing."""
    if count <= 1:
        return f"{index + 1}"
    return f"{index + 1} of {count}"


def _name_of(names: tuple[str, ...], index: int) -> str:
    """``names[index]``, or the raw index for a feature the plan invented."""
    if 0 <= index < len(names):
        return names[index]
    return f"index {index}"


def pass_phase(z_cut: float, config: PostConfig, section: str | None = None) -> str:
    """What a depth pass at ``z_cut`` leaves behind, as words.

    Z0 is the top of the spoilboard and the BOTTOM of the stock
    (:mod:`faceframe_cnc.post.model`), so the material a pass leaves under
    the cut is ``z_cut`` itself.  Four answers, all read off the table in hand
    rather than off the pass's position in it:

    *   at or below the bottom of the stock: ``"through"`` — the outline is cut
        right through (perimeter pass 2 at Z-0.006 scratches 0.006 into the
        spoilboard);
    *   above the top of the stock: ``"above the stock"`` — the dry-run
        table's air cut (:func:`~faceframe_cnc.post.job.dry_run_config`)
        touches nothing;
    *   in between, on a table whose tool for ``section`` declares a bite limit
        (:attr:`~faceframe_cnc.post.model.ToolSpec.max_bite`, the 2026-08-05
        ratified 0.4 for the 3/8 comp): a rung of the max-bite ladder, said as
        the BITE IT TAKES rather than its depth below the surface —
        ``"0.378 deep, 0.372 left"``.  The bite is the number the rule is about
        (it is the load on the bit), so it is the number shown: the second rung
        of the openings ladder sits 0.6 below the surface and takes 0.3.  That
        is a depth-of-cut pass, not a holding rib, and calling a 0.372 wall an
        onion skin would tell the operator the part is about to come loose;
    *   in between with no bite limit declared: the measured onion skin, with its
        thickness, which for the reference programs' perimeter pass 1 at Z0.06 is
        the 0.06 that holds the part to the sheet.

    ``section`` is which post-table section the pass belongs to, i.e. whose tool
    is being asked about its bite limit.  ``None`` means "no tool to ask", which
    reads as the measured wording.
    """
    bottom = config.stock_top_z - config.material_thickness
    if z_cut >= config.stock_top_z - EPS:
        return "above the stock"
    if z_cut <= bottom + EPS:
        return "through"
    tool = None if section is None else config.tools.get(section)
    if tool is not None and tool.max_bite is not None:
        bite = _bite_at(z_cut, _ladder_of(section, config), config.stock_top_z)
        return f"{bite:g} deep, {z_cut - bottom:g} left"
    return f"onion skin {z_cut - bottom:g} thick"


def _ladder_of(section: str, config: PostConfig) -> tuple:
    """The configured depth passes of ``section``, shallowest first."""
    if section == SECTION_PERIMETER:
        return config.perimeter_passes
    if section == SECTION_OPENINGS:
        return config.openings_passes
    return ()


def _bite_at(z_cut: float, passes, stock_top_z: float) -> float:
    """How much material the pass at ``z_cut`` takes: its bite, not its depth.

    Walks the ladder from the stock surface down, which is the same walk the
    verifier's ``max-bite`` rule makes (:func:`~faceframe_cnc.post.verifier`) —
    written out again here rather than shared, because this is a label and that
    is an authority.  A ``z_cut`` the ladder does not contain falls back to the
    depth below the surface, which is the answer for a single-pass table.
    """
    floor = stock_top_z
    for spec in passes:
        if abs(spec.z_cut - z_cut) <= EPS:
            return floor - z_cut
        floor = min(floor, spec.z_cut)
    return stock_top_z - z_cut


def cut_label(
    section: str,
    ref: FeatureRef,
    pass_index: int | None,
    part: PartProgram,
    nested: bool,
    config: PostConfig,
) -> str:
    """One line of operator-facing text for a cut occurrence.

    Every word comes from the plan, the program or the post table: the tool
    number is :meth:`~faceframe_cnc.post.model.PostConfig.tool`'s, the part
    number is the part's own, the pass count is the configured one and the
    onion-skin/through wording is :func:`pass_phase` reading the configured
    Z.  Nothing about a label is a constant that could drift away from what
    the machine is being told to do.

    ``(nested)`` marks a frame that sits inside another frame's opening
    (spec 4b): the operator watching that cut is watching a part being
    routed inside a slab that is still captive, which is the one thing about
    a nested sheet that surprises people.
    """
    tool = f"T{config.tool(section).number}"
    name = f"{part.part_number} (nested)" if nested else part.part_number

    if section == SECTION_PANEL:
        return (
            f"{tool} groove {_ordinal(ref.index, len(GROOVE_NAMES))} "
            f"({_name_of(GROOVE_NAMES, ref.index)}) — {name}"
        )
    if section == SECTION_WDC_SLOT:
        passes = len(config.wdc_slot.z_cuts)
        position = 0 if pass_index is None else pass_index
        return (
            f"{tool} stile slot, {_name_of(SLOT_NAMES, ref.index)}, "
            f"pass {_ordinal(position, passes)} — {name}"
        )
    if section == SECTION_OPENINGS:
        # One rung of the T11 roughing ladder (2026-08-05 max-bite amendment):
        # the pass wording appears only when the table configures more than one,
        # so a reference program's cut list reads exactly as it did.
        passes = config.openings_passes
        rung = ""
        if len(passes) > 1:
            position = 0 if pass_index is None else pass_index
            rung = (
                f" pass {_ordinal(position, len(passes))} "
                f"({pass_phase(passes[position].z_cut, config, SECTION_OPENINGS)})"
            )
        return (
            f"{tool} opening {_ordinal(ref.index, len(part.openings))}{rung} — {name}"
        )
    if section == SECTION_DETAIL:
        return (
            f"{tool} opening {_ordinal(ref.index, len(part.openings))} detail "
            f"— {name}"
        )
    if section == SECTION_PERIMETER:
        passes = config.perimeter_passes
        position = 0 if pass_index is None else pass_index
        phase = pass_phase(passes[position].z_cut, config, SECTION_PERIMETER)
        return (
            f"{tool} perimeter pass {_ordinal(position, len(passes))} "
            f"({phase}) — {name}"
        )
    if section == SECTION_RELEASE:
        # 2026-08-05 amendment §3c.  What this cut frees is the fact the
        # operator wants — the frame, or one of its dropouts — and the feed is
        # in the label because "slow" is the whole character of the pass and it
        # is the one place the ratified number shows on screen.
        what = (
            "part"
            if ref.kind == "perimeter"
            else f"opening {_ordinal(ref.index, len(part.openings))}"
        )
        feed = config.release.cut_feed if config.release is not None else None
        rate = "" if feed is None else f", {feed:g} ipm"
        return f"{tool} release — frees the {what}{rate} — {name}"
    return f"{tool} {ref.kind} {ref.index + 1} — {name}"


@dataclass(frozen=True)
class CutOccurrence:
    """One contiguous run of steps that cuts one feature at one depth.

    ``first_step``/``last_step`` are inclusive step indices; :attr:`start`
    and :attr:`end` are the cursor positions either side of the run, which is
    what the controller seeks to (its cursor sits BETWEEN steps).  The
    occurrence is finished — and its material effect applied — only once
    ``last_step`` has executed, i.e. at :attr:`end`.
    """

    index: int
    section: str
    feature: FeatureRef
    pass_index: int | None
    part_index: int
    part_number: str
    #: The part sits inside another frame's opening (spec 4b nesting).
    nested: bool
    first_step: int
    last_step: int
    label: str

    @property
    def start(self) -> int:
        """Cursor position at which this occurrence's first step is next."""
        return self.first_step

    @property
    def end(self) -> int:
        """Cursor position at which this occurrence is complete."""
        return self.last_step + 1

    @property
    def step_count(self) -> int:
        return self.last_step - self.first_step + 1

    def contains_step(self, step_index: int) -> bool:
        return self.first_step <= step_index <= self.last_step


@dataclass(frozen=True)
class SectionSpan:
    """One tool's whole section: its step range and its cut range.

    Sections are contiguous in the stream (one tool change each), so the
    spans tile it in :attr:`~faceframe_cnc.post.model.CutPlan.sections`
    order with the empty sections dropped — exactly the sections
    :func:`~faceframe_cnc.post.generator.emit` writes a header for.
    """

    index: int
    section: str
    first_step: int
    last_step: int
    first_cut: int
    last_cut: int

    @property
    def start(self) -> int:
        return self.first_step

    @property
    def end(self) -> int:
        return self.last_step + 1

    @property
    def step_count(self) -> int:
        return self.last_step - self.first_step + 1


@dataclass(frozen=True)
class SimTimeline:
    """Everything about one emitted program that a playback cursor needs.

    Built by :meth:`build` from the same ``(program, plan, config)`` triple
    the post takes, through :func:`~faceframe_cnc.post.generator.emit`, so a
    timeline and the ``.anc`` file the operator loaded into the machine are
    two views of one walk of the plan.  Nothing here re-derives a coordinate.
    """

    program: SheetProgram
    plan: CutPlan
    config: PostConfig
    emitted: EmittedProgram
    #: Every commanded move, in program order.  The playback unit.
    steps: tuple[Motion, ...]
    #: Every cut occurrence, in program order.  The operator's unit.
    cuts: tuple[CutOccurrence, ...]
    sections: tuple[SectionSpan, ...]
    #: Which occurrence each step belongs to.  Total: every step is part of
    #: exactly one cut (see :meth:`build`).
    cut_of_step: tuple[int, ...]
    #: Which :attr:`sections` entry each step belongs to.
    section_of_step: tuple[int, ...]
    #: Straight-line XY distance of each step, inches.
    xy_lengths: tuple[float, ...]
    #: Straight-line XYZ distance of each step, inches (dZ 0 where Z is
    #: unknown, which is only a section's first spindle-on rapid).
    path_lengths: tuple[float, ...]
    part_count: int
    _line_to_step: dict[int, int] = field(compare=False, repr=False)
    _cut_ends: tuple[int, ...] = field(compare=False, repr=False)

    # -- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        program: SheetProgram,
        plan: CutPlan,
        config: PostConfig | None = None,
    ) -> "SimTimeline":
        """Emit ``program`` under ``plan`` and index the result for playback.

        Raises ``ValueError`` when the stream holds a move the emitter did not
        tag with a feature: an untagged move would sit in no cut occurrence,
        and cut-by-cut stepping over a stream with a hole in it would skip
        material silently.  Every reference file and every planner order
        tags every move (``tests/test_motion.py`` pins it), so this is a
        guard on future emitter work, not a case to handle.
        """
        cfg = config if config is not None else default_config()
        emitted = emit(program, plan, cfg)
        steps = emitted.motions
        parts = program.flat_parts()
        depths = part_depths(program)

        for motion in steps:
            if motion.feature is None:
                raise ValueError(
                    f"the move on line {motion.line_index + 1} is not tagged with a "
                    f"feature, so it belongs to no cut - a simulation cannot step "
                    f"cut by cut over a stream with an untagged move in it"
                )

        cuts: list[CutOccurrence] = []
        cut_of_step: list[int] = []
        for index, motion in enumerate(steps):
            previous = steps[index - 1] if index else None
            same = previous is not None and (
                previous.section,
                previous.feature,
                previous.pass_index,
            ) == (motion.section, motion.feature, motion.pass_index)
            if same:
                cuts[-1] = replace(cuts[-1], last_step=index)
            else:
                part_index = motion.feature.part
                cuts.append(
                    CutOccurrence(
                        index=len(cuts),
                        section=motion.section,
                        feature=motion.feature,
                        pass_index=motion.pass_index,
                        part_index=part_index,
                        part_number=parts[part_index].part_number,
                        nested=depths[part_index] > 0,
                        first_step=index,
                        last_step=index,
                        label=cut_label(
                            motion.section,
                            motion.feature,
                            motion.pass_index,
                            parts[part_index],
                            depths[part_index] > 0,
                            cfg,
                        ),
                    )
                )
            cut_of_step.append(len(cuts) - 1)

        sections: list[SectionSpan] = []
        section_of_step: list[int] = []
        for index, motion in enumerate(steps):
            if sections and sections[-1].section == motion.section:
                sections[-1] = replace(
                    sections[-1], last_step=index, last_cut=cut_of_step[index]
                )
            else:
                sections.append(
                    SectionSpan(
                        index=len(sections),
                        section=motion.section,
                        first_step=index,
                        last_step=index,
                        first_cut=cut_of_step[index],
                        last_cut=cut_of_step[index],
                    )
                )
            section_of_step.append(len(sections) - 1)

        return cls(
            program=program,
            plan=plan,
            config=cfg,
            emitted=emitted,
            steps=steps,
            cuts=tuple(cuts),
            sections=tuple(sections),
            cut_of_step=tuple(cut_of_step),
            section_of_step=tuple(section_of_step),
            xy_lengths=tuple(_xy_length(m) for m in steps),
            path_lengths=tuple(_path_length(m) for m in steps),
            part_count=len(parts),
            _line_to_step={m.line_index: i for i, m in enumerate(steps)},
            _cut_ends=tuple(cut.end for cut in cuts),
        )

    # -- totals ------------------------------------------------------------

    @property
    def step_total(self) -> int:
        return len(self.steps)

    @property
    def cut_total(self) -> int:
        return len(self.cuts)

    @property
    def last_perimeter_pass(self) -> int:
        """Index of the deepest configured perimeter pass — the one that cuts a
        part's outline right through."""
        return len(self.config.perimeter_passes) - 1

    @property
    def tab_held(self) -> bool:
        """Does this program hold its pieces with tabs until a release section?

        Read off the PLAN, which is where the amendment's decision lands
        (:attr:`~faceframe_cnc.post.model.CutPlan.release`): a plan with a
        release section is a program whose through passes free nothing, and one
        without is every program written before 2026-08-05 and every reference
        file.  Not read off the post table, because a table can configure a
        release pass for a plan that does not use it — what the operator is
        watching is the program.
        """
        return bool(self.plan.release)

    # -- lookups -----------------------------------------------------------

    def cut_at_step(self, step_index: int) -> CutOccurrence:
        """The occurrence step ``step_index`` belongs to."""
        return self.cuts[self.cut_of_step[step_index]]

    def section_at_step(self, step_index: int) -> SectionSpan:
        return self.sections[self.section_of_step[step_index]]

    def step_for_line(self, line_index: int) -> int | None:
        """Which step the 0-based ``.anc`` line ``line_index`` commands.

        ``None`` for a line that commands no move (a comment, a section head,
        the fixed prologue).  This is the hop a verifier finding takes to
        reach a step: a :class:`~faceframe_cnc.post.verifier.Violation` cites
        a 1-based line number, so callers pass ``line - 1``.
        """
        return self._line_to_step.get(line_index)

    def completed_cuts(self, position: int) -> int:
        """How many occurrences are finished at cursor ``position``.

        Occurrences tile the stream in order, so their :attr:`~CutOccurrence.end`
        boundaries are sorted and the count is a bisection — which is what
        makes recomputing state cheap enough to do on every readout.
        """
        return bisect_right(self._cut_ends, position)

    def state_at(self, position: int) -> MaterialState:
        """Material state at cursor ``position``, folded from uncut stock.

        The definition of the material model, recomputed from nothing: the
        controller's incremental bookkeeping is held to this.
        """
        state = MaterialState.empty(self.part_count)
        for cut in self.cuts[: self.completed_cuts(position)]:
            state = state.apply(cut, self.last_perimeter_pass, self.tab_held)
        return state


def _xy_length(motion: Motion) -> float:
    return hypot(motion.to_x - motion.from_x, motion.to_y - motion.from_y)


def _path_length(motion: Motion) -> float:
    """XYZ travel.  Z is unknown at both ends of a section's first rapid, and
    an unknown Z has not moved as far as this program can say, so dZ is 0."""
    if motion.from_z is None or motion.to_z is None:
        dz = 0.0
    else:
        dz = motion.to_z - motion.from_z
    return hypot(_xy_length(motion), dz)
