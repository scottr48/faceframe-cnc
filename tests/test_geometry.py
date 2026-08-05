"""Unit tests for faceframe_cnc.geometry (Milestone 1a).

Exact worked examples come from docs/CLAUDE_CODE_PROMPT_Faceframe_Optimizer.md
section 3. Run with: python -m unittest discover tests -v
"""

import math
import unittest

from faceframe_cnc.geometry import FrameType, compute_geometry, infer_frame_type

TOL = 1e-9


def opening_by_label(geometry, label):
    for opening in geometry.openings:
        if opening.label == label:
            return opening
    raise AssertionError(f"no opening labeled {label!r} in {geometry.openings!r}")


class InferFrameTypeTests(unittest.TestCase):
    def test_three_drawer_prefix(self):
        self.assertEqual(infer_frame_type("3DB24"), FrameType.THREE_DRAWER)

    def test_base_prefix(self):
        self.assertEqual(infer_frame_type("B30"), FrameType.BASE)
        self.assertEqual(infer_frame_type("B18"), FrameType.BASE)

    def test_bbc_is_wall_not_base(self):
        self.assertEqual(infer_frame_type("BBC36"), FrameType.WALL)

    def test_wall_family_parts(self):
        for part in ("W3330", "LS36", "SD1212"):
            with self.subTest(part=part):
                self.assertEqual(infer_frame_type(part), FrameType.WALL)

    def test_wdc_is_its_own_frame_type(self):
        self.assertEqual(infer_frame_type("WDC2436"), FrameType.WDC)

    def test_wdc_case_insensitive(self):
        self.assertEqual(infer_frame_type("wdc2436"), FrameType.WDC)

    def test_case_insensitive_and_stripped(self):
        self.assertEqual(infer_frame_type("  b30  "), FrameType.BASE)
        self.assertEqual(infer_frame_type("3db24"), FrameType.THREE_DRAWER)
        self.assertEqual(infer_frame_type("bbc36"), FrameType.WALL)

    def test_unsupported_drawer_base_families(self):
        # 2026-08-04 review fix 4: these drawer-base families (from the
        # reference orders' "Drawer Bases" catalogue section) have cross
        # bars this app does not know how to lay out -- they must never
        # fall through to WALL (a single opening, no cross bars at all).
        for part in (
            "2DB24",
            "2DB30",
            "2DB33",
            "2DB36",
            "4DB18",
            "MICRO3DB24",
            "MICRO3DB27",
            "MICRO3DB30",
        ):
            with self.subTest(part=part):
                self.assertEqual(
                    infer_frame_type(part), FrameType.UNSUPPORTED_DRAWER_BASE
                )

    def test_3db_is_not_unsupported(self):
        # 3DB... is checked FIRST and returns THREE_DRAWER -- a layout
        # this app does know -- never UNSUPPORTED_DRAWER_BASE.
        self.assertEqual(infer_frame_type("3DB24"), FrameType.THREE_DRAWER)
        self.assertEqual(infer_frame_type("3DB30"), FrameType.THREE_DRAWER)

    def test_unsupported_drawer_base_case_insensitive(self):
        self.assertEqual(infer_frame_type("2db24"), FrameType.UNSUPPORTED_DRAWER_BASE)
        self.assertEqual(
            infer_frame_type("micro3db24"), FrameType.UNSUPPORTED_DRAWER_BASE
        )


class WallGeometryTests(unittest.TestCase):
    def test_w3036(self):
        g = compute_geometry("W3036", 30, 36)
        self.assertEqual(g.errors, [])
        self.assertEqual(len(g.openings), 1)
        o = g.openings[0]
        self.assertAlmostEqual(o.x, 1.5, delta=TOL)
        self.assertAlmostEqual(o.y, 1.5, delta=TOL)
        self.assertAlmostEqual(o.width, 27, delta=TOL)
        self.assertAlmostEqual(o.height, 33, delta=TOL)

    def test_stack_sums_to_height(self):
        g = compute_geometry("W3036", 30, 36)
        o = g.openings[0]
        self.assertAlmostEqual(o.y + o.height + 1.5, 36, delta=TOL)


class WdcGeometryTests(unittest.TestCase):
    """WDC frames (2026-08-03 amendment): 2" stiles, single opening."""

    def test_wdc2436_at_18x36(self):
        g = compute_geometry("WDC2436", 18, 36)
        self.assertEqual(g.errors, [])
        self.assertEqual(g.frame_type, FrameType.WDC)
        self.assertEqual(len(g.openings), 1)
        o = g.openings[0]
        self.assertAlmostEqual(o.x, 2.0, delta=TOL)
        self.assertAlmostEqual(o.y, 1.5, delta=TOL)
        self.assertAlmostEqual(o.width, 14, delta=TOL)
        self.assertAlmostEqual(o.height, 33, delta=TOL)

    def test_wdc2436_lowercase_part_number(self):
        g = compute_geometry("wdc2436", 18, 36)
        self.assertEqual(g.frame_type, FrameType.WDC)
        self.assertEqual(g.errors, [])

    def test_wdc2436_stack_sums_to_height(self):
        g = compute_geometry("WDC2436", 18, 36)
        o = g.openings[0]
        self.assertAlmostEqual(o.y + o.height + 1.5, 36, delta=TOL)

    def test_wdc2436_opening_right_edge_consistent_with_stile_width(self):
        g = compute_geometry("WDC2436", 18, 36)
        o = g.openings[0]
        self.assertAlmostEqual(o.x + o.width + 2.0, 18, delta=TOL)

    def test_wdc_width_at_boundary_gives_zero_opening_width(self):
        # 2" stiles on each side: width 4 -> opening width 0 -> error.
        g = compute_geometry("WDC2436", 4, 36)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_wdc_width_below_boundary(self):
        g = compute_geometry("WDC2436", 3, 36)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)


class UnsupportedDrawerBaseGeometryTests(unittest.TestCase):
    """2026-08-04 review fix 4: never silently produce WALL geometry for these."""

    def test_compute_geometry_refuses_2db24(self):
        with self.assertRaises(ValueError):
            compute_geometry("2DB24", 24, 30)

    def test_compute_geometry_refuses_4db18(self):
        with self.assertRaises(ValueError):
            compute_geometry("4DB18", 18, 30)

    def test_compute_geometry_refuses_micro3db24(self):
        with self.assertRaises(ValueError):
            compute_geometry("MICRO3DB24", 24, 30)


class BaseGeometryTests(unittest.TestCase):
    def test_b30_at_30x34_5(self):
        g = compute_geometry("B30", 30, 34.5)
        self.assertEqual(g.errors, [])
        self.assertEqual(g.frame_type, FrameType.BASE)
        self.assertEqual([o.label for o in g.openings], ["drawer", "door"])

        drawer = opening_by_label(g, "drawer")
        door = opening_by_label(g, "door")

        self.assertAlmostEqual(drawer.width, 27, delta=TOL)
        self.assertAlmostEqual(drawer.height, 5, delta=TOL)
        self.assertAlmostEqual(door.width, 27, delta=TOL)
        self.assertAlmostEqual(door.height, 25, delta=TOL)

        # Bottom-up: bottom rail 1.5 -> door y=1.5, top=26.5 -> bar to 28
        # -> drawer y=28, top=33 -> top rail to 34.5.
        self.assertAlmostEqual(door.y, 1.5, delta=TOL)
        self.assertAlmostEqual(door.y + door.height, 26.5, delta=TOL)
        self.assertAlmostEqual(drawer.y, 28.0, delta=TOL)
        self.assertAlmostEqual(drawer.y + drawer.height, 33.0, delta=TOL)

    def test_b30_stack_sums_to_height(self):
        g = compute_geometry("B30", 30, 34.5)
        drawer = opening_by_label(g, "drawer")
        door = opening_by_label(g, "door")
        total = 1.5 + drawer.height + 1.5 + door.height + 1.5
        self.assertAlmostEqual(total, 34.5, delta=TOL)
        self.assertAlmostEqual(door.y, 1.5, delta=TOL)
        self.assertAlmostEqual(drawer.y, door.y + door.height + 1.5, delta=TOL)

    def test_b30_at_30x30_matches_r730101n_sheet(self):
        # The real R730101N.anc sheet cuts a B30 at 30x30: drawer 27x5,
        # door 27x20.5 (H - 9.5), confirmed by decoding its T11 cut paths.
        g = compute_geometry("B30", 30, 30)
        self.assertEqual(g.errors, [])
        drawer = opening_by_label(g, "drawer")
        door = opening_by_label(g, "door")
        self.assertAlmostEqual(drawer.width, 27, delta=TOL)
        self.assertAlmostEqual(drawer.height, 5, delta=TOL)
        self.assertAlmostEqual(door.width, 27, delta=TOL)
        self.assertAlmostEqual(door.height, 20.5, delta=TOL)

    def test_b18_at_18x30(self):
        g = compute_geometry("B18", 18, 30)
        self.assertEqual(g.errors, [])
        drawer = opening_by_label(g, "drawer")
        door = opening_by_label(g, "door")
        self.assertAlmostEqual(drawer.width, 15, delta=TOL)
        self.assertAlmostEqual(drawer.height, 5, delta=TOL)
        self.assertAlmostEqual(door.width, 15, delta=TOL)
        self.assertAlmostEqual(door.height, 20.5, delta=TOL)

    def test_lowercase_part_number(self):
        g = compute_geometry("b30", 30, 34.5)
        self.assertEqual(g.frame_type, FrameType.BASE)
        self.assertEqual(g.errors, [])


class ThreeDrawerGeometryTests(unittest.TestCase):
    def test_3db30_at_30x30(self):
        g = compute_geometry("3DB30", 30, 30)
        self.assertEqual(g.errors, [])
        self.assertEqual([o.label for o in g.openings], ["top", "middle", "bottom"])

        top = opening_by_label(g, "top")
        middle = opening_by_label(g, "middle")
        bottom = opening_by_label(g, "bottom")

        for o in (top, middle, bottom):
            self.assertAlmostEqual(o.width, 27, delta=TOL)
        self.assertAlmostEqual(top.height, 5, delta=TOL)
        self.assertAlmostEqual(middle.height, 9.875, delta=TOL)
        self.assertAlmostEqual(bottom.height, 9.125, delta=TOL)

        # Bottom-up: bottom rail 1.5 -> bottom y=1.5 top=10.625 -> bar to
        # 12.125 -> middle y=12.125 top=22 -> bar to 23.5 -> top y=23.5
        # top=28.5 -> top rail to 30.
        self.assertAlmostEqual(bottom.y, 1.5, delta=TOL)
        self.assertAlmostEqual(bottom.y + bottom.height, 10.625, delta=TOL)
        self.assertAlmostEqual(middle.y, 12.125, delta=TOL)
        self.assertAlmostEqual(middle.y + middle.height, 22.0, delta=TOL)
        self.assertAlmostEqual(top.y, 23.5, delta=TOL)
        self.assertAlmostEqual(top.y + top.height, 28.5, delta=TOL)

    def test_3db24_at_24x30(self):
        g = compute_geometry("3DB24", 24, 30)
        self.assertEqual(g.errors, [])
        top = opening_by_label(g, "top")
        middle = opening_by_label(g, "middle")
        bottom = opening_by_label(g, "bottom")
        for o in (top, middle, bottom):
            self.assertAlmostEqual(o.width, 21, delta=TOL)
        self.assertAlmostEqual(top.height, 5, delta=TOL)
        self.assertAlmostEqual(middle.height, 9.875, delta=TOL)
        self.assertAlmostEqual(bottom.height, 9.125, delta=TOL)

    def test_stack_sums_to_height(self):
        g = compute_geometry("3DB30", 30, 30)
        top = opening_by_label(g, "top")
        middle = opening_by_label(g, "middle")
        bottom = opening_by_label(g, "bottom")
        total = 1.5 + top.height + 1.5 + middle.height + 1.5 + bottom.height + 1.5
        self.assertAlmostEqual(total, 30, delta=TOL)


class ErrorCaseTests(unittest.TestCase):
    def test_three_drawer_too_short(self):
        g = compute_geometry("3DB12", 12, 20)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_base_frame_exactly_zero_remainder(self):
        g = compute_geometry("B30", 30, 9.5)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_base_frame_negative_remainder(self):
        g = compute_geometry("B30", 30, 9.0)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_zero_width(self):
        g = compute_geometry("W3036", 0, 36)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_negative_width(self):
        g = compute_geometry("W3036", -30, 36)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_zero_height(self):
        g = compute_geometry("W3036", 30, 0)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_width_at_boundary_gives_zero_opening_width(self):
        g = compute_geometry("W3036", 3, 36)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_width_below_boundary(self):
        g = compute_geometry("W3036", 2, 36)
        self.assertEqual(g.openings, [])
        self.assertTrue(g.errors)

    def test_non_finite_inputs(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(bad=bad):
                g = compute_geometry("W3036", bad, 36)
                self.assertEqual(g.openings, [])
                self.assertTrue(g.errors)
                g2 = compute_geometry("W3036", 30, bad)
                self.assertEqual(g2.openings, [])
                self.assertTrue(g2.errors)


if __name__ == "__main__":
    unittest.main()
