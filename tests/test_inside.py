"""Tests for frame-inside-frame nesting (spec 4b) — Milestone 3.

Covers the eligibility table, the pairing/assignment phase, the sheet-coordinate
transform that centres an inner in its host's opening, the validator's new
containment rules, and the guarantee that turning inside nesting OFF reproduces
the Milestone 2 result byte for byte.  Stdlib only.
"""

from __future__ import annotations

import time
import unittest

from faceframe_cnc.geometry import compute_geometry
from faceframe_cnc.inside import (
    ROTATED,
    UPRIGHT,
    assign_inners,
    best_fit,
    eligibility_table,
)
from faceframe_cnc.nesting import (
    NestingConfig,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    nest,
    place_inner,
    validate_layouts,
)
from tests.test_nesting import ORDER_7_21_26, TOTAL_PARTS

#: The Milestone 2 sheet count for this order, with inside nesting off.
M2_BASELINE_SHEETS = 47

BY_NAME = {s.part_number: s for s in ORDER_7_21_26}


class EligibilityTableTests(unittest.TestCase):
    """Spec 4b's host -> inner candidate table, as amended.

    The §4b table in the prompt predates the 2026-08-03 amendment that made
    WDC2436 18" wide instead of 24".  Two consequences, both verified here:

    *   as a HOST its opening is 14 x 33, so only a rotated W3012 fits and
        B18 (18.75 needed) does not — the amendment says this explicitly;
    *   as an INNER an 18 x 36 footprint now fits W2742 (24 x 39 opening) and
        W2442 (21 x 39), which the §4b table does not list because a 24"-wide
        WDC2436 would not have fitted either.  That follows from the same
        amendment plus the unchanged clearance rule, so the table below is
        the amended truth, not a relaxation of it.
    """

    def setUp(self):
        self.table = eligibility_table(ORDER_7_21_26)

    def test_hosts_that_can_take_an_inner(self):
        self.assertEqual(
            sorted(host for host, row in self.table.items() if row),
            ["LS36", "W2436", "W2442", "W2742", "W3036", "WDC2436"],
        )

    def test_w3036_opening_27x33(self):
        self.assertEqual(
            self.table["W3036"],
            {
                "3DB24": (UPRIGHT,),
                "B18": (UPRIGHT,),
                "W3012": (ROTATED,),
                "W3024": (ROTATED,),
            },
        )

    def test_ls36_opening_33x27(self):
        self.assertEqual(
            self.table["LS36"],
            {
                "3DB24": (ROTATED,),
                "B18": (ROTATED,),
                "W3012": (UPRIGHT,),
                "W3024": (UPRIGHT,),
            },
        )

    def test_w2742_opening_24x39(self):
        self.assertEqual(
            self.table["W2742"],
            {"B18": (UPRIGHT,), "W3012": (ROTATED,), "WDC2436": (UPRIGHT,)},
        )

    def test_w2442_opening_21x39(self):
        self.assertEqual(
            self.table["W2442"],
            {"B18": (UPRIGHT,), "W3012": (ROTATED,), "WDC2436": (UPRIGHT,)},
        )

    def test_w2436_opening_21x33(self):
        self.assertEqual(
            self.table["W2436"], {"B18": (UPRIGHT,), "W3012": (ROTATED,)}
        )

    def test_wdc2436_opening_14x33_takes_only_a_rotated_w3012(self):
        # The amendment's own worked example: B18 is 18 wide and 18.75 > 14.
        opening = compute_geometry("WDC2436", 18.0, 36.0).openings[0]
        self.assertEqual((opening.width, opening.height), (14.0, 33.0))
        self.assertEqual(self.table["WDC2436"], {"W3012": (ROTATED,)})

    def test_parts_that_host_nothing_from_this_order(self):
        # W3330's opening is 30 x 27 and every candidate fails one axis.
        for host in ("W3330", "W3012", "W3024", "B18", "B30", "3DB24", "3DB30"):
            self.assertEqual(self.table[host], {}, f"{host} should host nothing")

    def test_w3330_misses_on_exactly_one_axis_each_time(self):
        opening = compute_geometry("W3330", 33.0, 30.0).openings[0]
        self.assertEqual((opening.width, opening.height), (30.0, 27.0))
        for inner in ("W3012", "W3024", "B18", "3DB24"):
            spec = BY_NAME[inner]
            for w, h in ((spec.width, spec.height), (spec.height, spec.width)):
                self.assertTrue(
                    w + 0.75 > opening.width or h + 0.75 > opening.height,
                    f"{inner} {w}x{h} should not fit W3330",
                )

    def test_clearance_is_a_hard_minimum(self):
        # W2436's opening is 21 x 33; a 20.25-wide inner leaves exactly 0.375
        # per side and fits, 20.26 does not.
        self.assertIsNotNone(best_fit("W2436", 24.0, 36.0, 20.25, 30.0))
        self.assertIsNone(best_fit("W2436", 24.0, 36.0, 20.26, 30.0))

    def test_a_drawer_frames_individual_openings_can_host(self):
        # Generic rule (spec 4b): a 3DB's openings are small, but a frame that
        # fits one of them is legal.  3DB30's middle opening is 27 x 9.875.
        fit = best_fit("3DB30", 30.0, 30.0, 26.0, 9.0)
        self.assertIsNotNone(fit)
        self.assertEqual(fit.opening_label, "middle")


class AssignmentTests(unittest.TestCase):
    """The pairing phase: who goes inside whom, and how many."""

    def test_max_count_assignment_is_ninety_two(self):
        """Every host slot that can be filled, is.

        Host slots: W3036 10 + LS36 25 + W2742 10 + W2442 10 + W2436 10 +
        WDC2436 30 = 95.  Inners other than WDC2436: 90.  Each WDC2436 spent
        as an inner adds one inner but removes one host slot, so the ceiling
        is ``min(95 - k, 90 + k)``, maximised at 92 or 93 around k = 2.5; 92
        is what the structure actually admits.
        """
        assignment = assign_inners(ORDER_7_21_26)
        self.assertEqual(assignment.total, 92)
        self.assertEqual(
            assignment.hosts_used(),
            {"LS36": 25, "W2436": 10, "W2442": 10, "W2742": 10, "W3036": 10, "WDC2436": 27},
        )
        self.assertEqual(
            assignment.inners_used(),
            {"3DB24": 25, "B18": 25, "W3012": 30, "W3024": 9, "WDC2436": 3},
        )

    def test_no_frame_is_both_a_host_and_an_inner_beyond_its_quantity(self):
        assignment = assign_inners(ORDER_7_21_26)
        hosts, inners = assignment.hosts_used(), assignment.inners_used()
        for name in set(hosts) | set(inners):
            self.assertLessEqual(
                hosts.get(name, 0) + inners.get(name, 0),
                BY_NAME[name].qty,
                f"{name} used more times than it was ordered",
            )
        # WDC2436 is the dual-role type on this order and must add up exactly.
        self.assertEqual(hosts["WDC2436"] + inners["WDC2436"], 30)

    def test_every_pair_is_geometrically_legal(self):
        for host, inner, count in assign_inners(ORDER_7_21_26).pairs:
            self.assertGreater(count, 0)
            h, i = BY_NAME[host], BY_NAME[inner]
            self.assertIsNotNone(
                best_fit(host, h.width, h.height, i.width, i.height),
                f"{inner} was assigned to {host} but does not fit",
            )

    def test_barring_an_inner_type_removes_it_entirely(self):
        assignment = assign_inners(ORDER_7_21_26, 0.375, ["B18"])
        self.assertNotIn("B18", assignment.inners_used())
        self.assertLess(assignment.total, 92)

    def test_assignment_is_deterministic(self):
        first = assign_inners(ORDER_7_21_26)
        second = assign_inners(list(reversed(ORDER_7_21_26)))
        self.assertEqual(first.pairs, second.pairs)

    def test_nothing_fits_gives_an_empty_assignment(self):
        assignment = assign_inners([PartSpec("W3012", 30.0, 12.0, 30)])
        self.assertEqual(assignment.total, 0)
        self.assertEqual(assignment.pairs, ())

    def test_smallest_inner_wins_when_a_host_must_choose(self):
        # One W2436 host (opening 21 x 33) and two candidates that both fit;
        # only one can go in, and the spec prefers the smaller (more residual
        # web = better vacuum hold).
        assignment = assign_inners(
            [
                PartSpec("W2436", 24.0, 36.0, 1),
                PartSpec("B18", 18.0, 30.0, 1),
                PartSpec("W3012", 30.0, 12.0, 1),
            ]
        )
        self.assertEqual(assignment.pairs, (("W2436", "W3012", 1),))


class CenteringTests(unittest.TestCase):
    """The inner is centred in the opening, in sheet coordinates."""

    def setUp(self):
        self.config = NestingConfig(inside_nesting=True)
        self.host_spec = BY_NAME["W2436"]      # 24 x 36, opening 21 x 33 @ (1.5, 1.5)
        self.inner_spec = BY_NAME["W3012"]     # 30 x 12, goes in rotated: 12 x 30

    def test_unrotated_host(self):
        # host-local x = 1.5 + (21 - 12)/2 = 6.0, y = 1.5 + (33 - 30)/2 = 3.0
        host = Placement("W2436", 4.0, 7.0, 24.0, 36.0, rotated=False)
        child = place_inner(host, self.host_spec, self.inner_spec, self.config)
        self.assertEqual(child.part_number, "W3012")
        self.assertAlmostEqual(child.x, 4.0 + 6.0)
        self.assertAlmostEqual(child.y, 7.0 + 3.0)
        self.assertAlmostEqual(child.width, 12.0)
        self.assertAlmostEqual(child.height, 30.0)
        self.assertTrue(child.rotated)

    def test_rotated_host_transforms_the_child(self):
        # The host is turned 90 deg CCW, so its footprint is 36 x 24 and a
        # host-local (lx, ly) lands at (ordered_h - ly - h, lx) = (36 - 3 - 30,
        # 6) = (3.0, 6.0) inside that footprint.
        host = Placement("W2436", 4.0, 7.0, 36.0, 24.0, rotated=True)
        child = place_inner(host, self.host_spec, self.inner_spec, self.config)
        self.assertAlmostEqual(child.x, 4.0 + 3.0)
        self.assertAlmostEqual(child.y, 7.0 + 6.0)
        self.assertAlmostEqual(child.width, 30.0)
        self.assertAlmostEqual(child.height, 12.0)
        # Turned inside a turned host comes out upright on the sheet.
        self.assertFalse(child.rotated)

    def test_the_child_is_centred_both_ways_round(self):
        for rotated, w, h in ((False, 24.0, 36.0), (True, 36.0, 24.0)):
            host = Placement("W2436", 2.0, 3.0, w, h, rotated=rotated)
            child = place_inner(host, self.host_spec, self.inner_spec, self.config)
            # Equal margins from the host footprint's opposite edges is what
            # centring in a centred opening means for a wall frame.
            self.assertAlmostEqual(
                child.x - host.x, (host.x + host.width) - (child.x + child.width)
            )
            self.assertAlmostEqual(
                child.y - host.y, (host.y + host.height) - (child.y + child.height)
            )

    def test_a_child_that_does_not_fit_is_refused(self):
        host = Placement("W3330", 0.0, 0.0, 33.0, 30.0)
        self.assertIsNone(
            place_inner(host, BY_NAME["W3330"], BY_NAME["B18"], self.config)
        )


class FullOrderInsideNestingTests(unittest.TestCase):
    """The real 7-21-26 order with spec 4b switched on."""

    @classmethod
    def setUpClass(cls):
        cls.config = NestingConfig(inside_nesting=True)
        start = time.perf_counter()
        cls.result = nest(ORDER_7_21_26, cls.config)
        cls.elapsed = time.perf_counter() - start

    def test_validator_finds_no_violations(self):
        problems = validate_layouts(self.result, self.config)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_sheet_count_beats_the_milestone_2_baseline(self):
        print("\n" + self.result.summary())
        print(f"  nest() elapsed: {self.elapsed * 1000:.0f} ms")
        self.assertEqual(self.result.baseline_sheets, M2_BASELINE_SHEETS)
        self.assertLess(self.result.total_sheets, M2_BASELINE_SHEETS)
        self.assertLessEqual(
            self.result.total_sheets,
            40,
            f"inside nesting quality regressed: {self.result.total_sheets} sheets",
        )
        self.assertEqual(self.result.sheets_saved, M2_BASELINE_SHEETS - self.result.total_sheets)
        self.assertGreaterEqual(
            self.result.total_sheets,
            self.result.area_lower_bound_sheets,
            "below the area floor is physically impossible",
        )

    def test_inside_placements(self):
        """80 frames nested, not the 92 the pairing phase can reach.

        Spec section 4 ranks "minimise total sheets" above "maximise inside
        placements", and on this order they conflict: the 92-inner assignment
        needs 41 sheets, while giving up B18 as an inner nests 80 and needs
        40.  B18 is 18 x 30 and rides for free in the 18" left beside a
        30"-wide frame, so hiding it inside a host recovers nothing and
        wastes a host slot.  The packer chooses; this pins the choice.
        """
        self.assertEqual(self.result.inside_placements, 80)
        counted = sum(
            layout.child_count() * run for layout, run in self.result.unique_sheets
        )
        self.assertEqual(counted, self.result.inside_placements)

    def test_every_part_placed_exactly_qty_times_counting_children(self):
        placed: dict[str, int] = {}
        for layout, run in self.result.unique_sheets:
            for part_number, count in layout.part_counts().items():
                placed[part_number] = placed.get(part_number, 0) + count * run
        for spec in ORDER_7_21_26:
            self.assertEqual(
                placed.get(spec.part_number, 0),
                spec.qty,
                f"{spec.part_number}: placed {placed.get(spec.part_number, 0)}, "
                f"ordered {spec.qty}",
            )
        self.assertEqual(sum(placed.values()), TOTAL_PARTS)

    def test_dimensions_are_never_altered_at_any_depth(self):
        def walk(items):
            for p in items:
                spec = BY_NAME[p.part_number]
                if p.rotated:
                    self.assertAlmostEqual(p.width, spec.height, places=9)
                    self.assertAlmostEqual(p.height, spec.width, places=9)
                else:
                    self.assertAlmostEqual(p.width, spec.width, places=9)
                    self.assertAlmostEqual(p.height, spec.height, places=9)
                walk(p.children)

        for layout, _run in self.result.unique_sheets:
            walk(layout.placements)

    def test_at_most_one_inner_per_host_and_no_recursion(self):
        def walk(items, depth):
            for p in items:
                self.assertLessEqual(
                    len(p.children), 1, f"{p.part_number} carries more than one inner"
                )
                self.assertLessEqual(depth, 1, "optimizer must not build depth-2 nests")
                walk(p.children, depth + 1)

        for layout, _run in self.result.unique_sheets:
            walk(layout.placements, 0)

    def test_identical_host_plus_inner_combos_group_into_runs(self):
        runs = [run for _layout, run in self.result.unique_sheets]
        self.assertEqual(sum(runs), self.result.total_sheets)
        self.assertLess(self.result.unique_sheet_count, self.result.total_sheets)
        canonicals = [layout.canonical() for layout, _run in self.result.unique_sheets]
        self.assertEqual(len(canonicals), len(set(canonicals)))
        # A repeated picture that actually carries an inner is the point: it
        # proves children participate in sheet identity rather than blocking it.
        nested_runs = [
            run
            for layout, run in self.result.unique_sheets
            if layout.child_count() > 0
        ]
        self.assertGreaterEqual(max(nested_runs), 2)

    def test_canonical_form_distinguishes_the_passenger(self):
        bare = SheetLayout([Placement("W2436", 0.0, 0.0, 24.0, 36.0)])
        loaded = SheetLayout(
            [
                Placement(
                    "W2436",
                    0.0,
                    0.0,
                    24.0,
                    36.0,
                    children=[Placement("W3012", 6.0, 3.0, 12.0, 30.0, rotated=True)],
                )
            ]
        )
        self.assertNotEqual(bare.canonical(), loaded.canonical())

    def test_determinism(self):
        again = nest(ORDER_7_21_26, NestingConfig(inside_nesting=True))
        self.assertEqual(again.total_sheets, self.result.total_sheets)
        self.assertEqual(again.inside_placements, self.result.inside_placements)
        self.assertEqual(
            [(l.canonical(), r) for l, r in again.unique_sheets],
            [(l.canonical(), r) for l, r in self.result.unique_sheets],
        )

    def test_determinism_is_order_independent(self):
        again = nest(list(reversed(ORDER_7_21_26)), NestingConfig(inside_nesting=True))
        self.assertEqual(
            [(l.canonical(), r) for l, r in again.unique_sheets],
            [(l.canonical(), r) for l, r in self.result.unique_sheets],
        )

    def test_runs_fast(self):
        self.assertLess(self.elapsed, 5.0, f"nest() took {self.elapsed:.2f}s")


class MilestoneTwoRegressionTests(unittest.TestCase):
    """Inside nesting off must change absolutely nothing."""

    def test_default_config_reproduces_the_47_sheet_layout(self):
        default = nest(ORDER_7_21_26, NestingConfig())
        explicit = nest(ORDER_7_21_26, NestingConfig(inside_nesting=False))
        self.assertEqual(default.total_sheets, M2_BASELINE_SHEETS)
        self.assertEqual(
            [(l.canonical(), r) for l, r in default.unique_sheets],
            [(l.canonical(), r) for l, r in explicit.unique_sheets],
        )
        self.assertEqual(default.inside_placements, 0)
        self.assertIsNone(default.baseline_sheets)
        for layout, _run in default.unique_sheets:
            for p in layout.placements:
                self.assertEqual(p.children, [])

    def test_inside_nesting_defaults_to_off(self):
        cfg = NestingConfig()
        self.assertFalse(cfg.inside_nesting)
        self.assertFalse(cfg.inside_recursion)

    def test_baseline_pass_can_be_switched_off(self):
        result = nest(
            ORDER_7_21_26, NestingConfig(inside_nesting=True, inside_baseline=False)
        )
        self.assertIsNone(result.baseline_sheets)
        self.assertIsNone(result.sheets_saved)
        self.assertGreater(result.inside_placements, 0)

    def test_an_order_where_nothing_fits_inside_anything(self):
        parts = [PartSpec("W3012", 30.0, 12.0, 12)]
        with_inside = nest(parts, NestingConfig(inside_nesting=True))
        without = nest(parts, NestingConfig())
        self.assertEqual(with_inside.inside_placements, 0)
        self.assertEqual(with_inside.total_sheets, without.total_sheets)
        self.assertEqual(with_inside.baseline_sheets, without.total_sheets)
        self.assertEqual(validate_layouts(with_inside, NestingConfig(inside_nesting=True)), [])


class ValidatorInsideRulesTests(unittest.TestCase):
    """The containment rules must be able to FAIL — this feeds the NC verifier."""

    def setUp(self):
        self.config = NestingConfig(inside_nesting=True)

    def _result(self, placements, demand, config=None):
        return NestingResult(
            unique_sheets=[(SheetLayout(list(placements)), 1)],
            total_sheets=1,
            demand=list(demand),
            config=config if config is not None else self.config,
        )

    def _host(self, children):
        """A W2436 at (4, 7), unrotated, carrying ``children``."""
        return Placement("W2436", 4.0, 7.0, 24.0, 36.0, children=list(children))

    def test_a_properly_centred_child_is_accepted(self):
        child = Placement("W3012", 10.0, 10.0, 12.0, 30.0, rotated=True)
        result = self._result(
            [self._host([child])],
            [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 1)],
        )
        self.assertEqual(validate_layouts(result, self.config), [])

    def test_child_too_big_for_the_opening(self):
        # W2436's opening is 21 x 33; a B30 footprint is 30 x 30.
        child = Placement("B30", 5.0, 10.0, 30.0, 30.0)
        result = self._result(
            [self._host([child])],
            [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("B30", 30.0, 30.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("does not fit inside any single opening" in p for p in problems), problems)

    def test_child_with_only_point_three_clearance(self):
        # Opening spans x[5.5, 26.5]; a 20.4-wide child centred there leaves
        # 0.3 per side, under the 0.375 hard minimum.
        child = Placement("N204", 5.8, 10.0, 20.4, 30.0)
        result = self._result(
            [self._host([child])],
            [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("N204", 20.4, 30.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("does not fit inside any single opening" in p for p in problems), problems)
        self.assertTrue(any("short by 0.0750" in p for p in problems), problems)

    def test_exactly_the_clearance_is_legal(self):
        # 20.25 wide leaves exactly 0.375 per side across the 21" opening.
        child = Placement("N2025", 5.875, 10.0, 20.25, 30.0)
        result = self._result(
            [self._host([child])],
            [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("N2025", 20.25, 30.0, 1)],
        )
        self.assertEqual(validate_layouts(result, self.config), [])

    def test_child_straddling_a_cross_bar_is_rejected(self):
        # 3DB30 @ 30x30 has three openings (27x5, 27x9.875, 27x9.125) split by
        # 1.5" bars.  A child centred on the frame covers a bar, so it fits no
        # SINGLE opening even though it would fit the openings' bounding box.
        host = Placement("3DB30", 0.0, 0.0, 30.0, 30.0)
        child = Placement("N", 3.0, 8.0, 20.0, 14.0, children=[])
        host.children.append(child)
        result = self._result(
            [host], [PartSpec("3DB30", 30.0, 30.0, 1), PartSpec("N", 20.0, 14.0, 1)]
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("does not fit inside any single opening" in p for p in problems), problems)

    def test_child_inside_one_drawer_opening_is_accepted(self):
        # Same host, but the child sits wholly in the 27 x 9.875 middle
        # opening, which spans x[1.5, 28.5] and y[12.125, 22.0].
        host = Placement("3DB30", 0.0, 0.0, 30.0, 30.0)
        host.children.append(Placement("N", 3.0, 13.0, 20.0, 8.0))
        result = self._result(
            [host], [PartSpec("3DB30", 30.0, 30.0, 1), PartSpec("N", 20.0, 8.0, 1)]
        )
        self.assertEqual(validate_layouts(result, self.config), [])

    def test_two_children_closer_than_the_gap_in_one_opening(self):
        # W2436's opening is x[5.5, 26.5]; two 7-wide children 0.2 apart.
        host = self._host(
            [
                Placement("N7", 6.0, 10.0, 7.0, 20.0),
                Placement("N7", 13.2, 10.0, 7.0, 20.0),
            ]
        )
        result = self._result(
            [host], [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("N7", 7.0, 20.0, 2)]
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("gap violation" in p for p in problems), problems)

    def test_two_children_a_full_gap_apart_are_legal(self):
        host = self._host(
            [
                Placement("N7", 6.0, 10.0, 7.0, 20.0),
                Placement("N7", 13.375, 10.0, 7.0, 20.0),
            ]
        )
        result = self._result(
            [host], [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("N7", 7.0, 20.0, 2)]
        )
        self.assertEqual(validate_layouts(result, self.config), [])

    def test_a_child_never_trips_the_gap_rule_against_its_own_host(self):
        # The child is inside the host's footprint by design; only the
        # containment rule polices that pair.
        child = Placement("W3012", 10.0, 10.0, 12.0, 30.0, rotated=True)
        result = self._result(
            [self._host([child])],
            [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertFalse(any("gap violation" in p for p in problems), problems)

    def test_a_child_still_gets_checked_against_other_top_level_parts(self):
        child = Placement("W3012", 10.0, 10.0, 12.0, 30.0, rotated=True)
        intruder = Placement("W3012", 10.1, 10.1, 12.0, 30.0, rotated=True)
        result = self._result(
            [self._host([child]), intruder],
            [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 2)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("gap violation" in p for p in problems), problems)

    def test_grandchild_is_rejected_unless_recursion_is_enabled(self):
        # A genuinely legal depth-2 chain, so the ONLY thing under test is the
        # config flag: B18 centred in W2742's 24 x 39 opening, and a 12 x 18
        # frame centred in B18's own 15 x 20.5 door opening.
        b18_spec = PartSpec("B18", 18.0, 30.0, 1)
        tiny_spec = PartSpec("N12", 12.0, 18.0, 1)
        host = Placement("W2742", 0.0, 0.0, 27.0, 42.0)
        child = place_inner(host, PartSpec("W2742", 27.0, 42.0, 1), b18_spec, self.config)
        grandchild = place_inner(child, b18_spec, tiny_spec, self.config)
        self.assertIsNotNone(grandchild)
        child.children.append(grandchild)
        host.children.append(child)
        demand = [PartSpec("W2742", 27.0, 42.0, 1), b18_spec, tiny_spec]

        off = self._result([host], demand)
        problems = validate_layouts(off, self.config)
        self.assertTrue(any("inside_recursion=False" in p for p in problems), problems)

        on_config = NestingConfig(inside_nesting=True, inside_recursion=True)
        on = self._result([host], demand, config=on_config)
        self.assertEqual(validate_layouts(on, on_config), [])

    def test_child_on_a_part_with_no_openings_is_rejected(self):
        # A 2"-wide frame is narrower than its own two 1.5" stiles, so the
        # geometry engine reports an error and it can never host anything.
        host = Placement("TINY", 0.0, 0.0, 2.0, 8.0)
        host.children.append(Placement("N1", 0.5, 2.0, 1.0, 1.0))
        result = self._result(
            [host], [PartSpec("TINY", 2.0, 8.0, 1), PartSpec("N1", 1.0, 1.0, 1)]
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(
            any("no routed openings" in p or "geometry is invalid" in p for p in problems),
            problems,
        )

    def test_children_count_toward_demand_accounting(self):
        child = Placement("W3012", 10.0, 10.0, 12.0, 30.0, rotated=True)
        result = self._result(
            [self._host([child])],
            [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 3)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("only 1 of 3" in p for p in problems), problems)

    def test_a_childs_altered_dimensions_are_caught(self):
        child = Placement("W3012", 10.0, 10.0, 11.0, 30.0, rotated=True)
        result = self._result(
            [self._host([child])],
            [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 1)],
        )
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("must never be altered" in p for p in problems), problems)

    def test_a_rotated_host_transform_is_validated_not_assumed(self):
        # The same child coordinates that are correct for an unrotated host
        # are wrong once the host turns; the validator recomputes and objects.
        good = Placement("W2436", 4.0, 7.0, 36.0, 24.0, rotated=True)
        good.children.append(
            place_inner(good, BY_NAME["W2436"], BY_NAME["W3012"], self.config)
        )
        demand = [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 1)]
        self.assertEqual(validate_layouts(self._result([good], demand), self.config), [])

        bad = Placement("W2436", 4.0, 7.0, 36.0, 24.0, rotated=True)
        bad.children.append(Placement("W3012", 10.0, 10.0, 12.0, 30.0, rotated=True))
        problems = validate_layouts(self._result([bad], demand), self.config)
        self.assertTrue(problems)

    def test_a_self_referential_placement_is_reported_not_crashed(self):
        host = Placement("W2436", 4.0, 7.0, 24.0, 36.0)
        host.children.append(host)
        result = self._result([host], [PartSpec("W2436", 24.0, 36.0, 1)])
        problems = validate_layouts(result, self.config)
        self.assertTrue(any("deeper than" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
