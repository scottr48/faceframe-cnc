"""Tests for the Milestone 2 sheet-nesting optimizer (spec 4a / 4c).

Frame-inside-frame placement (spec 4b) is Milestone 3 and is not exercised
here.  Stdlib only.
"""

from __future__ import annotations

import math
import time
import unittest

from faceframe_cnc.geometry import WDC_SLOT_END_REACH, wdc_slot_axis_is_height
from faceframe_cnc.nesting import (
    EPS,
    NestingConfig,
    NestingError,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    nest,
    slot_end_clearance,
    validate_layouts,
)

# The real 7-21-26 Cab Tec order, faceframe lines only.  WDC2436 is
# 18 x 36 per the 2026-08-03 amendment (its 2" stiles change the routed
# opening, not the outside footprint, so packing only cares about 18x36).
# SD1212 is excluded — it has no frame dimensions.
ORDER_7_21_26 = [
    PartSpec("W3330", 33.0, 30.0, 10),
    PartSpec("W2436", 24.0, 36.0, 10),
    PartSpec("W3036", 30.0, 36.0, 10),
    PartSpec("W2442", 24.0, 42.0, 10),
    PartSpec("W2742", 27.0, 42.0, 10),
    PartSpec("W3012", 30.0, 12.0, 30),
    PartSpec("W3024", 30.0, 24.0, 10),
    PartSpec("B18", 18.0, 30.0, 25),
    PartSpec("B30", 30.0, 30.0, 25),
    PartSpec("3DB24", 24.0, 30.0, 25),
    PartSpec("3DB30", 30.0, 30.0, 25),
    PartSpec("LS36", 36.0, 30.0, 25),
    PartSpec("WDC2436", 18.0, 36.0, 30),
]

# The itemised quantities above sum to 245 frames, not the 240 quoted in
# passing in the milestone brief; the line items are the authority.
TOTAL_PARTS = 245


def gap_between(a: Placement, b: Placement) -> float:
    """Rectangle gap distance: negative means the footprints overlap."""
    dx = max(a.x, b.x) - min(a.x + a.width, b.x + b.width)
    dy = max(a.y, b.y) - min(a.y + a.height, b.y + b.height)
    if dx >= 0 and dy >= 0:
        return math.hypot(dx, dy)
    return max(dx, dy)


def stile_end_clearances(result: NestingResult, config: NestingConfig):
    """Every WDC placement's real distance to the two SLOT-AXIS sheet edges.

    Returns ``(run, placement, before, after)`` per WDC on each unique sheet
    picture, the two distances in inches and ``run`` the number of physical
    sheets that picture is stamped onto — so the runs sum to the frames the
    machine will actually cut.  Measured off the emitted coordinates the way
    the machine will read them: nothing the packer reserved is consulted,
    which is the whole point when the reserved rectangle is the suspect.
    Nested children are walked too — a WDC is small enough to ride inside a
    W2742 opening, so it reaches the sheet edge as a passenger as well.
    """
    out = []

    def walk(run, placements):
        for p in placements:
            walk(run, p.children)
            if slot_end_clearance(p.part_number, config) <= config.part_gap:
                continue
            if wdc_slot_axis_is_height(p.rotated):
                low, high, limit = p.y, p.y + p.height, config.sheet_height
            else:
                low, high, limit = p.x, p.x + p.width, config.sheet_width
            out.append((run, p, low, limit - high))

    for layout, run in result.unique_sheets:
        walk(run, layout.placements)
    return out


class FullOrderTests(unittest.TestCase):
    """The real order, nested once and inspected from several angles."""

    @classmethod
    def setUpClass(cls):
        cls.config = NestingConfig()
        start = time.perf_counter()
        cls.result = nest(ORDER_7_21_26, cls.config)
        cls.elapsed = time.perf_counter() - start

    def test_demand_total_is_the_real_order(self):
        self.assertEqual(sum(p.qty for p in ORDER_7_21_26), TOTAL_PARTS)
        self.assertEqual(self.result.total_parts, TOTAL_PARTS)

    def test_validator_finds_no_violations(self):
        problems = validate_layouts(self.result, self.config)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_part_placed_exactly_qty_times_with_unmodified_dims(self):
        placed: dict[str, int] = {}
        for layout, run in self.result.unique_sheets:
            for p in layout.placements:
                placed[p.part_number] = placed.get(p.part_number, 0) + run
                spec = next(s for s in ORDER_7_21_26 if s.part_number == p.part_number)
                if p.rotated:
                    self.assertAlmostEqual(p.width, spec.height, places=9)
                    self.assertAlmostEqual(p.height, spec.width, places=9)
                else:
                    self.assertAlmostEqual(p.width, spec.width, places=9)
                    self.assertAlmostEqual(p.height, spec.height, places=9)
        for spec in ORDER_7_21_26:
            self.assertEqual(
                placed.get(spec.part_number, 0),
                spec.qty,
                f"{spec.part_number}: placed {placed.get(spec.part_number, 0)}, "
                f"ordered {spec.qty}",
            )
        self.assertEqual(sum(placed.values()), TOTAL_PARTS)

    def test_sheet_count_meets_area_lower_bound(self):
        lower = self.result.area_lower_bound_sheets
        self.assertEqual(lower, 41, "area lower bound for this order")
        self.assertGreaterEqual(
            self.result.total_sheets,
            lower,
            "sheet count below the area lower bound is physically impossible",
        )
        print("\n" + self.result.summary())
        print(f"  nest() elapsed: {self.elapsed * 1000:.0f} ms")
        # Guard against a silent quality regression.  The packer currently
        # returns 49 sheets against the 41-sheet area floor; 52 leaves room
        # for tuning without letting it rot.  (47 before 2026-08-03: the
        # WDC frames now reserve the 0.875 their T17 stile slot cuts past
        # each end, which costs two footprint-only sheets and one nested
        # one — see NestingConfig.part_gap and nesting.slot_end_clearance.)
        self.assertLessEqual(
            self.result.total_sheets,
            52,
            f"packing quality regressed: {self.result.total_sheets} sheets "
            f"vs an area lower bound of {lower}",
        )

    def test_run_quantities_sum_to_total_and_grouping_happens(self):
        runs = [run for _layout, run in self.result.unique_sheets]
        self.assertEqual(sum(runs), self.result.total_sheets)
        self.assertTrue(all(isinstance(r, int) and r >= 1 for r in runs))
        self.assertLess(
            self.result.unique_sheet_count,
            self.result.total_sheets,
            "a heavily repeated order must produce fewer unique pictures than sheets",
        )
        self.assertGreaterEqual(
            max(runs), 2, "expected at least one repeated sheet layout on this order"
        )
        canonicals = [layout.canonical() for layout, _run in self.result.unique_sheets]
        self.assertEqual(len(canonicals), len(set(canonicals)), "unique sheets not unique")

    def test_no_gap_violations_measured_independently(self):
        for i, (layout, _run) in enumerate(self.result.unique_sheets, start=1):
            ps = layout.placements
            for a_idx in range(len(ps)):
                for b_idx in range(a_idx + 1, len(ps)):
                    self.assertGreaterEqual(
                        gap_between(ps[a_idx], ps[b_idx]),
                        self.config.part_gap - EPS,
                        f"sheet {i}: {ps[a_idx].part_number} too close to "
                        f"{ps[b_idx].part_number}",
                    )

    def test_runs_fast(self):
        self.assertLess(self.elapsed, 10.0, f"nest() took {self.elapsed:.2f}s")

    def test_determinism(self):
        again = nest(ORDER_7_21_26, NestingConfig())
        self.assertEqual(again.total_sheets, self.result.total_sheets)
        self.assertEqual(
            [(l.canonical(), r) for l, r in again.unique_sheets],
            [(l.canonical(), r) for l, r in self.result.unique_sheets],
        )

    def test_determinism_is_order_independent(self):
        shuffled = list(reversed(ORDER_7_21_26))
        again = nest(shuffled, NestingConfig())
        self.assertEqual(
            [(l.canonical(), r) for l, r in again.unique_sheets],
            [(l.canonical(), r) for l, r in self.result.unique_sheets],
        )


class SmallCaseTests(unittest.TestCase):
    """Hand-checkable cases."""

    def setUp(self):
        self.config = NestingConfig()

    def test_two_squares_on_one_sheet_keep_the_gap(self):
        # Two 30x30 cannot sit side by side across 49"; they stack.
        result = nest([PartSpec("SQ30", 30.0, 30.0, 2)], self.config)
        self.assertEqual(result.total_sheets, 1)
        self.assertEqual(validate_layouts(result, self.config), [])
        layout = result.unique_sheets[0][0]
        self.assertEqual(len(layout.placements), 2)
        a, b = layout.placements
        self.assertGreaterEqual(gap_between(a, b), self.config.part_gap - EPS)

    def test_part_too_big_in_both_orientations_raises(self):
        with self.assertRaises(NestingError) as ctx:
            nest([PartSpec("HUGE", 60.0, 60.0, 1)], self.config)
        message = str(ctx.exception)
        self.assertIn("HUGE", message)
        self.assertIn("does not fit", message)

    def test_part_longer_than_the_sheet_length_raises(self):
        with self.assertRaises(NestingError) as ctx:
            nest([PartSpec("LONG", 20.0, 100.0, 1)], self.config)
        self.assertIn("LONG", str(ctx.exception))

    def test_rotation_is_used_when_the_part_only_fits_turned(self):
        # 50" exceeds the 49" sheet width, so it must be placed 20 x 50.
        result = nest([PartSpec("R50", 50.0, 20.0, 1)], self.config)
        self.assertEqual(validate_layouts(result, self.config), [])
        p = result.unique_sheets[0][0].placements[0]
        self.assertTrue(p.rotated)
        self.assertAlmostEqual(p.width, 20.0)
        self.assertAlmostEqual(p.height, 50.0)

    def test_wide_part_may_ride_the_sheet_edge(self):
        # 48.9 wide on a 49 sheet leaves 0.1 total slack: the 0.5 cushion
        # cannot be honoured, and that is legal (soft preference).
        result = nest([PartSpec("WIDE", 48.9, 20.0, 1)], self.config)
        self.assertEqual(validate_layouts(result, self.config), [])
        p = result.unique_sheets[0][0].placements[0]
        self.assertFalse(p.rotated)
        self.assertGreaterEqual(p.x, -EPS)
        self.assertLessEqual(p.x + p.width, self.config.sheet_width + EPS)
        self.assertLess(p.x, self.config.edge_cushion, "cushion had to yield here")
        self.assertGreater(result.edge_contact_parts(), 0)

    def test_cushion_is_honoured_when_there_is_room(self):
        result = nest([PartSpec("SMALL", 10.0, 10.0, 1)], self.config)
        p = result.unique_sheets[0][0].placements[0]
        cfg = self.config
        self.assertGreaterEqual(p.x, cfg.edge_cushion - EPS)
        self.assertGreaterEqual(p.y, cfg.edge_cushion - EPS)
        self.assertLessEqual(p.x + p.width, cfg.sheet_width - cfg.edge_cushion + EPS)
        self.assertLessEqual(p.y + p.height, cfg.sheet_height - cfg.edge_cushion + EPS)
        self.assertEqual(result.edge_contact_parts(), 0)

    def test_identical_parts_produce_one_unique_sheet_with_a_run(self):
        # 6 x (30x30) per sheet is not possible; whatever the packer picks,
        # 24 identical parts must collapse into repeated pictures.
        result = nest([PartSpec("SQ30", 30.0, 30.0, 24)], self.config)
        self.assertEqual(validate_layouts(result, self.config), [])
        self.assertEqual(sum(r for _l, r in result.unique_sheets), result.total_sheets)
        self.assertGreaterEqual(max(r for _l, r in result.unique_sheets), 2)

    def test_empty_and_zero_quantity_demand(self):
        self.assertEqual(nest([], self.config).total_sheets, 0)
        self.assertEqual(nest([PartSpec("Z", 10.0, 10.0, 0)], self.config).total_sheets, 0)

    def test_gap_is_configurable_and_respected(self):
        cfg = NestingConfig(part_gap=2.0)
        result = nest([PartSpec("SQ20", 20.0, 20.0, 2)], cfg)
        self.assertEqual(validate_layouts(result, cfg), [])
        a, b = result.unique_sheets[0][0].placements
        self.assertGreaterEqual(gap_between(a, b), 2.0 - EPS)

    def test_hand_checkable_perfect_pack(self):
        # 30 + 0.375 + 18 = 48.375 across the 49" width, and three 30" rows
        # plus two gaps is 90.75 <= 97, so all six parts share one sheet.
        result = nest(
            [PartSpec("W30", 30.0, 30.0, 3), PartSpec("N18", 18.0, 30.0, 3)],
            self.config,
        )
        self.assertEqual(validate_layouts(result, self.config), [])
        self.assertEqual(result.total_sheets, 1)
        layout = result.unique_sheets[0][0]
        self.assertEqual(layout.part_counts(), {"N18": 3, "W30": 3})
        self.assertAlmostEqual(layout.used_area(), 3 * 900 + 3 * 540)

    def test_part_exactly_the_size_of_the_sheet(self):
        result = nest([PartSpec("FULL", 49.0, 97.0, 2)], self.config)
        self.assertEqual(validate_layouts(result, self.config), [])
        self.assertEqual(result.total_sheets, 2)
        self.assertEqual(result.unique_sheet_count, 1)
        p = result.unique_sheets[0][0].placements[0]
        self.assertAlmostEqual(p.x, 0.0)
        self.assertAlmostEqual(p.y, 0.0)

    def test_fractional_dimensions_stay_valid_and_deterministic(self):
        parts = [PartSpec(f"F{i}", 12.3 + i * 1.7, 18.65 + i * 0.9, 6) for i in range(8)]
        a = nest(parts, self.config)
        b = nest(list(reversed(parts)), self.config)
        self.assertEqual(validate_layouts(a, self.config), [])
        self.assertEqual(
            [(l.canonical(), r) for l, r in a.unique_sheets],
            [(l.canonical(), r) for l, r in b.unique_sheets],
        )

    def test_children_are_left_empty_in_milestone_2(self):
        result = nest([PartSpec("A", 20.0, 20.0, 4)], self.config)
        for layout, _run in result.unique_sheets:
            for p in layout.placements:
                self.assertEqual(p.children, [])

    def test_duplicate_part_number_with_conflicting_dims_raises(self):
        with self.assertRaises(NestingError):
            nest([PartSpec("D", 10.0, 10.0, 1), PartSpec("D", 12.0, 10.0, 1)], self.config)

    def test_negative_quantity_raises(self):
        with self.assertRaises(NestingError):
            nest([PartSpec("N", 10.0, 10.0, -1)], self.config)


class ValidatorCatchesBadLayoutsTests(unittest.TestCase):
    """The validator must be able to fail, not just pass."""

    def setUp(self):
        self.config = NestingConfig()

    def _result(self, placements, run=1, total=1, demand=None):
        layout = SheetLayout(placements=list(placements))
        return NestingResult(
            unique_sheets=[(layout, run)],
            total_sheets=total,
            demand=demand if demand is not None else [],
            config=self.config,
        )

    def test_overlapping_parts_are_caught(self):
        result = self._result(
            [
                Placement("A", 1.0, 1.0, 20.0, 20.0),
                Placement("B", 10.0, 10.0, 20.0, 20.0),
            ],
            demand=[PartSpec("A", 20.0, 20.0, 1), PartSpec("B", 20.0, 20.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("footprints overlap" in p for p in problems), problems)
        self.assertTrue(any("A" in p and "B" in p for p in problems), problems)

    def test_parts_closer_than_the_gap_are_caught(self):
        result = self._result(
            [
                Placement("A", 1.0, 1.0, 20.0, 20.0),
                Placement("B", 21.2, 1.0, 20.0, 20.0),  # 0.2 clear, needs 0.455
            ],
            demand=[PartSpec("A", 20.0, 20.0, 1), PartSpec("B", 20.0, 20.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("gap violation" in p for p in problems), problems)

    def test_exactly_the_gap_is_legal(self):
        result = self._result(
            [
                Placement("A", 1.0, 1.0, 20.0, 20.0),
                Placement("B", 21.455, 1.0, 20.0, 20.0),
            ],
            demand=[PartSpec("A", 20.0, 20.0, 1), PartSpec("B", 20.0, 20.0, 1)],
        )
        self.assertEqual(validate_layouts(result, self.config), [])

    def test_off_sheet_part_is_caught(self):
        result = self._result(
            [Placement("A", 40.0, 1.0, 20.0, 20.0)],
            demand=[PartSpec("A", 20.0, 20.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("off the sheet" in p for p in problems), problems)

    def test_negative_coordinate_is_caught(self):
        result = self._result(
            [Placement("A", -0.5, 1.0, 20.0, 20.0)],
            demand=[PartSpec("A", 20.0, 20.0, 1)],
        )
        self.assertTrue(any("off the sheet" in p for p in validate_layouts(result, self.config)))

    def test_run_quantity_sum_mismatch_is_caught(self):
        result = self._result(
            [Placement("A", 1.0, 1.0, 20.0, 20.0)],
            run=1,
            total=5,
            demand=[PartSpec("A", 20.0, 20.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("total_sheets" in p for p in problems), problems)

    def test_under_and_over_production_are_caught(self):
        under = self._result(
            [Placement("A", 1.0, 1.0, 20.0, 20.0)],
            demand=[PartSpec("A", 20.0, 20.0, 3)],
        )
        self.assertTrue(any("only 1 of 3" in p for p in validate_layouts(under, self.config)))

        over = self._result(
            [Placement("A", 1.0, 1.0, 20.0, 20.0), Placement("A", 25.0, 1.0, 20.0, 20.0)],
            demand=[PartSpec("A", 20.0, 20.0, 1)],
        )
        self.assertTrue(any("only 1 were ordered" in p for p in validate_layouts(over, self.config)))

    def test_unordered_part_is_caught(self):
        result = self._result([Placement("GHOST", 1.0, 1.0, 20.0, 20.0)], demand=[])
        self.assertTrue(any("never ordered" in p for p in validate_layouts(result, self.config)))

    def test_altered_dimensions_are_caught(self):
        result = self._result(
            [Placement("A", 1.0, 1.0, 19.0, 20.0)],
            demand=[PartSpec("A", 20.0, 20.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("must never be altered" in p for p in problems), problems)

    def test_wrong_rotation_flag_is_caught(self):
        result = self._result(
            [Placement("A", 1.0, 1.0, 20.0, 30.0, rotated=False)],
            demand=[PartSpec("A", 30.0, 20.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("must never be altered" in p for p in problems), problems)

    def test_zero_run_quantity_is_caught(self):
        result = self._result(
            [Placement("A", 1.0, 1.0, 20.0, 20.0)],
            run=0,
            total=0,
            demand=[PartSpec("A", 20.0, 20.0, 0)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("at least 1" in p for p in problems), problems)


class CanonicalFormTests(unittest.TestCase):
    def test_placement_order_does_not_change_the_canonical_form(self):
        a = Placement("A", 1.0, 2.0, 10.0, 20.0)
        b = Placement("B", 20.0, 2.0, 10.0, 20.0)
        self.assertEqual(
            SheetLayout([a, b]).canonical(), SheetLayout([b, a]).canonical()
        )

    def test_sub_tolerance_coordinate_noise_groups_together(self):
        a = SheetLayout([Placement("A", 1.0, 2.0, 10.0, 20.0)])
        b = SheetLayout([Placement("A", 1.000001, 2.0, 10.0, 20.0)])
        self.assertEqual(a.canonical(), b.canonical())

    def test_different_layouts_differ(self):
        a = SheetLayout([Placement("A", 1.0, 2.0, 10.0, 20.0)])
        b = SheetLayout([Placement("A", 1.0, 3.0, 10.0, 20.0)])
        self.assertNotEqual(a.canonical(), b.canonical())

    def test_rotation_is_part_of_the_identity(self):
        a = SheetLayout([Placement("A", 1.0, 2.0, 10.0, 10.0, rotated=False)])
        b = SheetLayout([Placement("A", 1.0, 2.0, 10.0, 10.0, rotated=True)])
        self.assertNotEqual(a.canonical(), b.canonical())


class FrontMarginTests(unittest.TestCase):
    """2026-08-03 amendment: soft front-edge margin (spec 4a refinement).

    ``front_margin`` only nudges where an already-decided shelf stack sits
    vertically; it must never change which parts fit on a sheet, so the
    sheet count is pinned to whatever the packer reaches without it — 49
    since the WDC slot clearance landed (47 before it).
    """

    def test_full_order_front_margin_holds_and_sheet_count_is_unchanged(self):
        config = NestingConfig()
        result = nest(ORDER_7_21_26, config)

        self.assertEqual(result.total_sheets, 49, "front_margin changed the sheet count")
        self.assertEqual(validate_layouts(result, config), [])

        for i, (layout, _run) in enumerate(result.unique_sheets, start=1):
            top_level = layout.placements
            self.assertTrue(top_level, f"sheet {i}: empty")
            min_y = min(p.y for p in top_level)
            max_top = max(p.y + p.height for p in top_level)
            # Slack derived independently from the placements themselves,
            # not from anything the packer recorded.
            slack = config.sheet_height - (max_top - min_y)
            if slack >= config.front_margin - EPS:
                self.assertAlmostEqual(
                    min_y, config.front_margin, places=6,
                    msg=f"sheet {i}: expected the full front margin",
                )
            else:
                self.assertAlmostEqual(
                    min_y, max(0.0, slack), places=6,
                    msg=f"sheet {i}: expected all slack spent on the front",
                )

    def test_single_part_with_two_inches_of_slack_gets_the_full_margin(self):
        result = nest([PartSpec("P", 20.0, 95.0, 1)], NestingConfig())
        p = result.unique_sheets[0][0].placements[0]
        self.assertAlmostEqual(p.y, 1.0)

    def test_single_part_with_half_an_inch_of_slack_gets_all_of_it_up_front(self):
        # 97 - 96.5 = 0.5" of slack, less than the 1" default margin: all of
        # it goes to the front and the back edge sits flush.
        result = nest([PartSpec("P", 20.0, 96.5, 1)], NestingConfig())
        p = result.unique_sheets[0][0].placements[0]
        self.assertAlmostEqual(p.y, 0.5)
        self.assertAlmostEqual(p.y + p.height, 97.0)

    def test_single_part_with_no_slack_sits_flush(self):
        result = nest([PartSpec("P", 20.0, 97.0, 1)], NestingConfig())
        p = result.unique_sheets[0][0].placements[0]
        self.assertAlmostEqual(p.y, 0.0)

    def test_front_margin_zero_reproduces_flush_vertical_behaviour(self):
        cfg = NestingConfig(front_margin=0.0)
        result = nest([PartSpec("P", 20.0, 97.0, 1)], cfg)
        self.assertEqual(validate_layouts(result, cfg), [])
        p = result.unique_sheets[0][0].placements[0]
        self.assertAlmostEqual(p.y, 0.0)

        # Also true when there IS slack: with the margin off, none of it is
        # reserved for the front.
        cfg_slack = NestingConfig(front_margin=0.0)
        result_slack = nest([PartSpec("Q", 20.0, 50.0, 1)], cfg_slack)
        q = result_slack.unique_sheets[0][0].placements[0]
        self.assertAlmostEqual(q.y, 0.0)

    def test_front_margin_rejected_when_invalid(self):
        with self.assertRaises(NestingError):
            nest([PartSpec("P", 10.0, 10.0, 1)], NestingConfig(front_margin=-1.0))
        with self.assertRaises(NestingError):
            nest([PartSpec("P", 10.0, 10.0, 1)], NestingConfig(front_margin=float("nan")))


class WdcSheetEdgeTests(unittest.TestCase):
    """2026-08-04 review: the packer must satisfy its own hard edge rule.

    Since the 2026-08-03 amendment ``validate_layouts`` has enforced — hard,
    the only hard rule it applies to a sheet edge — that a WDC's stile END
    sits at least ``WDC_SLOT_END_REACH`` (0.875") from the sheet edge along
    the axis its T17 slot runs: the 45-degree cone breaks the surface that far
    past the part, so any less and the cut runs off the sheet.

    The packer only ever reserved ``slot_end_clearance - part_gap`` (0.420)
    past each stile end.  Between two parts the ordinary ``part_gap`` tops
    that up to exactly 0.875, which is correct and deliberately frugal.  At a
    sheet edge there is no neighbour and so nothing to top it up — only the
    SOFT ``edge_cushion``, which is compressible to zero by design.  So the
    optimizer could hand the owner a finished layout its own validator
    refused: an optimized job that could never reach Generate.

    Everything below is measured from the emitted coordinates.
    """

    def setUp(self):
        self.config = NestingConfig()

    def assert_clear_of_the_edges(self, result, config, frames=None):
        """No WDC stile end within the slot's reach of a slot-axis edge.

        ``frames``, when given, is how many WDC frames the machine must end up
        cutting — runs included, so a layout that quietly dropped one cannot
        pass by having every frame it did place sitting legally.
        """
        self.assertEqual(validate_layouts(result, config), [])
        measured = stile_end_clearances(result, config)
        if frames is not None:
            self.assertEqual(
                sum(run for run, *_rest in measured), frames, "wrong WDC frame count"
            )
        for _run, placement, before, after in measured:
            where = f"{placement.part_number} @({placement.x:.4f},{placement.y:.4f})"
            self.assertGreaterEqual(
                before, WDC_SLOT_END_REACH - EPS,
                f"{where}: only {before:.4f} to the near slot-axis edge",
            )
            self.assertGreaterEqual(
                after, WDC_SLOT_END_REACH - EPS,
                f"{where}: only {after:.4f} to the far slot-axis edge",
            )
        return measured

    # -- the root cause, pinned so it cannot drift back -------------------

    def test_the_reserved_rectangle_alone_cannot_satisfy_the_edge_rule(self):
        from faceframe_cnc.nesting import _edge_inset, _pack_pad

        cfg = self.config
        clearance = slot_end_clearance("WDC2436", cfg)
        self.assertAlmostEqual(clearance, WDC_SLOT_END_REACH)

        pad = _pack_pad("WDC2436", cfg)
        inset = _edge_inset("WDC2436", cfg)
        # The reserved rectangle stands 0.420 outside the stile end...
        self.assertAlmostEqual(pad, clearance - cfg.part_gap)
        # ...which falls a whole part_gap short of what the validator wants.
        # Between two parts the gap pays that difference; a sheet edge pays
        # nothing, so the rectangle itself has to stand off by the remainder.
        self.assertAlmostEqual(inset, cfg.part_gap)
        self.assertAlmostEqual(pad + inset, clearance)
        self.assertLess(pad, clearance)

        # None of it touches an ordinary frame.
        self.assertEqual(_pack_pad("B30", cfg), 0.0)
        self.assertEqual(_edge_inset("B30", cfg), 0.0)

    # -- the reported repro ------------------------------------------------

    def test_the_reported_heights_nest_with_no_violations(self):
        # Reported 2026-08-04: each of these produced an "optimized" sheet the
        # validator then rejected on both frames.  The first three rotate (the
        # slot axis lands along the 49" width), so the bug showed up in x.
        for height in (48.1, 48.0, 47.9):
            with self.subTest(height=height):
                result = nest([PartSpec("WDC2452", 18.0, height, 2)], self.config)
                self.assert_clear_of_the_edges(result, self.config, frames=2)

    def test_the_reported_heights_that_cannot_fit_are_refused_not_packed(self):
        # The other three from the same report are not packing failures at
        # all: 95.8 + 2 x 0.875 = 97.55 on a 97" sheet, and turning the frame
        # is no help (95.8 > 49).  No legal placement exists, so nest() has to
        # say so — quietly emitting a layout the validator refuses is the one
        # answer that is never acceptable.
        for height in (95.8, 96.0, 96.16):
            with self.subTest(height=height):
                with self.assertRaises(NestingError) as raised:
                    nest([PartSpec("WDC2452", 18.0, height, 2)], self.config)
                message = str(raised.exception)
                self.assertIn("WDC2452", message)
                self.assertIn("does not fit", message)
                self.assertIn("T17 stile slot", message)

    def test_the_boundary_between_a_makeable_and_an_impossible_wdc(self):
        # 97 - 2 x 0.875 = 95.25 exactly: the tallest WDC this sheet can hold.
        cfg = self.config
        limit = cfg.sheet_height - 2.0 * WDC_SLOT_END_REACH
        self.assertAlmostEqual(limit, 95.25)

        result = nest([PartSpec("WDC2436", 18.0, limit, 1)], cfg)
        placement = result.unique_sheets[0][0].placements[0]
        self.assertFalse(placement.rotated)
        self.assertAlmostEqual(placement.y, WDC_SLOT_END_REACH, places=6)
        self.assert_clear_of_the_edges(result, cfg, frames=1)

        with self.assertRaises(NestingError):
            nest([PartSpec("WDC2436", 18.0, limit + 0.01, 1)], cfg)

        # An ordinary frame of the same size is unaffected: it has no slot, so
        # it may still ride the sheet edge (the cushion is only a preference).
        plain = nest([PartSpec("B30", 18.0, 97.0, 1)], cfg)
        self.assertEqual(validate_layouts(plain, cfg), [])

    # -- the frugality the fix must not spend -----------------------------

    def test_a_wdc_beside_a_plain_frame_still_shares_exactly_the_slot_reach(self):
        # The anti-regression for the fix.  Between two parts the reserved
        # 0.420 plus part_gap already adds up to 0.875 and not a thou more,
        # and that must stay true: inflating it would cost real sheets on the
        # owner's order (41 today, pinned in test_inside).  30" wide apiece,
        # so the two cannot sit side by side across 49" — they stack, and at
        # 48" tall the WDC cannot be turned either (48 + 1.75 > 49), so its
        # stile end is guaranteed to be what faces the B30.
        cfg = self.config
        result = nest(
            [PartSpec("WDC3048", 30.0, 48.0, 1), PartSpec("B30", 30.0, 30.0, 1)], cfg
        )
        self.assertEqual(result.total_sheets, 1)
        placements = result.unique_sheets[0][0].placements
        self.assertEqual(len(placements), 2)
        self.assert_clear_of_the_edges(result, cfg, frames=1)

        wdc = next(p for p in placements if p.part_number == "WDC3048")
        plain = next(p for p in placements if p.part_number == "B30")
        self.assertFalse(wdc.rotated)
        gap = gap_between(wdc, plain)
        self.assertAlmostEqual(
            gap, WDC_SLOT_END_REACH, places=6,
            msg="the stile end must clear its neighbour by the reach and no more",
        )

    def test_the_edge_rule_costs_the_real_order_nothing(self):
        # The 7-21-26 order's WDC2436 frames are 18x36 and sit nowhere near a
        # slot-axis edge, so the correction must not touch the answer the shop
        # already accepted: 49 sheets footprint-only, 41 with inside nesting
        # (both pinned elsewhere), and a clean validator either way.
        footprint = nest(ORDER_7_21_26, self.config)
        self.assertEqual(footprint.total_sheets, 49)
        self.assert_clear_of_the_edges(footprint, self.config)

        inside_cfg = NestingConfig(inside_nesting=True)
        inside = nest(ORDER_7_21_26, inside_cfg)
        self.assertEqual(inside.total_sheets, 41)
        measured = self.assert_clear_of_the_edges(inside, inside_cfg)
        self.assertTrue(measured, "this order has WDC frames on it")

    # -- the ways the rule could have been dodged --------------------------

    def test_an_orientation_whose_slot_would_overhang_is_never_chosen(self):
        # Turned 90 degrees this frame needs 48 + 2 x 0.875 = 49.75 across a
        # 49" sheet, so the turn is illegal however well it fills a shelf —
        # and packing it turned is exactly what the reported bug did.
        cfg = self.config
        result = nest([PartSpec("WDC2452", 18.0, 48.0, 4)], cfg)
        self.assert_clear_of_the_edges(result, cfg, frames=4)
        for layout, _run in result.unique_sheets:
            for p in layout.placements:
                self.assertFalse(p.rotated, "turning it hangs the slot off the sheet")

    def test_turning_the_soft_settings_off_does_not_soften_the_hard_rule(self):
        # The shape of the original mistake: the edge cushion and the front
        # margin are preferences the packer gives up whenever it needs the
        # room, so neither could ever stand in for the slot's reach.  With
        # both at zero the hard rule has to hold on its own.
        for cfg in (
            NestingConfig(edge_cushion=0.0),
            NestingConfig(front_margin=0.0),
            NestingConfig(edge_cushion=0.0, front_margin=0.0),
        ):
            for height in (36.0, 47.9, 48.0, 90.0):
                with self.subTest(cushion=cfg.edge_cushion, margin=cfg.front_margin,
                                  height=height):
                    result = nest([PartSpec("WDC2436", 18.0, height, 3)], cfg)
                    self.assert_clear_of_the_edges(result, cfg, frames=3)

    def test_a_bigger_gap_moves_the_edge_rule_with_it(self):
        # ``part_gap`` and the slot reach are independent settings, and the
        # inset is derived from both.  At a 1.5" gap the gap alone already
        # exceeds the reach, so a WDC becomes an ordinary part and may ride
        # the edge like any other.
        cfg = NestingConfig(part_gap=1.5)
        result = nest([PartSpec("WDC2436", 18.0, 36.0, 4)], cfg)
        self.assertEqual(validate_layouts(result, cfg), [])
        self.assertEqual(stile_end_clearances(result, cfg), [])

        # Just under the reach it is still directional, and still enforced.
        cfg = NestingConfig(part_gap=0.8)
        result = nest([PartSpec("WDC2436", 18.0, 36.0, 4)], cfg)
        self.assert_clear_of_the_edges(result, cfg, frames=4)

    def test_a_sweep_of_wdc_sizes_never_violates_the_edge_rule(self):
        # One hand-picked size proves nothing here: the violation only
        # appeared when a reserved rectangle happened to land against an edge,
        # which depends on the size, the quantity and which orientation won.
        cfg = self.config
        widths = (6.0, 12.5, 18.0, 24.3, 30.0, 36.7, 43.1, 48.0)
        # Straddles the 95.25 ceiling deliberately: the last two heights have
        # no legal placement at all and must come back as errors, not layouts.
        heights = (
            6.0, 12.5, 18.0, 23.15, 30.0, 36.0, 42.5, 47.3, 48.0, 52.0,
            60.5, 72.0, 84.5, 95.0, 95.25, 95.3, 96.0,
        )
        checked = 0
        refused = 0
        for width in widths:
            for height in heights:
                for qty in (1, 3):
                    try:
                        result = nest([PartSpec("WDC2400", width, height, qty)], cfg)
                    except NestingError:
                        refused += 1
                        continue
                    with self.subTest(width=width, height=height, qty=qty):
                        self.assert_clear_of_the_edges(result, cfg, frames=qty)
                    checked += 1
        self.assertGreater(checked, 150, "the sweep must actually cover ground")
        self.assertGreater(refused, 0, "sizes past the slot's reach must be refused")

    def test_a_wdc_mixed_with_ordinary_frames_stays_clear_of_the_edges(self):
        # A WDC alone gets a sheet to itself and plenty of slack; the hard
        # cases are the ones where partners have already spent the width.
        cfg = self.config
        mixes = (
            [PartSpec("WDC2436", 18.0, 36.0, 7), PartSpec("LS36", 36.0, 30.0, 5)],
            [PartSpec("WDC2448", 18.0, 47.5, 6), PartSpec("B30", 30.0, 30.0, 9)],
            [PartSpec("WDC3648", 30.0, 47.5, 4), PartSpec("W3012", 30.0, 12.0, 12)],
            [PartSpec("WDC2436", 46.0, 46.0, 5), PartSpec("3DB24", 24.0, 30.0, 8)],
        )
        for mix in mixes:
            with self.subTest(mix=[s.part_number for s in mix]):
                result = nest(mix, cfg)
                self.assert_clear_of_the_edges(result, cfg)

    def test_the_row_correction_does_not_strand_a_full_width_neighbour(self):
        # Two of these turned WDCs make a 48.435" row: legal on its own, but
        # its end stile is then 0.42 from the side edge, so the row has to be
        # rebuilt.  The rebuild charges the turned WDC for its insets rather
        # than shrinking the sheet, precisely so the 48.5" frame sharing the
        # job stays placeable — it would not fit a sheet narrowed to 48.09".
        cfg = self.config
        result = nest(
            [PartSpec("WDC0624", 6.0, 23.15, 3), PartSpec("WIDE", 48.5, 20.0, 2)], cfg
        )
        self.assert_clear_of_the_edges(result, cfg, frames=3)
        placed = {}
        for layout, run in result.unique_sheets:
            for p in layout.placements:
                placed[p.part_number] = placed.get(p.part_number, 0) + run
        self.assertEqual(placed, {"WDC0624": 3, "WIDE": 2})

    def test_the_correction_keeps_the_packer_deterministic(self):
        # The fix re-runs shelf selection against a shrunk sheet, so it must
        # not leak state between runs or depend on demand order.
        cfg = self.config
        parts = [
            PartSpec("WDC2452", 18.0, 47.9, 5),
            PartSpec("B30", 30.0, 30.0, 4),
            PartSpec("W3012", 30.0, 12.0, 6),
        ]
        first = nest(parts, cfg)
        again = nest(parts, cfg)
        reversed_order = nest(list(reversed(parts)), cfg)
        snapshot = [(l.canonical(), r) for l, r in first.unique_sheets]
        self.assertEqual(snapshot, [(l.canonical(), r) for l, r in again.unique_sheets])
        self.assertEqual(
            snapshot, [(l.canonical(), r) for l, r in reversed_order.unique_sheets]
        )
        self.assert_clear_of_the_edges(first, cfg, frames=5)


class MinPartGapCrossCheckTests(unittest.TestCase):
    """MIN_PART_GAP encodes a machine fact; this pins it to the machine.

    The constant exists because the T11 perimeter lead-in sweeps past the
    part edge, so any packing gap inside that sweep produces sheets the NC
    verifier must refuse.  ``nesting`` must not import the post package
    (core modules stay stdlib-and-geometry only), so THIS TEST is the
    layering-safe cross-check: it recomputes the worst-case sweep from the
    post's own measured table and fails the moment the table grows a
    lead-in that MIN_PART_GAP no longer clears.
    """

    def test_min_part_gap_clears_the_perimeter_lead_in_sweep(self):
        from faceframe_cnc.nesting import MIN_PART_GAP
        from faceframe_cnc.post.model import SECTION_PERIMETER, default_config

        config = default_config()
        tool = config.tools[SECTION_PERIMETER]
        # Per pass: the ramp line stands ``lateral_lead`` outside a profile
        # that is itself ``offset`` outside the part edge, and the tool
        # cuts a further radius beyond its centre.  The worst pass governs.
        reach = (
            max(p.offset + p.lateral_lead for p in config.perimeter_passes)
            + tool.radius
        )
        # Pass 1 (spring stock 0.1895 + lead 0.05 + radius 0.1875) = 0.427;
        # the through pass is the 0.425 the docs quote.
        self.assertAlmostEqual(reach, 0.427)
        self.assertLess(
            reach,
            MIN_PART_GAP,
            "the post's perimeter lead-in now sweeps past MIN_PART_GAP: "
            "parts packed at the minimum gap would be cut into",
        )

    def test_the_default_part_gap_is_the_floor(self):
        from faceframe_cnc.nesting import MIN_PART_GAP

        self.assertEqual(MIN_PART_GAP, 0.455)
        self.assertEqual(NestingConfig().part_gap, MIN_PART_GAP)


if __name__ == "__main__":
    unittest.main()
