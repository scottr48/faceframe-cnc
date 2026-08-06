"""Milestone 4 of the 3D cut simulation: the verifier's findings, made visible.

``faceframe_cnc/post/verifier.py`` is this project's machine-safety authority.
The simulation VISUALISES what it says and never judges for itself, so the
acceptance for this milestone is an equality rather than a feature list: the
marks the sim shows are EXACTLY
:func:`~faceframe_cnc.post.verifier.verify`'s findings — same moves, same
features, same reasons, none invented and none missed.

Covered here:
  (a) the mapper (:mod:`faceframe_cnc.sim.findings`): one
      :class:`~faceframe_cnc.sim.Finding` per violation, in the verifier's own
      order, with its display text verbatim; a violation whose line commands a
      move resolves to THAT move's step, and one whose line commands none (a
      fixed section tail, a section head's restated position) or cites no line
      at all is still a finding, located globally;
  (b) three sheets that EMIT but verify dirty, each built by hand where the
      planner would refuse: a WDC cone reaching into a neighbour, a short
      part whose forced lead-in leaves the sheet, and a stale 0.35 gap whose
      perimeter kerfs enter the part next door.  Plus a part hanging off the
      sheet, which is the one fixture that produces whole-file findings;
  (c) the overlay geometry: a cone-reach envelope ending exactly
      ``2 * wdc_slot_reach(deepest)`` past the stile ends, and a lead-in
      envelope that IS
      :func:`~faceframe_cnc.post.generator.loop_extent` of the loop the
      emitter wrote, one per loop the plan cuts;
  (d) the scene, offscreen: the error tint on a flagged feature (or on its
      part's face when the feature has no entity yet), one red bar per flagged
      move, the bit red exactly while the move about to run is flagged, and
      the envelopes appearing only when switched on;
  (e) the window: the banner's count, the panel's verbatim rows, a click
      seeking to the move a finding names, and a clean sheet rendering
      IDENTICALLY to Milestone 3;
  (f) the refusal view: a sheet the planner would not plan at all, its own
      words in the banner and the part it names outlined.

Every fixture is a real reference reconstruction, a sheet the planner built, or
a sheet built with the planner's own program builder and a hand-written plan —
the generator emits what the plan says, and the verifier is the one that
judges.  Every expected number is read from the post table at assertion time.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass

# Must be set before the first QGuiApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from faceframe_cnc.gui.sim3d import viewmodel as vm
from faceframe_cnc.nesting import NestingConfig, PartSpec, Placement, SheetLayout
from faceframe_cnc.post import (
    CutPlan,
    PostConfig,
    ProgramHeader,
    SheetProgram,
    default_config,
    plan_sheet,
    reconstruct,
)
from faceframe_cnc.post.from_layout import (
    SheetPlanError,
    WdcNotSupportedError,
    is_wdc,
    panel_groove_indices,
    part_depths,
    sheet_program_from_layout,
    wdc_slot_sweep,
)
from faceframe_cnc.post.generator import entry_side_for, loop_extent
from faceframe_cnc.post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PERIMETER,
    SECTION_WDC_SLOT,
    Box,
    FeatureRef,
)
from faceframe_cnc.post.verifier import Violation, expected_work, verify
from faceframe_cnc.sim import FindingSet, SimController, SimTimeline, run_verifier

try:
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_QT = False

if HAVE_QT:
    from faceframe_cnc.gui.sheet_canvas import GHOST_BAD
    from faceframe_cnc.gui.sim3d.refusal import (
        ERROR_MARK_NAME,
        NO_PROGRAM_TEXT,
        RefusalView,
    )
    from faceframe_cnc.gui.sim3d.scene import ERROR_FILL, SimScene
    from faceframe_cnc.gui.sim3d.window import (
        CONE_KINDS,
        ENVELOPE_KINDS,
        Sim3DWindow,
    )

NC_DIR = os.path.join(os.path.dirname(__file__), "..", "reference", "nc_files")

CREATED = "01 JAN 27 - 08:00"

REFERENCES = ("R710101N", "R720101N", "R730101N")

TOL = 1e-9

_APP = None


def setUpModule():  # noqa: N802 - unittest naming
    global _APP
    if HAVE_QT:
        _APP = QApplication.instance() or QApplication([])


def path_of(name: str) -> str:
    return os.path.join(NC_DIR, f"{name}.anc")


def no_viewport(root):
    """The viewport hook a test injects: no GL context anywhere."""
    return None


# --------------------------------------------------------------------------
# Fixtures (the pattern of tests/test_sim.py and tests/test_sim3d.py, rebuilt
# here on purpose: a test module that imported another test module's fixtures
# would make one file's failure the other's)
# --------------------------------------------------------------------------


@dataclass
class Case:
    """One sheet, its timeline, and the authority's verdict on it."""

    label: str
    program: SheetProgram
    plan: CutPlan
    config: PostConfig
    timeline: SimTimeline
    findings: FindingSet
    #: The optimizer layout behind it, where there was one.
    layout: object = None

    def controller(self) -> SimController:
        return SimController(self.timeline)

    def violations(self) -> list[Violation]:
        return run_verifier(self.timeline)


def unchecked_plan(
    program: SheetProgram,
    perimeter_entry: str | None = None,
    opening_entry: str | None = None,
) -> CutPlan:
    """The planner's own sequence, with the planner's REFUSALS left out.

    :func:`~faceframe_cnc.post.from_layout.cut_plan_for` refuses a sheet whose
    WDC cone reaches a neighbour, which is exactly the sheet a finding test
    needs, so the same order is written out here: parts in canonical order for
    the grooves and the slots, deepest nesting first for the openings, and the
    measured table's perimeter pair (everything, then inners first — this module
    emits against :func:`~faceframe_cnc.post.model.default_config`, so two
    passes; a generated sheet is cut with the through pass alone since the
    2026-08-05 amendment, and the mapping under test here is per LINE, which
    neither number changes).  A plan carries
    no coordinate and no depth — those come from the post table — so what is
    emitted is the real program for this geometry, and the verifier judges it
    with no help and no hindrance from this file.

    ``perimeter_entry`` / ``opening_entry`` fill in
    :attr:`~faceframe_cnc.post.model.FeatureRef.entry`, which is what that
    field is FOR: replicating a lead-in edge somebody else chose, good or bad.
    """
    parts = program.flat_parts()
    depths = part_depths(program)
    canonical = list(range(len(parts)))
    inners_first = sorted(canonical, key=lambda index: (-depths[index], index))

    panel: list[FeatureRef] = []
    slots: list[FeatureRef] = []
    for index in canonical:
        part = parts[index]
        for groove in panel_groove_indices(part.part_number):
            panel.append(FeatureRef(index, "groove", groove))
        if is_wdc(part.part_number):
            slots.extend(FeatureRef(index, "wdc_slot", stile) for stile in (0, 1))

    openings = [
        FeatureRef(index, "opening", opening, entry=opening_entry)
        for index in inners_first
        for opening in range(len(parts[index].openings))
    ]
    perimeter = [
        [FeatureRef(index, "perimeter", entry=perimeter_entry) for index in canonical],
        [
            FeatureRef(index, "perimeter", entry=perimeter_entry)
            for index in inners_first
        ],
    ]
    return CutPlan(
        panel=panel, wdc_slot=slots, openings=openings, perimeter=perimeter
    )


def program_of(layout: SheetLayout, demand, name: str) -> SheetProgram:
    """The same :class:`~faceframe_cnc.post.model.SheetProgram` the planner
    builds — only the plan is hand-written (see :func:`unchecked_plan`)."""
    return sheet_program_from_layout(
        layout, ProgramHeader(name=name, created=CREATED), demand, NestingConfig()
    )


#: A WDC frame with a neighbour 0.35 past the end of its stiles.
#:
#: 0.35 is inside BOTH numbers this sheet breaks, which is the point of it:
#:
#: *   the T17 cone's material ends 0.875 past a stile end
#:     (``2 * wdc_slot_reach``), so the deep pass carves that neighbour — the
#:     ``v-slot`` findings, on the SLOT's own moves;
#: *   the perimeter through-kerf sweeps 0.375 past a part edge, so each
#:     part's own profile enters the other one — the ``foreign-cut`` findings,
#:     on each part's own moves.
#:
#: The second is why the neighbour appears in ``flagged_parts``: a finding
#: names the part whose CUT is condemned, never the part a message says gets
#: damaged (:mod:`faceframe_cnc.sim.findings`).
CONE_GAP = 0.35

#: A stale part gap: the spec's old 0.375 was replaced by a 0.455 hard floor
#: because the perimeter lead-in sweeps 0.425 past a part edge.  0.35 is a
#: sheet nested under the old rule and then tightened, and it is below 0.375,
#: which is where the through-kerf itself starts entering the neighbour.  At
#: exactly 0.375 the two kerfs meet edge to edge and the verifier calls that
#: touching rather than entering — its call to make, not this file's.
STALE_GAP = 0.35


def cone_case() -> tuple[SheetProgram, CutPlan, SheetLayout]:
    """A WDC frame whose slot cone reaches into the part beside it."""
    layout = SheetLayout(
        [
            Placement("WDC2436", 4.0, 4.0, 18.0, 36.0),
            Placement("W2436", 4.0, 40.0 + CONE_GAP, 24.0, 36.0),
        ]
    )
    demand = [PartSpec("WDC2436", 18.0, 36.0, 1), PartSpec("W2436", 24.0, 36.0, 1)]
    program = program_of(layout, demand, "R990201N")
    return program, unchecked_plan(program), layout


def lead_in_case() -> tuple[SheetProgram, CutPlan, SheetLayout]:
    """A short part whose perimeter lead-in ramp runs off the sheet.

    The measured default entry side for a perimeter is the RIGHT edge, and on
    a 6" tall part that puts the ramp four inches below the part — over the
    fence.  :func:`~faceframe_cnc.post.generator.entry_side_for` would fall
    back to an edge that fits (2026-08-04 review, fix 6), so the bad choice
    has to be made explicitly, which is what the entry override is for.
    """
    layout = SheetLayout([Placement("W3606", 0.4, 0.5, 36.0, 6.0)])
    demand = [PartSpec("W3606", 36.0, 6.0, 1)]
    program = program_of(layout, demand, "R990202N")
    return program, unchecked_plan(program, perimeter_entry="right"), layout


def stale_gap_case() -> tuple[SheetProgram, CutPlan, SheetLayout]:
    """Two ordinary frames a stale gap apart, kerf into kerf."""
    layout = SheetLayout(
        [
            Placement("W2436", 4.0, 4.0, 24.0, 36.0),
            Placement("W2430", 4.0, 40.0 + STALE_GAP, 24.0, 30.0),
        ]
    )
    demand = [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("W2430", 24.0, 30.0, 1)]
    program = program_of(layout, demand, "R990203N")
    return program, unchecked_plan(program), layout


def off_sheet_case() -> tuple[SheetProgram, CutPlan, SheetLayout]:
    """A part hanging five inches off the right-hand edge of the sheet.

    The fixture that produces the findings no move can own: ``part-bounds``
    cites no line at all (it is about the recovered footprint), and two of the
    ``bounds`` findings land on lines that command nothing — a fixed section
    tail and the position a section head restates before its ``Tn``.
    """
    layout = SheetLayout([Placement("W2436", 30.0, 4.0, 24.0, 36.0)])
    demand = [PartSpec("W2436", 24.0, 36.0, 1)]
    program = program_of(layout, demand, "R990204N")
    return program, unchecked_plan(program, "left", "left"), layout


def clean_wdc_case() -> tuple[SheetProgram, CutPlan, SheetLayout]:
    """A planner-built sheet with a WDC frame clear of everything."""
    layout = SheetLayout(
        [
            Placement("WDC2436", 4.0, 4.0, 18.0, 36.0),
            Placement("W2436", 4.0, 44.0, 24.0, 36.0),
        ]
    )
    demand = [PartSpec("WDC2436", 18.0, 36.0, 1), PartSpec("W2436", 24.0, 36.0, 1)]
    program, plan = plan_sheet(
        layout,
        ProgramHeader(name="R990102N", created=CREATED),
        demand,
        NestingConfig(),
    )
    return program, plan, layout


def clean_nested_case() -> tuple[SheetProgram, CutPlan, SheetLayout]:
    """A W3012 turned 90 degrees inside a W2742's opening, and one beside it."""
    layout = SheetLayout(
        [
            Placement(
                "W2742",
                0.0,
                0.0,
                27.0,
                42.0,
                False,
                [Placement("W3012", 5.0, 6.0, 12.0, 30.0, True, [])],
            ),
            Placement("W3012", 30.0, 0.0, 12.0, 30.0, True, []),
        ]
    )
    demand = [PartSpec("W2742", 27.0, 42.0, 1), PartSpec("W3012", 30.0, 12.0, 2)]
    program, plan = plan_sheet(
        layout,
        ProgramHeader(name="R990103N", created=CREATED),
        demand,
        NestingConfig(),
    )
    return program, plan, layout


#: The sheets that must verify CLEAN, and the ones that must not.
CLEAN_LABELS = REFERENCES + ("WDC", "NESTED")
DIRTY_LABELS = ("CONE", "LEAD-IN", "STALE-GAP", "OFF-SHEET")

_CASES: dict[str, Case] = {}


def cases() -> dict[str, Case]:
    """Every fixture, built once: reconstructing three files is not free."""
    if not _CASES:
        sources: list[tuple[str, tuple]] = [
            (name, reconstruct(path_of(name)) + (None,)) for name in REFERENCES
        ]
        sources.append(("WDC", clean_wdc_case()))
        sources.append(("NESTED", clean_nested_case()))
        sources.append(("CONE", cone_case()))
        sources.append(("LEAD-IN", lead_in_case()))
        sources.append(("STALE-GAP", stale_gap_case()))
        sources.append(("OFF-SHEET", off_sheet_case()))
        for label, (program, plan, layout) in sources:
            config = default_config()
            timeline = SimTimeline.build(program, plan, config)
            _CASES[label] = Case(
                label=label,
                program=program,
                plan=plan,
                config=config,
                timeline=timeline,
                findings=FindingSet.verified(timeline),
                layout=layout,
            )
    return _CASES


def case(label: str) -> Case:
    return cases()[label]


def loop_count(plan: CutPlan) -> int:
    """How many closed profile loops the plan cuts, over every section."""
    return (
        len(plan.openings)
        + len(plan.detail_order())
        + sum(len(refs) for refs in plan.perimeter)
    )


def cuts_matching(item: Case, **match):
    """Every cut occurrence whose attributes equal ``match``."""
    found = []
    for cut in item.timeline.cuts:
        if all(getattr(cut, key) == value for key, value in match.items()):
            found.append(cut)
    return found


def commands_a_move(item: Case, line: int) -> bool:
    """Does 1-based ``line`` of the emitted text command a move at all?

    Answered from the emitter's own event stream rather than from the mapper,
    so the mapper's answer has something independent to be right about.
    """
    return item.timeline.emitted.events[line - 1].motion is not None


# --------------------------------------------------------------------------
# (a) the mapper: total, faithful, and the verifier's own words
# --------------------------------------------------------------------------


class RunVerifierTest(unittest.TestCase):
    """One call to the authority, with nothing done to the answer."""

    def test_it_is_verify_of_the_programs_own_text_and_table(self):
        for label in CLEAN_LABELS + DIRTY_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                self.assertEqual(
                    run_verifier(item.timeline),
                    verify(item.timeline.emitted.text, item.config),
                )

    def test_an_expected_work_manifest_is_passed_straight_through(self):
        item = case("WDC")
        manifest = expected_work(item.layout, item.config)
        self.assertEqual(
            run_verifier(item.timeline, manifest),
            verify(item.timeline.emitted.text, item.config, manifest),
        )

    def test_nothing_is_filtered_out_on_the_way_back(self):
        """The dirty sheets' answers come back whole, not summarised."""
        for label in DIRTY_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                direct = verify(item.timeline.emitted.text, item.config)
                self.assertTrue(direct, "this fixture is supposed to verify dirty")
                self.assertEqual(run_verifier(item.timeline), direct)


class MappingTest(unittest.TestCase):
    """Every violation appears exactly once, in order, unchanged."""

    def test_a_finding_set_is_one_finding_per_violation_in_order(self):
        for label in CLEAN_LABELS + DIRTY_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                violations = item.violations()
                self.assertEqual(len(item.findings.all), len(violations))
                self.assertEqual(
                    [f.violation for f in item.findings.all], violations
                )
                self.assertEqual(item.findings.count, len(violations))

    def test_the_display_text_is_the_verifiers_own_string(self):
        for label in DIRTY_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                for finding in item.findings.all:
                    self.assertEqual(finding.display, str(finding.violation))
                    self.assertIn(finding.code, finding.display)
                    self.assertIn(finding.message, finding.display)

    def test_a_line_that_commands_a_move_resolves_to_that_move(self):
        for label in DIRTY_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                named = [
                    f
                    for f in item.findings.all
                    if f.line > 0 and commands_a_move(item, f.line)
                ]
                self.assertTrue(named, "this fixture should flag some moves")
                for finding in named:
                    self.assertIsNotNone(finding.step_index)
                    step = item.timeline.steps[finding.step_index]
                    self.assertEqual(step.line_index, finding.line - 1)

    def test_a_line_that_commands_no_move_lands_in_the_global_list(self):
        item = case("OFF-SHEET")
        unmoving = [
            f
            for f in item.findings.all
            if f.line > 0 and not commands_a_move(item, f.line)
        ]
        self.assertTrue(
            unmoving,
            "the off-sheet fixture is here because it flags lines that move nothing",
        )
        for finding in unmoving:
            self.assertIsNone(finding.step_index)
            self.assertIsNone(finding.cut_index)
            self.assertIsNone(finding.part_index)
            self.assertTrue(finding.is_global)
            self.assertIn(finding, item.findings.global_findings)

    def test_a_whole_file_finding_cites_no_line_and_is_still_kept(self):
        item = case("OFF-SHEET")
        whole_file = [f for f in item.findings.all if f.line == 0]
        self.assertTrue(whole_file, "part-bounds is a whole-file finding")
        for finding in whole_file:
            self.assertTrue(finding.is_global)
            self.assertIn(finding, item.findings.global_findings)

    def test_the_global_findings_are_exactly_the_unlocated_ones(self):
        for label in DIRTY_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                self.assertEqual(
                    item.findings.global_findings,
                    tuple(f for f in item.findings.all if f.step_index is None),
                )

    def test_a_located_finding_names_the_cut_and_part_of_its_own_move(self):
        for label in DIRTY_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                for finding in item.findings.all:
                    if finding.step_index is None:
                        continue
                    cut = item.timeline.cut_at_step(finding.step_index)
                    self.assertEqual(finding.cut_index, cut.index)
                    self.assertEqual(finding.part_index, cut.part_index)
                    self.assertTrue(cut.contains_step(finding.step_index))

    def test_for_step_and_for_cut_hold_every_located_finding_once(self):
        for label in DIRTY_LABELS:
            item = case(label)
            found = item.findings
            with self.subTest(sheet=label):
                located = [f for f in found.all if f.step_index is not None]
                by_step = [f for k in sorted(found.flagged_steps) for f in found.for_step(k)]
                by_cut = [f for k in sorted(found.flagged_cuts) for f in found.for_cut(k)]
                self.assertEqual(len(by_step), len(located))
                self.assertEqual(len(by_cut), len(located))
                self.assertEqual(set(id(f) for f in by_step), set(id(f) for f in located))
                self.assertEqual(set(id(f) for f in by_cut), set(id(f) for f in located))

    def test_flagged_sets_are_exactly_what_the_findings_say(self):
        for label in DIRTY_LABELS:
            item = case(label)
            found = item.findings
            with self.subTest(sheet=label):
                self.assertEqual(
                    found.flagged_steps,
                    frozenset(
                        f.step_index for f in found.all if f.step_index is not None
                    ),
                )
                self.assertEqual(
                    found.flagged_cuts,
                    frozenset(
                        f.cut_index for f in found.all if f.cut_index is not None
                    ),
                )
                self.assertEqual(
                    found.flagged_parts,
                    frozenset(
                        f.part_index for f in found.all if f.part_index is not None
                    ),
                )

    def test_a_clean_sheet_flags_absolutely_nothing(self):
        for label in CLEAN_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                found = item.findings
                self.assertEqual(found.count, 0, [f.display for f in found.all])
                self.assertEqual(found.all, ())
                self.assertEqual(found.global_findings, ())
                self.assertEqual(found.flagged_cuts, frozenset())
                self.assertEqual(found.flagged_parts, frozenset())
                self.assertEqual(found.flagged_steps, frozenset())
                self.assertFalse(found)
                self.assertEqual(found.for_step(0), ())
                self.assertEqual(found.for_cut(0), ())

    def test_an_empty_set_is_the_same_as_a_clean_sheets_set(self):
        item = case("WDC")
        self.assertEqual(FindingSet.empty(), item.findings)

    def test_a_clean_sheet_is_clean_for_a_step_it_has_no_such_step_for(self):
        item = case("WDC")
        self.assertEqual(item.findings.for_step(item.timeline.step_total + 99), ())

    def test_two_mappings_of_one_program_agree_exactly(self):
        item = case("CONE")
        again = FindingSet.build(item.timeline, item.violations())
        self.assertEqual(again, item.findings)
        self.assertEqual(again.all, item.findings.all)

    def test_a_finding_on_a_non_motion_line_survives_a_hand_built_set(self):
        """The mapper's own answer for lines it cannot place, in isolation.

        The wrapper line ``%`` and line 0 are the two shapes a global finding
        takes; both must come back as findings, not as nothing.
        """
        item = case("WDC")
        made = [
            Violation("wrapper", "the program is not wrapped", 0),
            Violation("header", "the identity block is wrong", 1),
        ]
        found = FindingSet.build(item.timeline, made)
        self.assertEqual([f.violation for f in found.all], made)
        self.assertEqual(found.global_findings, found.all)
        self.assertEqual(found.flagged_cuts, frozenset())
        self.assertFalse(commands_a_move(item, 1))


# --------------------------------------------------------------------------
# (b) the three bad sheets
# --------------------------------------------------------------------------



class ReleaseSectionFindingsTest(unittest.TestCase):
    """A finding on a release cut lands on the release cut (2026-08-05 §3c).

    The newest section is also the one whose lines a 3D view is least likely to
    have a home for, so this is the mapping stated for it explicitly: every
    ``hold`` finding the verifier reports about a release cut resolves to a step,
    to the release occurrence that owns that step, and to the part that
    occurrence frees.  Nothing lands in the global bucket for want of a step.
    """

    @classmethod
    def setUpClass(cls):
        from dataclasses import replace

        from faceframe_cnc.post.from_layout import plan_sheet, post_config_for
        from faceframe_cnc.post.model import SECTION_RELEASE
        from faceframe_cnc.post.verifier import verify
        from tests.test_r0805_regression import r0805_layout

        layout, specs, nesting = r0805_layout()
        cls.section = SECTION_RELEASE
        cls.post = post_config_for(nesting)
        program, plan = plan_sheet(
            layout,
            ProgramHeader(name="R080501N", created="05 AUG 26 - 07:30"),
            specs,
            nesting,
            cls.post,
        )
        cls.plan = plan
        cls.timeline = SimTimeline.build(program, plan, cls.post)
        # the release pass run at the DETAIL pass's feeds: legal everywhere else,
        # and exactly what "very slowly" rules out
        fast = replace(
            cls.post,
            release=replace(cls.post.release, cut_feed=293.0, entry_feed=100.0),
        )
        cls.violations = [
            v for v in verify(cls.timeline.emitted.text, fast) if v.code == "hold"
        ]
        cls.findings = FindingSet.build(cls.timeline, cls.violations)

    def test_the_crafted_program_really_is_refused(self):
        total = sum(len(zones) for zones in self.plan.tabs.values())
        self.assertEqual(len(self.violations), 2 * total, "each plunge and each cut")

    def test_every_release_finding_lands_on_a_release_cut(self):
        self.assertEqual(len(self.findings.findings), len(self.violations))
        self.assertEqual(self.findings.global_findings, ())
        for finding in self.findings.findings:
            with self.subTest(line=finding.violation.line):
                self.assertIsNotNone(finding.step_index)
                cut = self.timeline.cuts[finding.cut_index]
                self.assertEqual(cut.section, self.section)
                self.assertEqual(finding.part_index, cut.part_index)
                self.assertEqual(finding.display, str(finding.violation))

    def test_the_flagged_cuts_are_exactly_the_release_occurrences(self):
        release = [c for c in self.timeline.cuts if c.section == self.section]
        self.assertEqual(
            sorted(self.findings.flagged_cuts), sorted(c.index for c in release)
        )
        for cut in release:
            with self.subTest(label=cut.label):
                self.assertEqual(
                    {f.violation.code for f in self.findings.for_cut(cut.index)},
                    {"hold"},
                )

class ConeSheetTest(unittest.TestCase):
    """A WDC frame with a neighbour inside the cone's end reach."""

    def setUp(self):
        self.item = case("CONE")
        self.parts = self.item.program.flat_parts()
        self.wdc = next(
            index
            for index, part in enumerate(self.parts)
            if is_wdc(part.part_number)
        )
        self.neighbour = next(
            index
            for index, part in enumerate(self.parts)
            if not is_wdc(part.part_number)
        )

    def test_the_planner_refuses_this_sheet_which_is_why_it_is_hand_built(self):
        with self.assertRaises(WdcNotSupportedError):
            plan_sheet(
                self.item.layout,
                ProgramHeader(name="R990201N", created=CREATED),
                [
                    PartSpec("WDC2436", 18.0, 36.0, 1),
                    PartSpec("W2436", 24.0, 36.0, 1),
                ],
                NestingConfig(),
            )

    def test_the_verifier_finds_the_swept_cone(self):
        codes = {f.code for f in self.item.findings.all}
        self.assertIn("v-slot", codes, [f.display for f in self.item.findings.all])

    def test_every_cone_finding_lands_on_the_slot_that_swept_it(self):
        slot_findings = [f for f in self.item.findings.all if f.code == "v-slot"]
        self.assertTrue(slot_findings)
        slot_cuts = {
            cut.index for cut in cuts_matching(self.item, section=SECTION_WDC_SLOT)
        }
        for finding in slot_findings:
            self.assertIsNotNone(finding.step_index)
            self.assertIn(finding.cut_index, slot_cuts)
            cut = self.item.timeline.cuts[finding.cut_index]
            self.assertEqual(cut.section, SECTION_WDC_SLOT)
            self.assertEqual(cut.part_index, self.wdc)
            self.assertEqual(finding.part_index, self.wdc)

    def test_both_stiles_and_both_depth_passes_are_flagged(self):
        """The cone reaches the neighbour on every bite it takes."""
        flagged = {
            (
                self.item.timeline.cuts[f.cut_index].feature.index,
                self.item.timeline.cuts[f.cut_index].pass_index,
            )
            for f in self.item.findings.all
            if f.code == "v-slot"
        }
        passes = range(len(self.item.config.wdc_slot.z_cuts))
        self.assertEqual(
            flagged, {(stile, position) for stile in (0, 1) for position in passes}
        )

    def test_the_neighbour_is_flagged_by_its_own_condemned_cuts(self):
        """It IS in ``flagged_parts`` — and here is why, precisely.

        A finding names the part whose cut the offending line belongs to, so
        the cone findings all name the WDC frame.  The neighbour is flagged
        because at this gap ITS own moves are condemned too: the verifier
        reports ``foreign-cut`` on the neighbour's groove overrun and on its
        perimeter kerf.  Nothing here reads a part out of a message.
        """
        found = self.item.findings
        self.assertIn(self.neighbour, found.flagged_parts)
        own = [
            f
            for f in found.all
            if f.part_index == self.neighbour
        ]
        self.assertTrue(own)
        for finding in own:
            self.assertEqual(finding.code, "foreign-cut")
            cut = self.item.timeline.cuts[finding.cut_index]
            self.assertEqual(cut.part_index, self.neighbour)

    def test_the_cone_findings_name_the_neighbour_in_the_verifiers_words(self):
        """The damaged part is in the message, which is where it belongs."""
        box = self.parts[self.neighbour].box
        said = [
            f.display
            for f in self.item.findings.all
            if f.code == "v-slot" and f"{box.y0:g}" in f.display
        ]
        self.assertTrue(said, [f.display for f in self.item.findings.all])

    def test_the_neighbour_sits_inside_the_reach_the_config_states(self):
        """The fixture is bad for the reason it claims to be bad for."""
        config = self.item.config
        position = vm.deepest_slot_pass(config)
        reach = config.wdc_slot_reach(position)
        swept = wdc_slot_sweep(self.parts[self.wdc], 0, position, config)
        self.assertTrue(swept.overlaps(self.parts[self.neighbour].box, TOL))
        self.assertLess(CONE_GAP, 2.0 * reach)


class LeadInSheetTest(unittest.TestCase):
    """A short part whose forced lead-in leaves the sheet."""

    def setUp(self):
        self.item = case("LEAD-IN")

    def test_the_verifier_finds_the_ramp_outside_the_fence(self):
        codes = {f.code for f in self.item.findings.all}
        self.assertIn("bounds", codes, [f.display for f in self.item.findings.all])

    def test_every_bounds_finding_lands_on_that_parts_perimeter(self):
        found = self.item.findings
        bounds = [f for f in found.all if f.code == "bounds"]
        self.assertTrue(bounds)
        perimeter = {
            cut.index for cut in cuts_matching(self.item, section=SECTION_PERIMETER)
        }
        for finding in bounds:
            self.assertIsNotNone(finding.step_index)
            self.assertIn(finding.cut_index, perimeter)
            self.assertEqual(finding.part_index, 0)
        self.assertEqual(found.flagged_parts, frozenset({0}))

    def test_the_same_sheet_is_clean_without_the_override(self):
        """The override is the mistake; the emitter's own fallback avoids it.

        With ``entry=None`` :func:`~faceframe_cnc.post.generator.entry_side_for`
        tries the measured default, sees the ramp leave the sheet and picks an
        edge that fits (2026-08-04 review, fix 6) — so this fixture is a bad
        CHOICE being replicated, not a bad part.
        """
        program, _, _ = lead_in_case()
        timeline = SimTimeline.build(
            program, unchecked_plan(program), default_config()
        )
        self.assertEqual(FindingSet.verified(timeline).count, 0)

    def test_the_emitted_ramp_really_does_leave_the_sheet(self):
        """Read off the geometry, so the fixture cannot rot into a clean one."""
        config = self.item.config
        part = self.item.program.flat_parts()[0]
        spec = config.perimeter_passes[-1]
        tool = config.tool(SECTION_PERIMETER)
        cut = part.box.grow(spec.offset)
        extent = loop_extent(cut, "right", tool, spec, config)
        self.assertFalse(vm.sheet_fence(config).contains(extent, TOL))


class StaleGapSheetTest(unittest.TestCase):
    """Two frames a stale gap apart: each one's kerf enters the other."""

    def setUp(self):
        self.item = case("STALE-GAP")

    def test_the_verifier_finds_the_kerf_in_the_neighbour(self):
        codes = {f.code for f in self.item.findings.all}
        self.assertEqual(codes, {"foreign-cut"}, [f.display for f in self.item.findings.all])

    def test_both_parts_are_flagged_on_their_own_cuts(self):
        found = self.item.findings
        self.assertEqual(found.flagged_parts, frozenset({0, 1}))
        for finding in found.all:
            cut = self.item.timeline.cuts[finding.cut_index]
            self.assertEqual(finding.part_index, cut.part_index)

    def test_the_perimeter_through_pass_is_among_the_flagged_cuts(self):
        through = len(self.item.config.perimeter_passes) - 1
        flagged = {
            (
                self.item.timeline.cuts[index].section,
                self.item.timeline.cuts[index].pass_index,
            )
            for index in self.item.findings.flagged_cuts
        }
        self.assertIn((SECTION_PERIMETER, through), flagged)

    def test_the_gap_is_tighter_than_the_kerf_reaches(self):
        """The fixture's badness, from the post table rather than a literal."""
        config = self.item.config
        spec = config.perimeter_passes[-1]
        reach = spec.offset + config.tool(SECTION_PERIMETER).radius
        self.assertLess(STALE_GAP, reach)


# --------------------------------------------------------------------------
# (c) the overlay geometry
# --------------------------------------------------------------------------


class ConeOverlayTest(unittest.TestCase):
    def test_there_is_one_overlay_per_slot_the_plan_cuts(self):
        for label in ("WDC", "CONE"):
            item = case(label)
            with self.subTest(sheet=label):
                found = vm.wdc_cone_overlays(item.program, item.plan, item.config)
                self.assertEqual(len(found), len(item.plan.wdc_slot))
                self.assertEqual(
                    [o.feature_index for o in found],
                    [ref.index for ref in item.plan.wdc_slot],
                )

    def test_a_sheet_with_no_wdc_frame_has_no_cone_overlay(self):
        item = case("NESTED")
        self.assertEqual(vm.wdc_cone_overlays(item.program, item.plan, item.config), ())
        self.assertEqual(vm.wdc_slot_positions(item.program, None), ())

    def test_without_a_plan_the_slots_are_the_ones_a_plan_would_hold(self):
        """The refusal path's fallback, held to the planner's own answer."""
        for label in ("WDC", "CONE", "NESTED"):
            item = case(label)
            with self.subTest(sheet=label):
                self.assertEqual(
                    vm.wdc_slot_positions(item.program, None),
                    vm.wdc_slot_positions(item.program, item.plan),
                )
                self.assertEqual(
                    vm.wdc_cone_overlays(item.program, None, item.config),
                    vm.wdc_cone_overlays(item.program, item.plan, item.config),
                )

    def test_the_deepest_pass_is_the_one_with_the_lowest_z(self):
        config = case("WDC").config
        position = vm.deepest_slot_pass(config)
        z_cuts = config.wdc_slot.z_cuts
        self.assertEqual(z_cuts[position], min(z_cuts))

    def test_the_overlay_ends_twice_the_reach_past_the_stile_ends(self):
        item = case("WDC")
        config = item.config
        position = vm.deepest_slot_pass(config)
        reach = config.wdc_slot_reach(position)
        parts = item.program.flat_parts()
        for overlay in vm.wdc_cone_overlays(item.program, item.plan, item.config):
            with self.subTest(slot=overlay.key):
                box = parts[overlay.part_index].box
                # The slots of an upright frame run in Y; the overlay's long
                # axis is the one that overhangs the part.
                self.assertAlmostEqual(overlay.box.y0, box.y0 - 2.0 * reach)
                self.assertAlmostEqual(overlay.box.y1, box.y1 + 2.0 * reach)
                # Across the slot it is the cone's full width, centred on the
                # commanded centreline.
                (x0, _), (x1, _) = overlay.segment
                self.assertAlmostEqual(x0, x1)
                self.assertAlmostEqual(overlay.box.x0, x0 - reach)
                self.assertAlmostEqual(overlay.box.x1, x1 + reach)
                self.assertAlmostEqual(overlay.box.width, 2.0 * reach)

    def test_the_overlay_is_the_swept_material_the_post_computes(self):
        """The same helper the planner refuses with, not a second derivation."""
        item = case("WDC")
        position = vm.deepest_slot_pass(item.config)
        parts = item.program.flat_parts()
        for overlay in vm.wdc_cone_overlays(item.program, item.plan, item.config):
            self.assertEqual(
                overlay.box,
                wdc_slot_sweep(
                    parts[overlay.part_index],
                    overlay.feature_index,
                    position,
                    item.config,
                ),
            )

    def test_the_overlay_carries_the_deep_passs_own_z_and_depth(self):
        item = case("WDC")
        config = item.config
        position = vm.deepest_slot_pass(config)
        z_cut = config.wdc_slot.z_cuts[position]
        for overlay in vm.wdc_cone_overlays(item.program, item.plan, item.config):
            self.assertEqual(overlay.pass_index, position)
            self.assertAlmostEqual(overlay.z_cut, z_cut)
            self.assertAlmostEqual(overlay.depth, config.stock_top_z - z_cut)


class LeadInOverlayTest(unittest.TestCase):
    def test_there_is_one_envelope_per_loop_the_plan_cuts(self):
        for label in CLEAN_LABELS + DIRTY_LABELS:
            item = case(label)
            with self.subTest(sheet=label):
                found = vm.lead_in_overlays(item.program, item.plan, item.config)
                self.assertEqual(len(found), loop_count(item.plan))

    def test_every_opening_detail_and_perimeter_pass_is_covered(self):
        item = case("NESTED")
        found = vm.lead_in_overlays(item.program, item.plan, item.config)
        sections = [o.section for o in found]
        self.assertEqual(sections.count(SECTION_OPENINGS), len(item.plan.openings))
        self.assertEqual(
            sections.count(SECTION_DETAIL), len(item.plan.detail_order())
        )
        for index, refs in enumerate(item.plan.perimeter):
            same = [
                o
                for o in found
                if o.section == SECTION_PERIMETER and o.pass_index == index
            ]
            self.assertEqual(len(same), len(refs))

    def test_an_envelope_is_loop_extent_of_the_side_the_emitter_chose(self):
        for label in ("WDC", "NESTED", "LEAD-IN"):
            item = case(label)
            config = item.config
            parts = item.program.flat_parts()
            with self.subTest(sheet=label):
                for overlay in vm.lead_in_overlays(item.program, item.plan, config):
                    perimeter = overlay.section == SECTION_PERIMETER
                    spec = (
                        config.perimeter_passes[overlay.pass_index]
                        if perimeter
                        else (
                            # One overlay per rung of the T11 opening ladder
                            # (2026-08-05 max-bite amendment): the pass index is
                            # None on a single-rung table and numbers the rungs
                            # otherwise, exactly as the emitter tags the motions.
                            config.openings_passes[overlay.pass_index or 0]
                            if overlay.section == SECTION_OPENINGS
                            else config.detail_pass
                        )
                    )
                    tool = config.tool(overlay.section)
                    part = parts[overlay.part_index]
                    base = (
                        part.box
                        if perimeter
                        else part.openings[overlay.feature_index]
                    )
                    cut = base.grow(spec.offset)
                    self.assertEqual(
                        overlay.box, loop_extent(cut, overlay.side, tool, spec, config)
                    )

    def test_the_side_is_the_one_the_ref_asked_for_when_it_asked(self):
        """A ref with an entry override gets that edge, right or wrong."""
        item = case("LEAD-IN")
        found = vm.lead_in_overlays(item.program, item.plan, item.config)
        sides = {
            o.side for o in found if o.section == SECTION_PERIMETER
        }
        self.assertEqual(sides, {"right"})

    def test_the_default_side_is_the_emitters_own_resolution(self):
        item = case("WDC")
        config = item.config
        parts = item.program.flat_parts()
        for overlay in vm.lead_in_overlays(item.program, item.plan, config):
            if overlay.section != SECTION_PERIMETER:
                continue
            spec = config.perimeter_passes[overlay.pass_index]
            tool = config.tool(SECTION_PERIMETER)
            cut = parts[overlay.part_index].box.grow(spec.offset)
            self.assertEqual(
                overlay.side,
                entry_side_for(cut, "perimeter", tool, spec, config, override=None),
            )

    def test_a_good_sheets_envelopes_all_stay_inside_the_fence(self):
        for label in ("WDC", "NESTED"):
            item = case(label)
            fence = vm.sheet_fence(item.config)
            with self.subTest(sheet=label):
                for overlay in vm.lead_in_overlays(
                    item.program, item.plan, item.config
                ):
                    self.assertTrue(
                        fence.contains(overlay.box, TOL), overlay.key
                    )

    def test_the_bad_sheets_envelope_leaves_the_fence(self):
        item = case("LEAD-IN")
        fence = vm.sheet_fence(item.config)
        outside = [
            o
            for o in vm.lead_in_overlays(item.program, item.plan, item.config)
            if not fence.contains(o.box, TOL)
        ]
        self.assertTrue(outside)
        self.assertTrue(all(o.section == SECTION_PERIMETER for o in outside))


class FenceOverlayTest(unittest.TestCase):
    def test_the_fence_is_the_sheet_plus_its_measured_overhang(self):
        config = case("WDC").config
        overlay = vm.sheet_fence_overlay(config)
        self.assertEqual(
            overlay.box,
            Box(
                -config.overhang,
                -config.overhang,
                config.sheet_width + config.overhang,
                config.sheet_length + config.overhang,
            ),
        )
        self.assertEqual(overlay.kind, vm.OverlayKind.FENCE)
        self.assertIsNone(overlay.part_index)
        self.assertEqual(overlay.depth, 0.0)

    def test_the_full_overlay_list_is_the_three_families_and_nothing_else(self):
        item = case("CONE")
        found = vm.overlays(item.program, item.plan, item.config)
        self.assertEqual(
            len(found),
            len(item.plan.wdc_slot) + loop_count(item.plan) + 1,
        )
        self.assertEqual(
            {o.kind for o in found},
            {
                vm.OverlayKind.CONE_REACH,
                vm.OverlayKind.LEAD_IN,
                vm.OverlayKind.FENCE,
            },
        )
        self.assertEqual(len({o.key for o in found}), len(found))

    def test_the_overlays_are_the_same_two_calls_running(self):
        item = case("WDC")
        self.assertEqual(
            vm.overlays(item.program, item.plan, item.config),
            vm.overlays(item.program, item.plan, item.config),
        )


# --------------------------------------------------------------------------
# (c2) the danger model: findings turned into things to draw
# --------------------------------------------------------------------------


class DangerModelTest(unittest.TestCase):
    def test_no_findings_means_envelopes_and_nothing_else(self):
        item = case("WDC")
        model = vm.DangerModel.build(item.timeline, None)
        self.assertTrue(model.overlays)
        self.assertEqual(model.flagged, ())
        self.assertEqual(model.marks, ())
        self.assertEqual(model.flagged_steps, frozenset())
        self.assertEqual(model.flagged_parts, frozenset())
        self.assertFalse(model.is_flagged_step(0))
        self.assertFalse(model.is_flagged_step(None))

    def test_an_empty_finding_set_flags_nothing_either(self):
        item = case("WDC")
        self.assertEqual(
            vm.DangerModel.build(item.timeline, FindingSet.empty()),
            vm.DangerModel.build(item.timeline, None),
        )

    def test_a_flagged_cut_is_reported_once_per_condemned_cut(self):
        item = case("CONE")
        model = vm.DangerModel.build(item.timeline, item.findings)
        self.assertEqual(
            [flag.cut_index for flag in model.flagged],
            sorted(item.findings.flagged_cuts),
        )
        for flag in model.flagged:
            cut = item.timeline.cuts[flag.cut_index]
            self.assertEqual(flag.part_index, cut.part_index)
            self.assertEqual(flag.label, cut.label)
            self.assertEqual(
                flag.codes,
                tuple(f.code for f in item.findings.for_cut(flag.cut_index)),
            )

    def test_a_flagged_cut_points_at_the_feature_it_reveals(self):
        """The key the scene caches that feature's entity under, exactly."""
        item = case("CONE")
        model = vm.DangerModel.build(item.timeline, item.findings)
        controller = item.controller()
        controller.to_end()
        keys = {reveal.key for reveal in vm.reveals(
            controller.state, item.program, item.config
        )}
        for flag in model.flagged:
            if flag.reveal_key is None:
                continue
            self.assertIn(flag.reveal_key, keys)

    def test_the_pass_that_frees_a_part_has_no_feature_to_redden(self):
        item = case("STALE-GAP")
        through = len(item.config.perimeter_passes) - 1
        model = vm.DangerModel.build(item.timeline, item.findings)
        looked = 0
        for flag in model.flagged:
            cut = item.timeline.cuts[flag.cut_index]
            if cut.section == SECTION_PERIMETER and cut.pass_index == through:
                self.assertIsNone(flag.reveal_key)
                looked += 1
        self.assertTrue(looked, "this fixture flags the through pass")

    def test_the_onion_skin_pass_reddens_its_scored_outline(self):
        item = case("LEAD-IN")
        model = vm.DangerModel.build(item.timeline, item.findings)
        keys = {
            flag.reveal_key
            for flag in model.flagged
            if item.timeline.cuts[flag.cut_index].pass_index == 0
        }
        self.assertIn(
            vm.reveal_key(vm.RevealKind.SKIN, 0, 0, 0), keys
        )

    def test_a_mark_is_the_moves_own_path_at_its_own_depth(self):
        item = case("CONE")
        model = vm.DangerModel.build(item.timeline, item.findings)
        self.assertEqual(
            [mark.step_index for mark in model.marks],
            sorted(item.findings.flagged_steps),
        )
        for mark in model.marks:
            motion = item.timeline.steps[mark.step_index]
            self.assertEqual(
                mark.segment,
                ((motion.from_x, motion.from_y), (motion.to_x, motion.to_y)),
            )
            zs = [z for z in (motion.from_z, motion.to_z) if z is not None]
            self.assertAlmostEqual(mark.z, min(zs))
            self.assertEqual(
                mark.codes,
                tuple(f.code for f in item.findings.for_step(mark.step_index)),
            )

    def test_one_move_gets_one_mark_however_many_rules_it_breaks(self):
        item = case("OFF-SHEET")
        model = vm.DangerModel.build(item.timeline, item.findings)
        self.assertEqual(
            len(model.marks), len(item.findings.flagged_steps)
        )
        self.assertEqual(
            len({mark.key for mark in model.marks}), len(model.marks)
        )

    def test_the_flagged_step_test_is_the_finding_sets_own_answer(self):
        item = case("CONE")
        model = vm.DangerModel.build(item.timeline, item.findings)
        for step in range(item.timeline.step_total):
            self.assertEqual(
                model.is_flagged_step(step),
                bool(item.findings.for_step(step)),
                step,
            )


class BannerAndRowsTest(unittest.TestCase):
    def test_a_clean_program_says_nothing(self):
        self.assertEqual(vm.banner_text(None), "")
        self.assertEqual(vm.banner_text(FindingSet.empty()), "")
        self.assertEqual(vm.finding_rows(None), ())
        self.assertEqual(vm.finding_rows(FindingSet.empty()), ())

    def test_the_banner_carries_the_count_and_the_refusal(self):
        item = case("CONE")
        text = vm.banner_text(item.findings)
        self.assertIn(str(item.findings.count), text)
        self.assertIn(vm.BANNER_VERDICT, text)
        self.assertIn("findings", text)

    def test_one_finding_is_singular(self):
        item = case("WDC")
        one = FindingSet.build(
            item.timeline, [Violation("wrapper", "the program is empty", 0)]
        )
        text = vm.banner_text(one)
        self.assertIn("1 verifier finding on", text)
        self.assertIn(vm.BANNER_VERDICT, text)

    def test_the_rows_are_the_findings_own_words_in_order(self):
        item = case("OFF-SHEET")
        self.assertEqual(
            vm.finding_rows(item.findings),
            tuple(f.display for f in item.findings.all),
        )
        self.assertEqual(
            vm.finding_rows(item.findings),
            tuple(str(v) for v in item.violations()),
        )


# --------------------------------------------------------------------------
# (d) the scene, offscreen
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class SceneFindingsTest(unittest.TestCase):
    def scene_for(self, label: str, with_findings: bool = True) -> "SimScene":
        item = case(label)
        danger = vm.DangerModel.build(
            item.timeline, item.findings if with_findings else None
        )
        return SimScene(item.program, item.config, danger=danger)

    def test_the_error_colour_is_the_two_d_previews_bad_red(self):
        self.assertEqual(
            (ERROR_FILL.red(), ERROR_FILL.green(), ERROR_FILL.blue()),
            (GHOST_BAD.red(), GHOST_BAD.green(), GHOST_BAD.blue()),
        )

    def test_a_clean_sheet_renders_exactly_as_it_did_without_findings(self):
        item = case("WDC")
        plain = SimScene(item.program, item.config)
        with_set = SimScene(
            item.program,
            item.config,
            danger=vm.DangerModel.build(item.timeline, item.findings),
        )
        controller = item.controller()
        for _ in range(40):
            controller.step_forward()
            plain.update_from(controller)
            with_set.update_from(controller)
            self.assertEqual(plain.snapshot(), with_set.snapshot())
        self.assertEqual(with_set.mark_keys(), frozenset())
        self.assertEqual(with_set.flagged_features, frozenset())
        self.assertEqual(with_set.flagged_faces, frozenset())

    def test_a_flagged_cuts_feature_shows_the_error_tint_once_it_is_cut(self):
        item = case("CONE")
        scene = self.scene_for("CONE")
        controller = item.controller()
        controller.to_end()
        scene.update_from(controller)
        model = vm.DangerModel.build(item.timeline, item.findings)
        wanted = {
            flag.reveal_key for flag in model.flagged if flag.reveal_key is not None
        }
        self.assertTrue(wanted)
        self.assertEqual(scene.flagged_features, frozenset(wanted))

    def test_a_flagged_cut_with_no_feature_yet_reddens_the_parts_face(self):
        item = case("CONE")
        scene = self.scene_for("CONE")
        controller = item.controller()
        scene.update_from(controller)  # nothing cut yet
        self.assertEqual(scene.flagged_features, frozenset())
        self.assertEqual(scene.flagged_faces, item.findings.flagged_parts)

    def test_the_tint_moves_from_the_face_to_the_feature_and_back(self):
        item = case("CONE")
        scene = self.scene_for("CONE")
        controller = item.controller()
        slot = next(
            cut
            for cut in item.timeline.cuts
            if cut.index in item.findings.flagged_cuts
            and cut.section == SECTION_WDC_SLOT
        )
        key = vm.cut_reveal_key(slot, item.config)
        controller.seek(slot.end)
        scene.update_from(controller)
        self.assertIn(key, scene.flagged_features)
        controller.seek(slot.start)
        scene.update_from(controller)
        self.assertNotIn(key, scene.flagged_features)
        self.assertIn(slot.part_index, scene.flagged_faces)

    def test_there_is_one_red_bar_per_flagged_move(self):
        item = case("STALE-GAP")
        scene = self.scene_for("STALE-GAP")
        self.assertEqual(len(scene.mark_keys()), len(item.findings.flagged_steps))
        for step in item.findings.flagged_steps:
            self.assertIn(f"mark:{step}", scene.mark_keys())

    def test_a_clean_sheet_has_no_bars_at_all(self):
        scene = self.scene_for("WDC")
        self.assertEqual(scene.mark_keys(), frozenset())

    def test_the_bit_is_red_exactly_while_the_flagged_move_is_next(self):
        item = case("LEAD-IN")
        scene = self.scene_for("LEAD-IN")
        controller = item.controller()
        flagged = item.findings.flagged_steps
        self.assertTrue(flagged)
        for step in range(item.timeline.step_total + 1):
            controller.seek(step)
            scene.update_from(controller)
            self.assertEqual(
                scene.bit_flagged, step in flagged, f"at step {step}"
            )

    def test_the_bit_never_reddens_on_a_clean_sheet(self):
        item = case("WDC")
        scene = self.scene_for("WDC")
        controller = item.controller()
        for step in range(0, item.timeline.step_total, 7):
            controller.seek(step)
            scene.update_from(controller)
            self.assertFalse(scene.bit_flagged)

    def test_an_envelope_appears_and_disappears_with_its_family(self):
        item = case("CONE")
        scene = self.scene_for("CONE")
        self.assertEqual(scene.visible_overlay_keys(), frozenset())

        cones = vm.wdc_cone_overlays(item.program, item.plan, item.config)
        scene.set_overlay_visible(vm.OverlayKind.CONE_REACH, True)
        self.assertEqual(
            scene.visible_overlay_keys(), frozenset(o.key for o in cones)
        )
        self.assertEqual(scene.overlays_shown(), frozenset({vm.OverlayKind.CONE_REACH}))

        scene.set_overlay_visible(vm.OverlayKind.LEAD_IN, True)
        scene.set_overlay_visible(vm.OverlayKind.FENCE, True)
        every = vm.overlays(item.program, item.plan, item.config)
        self.assertEqual(
            scene.visible_overlay_keys(), frozenset(o.key for o in every)
        )

        for kind in vm.OverlayKind:
            scene.set_overlay_visible(kind, False)
        self.assertEqual(scene.visible_overlay_keys(), frozenset())
        self.assertEqual(scene.overlays_shown(), frozenset())

    def test_an_envelope_never_shows_by_itself(self):
        """Playing the whole program must not switch one on."""
        item = case("CONE")
        scene = self.scene_for("CONE")
        controller = item.controller()
        controller.to_end()
        scene.update_from(controller)
        self.assertEqual(scene.visible_overlay_keys(), frozenset())

    def test_an_error_mark_outlines_a_box_and_nothing_else_does(self):
        item = case("WDC")
        scene = SimScene(item.program, item.config)
        self.assertEqual(scene.error_mark_names(), ())
        scene.add_error_mark(item.program.flat_parts()[0].box, "refused")
        self.assertEqual(scene.error_mark_names(), ("refused",))

    def test_two_scenes_over_one_dirty_sheet_show_the_same_thing(self):
        item = case("CONE")
        first = self.scene_for("CONE")
        second = self.scene_for("CONE")
        controller = item.controller()
        for _ in range(0, item.timeline.step_total, 13):
            controller.seek(controller.step_index + 13)
            first.update_from(controller)
            second.update_from(controller)
            self.assertEqual(first.snapshot(), second.snapshot())


# --------------------------------------------------------------------------
# (e) the window
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class WindowFindingsTest(unittest.TestCase):
    def window_for(self, label: str, findings=...) -> "Sim3DWindow":
        item = case(label)
        if findings is ...:
            findings = item.findings
        window = Sim3DWindow(
            item.timeline, create_viewport=no_viewport, findings=findings
        )
        self.addCleanup(window.close)
        return window

    def test_a_clean_sheet_is_indistinguishable_from_milestone_three(self):
        item = case("WDC")
        plain = Sim3DWindow(item.timeline, create_viewport=no_viewport)
        self.addCleanup(plain.close)
        for findings in (None, FindingSet.empty(), item.findings):
            with self.subTest(findings=type(findings).__name__):
                window = self.window_for("WDC", findings)
                self.assertEqual(window.scene.snapshot(), plain.scene.snapshot())
                self.assertEqual(window.banner.text(), "")
                self.assertFalse(window.banner.isVisibleTo(window))
                self.assertEqual(window.findings_list.count(), 0)
                self.assertFalse(window.findings_list.isVisibleTo(window))

    def test_a_clean_window_still_animates_the_way_it_did(self):
        item = case("WDC")
        plain = Sim3DWindow(item.timeline, create_viewport=no_viewport)
        self.addCleanup(plain.close)
        window = self.window_for("WDC", item.findings)
        for _ in range(30):
            plain.step_forward()
            window.step_forward()
            self.assertEqual(window.scene.snapshot(), plain.scene.snapshot())

    def test_the_banner_states_the_count_and_the_verdict(self):
        item = case("CONE")
        window = self.window_for("CONE")
        self.assertTrue(window.banner.isVisibleTo(window))
        self.assertIn(str(item.findings.count), window.banner.text())
        self.assertIn(vm.BANNER_VERDICT, window.banner.text())
        self.assertEqual(window.banner.text(), vm.banner_text(item.findings))

    def test_the_panel_rows_are_the_findings_verbatim_and_in_order(self):
        item = case("OFF-SHEET")
        window = self.window_for("OFF-SHEET")
        self.assertTrue(window.findings_list.isVisibleTo(window))
        self.assertEqual(window.findings_list.count(), item.findings.count)
        for row, finding in enumerate(item.findings.all):
            self.assertEqual(window.findings_list.item(row).text(), finding.display)

    def test_clicking_a_row_seeks_to_its_move_and_pauses(self):
        item = case("CONE")
        window = self.window_for("CONE")
        window.play()
        self.assertTrue(window.playing)
        row = next(
            index
            for index, finding in enumerate(item.findings.all)
            if finding.step_index is not None
        )
        finding = item.findings.all[row]
        window.findings_list.itemClicked.emit(window.findings_list.item(row))
        self.assertFalse(window.playing, "taking the wheel stops playback")
        self.assertEqual(window.controller.step_index, finding.step_index)
        self.assertEqual(window.fraction, 0.0)
        self.assertTrue(window.scene.bit_flagged, "the bit sits on the bad move")

    def test_clicking_every_step_bearing_row_lands_on_its_own_move(self):
        item = case("STALE-GAP")
        window = self.window_for("STALE-GAP")
        for row, finding in enumerate(item.findings.all):
            if finding.step_index is None:
                continue
            window.select_finding(row)
            self.assertEqual(window.controller.step_index, finding.step_index)
            motion = item.timeline.steps[window.controller.step_index]
            self.assertEqual(motion.line_index, finding.line - 1)

    def test_clicking_a_whole_file_row_seeks_nowhere(self):
        item = case("OFF-SHEET")
        window = self.window_for("OFF-SHEET")
        row = next(
            index
            for index, finding in enumerate(item.findings.all)
            if finding.step_index is None
        )
        window.step_forward()
        window.step_forward()
        before = window.controller.step_index
        window.findings_list.itemClicked.emit(window.findings_list.item(row))
        self.assertEqual(window.controller.step_index, before)
        self.assertFalse(window.playing)

    def test_a_row_click_on_a_window_with_no_findings_is_harmless(self):
        window = self.window_for("WDC", None)
        window.select_finding(0)
        self.assertEqual(window.controller.step_index, 0)

    def test_the_toggles_start_off_and_switch_the_envelope_families(self):
        item = case("CONE")
        window = self.window_for("CONE")
        self.assertFalse(window.cone_toggle.isChecked())
        self.assertFalse(window.envelope_toggle.isChecked())
        self.assertEqual(window.scene.visible_overlay_keys(), frozenset())

        window.cone_toggle.setChecked(True)
        self.assertEqual(
            window.scene.overlays_shown(), frozenset(CONE_KINDS)
        )
        self.assertEqual(
            len(window.scene.visible_overlay_keys()), len(item.plan.wdc_slot)
        )

        window.envelope_toggle.setChecked(True)
        self.assertEqual(
            window.scene.overlays_shown(), frozenset(CONE_KINDS + ENVELOPE_KINDS)
        )
        self.assertEqual(
            len(window.scene.visible_overlay_keys()),
            len(vm.overlays(item.program, item.plan, item.config)),
        )

        window.cone_toggle.setChecked(False)
        window.envelope_toggle.setChecked(False)
        self.assertEqual(window.scene.visible_overlay_keys(), frozenset())

    def test_the_scene_shows_the_windows_findings_and_no_others(self):
        item = case("CONE")
        window = self.window_for("CONE")
        self.assertEqual(
            len(window.scene.mark_keys()), len(item.findings.flagged_steps)
        )
        window.to_end()
        model = vm.DangerModel.build(item.timeline, item.findings)
        self.assertEqual(
            window.scene.flagged_features,
            frozenset(
                flag.reveal_key
                for flag in model.flagged
                if flag.reveal_key is not None
            ),
        )
        self.assertEqual(
            window.scene.flagged_faces,
            frozenset(
                flag.part_index for flag in model.flagged if flag.reveal_key is None
            ),
        )

    def test_a_dirty_window_paints_offscreen(self):
        window = self.window_for("CONE")
        window.resize(1100, 800)
        self.assertFalse(window.grab().isNull())

    def test_the_window_never_verifies_anything_by_itself(self):
        """A window handed no findings claims nothing about the program.

        The fixture here is a sheet that WOULD verify dirty: nothing red may
        appear, because judging it was not this window's job.
        """
        item = case("CONE")
        window = self.window_for("CONE", None)
        self.assertIsNone(window.findings)
        self.assertEqual(window.banner.text(), "")
        self.assertEqual(window.findings_list.count(), 0)
        window.to_end()
        self.assertEqual(window.scene.mark_keys(), frozenset())
        self.assertEqual(window.scene.flagged_features, frozenset())
        self.assertEqual(window.scene.flagged_faces, frozenset())
        self.assertFalse(window.scene.bit_flagged)
        self.assertTrue(item.findings.count, "the sheet really is dirty")


# --------------------------------------------------------------------------
# (f) the refused sheet
# --------------------------------------------------------------------------


def refusal_layout() -> tuple[SheetLayout, list]:
    """A WDC frame with a neighbour 0.5 past its stile end.

    0.5 clears the 0.455 part gap and is still inside the slot's 0.875 end
    reach, so this is a layout somebody could hand-place and the planner has
    to refuse it (``tests/test_nc_job.py`` pins the same fact from the
    optimizer's side).
    """
    layout = SheetLayout(
        [
            Placement("WDC2436", 4.0, 4.0, 18.0, 36.0),
            Placement("W2436", 4.0, 40.5, 24.0, 36.0),
        ]
    )
    demand = [PartSpec("WDC2436", 18.0, 36.0, 1), PartSpec("W2436", 24.0, 36.0, 1)]
    return layout, demand


def refusal() -> WdcNotSupportedError:
    layout, demand = refusal_layout()
    try:
        plan_sheet(
            layout, ProgramHeader(name="R990301N", created=CREATED), demand,
            NestingConfig(),
        )
    except WdcNotSupportedError as exc:
        return exc
    raise AssertionError("plan_sheet was supposed to refuse this sheet")


class RefusalErrorTest(unittest.TestCase):
    """The refusal now says which part it is about, and says it the same way."""

    def test_the_message_still_reads_exactly_as_it_did(self):
        message = str(refusal())
        for fragment in ("WDC2436", "W2436", "T17", "0.875"):
            self.assertIn(fragment, message)
        self.assertTrue(
            message.startswith("refusing to generate NC for this sheet:"), message
        )

    def test_it_carries_the_part_and_the_footprint_it_refuses(self):
        error = refusal()
        layout, demand = refusal_layout()
        program = program_of(layout, demand, "R990301N")
        wdc = next(
            part for part in program.flat_parts() if is_wdc(part.part_number)
        )
        self.assertEqual(error.part_number, wdc.part_number)
        self.assertEqual(error.box, wdc.box)

    def test_the_edge_refusal_carries_them_too(self):
        layout = SheetLayout([Placement("WDC2436", 4.0, 0.5, 18.0, 36.0)])
        demand = [PartSpec("WDC2436", 18.0, 36.0, 1)]
        with self.assertRaises(WdcNotSupportedError) as caught:
            plan_sheet(
                layout, ProgramHeader(name="R990302N", created=CREATED), demand,
                NestingConfig(),
            )
        self.assertEqual(caught.exception.part_number, "WDC2436")
        self.assertEqual(caught.exception.box, Box(4.0, 0.5, 22.0, 36.5))
        self.assertIn("off the 49x97 sheet", str(caught.exception))

    def test_a_refusal_that_knows_no_part_carries_none(self):
        with self.assertRaises(SheetPlanError) as caught:
            plan_sheet(
                SheetLayout([]),
                ProgramHeader(name="R990303N", created=CREATED),
                [],
                NestingConfig(),
            )
        self.assertIsNone(caught.exception.part_number)
        self.assertIsNone(caught.exception.box)

    def test_the_old_one_argument_constructor_still_works(self):
        error = SheetPlanError("no part on this sheet has a routed opening")
        self.assertEqual(str(error), "no part on this sheet has a routed opening")
        self.assertIsNone(error.part_number)
        self.assertIsNone(error.box)
        self.assertIsInstance(WdcNotSupportedError("bare"), SheetPlanError)


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class RefusalViewTest(unittest.TestCase):
    def build(self, error, program=None):
        view = RefusalView(
            error, program, default_config(), create_viewport=no_viewport
        )
        self.addCleanup(view.close)
        return view

    def test_it_shows_the_refusals_own_words_and_nothing_added(self):
        error = refusal()
        layout, demand = refusal_layout()
        view = self.build(error, program_of(layout, demand, "R990301N"))
        self.assertEqual(view.banner.text(), str(error))

    def test_it_outlines_the_part_the_refusal_names(self):
        error = refusal()
        layout, demand = refusal_layout()
        program = program_of(layout, demand, "R990301N")
        view = self.build(error, program)
        self.assertEqual(view.marked_part, error.box)
        self.assertEqual(view.scene.error_mark_names(), (ERROR_MARK_NAME,))
        self.assertEqual(len(view.scene.part_groups), len(program.flat_parts()))

    def test_it_draws_the_sheet_the_way_playback_would(self):
        layout, demand = refusal_layout()
        program = program_of(layout, demand, "R990301N")
        view = self.build(refusal(), program)
        twin = SimScene(program, default_config())
        self.assertEqual(
            len(view.scene.part_groups), len(twin.part_groups)
        )
        self.assertEqual(view.scene.visible_keys(), twin.visible_keys())
        self.assertEqual(view.scene.freed, frozenset())

    def test_an_error_naming_no_part_gets_no_outline(self):
        layout, demand = refusal_layout()
        program = program_of(layout, demand, "R990301N")
        view = self.build(SheetPlanError("the sheet is empty"), program)
        self.assertIsNone(view.marked_part)
        self.assertEqual(view.scene.error_mark_names(), ())
        self.assertEqual(view.banner.text(), "the sheet is empty")

    def test_a_part_named_without_a_box_is_found_on_the_sheet(self):
        layout, demand = refusal_layout()
        program = program_of(layout, demand, "R990301N")
        wdc = next(p for p in program.flat_parts() if is_wdc(p.part_number))
        error = SheetPlanError("re-nest this sheet", part_number=wdc.part_number)
        view = self.build(error, program)
        self.assertEqual(view.marked_part, wdc.box)

    def test_with_no_program_at_all_it_is_still_a_view(self):
        error = refusal()
        view = RefusalView(error)
        self.addCleanup(view.close)
        self.assertIsNone(view.scene)
        self.assertEqual(view.banner.text(), str(error))
        self.assertIsNone(view.marked_part)
        self.assertEqual(view.viewport_widget.text(), NO_PROGRAM_TEXT)
        self.assertFalse(view.cone_toggle.isEnabled())
        view.show_overlays(CONE_KINDS, True)  # must not raise

    def test_it_paints_offscreen(self):
        layout, demand = refusal_layout()
        view = self.build(refusal(), program_of(layout, demand, "R990301N"))
        view.resize(900, 700)
        self.assertFalse(view.grab().isNull())

    def test_the_toggles_show_the_reach_that_took_the_room(self):
        """The reason this view exists: a cone refusal is a cone-reach fact.

        There is no plan behind a refused sheet, so the cone reaches come from
        the sheet itself (both stiles of every WDC frame) and the lead-in
        envelopes — which are a property of loops nobody wrote — are absent.
        """
        layout, demand = refusal_layout()
        program = program_of(layout, demand, "R990301N")
        config = default_config()
        view = self.build(refusal(), program)
        self.assertEqual(view.scene.visible_overlay_keys(), frozenset())

        view.cone_toggle.setChecked(True)
        cones = vm.wdc_cone_overlays(program, None, config)
        self.assertEqual(len(cones), 2, "one per stile of the one WDC frame")
        self.assertEqual(
            view.scene.visible_overlay_keys(), frozenset(o.key for o in cones)
        )
        self.assertEqual(view.scene.overlays_shown(), frozenset(CONE_KINDS))

        view.envelope_toggle.setChecked(True)
        self.assertEqual(
            view.scene.visible_overlay_keys(),
            frozenset([o.key for o in cones] + [vm.sheet_fence_overlay(config).key]),
        )

        view.cone_toggle.setChecked(False)
        view.envelope_toggle.setChecked(False)
        self.assertEqual(view.scene.visible_overlay_keys(), frozenset())

    def test_the_cone_reach_shown_is_the_one_that_overlaps_the_neighbour(self):
        """The picture explains the sentence: the swept cone reaches the part."""
        layout, demand = refusal_layout()
        program = program_of(layout, demand, "R990301N")
        config = default_config()
        neighbour = next(
            part for part in program.flat_parts() if not is_wdc(part.part_number)
        )
        touching = [
            overlay
            for overlay in vm.wdc_cone_overlays(program, None, config)
            if overlay.box.overlaps(neighbour.box, TOL)
        ]
        self.assertTrue(touching)


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
