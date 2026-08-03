"""Tests for the Milestone 2 sheet-nesting optimizer (spec 4a / 4c).

Frame-inside-frame placement (spec 4b) is Milestone 3 and is not exercised
here.  Stdlib only.
"""

from __future__ import annotations

import math
import time
import unittest

from faceframe_cnc.nesting import (
    EPS,
    NestingConfig,
    NestingError,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    nest,
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
        # returns 47 sheets (85.8% overall fill) against the 41-sheet area
        # floor; 50 leaves room for tuning without letting it rot.
        self.assertLessEqual(
            self.result.total_sheets,
            50,
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
                Placement("B", 21.2, 1.0, 20.0, 20.0),  # 0.2 clear, needs 0.375
            ],
            demand=[PartSpec("A", 20.0, 20.0, 1), PartSpec("B", 20.0, 20.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("gap violation" in p for p in problems), problems)

    def test_exactly_the_gap_is_legal(self):
        result = self._result(
            [
                Placement("A", 1.0, 1.0, 20.0, 20.0),
                Placement("B", 21.375, 1.0, 20.0, 20.0),
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


if __name__ == "__main__":
    unittest.main()
