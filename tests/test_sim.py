"""Milestone 2 of the 3D cut simulation: headless playback of a program.

Milestone 1 gave the post a typed motion stream; :mod:`faceframe_cnc.sim`
turns it into the two units an operator works in -- a step (one commanded
move) and a CUT OCCURRENCE (one contiguous run of steps cutting one feature
at one depth) -- plus a cursor over them that knows what has been cut.  There
is no 3D, no Qt and no clock in any of it, and this file proves that by
walking the package's own syntax tree.

Covered here:
  (a) the timeline: occurrences are exactly the planned cuts, once per depth
      pass, tiling the stream with no step in two of them and none in none;
      section spans match the plan with the empty sections dropped; a
      verifier finding's line number still reaches its step;
  (b) labels: built from the plan, the program and the post table -- the
      section's real tool number, the part's own number, a nested frame
      marked as nested, and the onion-skin pass distinguishable from the
      through pass by the Z the table configures;
  (c) stepping: a full forward walk executes every step once and ends at
      100%; cut and section stepping land on boundaries and are mutual
      inverses; everything clamps; ``step_back`` is exact, i.e. the state the
      controller holds at position k is the state recomputed from uncut stock
      at position k, whatever route it took to get there;
  (d) material state: a cut counts only when its LAST move has run; a part is
      skinned by perimeter pass 1 and freed by the last pass; on a nested sheet
      the host is freed only after its passengers, and a nested frame's opening
      is routed while its own slab is still captive (the R720101N fact);
  (d2) the same, on the table a generated sheet is really cut with since the
      2026-08-05 amendments: no onion skin, and both T11 operations run as
      max-bite ladders -- two occurrences per opening and per perimeter, the
      rungs labelled by the bite they take, and only the last perimeter rung
      "through" (:class:`GeneratedTableLadderTest`);
  (e) readouts at the cursor: tool, feed, Z and "cut i of N";
  (f) purity: no Qt / PySide6 / time / datetime / random anywhere under
      ``faceframe_cnc/sim/``, and two identically driven controllers agree.

Every fixture is either a reconstruction of a real reference file or a sheet
the optimizer route (:func:`~faceframe_cnc.post.from_layout.plan_sheet`)
built; no tool number, feed, depth or coordinate is written down in this
module.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import ast
import os
import unittest
from dataclasses import dataclass
from itertools import groupby
from math import hypot

import faceframe_cnc.sim as sim_package
from faceframe_cnc.nesting import NestingConfig, PartSpec, Placement, SheetLayout
from faceframe_cnc.post import (
    CutPlan,
    MotionKind,
    PostConfig,
    ProgramHeader,
    SheetProgram,
    default_config,
    emit,
    plan_sheet,
    reconstruct,
)
from faceframe_cnc.post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_RELEASE,
    SECTION_WDC_SLOT,
)
from faceframe_cnc.sim import (
    MaterialState,
    SimController,
    SimTimeline,
)

NC_DIR = os.path.join(os.path.dirname(__file__), "..", "reference", "nc_files")

CREATED = "01 JAN 27 - 08:00"

REFERENCES = ("R710101N", "R720101N", "R730101N")

TOL = 1e-9


def path_of(name: str) -> str:
    return os.path.join(NC_DIR, f"{name}.anc")


# --------------------------------------------------------------------------
# Fixtures (the pattern of tests/test_motion.py, rebuilt here on purpose:
# a test module that imported another test module's fixtures would make one
# file's failure the other's)
# --------------------------------------------------------------------------


@dataclass
class Case:
    """One (program, plan, config) triple, its timeline and its nesting."""

    label: str
    program: SheetProgram
    plan: CutPlan
    config: PostConfig
    timeline: SimTimeline

    def controller(self) -> SimController:
        return SimController(self.timeline)

    def parents(self) -> list[int | None]:
        """Flat-part index of each part's HOST, or ``None`` (spec 4b nesting)."""
        flat = self.program.flat_parts()
        index_of = {id(part): i for i, part in enumerate(flat)}
        parent: dict[int, int | None] = {id(p): None for p in flat}

        def walk(items, host):
            for part in items:
                parent[id(part)] = host
                walk(part.children, index_of[id(part)])

        walk(self.program.parts, None)
        return [parent[id(part)] for part in flat]


def wdc_case() -> tuple[SheetProgram, CutPlan]:
    """A planner-built sheet with a WDC frame on it, so a T17 section exists.

    The layout ``tests/test_nc_job.py`` and ``tests/test_motion.py`` both use:
    a WDC2436 clear of the sheet edge by more than the slot's end reach, with
    an ordinary frame beside it.
    """
    layout = SheetLayout(
        [
            Placement("WDC2436", 4.0, 4.0, 18.0, 36.0),
            Placement("W2436", 4.0, 44.0, 24.0, 36.0),
        ]
    )
    demand = [PartSpec("WDC2436", 18.0, 36.0, 1), PartSpec("W2436", 24.0, 36.0, 1)]
    return plan_sheet(
        layout,
        ProgramHeader(name="R990102N", created=CREATED),
        demand,
        NestingConfig(),
    )


def nested_case() -> tuple[SheetProgram, CutPlan]:
    """A planner-built sheet with a frame nested in another frame's opening.

    A W3012 turned 90 degrees inside a W2742's opening, and a second W3012
    beside it -- planned by :func:`~faceframe_cnc.post.from_layout.plan_sheet`,
    which is what applies the inners-before-hosts order.

    No post table is handed over, so the plan follows the MEASURED one: two
    perimeter passes, the references' dialect.  That is deliberate -- see
    :func:`cases` -- and :class:`GeneratedTableLadderTest` covers the max-bite
    ladders a generated sheet is really cut with.
    """
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
    return plan_sheet(
        layout,
        ProgramHeader(name="R990103N", created=CREATED),
        demand,
        NestingConfig(),
    )


_CASES: dict[str, Case] = {}


def cases() -> dict[str, Case]:
    """Every fixture, built once: reconstructing three files is not free.

    All five are on the MEASURED post table (two perimeter passes).  For the
    three reference files that is the only table that can read them; for the two
    planner-built sheets it is a choice, and the choice is deliberate — the
    timeline is generic over the pass table, and two passes is the case with
    more structure to get wrong (an occurrence per pass, skinned before freed,
    the onion-skin wording).  The table a generated sheet is cut with since the
    2026-08-05 amendments gets its own class,
    :class:`GeneratedTableLadderTest`, because several assertions here are
    specifically about the measured dialect's two passes -- a skin and a through
    pass -- being different events, which a max-bite ladder's two rungs are not.
    """
    if not _CASES:
        sources = [(name, reconstruct(path_of(name))) for name in REFERENCES]
        sources.append(("WDC", wdc_case()))
        sources.append(("NESTED", nested_case()))
        for label, (program, plan) in sources:
            config = default_config()
            _CASES[label] = Case(
                label=label,
                program=program,
                plan=plan,
                config=config,
                timeline=SimTimeline.build(program, plan, config),
            )
    return _CASES


_ONE_PASS: dict[str, object] = {}


def one_pass_case():
    """The nested sample sheet planned the way Generate plans it, built once.

    :func:`cases` uses the MEASURED table (two perimeter passes, no release
    section) because that is the dialect the reference files are in.  This is the
    other table — one through pass and a T12 tab release, which is what
    :func:`~faceframe_cnc.post.from_layout.post_config_for` hands every generated
    sheet since the 2026-08-05 amendment — and the classes that are about the
    amendment use it.
    """
    if not _ONE_PASS:
        from faceframe_cnc.post import post_config_for

        config = post_config_for(NestingConfig())
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
            ProgramHeader(name="R990104N", created=CREATED),
            demand,
            NestingConfig(),
            config,
        )
        _ONE_PASS["case"] = Case(
            label="ONE_PASS",
            program=program,
            plan=plan,
            config=config,
            timeline=SimTimeline.build(program, plan, config),
        )
    return _ONE_PASS["case"]


def one_pass_timeline():
    return one_pass_case().timeline


def case(label: str) -> Case:
    return cases()[label]


def planned_features(plan: CutPlan) -> dict[str, list]:
    """The features each section cuts, in plan order."""
    return {
        SECTION_PANEL: list(plan.panel),
        SECTION_WDC_SLOT: list(plan.wdc_slot),
        SECTION_OPENINGS: list(plan.openings),
        SECTION_DETAIL: list(plan.detail_order()),
        SECTION_PERIMETER: [ref for refs in plan.perimeter for ref in refs],
        # The final T12 tab-release section (2026-08-05 amendment, spec §3c):
        # one entry per PROFILE it frees, however many tabs that profile has.
        SECTION_RELEASE: list(plan.release),
    }


def planned_sections(plan: CutPlan) -> list[str]:
    """The sections that will be emitted: the plan's order, empties dropped."""
    features = planned_features(plan)
    return [section for section in plan.sections if features[section]]


def planned_occurrences(item: Case) -> list[tuple[str, object, int | None]]:
    """``(section, feature, pass index)`` for every cut the plan asks for.

    The plan names a perimeter once per depth pass and a WDC slot once for
    both of its bites, so this is where the "one occurrence per (feature,
    depth pass)" contract is written down independently of the code that
    groups the stream.
    """
    plan = item.plan
    config = item.config
    features = planned_features(plan)
    expected: list[tuple[str, object, int | None]] = []
    for section in planned_sections(plan):
        if section == SECTION_WDC_SLOT:
            for ref in plan.wdc_slot:
                for position in range(len(config.wdc_slot.z_cuts)):
                    expected.append((section, ref, position))
        elif section == SECTION_PERIMETER:
            for position, refs in enumerate(plan.perimeter):
                for ref in refs:
                    expected.append((section, ref, position))
        else:
            for ref in features[section]:
                expected.append((section, ref, None))
    return expected


def runs(values) -> list:
    """Consecutive duplicates collapsed: the order things happened IN."""
    return [key for key, _ in groupby(values)]


def cut_steps(item: Case, cut):
    """The motions of one occurrence, with their step indices."""
    return [
        (index, item.timeline.steps[index])
        for index in range(cut.first_step, cut.last_step + 1)
    ]


# --------------------------------------------------------------------------
# (a) the timeline
# --------------------------------------------------------------------------


class TimelineTest(unittest.TestCase):
    """The occurrences are the plan's cuts, and they tile the stream."""

    def test_occurrences_are_the_planned_cuts_once_per_depth_pass(self):
        """Order AND count, against the plan rather than against the stream.

        A perimeter :class:`~faceframe_cnc.post.model.FeatureRef` is cut once
        per configured depth pass and one WDC slot entry is cut once per
        configured bite, so a sheet's occurrence count is not its feature
        count.
        """
        for label, item in cases().items():
            with self.subTest(case=label):
                self.assertEqual(
                    [(c.section, c.feature, c.pass_index) for c in item.timeline.cuts],
                    planned_occurrences(item),
                )

    def test_a_perimeter_reference_yields_one_occurrence_per_pass(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                passes = len(item.config.perimeter_passes)
                self.assertGreater(passes, 1, "the measured table has two passes")
                for ref in item.plan.perimeter[0]:
                    found = [
                        c
                        for c in item.timeline.cuts
                        if c.section == SECTION_PERIMETER and c.feature == ref
                    ]
                    self.assertEqual([c.pass_index for c in found], list(range(passes)))

    def test_a_wdc_slot_reference_yields_one_occurrence_per_depth_bite(self):
        item = case("WDC")
        bites = len(item.config.wdc_slot.z_cuts)
        self.assertGreater(bites, 1, "the measured table takes two bites")
        self.assertTrue(item.plan.wdc_slot, "fixture must hold a WDC frame")
        for ref in item.plan.wdc_slot:
            found = [
                c
                for c in item.timeline.cuts
                if c.section == SECTION_WDC_SLOT and c.feature == ref
            ]
            self.assertEqual([c.pass_index for c in found], list(range(bites)))

    def test_every_step_belongs_to_exactly_one_occurrence(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                timeline = item.timeline
                self.assertEqual(len(timeline.cut_of_step), timeline.step_total)
                owner = [None] * timeline.step_total
                for cut in timeline.cuts:
                    for index in range(cut.first_step, cut.last_step + 1):
                        self.assertIsNone(owner[index], f"step {index} in two cuts")
                        owner[index] = cut.index
                self.assertEqual(owner, list(timeline.cut_of_step))
                self.assertNotIn(None, owner, "a step in no cut at all")

    def test_occurrence_spans_tile_the_stream_end_to_end(self):
        """Contiguous and gapless: cut i starts where cut i-1 ended.

        The controller's cut stepping is only reversible because of this --
        ``next_cut`` lands on an end boundary and ``prev_cut`` on a start
        boundary, and they are the same number.
        """
        for label, item in cases().items():
            with self.subTest(case=label):
                cuts = item.timeline.cuts
                self.assertTrue(cuts)
                self.assertEqual(cuts[0].first_step, 0)
                self.assertEqual(cuts[-1].last_step, item.timeline.step_total - 1)
                for previous, cut in zip(cuts, cuts[1:]):
                    self.assertEqual(cut.first_step, previous.last_step + 1)
                    self.assertEqual(cut.start, previous.end)
                for index, cut in enumerate(cuts):
                    self.assertEqual(cut.index, index)
                    self.assertGreaterEqual(cut.step_count, 1)

    def test_sections_are_the_plans_sections_with_the_empty_ones_dropped(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                timeline = item.timeline
                self.assertEqual(
                    [span.section for span in timeline.sections],
                    planned_sections(item.plan),
                )
                self.assertEqual(
                    [span.section for span in timeline.sections],
                    runs(m.section for m in timeline.steps),
                )
                for previous, span in zip(timeline.sections, timeline.sections[1:]):
                    self.assertEqual(span.first_step, previous.last_step + 1)
                self.assertEqual(timeline.sections[0].first_step, 0)
                self.assertEqual(
                    timeline.sections[-1].last_step, timeline.step_total - 1
                )

    def test_a_section_span_names_the_cuts_it_covers(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                timeline = item.timeline
                for span in timeline.sections:
                    covered = {
                        timeline.cut_of_step[index]
                        for index in range(span.first_step, span.last_step + 1)
                    }
                    self.assertEqual(
                        covered, set(range(span.first_cut, span.last_cut + 1))
                    )
                    for index in range(span.first_cut, span.last_cut + 1):
                        self.assertEqual(timeline.cuts[index].section, span.section)

    def test_the_totals_are_the_two_the_readout_needs(self):
        """"cut 34 of 512" needs both counts, and both come off the stream."""
        for label, item in cases().items():
            with self.subTest(case=label):
                timeline = item.timeline
                emitted = emit(item.program, item.plan, item.config)
                self.assertEqual(timeline.step_total, len(emitted.motions))
                self.assertEqual(timeline.steps, emitted.motions)
                self.assertEqual(timeline.cut_total, len(timeline.cuts))
                self.assertLess(
                    timeline.cut_total,
                    timeline.step_total,
                    "a cut is many moves; the two units are not the same unit",
                )

    def test_a_line_number_still_reaches_its_step(self):
        """Milestone 4 maps a verifier finding onto a step through its line.

        The verifier cites 1-based line numbers, so the hop is
        ``step_for_line(line - 1)``; a line that commands no move has no step.
        """
        for label, item in cases().items():
            with self.subTest(case=label):
                timeline = item.timeline
                for index, motion in enumerate(timeline.steps):
                    self.assertEqual(timeline.step_for_line(motion.line_index), index)
                moveless = [
                    event.line_index
                    for event in timeline.emitted.events
                    if event.motion is None
                ]
                self.assertTrue(moveless, "the header alone is several lines")
                for line_index in moveless:
                    self.assertIsNone(timeline.step_for_line(line_index))

    def test_step_lengths_are_the_travel_the_line_commands(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                timeline = item.timeline
                self.assertEqual(len(timeline.xy_lengths), timeline.step_total)
                self.assertEqual(len(timeline.path_lengths), timeline.step_total)
                for index, motion in enumerate(timeline.steps):
                    xy = hypot(motion.to_x - motion.from_x, motion.to_y - motion.from_y)
                    self.assertAlmostEqual(
                        timeline.xy_lengths[index], xy, delta=TOL
                    )
                    self.assertGreaterEqual(
                        timeline.path_lengths[index] + TOL, timeline.xy_lengths[index]
                    )
                    if motion.from_z is None or motion.to_z is None:
                        self.assertAlmostEqual(
                            timeline.path_lengths[index],
                            timeline.xy_lengths[index],
                            delta=TOL,
                            msg="an unknown Z has not travelled",
                        )
                    else:
                        self.assertAlmostEqual(
                            timeline.path_lengths[index],
                            hypot(xy, motion.to_z - motion.from_z),
                            delta=TOL,
                        )

    def test_a_groove_cut_step_is_as_long_as_the_groove(self):
        """The clamped T13 stile groove, read back out of the step geometry.

        An upright part's stile groove runs the part's full length less one
        tool radius at each end since the 2026-08-05 amendment (job R0805 --
        the SWEPT cut is what runs the full length, flush to flush), so the
        one FEED step of that occurrence travels exactly that far, which is
        the check that these lengths are the commanded path and not a bounding
        box.
        """
        item = case("WDC")
        parts = item.program.flat_parts()
        target = next(
            cut
            for cut in item.timeline.cuts
            if cut.section == SECTION_PANEL
            and cut.feature.index == 0
            and not parts[cut.part_index].rotated
        )
        feeds = [
            (index, motion)
            for index, motion in cut_steps(item, target)
            if motion.kind is MotionKind.FEED
        ]
        self.assertEqual(len(feeds), 1, "a groove is one straight cut move")
        index, _ = feeds[0]
        box = parts[target.part_index].box
        radius = item.config.tool(SECTION_PANEL).radius
        self.assertAlmostEqual(
            item.timeline.xy_lengths[index],
            box.height - 2 * radius,
            delta=TOL,
        )

    def test_two_builds_of_one_sheet_are_the_same_timeline(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                twin = SimTimeline.build(item.program, item.plan, item.config)
                self.assertEqual(twin.steps, item.timeline.steps)
                self.assertEqual(twin.cuts, item.timeline.cuts)
                self.assertEqual(twin.sections, item.timeline.sections)


# --------------------------------------------------------------------------
# (b) labels
# --------------------------------------------------------------------------


class LabelTest(unittest.TestCase):
    """Every word of a label comes from the plan, the program or the table."""

    def test_every_label_names_its_own_tool_and_part(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                for cut in item.timeline.cuts:
                    tool = item.config.tool(cut.section).number
                    self.assertIn(f"T{tool}", cut.label, cut.label)
                    self.assertIn(cut.part_number, cut.label, cut.label)
                    self.assertEqual(
                        cut.part_number,
                        item.program.flat_parts()[cut.part_index].part_number,
                    )

    def test_a_nested_frames_label_says_it_is_nested(self):
        """The one thing about a nested sheet that surprises people."""
        for label in ("R720101N", "NESTED"):
            with self.subTest(case=label):
                item = case(label)
                parents = item.parents()
                self.assertTrue(any(p is not None for p in parents))
                nested = [c for c in item.timeline.cuts if c.nested]
                self.assertTrue(nested, "fixture must actually nest something")
                for cut in item.timeline.cuts:
                    self.assertEqual(
                        cut.nested,
                        parents[cut.part_index] is not None,
                        cut.label,
                    )
                    self.assertEqual("(nested)" in cut.label, cut.nested, cut.label)

    def test_the_onion_skin_pass_reads_differently_from_the_through_pass(self):
        """Pass 1 leaves the measured 0.06 skin; pass 2 cuts through.

        The wording comes from the configured Z against the stock, so a
        table that stopped leaving a skin would stop saying it did.
        """
        for label, item in cases().items():
            with self.subTest(case=label):
                by_pass: dict[int, set[str]] = {}
                for cut in item.timeline.cuts:
                    if cut.section != SECTION_PERIMETER:
                        continue
                    by_pass.setdefault(cut.pass_index, set()).add(cut.label)
                last = len(item.config.perimeter_passes) - 1
                for position, labels in by_pass.items():
                    for text in labels:
                        if position == last:
                            self.assertIn("through", text)
                            self.assertNotIn("onion skin", text)
                        else:
                            self.assertIn("onion skin", text)
                            self.assertNotIn("through", text)
                # ... and the same part's two passes are never the same words.
                for cut in item.timeline.cuts:
                    if cut.section != SECTION_PERIMETER or cut.pass_index != 0:
                        continue
                    twin = next(
                        c
                        for c in item.timeline.cuts
                        if c.section == SECTION_PERIMETER
                        and c.feature == cut.feature
                        and c.pass_index == last
                    )
                    self.assertNotEqual(cut.label, twin.label)

    def test_the_onion_skin_label_states_the_skin_it_leaves(self):
        item = case("WDC")
        first = item.config.perimeter_passes[0]
        thickness = first.z_cut - (
            item.config.stock_top_z - item.config.material_thickness
        )
        cut = next(
            c
            for c in item.timeline.cuts
            if c.section == SECTION_PERIMETER and c.pass_index == 0
        )
        self.assertIn(f"onion skin {thickness:g} thick", cut.label)

    def test_a_wdc_slots_two_bites_are_labelled_as_two_passes(self):
        item = case("WDC")
        bites = len(item.config.wdc_slot.z_cuts)
        slots = [c for c in item.timeline.cuts if c.section == SECTION_WDC_SLOT]
        self.assertEqual(len(slots), bites * len(item.plan.wdc_slot))
        for cut in slots:
            self.assertIn(f"pass {cut.pass_index + 1} of {bites}", cut.label)
            self.assertIn("stile slot", cut.label)
        self.assertEqual(
            len({c.label for c in slots}), len(slots), "two bites, two labels"
        )

    def test_a_groove_label_names_the_pattern_position_it_cuts(self):
        """A WDC frame gives its stile grooves up to the T17 slot.

        Its T13 section therefore cuts only the rail pair, and the labels say
        which of the four pattern positions those are rather than renumbering
        them 1 and 2.
        """
        item = case("WDC")
        parts = item.program.flat_parts()
        wdc = next(
            i for i, part in enumerate(parts) if part.part_number.startswith("WDC")
        )
        grooves = [
            cut
            for cut in item.timeline.cuts
            if cut.section == SECTION_PANEL and cut.part_index == wdc
        ]
        self.assertEqual([cut.feature.index for cut in grooves], [1, 3])
        self.assertIn("groove 2 of 4 (rail, low side)", grooves[0].label)
        self.assertIn("groove 4 of 4 (rail, high side)", grooves[1].label)

    def test_the_opening_and_its_detail_pass_read_as_two_different_cuts(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                openings = [
                    c for c in item.timeline.cuts if c.section == SECTION_OPENINGS
                ]
                detail = [c for c in item.timeline.cuts if c.section == SECTION_DETAIL]
                self.assertTrue(openings and detail)
                self.assertFalse(
                    {c.label for c in openings} & {c.label for c in detail},
                    "the T11 rough and the T12 finish must not read alike",
                )


# --------------------------------------------------------------------------
# (c) stepping
# --------------------------------------------------------------------------


class SteppingTest(unittest.TestCase):
    """The cursor sits between steps, clamps at the ends, and is reversible."""

    def test_a_full_forward_walk_executes_every_step_once(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                self.assertEqual(controller.step_index, 0)
                self.assertEqual(controller.progress, 0.0)
                seen = []
                while controller.step_forward():
                    seen.append(controller.last_motion)
                self.assertEqual(seen, list(item.timeline.steps))
                self.assertEqual(controller.step_index, controller.step_total)
                self.assertEqual(controller.progress, 1.0)
                self.assertTrue(controller.at_end)
                self.assertIsNone(controller.current_motion)

    def test_next_cut_from_the_start_lands_on_the_first_cuts_end(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                first = item.timeline.cuts[0]
                self.assertIs(controller.current_cut, first)
                self.assertTrue(controller.next_cut())
                self.assertEqual(controller.step_index, first.end)
                self.assertEqual(controller.completed_cuts, 1)

    def test_seek_cut_then_next_cut_executes_exactly_that_cut(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                for cut in item.timeline.cuts:
                    controller.seek_cut(cut.index)
                    self.assertEqual(controller.step_index, cut.first_step)
                    self.assertIs(controller.current_cut, cut)
                    self.assertIs(controller.current_motion, item.timeline.steps[cut.first_step])
                    before = controller.step_index
                    controller.next_cut()
                    self.assertEqual(controller.step_index - before, cut.step_count)
                    self.assertEqual(controller.step_index, cut.last_step + 1)

    def test_cut_stepping_is_reversible(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                boundaries = []
                while controller.next_cut():
                    boundaries.append(controller.step_index)
                self.assertEqual(
                    boundaries, [cut.end for cut in item.timeline.cuts]
                )
                backwards = []
                while controller.prev_cut():
                    backwards.append(controller.step_index)
                self.assertEqual(
                    backwards,
                    [cut.start for cut in reversed(item.timeline.cuts)],
                )
                self.assertEqual(controller.step_index, 0)

    def test_section_stepping_lands_on_section_boundaries(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                ends = []
                while controller.next_section():
                    ends.append(controller.step_index)
                self.assertEqual(
                    ends, [span.end for span in item.timeline.sections]
                )
                starts = []
                while controller.prev_section():
                    starts.append(controller.step_index)
                self.assertEqual(
                    starts,
                    [span.start for span in reversed(item.timeline.sections)],
                )
                self.assertEqual(controller.step_index, 0)

    def test_a_section_boundary_is_where_the_tool_changes(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                for span in item.timeline.sections:
                    controller.seek(span.start)
                    self.assertEqual(controller.section, span.section)
                    self.assertEqual(
                        controller.tool, item.config.tool(span.section)
                    )

    def test_everything_clamps_at_both_ends(self):
        """Holding a key down at the end of the program is not an error."""
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                self.assertFalse(controller.step_back())
                self.assertFalse(controller.prev_cut())
                self.assertFalse(controller.prev_section())
                self.assertFalse(controller.reset())
                self.assertEqual(controller.step_index, 0)
                self.assertFalse(controller.seek(-10))
                self.assertEqual(controller.step_index, 0)

                self.assertTrue(controller.to_end())
                self.assertFalse(controller.step_forward())
                self.assertFalse(controller.next_cut())
                self.assertFalse(controller.next_section())
                self.assertFalse(controller.to_end())
                self.assertFalse(controller.seek(controller.step_total + 500))
                self.assertEqual(controller.step_index, controller.step_total)

                controller.seek_cut(-5)
                self.assertEqual(controller.step_index, item.timeline.cuts[0].start)
                controller.seek_cut(item.timeline.cut_total + 5)
                self.assertEqual(controller.step_index, item.timeline.cuts[-1].start)

    def test_step_back_is_the_exact_inverse_of_step_forward(self):
        """Not approximately: the same cursor and the same material state.

        The state is compared against
        :meth:`~faceframe_cnc.sim.SimTimeline.state_at`, which refolds from
        uncut stock, so the controller's incremental bookkeeping cannot drift
        no matter which direction it was driven.
        """
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                forward = []
                while True:
                    forward.append(
                        (controller.step_index, controller.state, controller.position)
                    )
                    self.assertEqual(
                        controller.state,
                        item.timeline.state_at(controller.step_index),
                    )
                    if not controller.step_forward():
                        break
                backward = []
                while True:
                    backward.append(
                        (controller.step_index, controller.state, controller.position)
                    )
                    self.assertEqual(
                        controller.state,
                        item.timeline.state_at(controller.step_index),
                    )
                    if not controller.step_back():
                        break
                self.assertEqual(backward, list(reversed(forward)))

    def test_a_jump_agrees_with_a_walk(self):
        """``seek`` is not a different machine from ``step_forward``."""
        for label, item in cases().items():
            with self.subTest(case=label):
                walker = item.controller()
                jumper = item.controller()
                total = item.timeline.step_total
                for target in (0, 1, total // 3, total // 2, total - 1, total, 7):
                    walker.reset()
                    for _ in range(target):
                        walker.step_forward()
                    jumper.seek(target)
                    self.assertEqual(walker.step_index, jumper.step_index)
                    self.assertEqual(walker.state, jumper.state)
                    self.assertEqual(walker.position, jumper.position)
                    self.assertEqual(walker.cut_index, jumper.cut_index)


# --------------------------------------------------------------------------
# (d) material state
# --------------------------------------------------------------------------


class MaterialStateTest(unittest.TestCase):
    """What has been cut at the cursor, and only what has been cut."""

    def test_nothing_is_cut_before_the_program_starts(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                self.assertEqual(
                    controller.state, MaterialState.empty(item.timeline.part_count)
                )
                self.assertEqual(len(controller.state), len(item.program.flat_parts()))
                for part in controller.state.parts:
                    self.assertFalse(part.touched)

    def test_a_cut_counts_only_once_its_last_move_has_run(self):
        """A groove being cut is not a groove that is cut.

        The cursor is walked through one whole T13 occurrence: the groove
        joins ``grooves_cut`` at the boundary AFTER its retract, not at the
        plunge, not at the cut move.
        """
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                cut = next(
                    c for c in item.timeline.cuts if c.section == SECTION_PANEL
                )
                for position in range(cut.first_step, cut.end):
                    controller.seek(position)
                    self.assertNotIn(
                        cut.feature.index,
                        controller.state[cut.part_index].grooves_cut,
                        f"{label}: groove counted at step {position} of "
                        f"{cut.first_step}..{cut.last_step}",
                    )
                    self.assertIs(controller.current_cut, cut)
                controller.seek(cut.end)
                self.assertIn(
                    cut.feature.index, controller.state[cut.part_index].grooves_cut
                )

    def test_every_grooves_last_move_is_the_one_that_records_it(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                for cut in item.timeline.cuts:
                    if cut.section != SECTION_PANEL:
                        continue
                    controller.seek(cut.last_step)
                    before = controller.state[cut.part_index].grooves_cut
                    controller.step_forward()
                    after = controller.state[cut.part_index].grooves_cut
                    self.assertEqual(after - before, {cut.feature.index})

    def test_a_part_is_skinned_by_pass_one_and_freed_by_the_last_pass(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                last = len(item.config.perimeter_passes) - 1
                for cut in item.timeline.cuts:
                    if cut.section != SECTION_PERIMETER or cut.pass_index != 0:
                        continue
                    controller.seek(cut.end)
                    state = controller.state[cut.part_index]
                    self.assertTrue(state.skinned, cut.label)
                    self.assertFalse(
                        state.freed,
                        f"{cut.label}: the onion skin still holds this part",
                    )
                    through = next(
                        c
                        for c in item.timeline.cuts
                        if c.section == SECTION_PERIMETER
                        and c.feature == cut.feature
                        and c.pass_index == last
                    )
                    controller.seek(through.end)
                    self.assertTrue(controller.state[cut.part_index].freed)
                    controller.seek(through.last_step)
                    self.assertFalse(
                        controller.state[cut.part_index].freed,
                        "a part is loose when the last move of the last pass is done",
                    )

    def test_no_part_is_freed_before_every_part_is_skinned(self):
        """The onion-skin invariant, asserted through the state model.

        Pass 0 of every perimeter precedes pass 1 of any (the plan guarantees
        it), so at the moment the first part comes loose every part on the
        sheet is already scored to size.
        """
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                perimeter = [
                    c for c in item.timeline.cuts if c.section == SECTION_PERIMETER
                ]
                self.assertEqual(
                    runs(c.pass_index for c in perimeter),
                    list(range(len(item.config.perimeter_passes))),
                )
                first_free = min(
                    c.end for c in perimeter if c.pass_index == len(item.config.perimeter_passes) - 1
                )
                controller.seek(first_free)
                skinned = controller.state.skinned_parts
                cut_parts = {c.part_index for c in perimeter}
                self.assertEqual(skinned, frozenset(cut_parts))
                self.assertEqual(len(controller.state.freed_parts), 1)

    def test_a_host_is_freed_only_after_its_passengers(self):
        """The 2026-08-03 onion-skin order, read off the material state.

        At the step where a host slab comes loose, every frame nested in it is
        already loose -- otherwise the host would carry its passengers away
        while they were still being cut.
        """
        for label in ("R720101N", "NESTED"):
            with self.subTest(case=label):
                item = case(label)
                parents = item.parents()
                self.assertTrue(any(p is not None for p in parents))
                controller = item.controller()
                last = len(item.config.perimeter_passes) - 1
                children: dict[int, list[int]] = {}
                for child, host in enumerate(parents):
                    if host is not None:
                        children.setdefault(host, []).append(child)
                self.assertTrue(children)
                for host, kids in children.items():
                    cut = next(
                        c
                        for c in item.timeline.cuts
                        if c.section == SECTION_PERIMETER
                        and c.pass_index == last
                        and c.part_index == host
                    )
                    controller.seek(cut.last_step)
                    self.assertFalse(controller.state[host].freed)
                    controller.seek(cut.end)
                    self.assertTrue(controller.state[host].freed)
                    for kid in kids:
                        self.assertTrue(
                            controller.state[kid].freed,
                            f"{label}: part {kid} was still attached when its host "
                            f"came loose",
                        )

    def test_a_nested_frames_opening_is_routed_while_its_slab_is_captive(self):
        """The R720101N fact the spec calls out (spec 4b).

        A nested inner's openings are cut while the inner is still part of its
        host's interior waste: at the cursor just past that opening, the inner
        is neither skinned nor freed.
        """
        for label in ("R720101N", "NESTED"):
            with self.subTest(case=label):
                item = case(label)
                parents = item.parents()
                inner = next(i for i, host in enumerate(parents) if host is not None)
                cut = next(
                    c
                    for c in item.timeline.cuts
                    if c.section == SECTION_OPENINGS and c.part_index == inner
                )
                controller = item.controller()
                controller.seek(cut.end)
                state = controller.state[inner]
                self.assertIn(cut.feature.index, state.openings_cut)
                self.assertFalse(state.skinned, "the inner is not even scored yet")
                self.assertFalse(state.freed, "the inner is still captive")
                self.assertFalse(controller.state[parents[inner]].freed)

    def test_the_detail_pass_is_tracked_apart_from_the_opening(self):
        """Between T11 and T12 an opening is roughed but not finished."""
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                openings = next(
                    span
                    for span in item.timeline.sections
                    if span.section == SECTION_OPENINGS
                )
                controller.seek(openings.end)
                for cut in item.timeline.cuts:
                    if cut.section != SECTION_OPENINGS:
                        continue
                    state = controller.state[cut.part_index]
                    self.assertIn(cut.feature.index, state.openings_cut)
                    self.assertNotIn(cut.feature.index, state.openings_detailed)
                detail = next(
                    span
                    for span in item.timeline.sections
                    if span.section == SECTION_DETAIL
                )
                controller.seek(detail.end)
                for cut in item.timeline.cuts:
                    if cut.section != SECTION_DETAIL:
                        continue
                    self.assertIn(
                        cut.feature.index,
                        controller.state[cut.part_index].openings_detailed,
                    )

    def test_the_wdc_slot_records_each_bite_separately(self):
        """Two bites on one centreline are two facts, not one.

        A view drawing the shallow bite must not draw the deep one, so the
        state keys on ``(stile, pass)``.
        """
        item = case("WDC")
        controller = item.controller()
        slots = [c for c in item.timeline.cuts if c.section == SECTION_WDC_SLOT]
        self.assertTrue(slots)
        expected: set[tuple[int, int]] = set()
        for cut in slots:
            controller.seek(cut.last_step)
            self.assertEqual(controller.state[cut.part_index].slots_cut, expected)
            controller.seek(cut.end)
            expected.add((cut.feature.index, cut.pass_index))
            self.assertEqual(controller.state[cut.part_index].slots_cut, expected)
        self.assertEqual(
            len(expected),
            2 * len(item.config.wdc_slot.z_cuts),
            "two stiles, two bites each",
        )

    def test_at_the_end_of_the_program_every_part_is_free(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                controller.to_end()
                self.assertEqual(
                    controller.state.freed_parts,
                    frozenset(range(item.timeline.part_count)),
                )


# --------------------------------------------------------------------------
# (d2) the sheet as the app really simulates it: the max-bite ladder
# --------------------------------------------------------------------------


class GeneratedTableLadderTest(unittest.TestCase):
    """What the app's own post table simulates as, and how it differs.

    :func:`cases` builds its generated fixtures with the measured table, which
    is the dialect the three reference programs are in and the one the
    onion-skin wording above is about.  What Generate and the Simulate button
    actually hand the timeline is
    :func:`~faceframe_cnc.post.from_layout.post_config_for`, and since the
    2026-08-05 amendments that table has NO onion skin (the parts are tab-held
    from milestone 2b on, so the 0.06 skin has no holding job left) and runs both
    T11 operations as max-bite ladders instead (Scott: at most 0.4 of material
    per pass, to reduce the load on the 3/8 comp).  So this class plans the same
    nested sheet the way the app does and states what changes:

    *   two perimeter occurrences per part again — but at Z0.372 and Z-0.006, not
        at the measured dialect's Z0.06 and Z-0.006, and two OPENING occurrences
        per opening where the references have one;
    *   the labels never say "onion skin": a rung of a ladder is described by the
        bite it takes and the material it leaves, and only the last rung of a
        perimeter ladder says "through".  The wording comes from the configured Z
        and the tool's declared bite limit, nothing else;
    *   the through pass SCORES the part and does not free it: the piece is
        held by its tabs until the final T12 release section takes them away
        (2026-08-05 amendment §3d, :mod:`faceframe_cnc.sim.state`'s own words).
        "Scored to size but still held" is now every part's state for most of the
        program instead of no part's.
    """

    @classmethod
    def setUpClass(cls):
        item = one_pass_case()
        cls.config = item.config
        cls.program = item.program
        cls.plan = item.plan
        cls.timeline = item.timeline

    def perimeter_cuts(self):
        return [c for c in self.timeline.cuts if c.section == SECTION_PERIMETER]

    def opening_cuts(self):
        return [c for c in self.timeline.cuts if c.section == SECTION_OPENINGS]

    def test_the_table_under_test_really_is_the_generated_one(self):
        self.assertEqual(
            [p.z_cut for p in self.config.perimeter_passes], [0.372, -0.006]
        )
        self.assertEqual([p.z_cut for p in self.config.openings_passes], [0.45, 0.15])
        self.assertEqual(len(self.plan.perimeter), 2)

    def test_one_perimeter_occurrence_per_part_per_rung(self):
        cuts = self.perimeter_cuts()
        rungs = len(self.config.perimeter_passes)
        self.assertEqual(len(cuts), self.timeline.part_count * rungs)
        self.assertEqual({c.pass_index for c in cuts}, set(range(rungs)))
        self.assertEqual(
            {c.part_index for c in cuts}, set(range(self.timeline.part_count))
        )

    def test_one_opening_occurrence_per_opening_per_rung(self):
        """The openings gained a rung too (flagged for Scott's ratification)."""
        cuts = self.opening_cuts()
        rungs = len(self.config.openings_passes)
        self.assertEqual(len(cuts), len(self.plan.openings) * rungs)
        self.assertEqual({c.pass_index for c in cuts}, set(range(rungs)))

    def test_only_the_last_rung_says_through_and_none_says_onion_skin(self):
        last = len(self.config.perimeter_passes) - 1
        for cut in self.perimeter_cuts():
            with self.subTest(label=cut.label):
                self.assertNotIn("onion skin", cut.label)
                if cut.pass_index == last:
                    self.assertIn("through", cut.label)
                else:
                    # a rung is described by the bite it takes and what it leaves
                    self.assertIn("0.378 deep, 0.372 left", cut.label)

    def test_an_opening_rung_says_which_rung_it_is(self):
        for cut in self.opening_cuts():
            with self.subTest(label=cut.label):
                self.assertNotIn("onion skin", cut.label)
                self.assertIn(f"pass {cut.pass_index + 1} of 2", cut.label)
                self.assertIn("0.3 deep", cut.label)

    def release_cuts(self):
        return [c for c in self.timeline.cuts if c.section == SECTION_RELEASE]

    def test_the_one_pass_scores_the_part_but_does_not_free_it(self):
        """The 2026-08-05 amendment, at the resolution the operator watches.

        The through pass cuts the outline right through and the part still does
        not move: it is held by its tabs.  What frees it is the final T12 release
        section, and until that has run the part is scored and not freed -- which
        is the whole reason the amendment exists (two frames broke because a
        piece was loose while the sheet was still being cut).
        """
        self.assertTrue(self.timeline.tab_held, "a generated sheet is tab-held")
        controller = SimController(self.timeline)
        for cut in self.perimeter_cuts():
            controller.seek(cut.end)
            state = controller.state[cut.part_index]
            with self.subTest(label=cut.label):
                self.assertTrue(state.skinned, "cut to size")
                self.assertFalse(state.freed, "and still held by its tabs")

    def test_the_release_section_is_what_frees_each_part(self):
        controller = SimController(self.timeline)
        perimeter_release = [
            c for c in self.release_cuts() if c.feature.kind == "perimeter"
        ]
        self.assertEqual(len(perimeter_release), self.timeline.part_count)
        for cut in perimeter_release:
            controller.seek(cut.last_step)
            self.assertFalse(
                controller.state[cut.part_index].freed,
                "not until the LAST tab of this profile has gone",
            )
            controller.seek(cut.end)
            self.assertTrue(controller.state[cut.part_index].freed, cut.label)

    def test_a_dropout_is_released_by_its_own_release_cut(self):
        controller = SimController(self.timeline)
        for cut in [c for c in self.release_cuts() if c.feature.kind == "opening"]:
            detail = next(
                c
                for c in self.timeline.cuts
                if c.section == SECTION_DETAIL
                and c.part_index == cut.part_index
                and c.feature.index == cut.feature.index
            )
            controller.seek(detail.end)
            state = controller.state[cut.part_index]
            with self.subTest(label=cut.label):
                self.assertIn(cut.feature.index, state.openings_detailed)
                self.assertNotIn(
                    cut.feature.index,
                    state.openings_released,
                    "the slug is cut through and still hanging on its tabs",
                )
            controller.seek(cut.end)
            self.assertIn(
                cut.feature.index, controller.state[cut.part_index].openings_released
            )

    def test_an_inner_is_still_freed_before_its_host(self):
        """The one ordering rule the single pass still has to honour.

        Freed now means RELEASED, so the relation is read off the release
        section -- which spec 3c orders inners-before-hosts for exactly this
        reason.
        """
        parents = Case(
            "ONE_PASS", self.program, self.plan, self.config, self.timeline
        ).parents()
        self.assertTrue(any(p is not None for p in parents))
        freed_at = {
            c.part_index: c.end
            for c in self.release_cuts()
            if c.feature.kind == "perimeter"
        }
        for child, host in enumerate(parents):
            if host is None:
                continue
            self.assertLess(freed_at[child], freed_at[host])

    def test_nothing_is_left_captive_at_the_end(self):
        controller = SimController(self.timeline)
        controller.to_end()
        self.assertEqual(
            controller.state.freed_parts, frozenset(range(self.timeline.part_count))
        )


class ReleaseSectionTest(unittest.TestCase):
    """The final T12 release, on the timeline (2026-08-05 amendment §3c).

    One occurrence per PROFILE, whatever number of tabs it has, because that is
    what the general grouping rule says (one contiguous run of one feature at one
    depth) and it is the grouping the freed-at-release semantics needs: the
    occurrence completes when the last tab has gone.
    """

    @classmethod
    def setUpClass(cls):
        cls.timeline = one_pass_timeline()

    def release(self):
        return [c for c in self.timeline.cuts if c.section == SECTION_RELEASE]

    def test_the_release_is_the_last_section_of_the_timeline(self):
        self.assertEqual(self.timeline.sections[-1].section, SECTION_RELEASE)
        last = self.timeline.cuts[-1]
        self.assertEqual(last.section, SECTION_RELEASE)
        self.assertEqual(last.feature.kind, "perimeter", "an outermost part, last")

    def test_one_occurrence_per_profile_however_many_tabs(self):
        plan = self.timeline.plan
        self.assertEqual(
            [c.feature.profile for c in self.release()],
            [ref.profile for ref in plan.release],
        )
        for cut in self.release():
            zones = plan.tabs[cut.feature.profile]
            with self.subTest(label=cut.label):
                self.assertGreater(len(zones), 1, "more than one tab per profile")
                # 5 moves per tab: preposition, drop to the ramp plane, plunge,
                # cut, retract -- and the first tab's preposition is two lines
                # (the spindle start and the G43) on the section's first cut.
                self.assertGreaterEqual(cut.step_count, 5 * len(zones))

    def test_the_label_says_what_the_cut_frees_and_how_fast(self):
        for cut in self.release():
            with self.subTest(label=cut.label):
                self.assertTrue(cut.label.startswith("T12 release — frees the "))
                self.assertIn("150 ipm", cut.label)
                self.assertIn(cut.part_number, cut.label)
        kinds = [c.feature.kind for c in self.release()]
        self.assertEqual(kinds, sorted(kinds, key=lambda k: k != "opening"))
        self.assertTrue(
            any("frees the opening" in c.label for c in self.release())
            and any("frees the part" in c.label for c in self.release())
        )

    def test_every_release_step_is_inside_a_cut_occurrence(self):
        """The totality guard, on the newest section."""
        for index, motion in enumerate(self.timeline.steps):
            if motion.section != SECTION_RELEASE:
                continue
            cut = self.timeline.cut_at_step(index)
            self.assertEqual(cut.section, SECTION_RELEASE)
            self.assertIs(cut.feature, motion.feature)


# --------------------------------------------------------------------------
# (e) readouts
# --------------------------------------------------------------------------


class ReadoutTest(unittest.TestCase):
    """What the cursor says is happening."""

    def test_inside_a_groove_the_tool_and_feed_are_the_panel_tables(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                cut = next(
                    c for c in item.timeline.cuts if c.section == SECTION_PANEL
                )
                feed_step = next(
                    index
                    for index, motion in cut_steps(item, cut)
                    if motion.kind is MotionKind.FEED
                )
                controller.seek(feed_step)
                self.assertEqual(controller.section, SECTION_PANEL)
                self.assertEqual(controller.tool, item.config.tool(SECTION_PANEL))
                self.assertAlmostEqual(
                    controller.feed, item.config.panel.cut_feed, delta=TOL
                )
                self.assertIs(controller.current_cut, cut)

    def test_the_plunge_of_a_groove_leaves_the_tool_at_the_groove_depth(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                cut = next(
                    c for c in item.timeline.cuts if c.section == SECTION_PANEL
                )
                plunge = next(
                    index
                    for index, motion in cut_steps(item, cut)
                    if motion.kind is MotionKind.PLUNGE
                )
                controller.seek(plunge)
                self.assertAlmostEqual(
                    controller.feed, item.config.panel.entry_feed, delta=TOL
                )
                controller.step_forward()
                self.assertAlmostEqual(
                    controller.position[2], item.config.panel.z_cut, delta=TOL
                )

    def test_the_position_readout_is_the_last_executed_moves_endpoint(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                self.assertEqual(
                    controller.position,
                    (0.0, 0.0, None),
                    "before the first G43 the work Z is a machine position",
                )
                self.assertIsNone(controller.last_motion)
                while controller.step_forward():
                    motion = controller.last_motion
                    self.assertEqual(
                        controller.position, (motion.to_x, motion.to_y, motion.to_z)
                    )

    def test_cut_i_of_n_counts_the_way_a_readout_needs(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                total = item.timeline.cut_total
                self.assertEqual(controller.cut_total, total)
                self.assertEqual(controller.cut_index, 0)
                seen = [controller.cut_index]
                while controller.next_cut():
                    seen.append(controller.cut_index)
                self.assertEqual(seen, list(range(total + 1)))
                self.assertEqual(
                    controller.cut_index,
                    total,
                    "one past the end when the program is finished, like step_index",
                )
                self.assertIsNone(controller.current_cut)
                self.assertEqual(controller.completed_cuts, total)

    def test_the_readouts_track_the_cut_in_progress(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                for cut in item.timeline.cuts:
                    for position in range(cut.first_step, cut.end):
                        controller.seek(position)
                        self.assertIs(controller.current_cut, cut)
                        self.assertEqual(controller.cut_index, cut.index)
                        self.assertEqual(controller.section, cut.section)
                        self.assertEqual(
                            controller.tool, item.config.tool(cut.section)
                        )

    def test_at_the_end_the_tool_is_the_last_one_used(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                controller = item.controller()
                controller.to_end()
                last = item.timeline.steps[-1]
                self.assertIsNone(controller.current_motion)
                self.assertIs(controller.last_motion, last)
                self.assertEqual(controller.tool, last.tool)
                self.assertEqual(controller.section, last.section)
                self.assertIsNone(controller.feed)


# --------------------------------------------------------------------------
# (f) purity and determinism
# --------------------------------------------------------------------------

#: A GUI toolkit or a clock in the simulation model would make it undrawable
#: from a test and unsteppable from a keyboard.  ``time``/``datetime`` are
#: banned outright rather than "used carefully": a simulation that measures
#: elapsed time cannot be replayed, and this package deliberately provides
#: geometry (path lengths) and lets the view own the animation.
FORBIDDEN_ROOTS = frozenset(
    {
        "PySide6",
        "PySide2",
        "PyQt5",
        "PyQt6",
        "shiboken2",
        "shiboken6",
        "time",
        "datetime",
        "random",
        "secrets",
    }
)

#: The sim package models the machine, not the app; reaching into the GUI
#: layer would invert the dependency the whole design rests on.
FORBIDDEN_MODULES = ("faceframe_cnc.gui",)


def sim_sources() -> list[str]:
    directory = os.path.dirname(sim_package.__file__)
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(".py")
    )


def imported_modules(source: str) -> list[str]:
    """Every module name a file imports, dotted and absolute where stated."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import cannot name anything outside this package.
                continue
            if node.module:
                names.append(node.module)
    return names


class PurityTest(unittest.TestCase):
    """The simulation model is stdlib, headless and clock-free."""

    def test_the_package_has_the_modules_this_test_thinks_it_has(self):
        """So the AST sweep below cannot pass by finding nothing."""
        found = {os.path.basename(path) for path in sim_sources()}
        self.assertEqual(
            found,
            {
                "__init__.py",
                "timeline.py",
                "state.py",
                "controller.py",
                # Milestone 4: the verifier's findings located on the timeline.
                # Named here so the AST sweeps below cover it too.
                "findings.py",
            },
        )

    def test_no_gui_toolkit_and_no_wall_clock_is_imported(self):
        for path in sim_sources():
            with self.subTest(module=os.path.basename(path)):
                with open(path, "r", encoding="utf-8") as handle:
                    modules = imported_modules(handle.read())
                for module in modules:
                    root = module.split(".")[0]
                    self.assertNotIn(
                        root,
                        FORBIDDEN_ROOTS,
                        f"{os.path.basename(path)} imports {module}",
                    )
                    for forbidden in FORBIDDEN_MODULES:
                        self.assertFalse(
                            module == forbidden or module.startswith(forbidden + "."),
                            f"{os.path.basename(path)} imports {module}",
                        )

    def test_the_package_imports_no_third_party_dependency(self):
        """Stdlib and this project only -- the simulation adds no dependency."""
        allowed_roots = {"faceframe_cnc", "__future__"}
        stdlib = {"ast", "bisect", "dataclasses", "enum", "math", "os", "re", "typing"}
        for path in sim_sources():
            with self.subTest(module=os.path.basename(path)):
                with open(path, "r", encoding="utf-8") as handle:
                    modules = imported_modules(handle.read())
                for module in modules:
                    root = module.split(".")[0]
                    self.assertIn(
                        root,
                        allowed_roots | stdlib,
                        f"{os.path.basename(path)} imports {module}",
                    )


class DeterminismTest(unittest.TestCase):
    """A simulation that is not reproducible cannot be trusted about a cut."""

    def drive(self, controller: SimController) -> list[tuple]:
        """One fixed script of gestures, and what the controller read after each.

        Deliberately mixes the movers so a cached material state has to
        survive going backwards, jumping and re-walking.
        """
        script = [
            "next_cut",
            "next_cut",
            "step_forward",
            "step_forward",
            "next_section",
            "step_back",
            "prev_cut",
            "to_end",
            "prev_section",
            "prev_cut",
            "step_forward",
            "reset",
            "next_section",
            "next_cut",
            "step_back",
            "step_back",
        ]
        trace = []
        for gesture in script:
            moved = getattr(controller, gesture)()
            trace.append(
                (
                    gesture,
                    moved,
                    controller.step_index,
                    controller.cut_index,
                    controller.position,
                    controller.section,
                    controller.state,
                )
            )
        return trace

    def test_two_controllers_driven_identically_agree_exactly(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                first = SimController(item.timeline)
                second = SimController(item.timeline)
                self.assertEqual(self.drive(first), self.drive(second))

    def test_a_controller_over_a_rebuilt_timeline_agrees_too(self):
        for label, item in cases().items():
            with self.subTest(case=label):
                twin = SimTimeline.build(item.program, item.plan, item.config)
                self.assertEqual(
                    self.drive(SimController(item.timeline)),
                    self.drive(SimController(twin)),
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
