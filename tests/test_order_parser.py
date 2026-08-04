"""Unit tests for faceframe_cnc.order_parser (Milestone 1b).

Requires pandas + xlrd (not part of the bare stdlib environment). Skips
cleanly when either is unavailable so ``python -m unittest discover tests``
keeps passing the Milestone 1a geometry tests on bare Python. Run the real
thing with the project venv:

    .\\.venv\\Scripts\\python.exe -m unittest discover tests -v
"""

import os
import unittest

try:
    import pandas  # noqa: F401
    import xlrd  # noqa: F401

    from faceframe_cnc.order_parser import (
        DrawerFaces,
        NeedsAttentionLine,
        OrderLine,
        derive_wdc_dimensions,
        parse_order,
        resolve,
        safe_float,
        safe_int,
    )
    from faceframe_cnc.geometry import FrameType

    _IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - exercised on bare stdlib Python
    _IMPORT_ERROR = exc

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORDERS_DIR = os.path.join(_HERE, "..", "reference", "orders")
ORDER_7_21 = os.path.join(_ORDERS_DIR, "7-21-26_Cab_Tec_Order_with_specs.xls")
ORDER_7_7 = os.path.join(_ORDERS_DIR, "7-7-26_Cab_Tec_Order_with_specs.xls")


def _line_by_part(lines, part_number):
    for line in lines:
        if line.part_number == part_number:
            return line
    return None


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class SafeFloatTests(unittest.TestCase):
    def test_junk_question_mark(self):
        self.assertIsNone(safe_float("?"))

    def test_empty_string(self):
        self.assertIsNone(safe_float(""))

    def test_none(self):
        self.assertIsNone(safe_float(None))

    def test_numeric_string_int(self):
        self.assertEqual(safe_float("24"), 24.0)

    def test_numeric_string_float(self):
        self.assertEqual(safe_float("24.5"), 24.5)

    def test_int_passthrough(self):
        self.assertEqual(safe_float(24), 24.0)

    def test_whitespace_padded(self):
        self.assertEqual(safe_float("  30 "), 30.0)

    def test_non_numeric_word(self):
        self.assertIsNone(safe_float("abc"))

    def test_nan_float(self):
        self.assertIsNone(safe_float(float("nan")))


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class SafeIntTests(unittest.TestCase):
    def test_junk(self):
        self.assertIsNone(safe_int("?"))

    def test_star(self):
        self.assertIsNone(safe_int("*"))

    def test_valid(self):
        self.assertEqual(safe_int("25"), 25)
        self.assertEqual(safe_int(25.0), 25)


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class Parse7_21Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = parse_order(ORDER_7_21)

    def test_lines_non_empty(self):
        self.assertGreater(len(self.result.lines), 0)

    def test_no_quantity_total_pseudo_line(self):
        for line in self.result.lines:
            self.assertNotEqual(line.part_number.strip().lower(), "quantity total")
        for line in self.result.needs_attention:
            self.assertNotEqual(line.part_number.strip().lower(), "quantity total")

    def test_every_good_line_has_positive_qty_and_numeric_dims(self):
        for line in self.result.lines:
            self.assertGreater(line.qty, 0)
            self.assertIsInstance(line.frame_width, float)
            self.assertIsInstance(line.frame_height, float)
            self.assertGreater(line.frame_width, 0)
            self.assertGreater(line.frame_height, 0)

    def test_wdc2436_missing_width_is_auto_resolved_from_the_part_number(self):
        # 2026-08-03 amendment ("WDC single-missing-dim auto-resolution"):
        # the 7-21 file's WDC2436 row has a blank width and height 36, and
        # 36 is exactly what the part number encodes -- so the parser fills
        # in 24 - 6 = 18 itself instead of prompting the owner on every
        # single load, and says where the number came from.
        wdc = _line_by_part(self.result.lines, "WDC2436")
        self.assertIsNotNone(wdc, "WDC2436 should now parse as a READY line")
        self.assertEqual(wdc.frame_width, 18.0)
        self.assertEqual(wdc.frame_height, 36.0)
        self.assertEqual(wdc.frame_type, FrameType.WDC)
        self.assertIn("width 18 derived from part number", wdc.note)
        self.assertIn("24x36", wdc.note)
        # And it must no longer appear in needs_attention.
        self.assertIsNone(
            next((l for l in self.result.needs_attention if l.part_number == "WDC2436"), None)
        )

    def test_the_7_21_order_now_has_nothing_needing_attention(self):
        # WDC2436 was the only single-missing-dim row in this file, and it
        # auto-resolves; SD1212 is a no_frame row.  Nothing prompts.
        self.assertEqual(self.result.needs_attention, [])

    def test_fully_specified_lines_carry_no_note(self):
        for line in self.result.lines:
            if line.part_number != "WDC2436":
                self.assertEqual(line.note, "", line.part_number)

    def test_sd1212_is_no_frame_not_needs_attention(self):
        # 2026-08-03 amendment ("SD1212 / no-faceframe lines"): SD1212 is a
        # sample door with BOTH frame dims blank on the order form (N/A --
        # no faceframe is cut for it), so it belongs in ``no_frame``, not
        # ``needs_attention`` (which is reserved for exactly-one-missing
        # rows like WDC2436).
        sd = next((l for l in self.result.no_frame if l.part_number == "SD1212"), None)
        self.assertIsNotNone(sd, "SD1212 should be in no_frame (missing both dims)")
        self.assertIsNone(sd.frame_width)
        self.assertIsNone(sd.frame_height)
        self.assertEqual(sd.missing, ("width", "height"))
        self.assertIsNone(_line_by_part(self.result.lines, "SD1212"))
        self.assertIsNone(
            next((l for l in self.result.needs_attention if l.part_number == "SD1212"), None)
        )

    def test_known_parts_and_frame_type_inference(self):
        expected = {
            "W3330": (FrameType.WALL, 33.0, 30.0),
            "W3012": (FrameType.WALL, 30.0, 12.0),
            "B18": (FrameType.BASE, 18.0, 30.0),
            "B30": (FrameType.BASE, 30.0, 30.0),
            "3DB24": (FrameType.THREE_DRAWER, 24.0, 30.0),
            "3DB30": (FrameType.THREE_DRAWER, 30.0, 30.0),
            "LS36": (FrameType.WALL, 36.0, 30.0),
        }
        for part, (ftype, width, height) in expected.items():
            with self.subTest(part=part):
                line = _line_by_part(self.result.lines, part)
                self.assertIsNotNone(line, f"{part} should be a parsed good line")
                self.assertEqual(line.frame_type, ftype)
                self.assertEqual(line.frame_width, width)
                self.assertEqual(line.frame_height, height)

    def test_any_3db_line_is_three_drawer(self):
        three_db_lines = [l for l in self.result.lines if l.part_number.upper().startswith("3DB")]
        self.assertGreater(len(three_db_lines), 0)
        for line in three_db_lines:
            self.assertEqual(line.frame_type, FrameType.THREE_DRAWER)


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class Parse7_7Tests(unittest.TestCase):
    def test_parses_without_exception_and_non_empty(self):
        result = parse_order(ORDER_7_7)
        self.assertGreater(len(result.lines), 0)
        for line in result.lines:
            self.assertGreater(line.qty, 0)
            self.assertGreater(line.frame_width, 0)
            self.assertGreater(line.frame_height, 0)

    def test_sd1212_is_no_frame_and_needs_attention_is_empty(self):
        # 2026-08-03 amendment: in the 7-7 file SD1212 is the only
        # incomplete row, and it is missing BOTH dims, so it lands in
        # no_frame -- leaving needs_attention empty for this file.
        result = parse_order(ORDER_7_7)
        sd = next((l for l in result.no_frame if l.part_number == "SD1212"), None)
        self.assertIsNotNone(sd, "SD1212 should be in no_frame (missing both dims)")
        self.assertEqual(sd.missing, ("width", "height"))
        self.assertEqual(result.needs_attention, [])
        self.assertIsNone(_line_by_part(result.lines, "SD1212"))


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class DeriveWdcTests(unittest.TestCase):
    """The 2026-08-03 WDC auto-resolution rule, one case at a time.

    The parser only ever derives when the dimension it HAS agrees with the
    part number -- a contradiction, or a row missing both dims, is never
    guessed over (spec section 2's "do NOT silently guess" stands).
    """

    def test_missing_width_with_matching_height_derives_18(self):
        derived = derive_wdc_dimensions("WDC2436", None, 36.0)
        self.assertIsNotNone(derived)
        width, height, note = derived
        self.assertEqual((width, height), (18.0, 36.0))
        self.assertIn("width 18 derived from part number", note)
        self.assertIn("WDC cabinet 24x36", note)
        self.assertIn("6in narrower", note)

    def test_missing_height_with_matching_width_derives_36(self):
        # The mirror rule: the present width must equal encoded width - 6.
        derived = derive_wdc_dimensions("WDC2436", 18.0, None)
        self.assertIsNotNone(derived)
        width, height, note = derived
        self.assertEqual((width, height), (18.0, 36.0))
        self.assertIn("height 36 derived from part number", note)

    def test_a_contradicting_height_is_never_guessed_over(self):
        self.assertIsNone(derive_wdc_dimensions("WDC2436", None, 35.0))

    def test_a_contradicting_width_is_never_guessed_over(self):
        # 24 is the CABINET width; a user or form saying the frame is 24
        # wide contradicts the name (frame = 18) and must be looked at.
        self.assertIsNone(derive_wdc_dimensions("WDC2436", 24.0, None))

    def test_both_missing_stays_a_no_frame_row(self):
        # The SD1212 amendment owns this case: nothing to derive from.
        self.assertIsNone(derive_wdc_dimensions("WDC2436", None, None))

    def test_both_present_derives_nothing(self):
        self.assertIsNone(derive_wdc_dimensions("WDC2436", 18.0, 36.0))

    def test_non_wdc_and_unparseable_names_derive_nothing(self):
        self.assertIsNone(derive_wdc_dimensions("W3036", None, 36.0))
        self.assertIsNone(derive_wdc_dimensions("WDC24365", None, 36.0))
        self.assertIsNone(derive_wdc_dimensions("WDCX436", None, 36.0))

    def test_an_impossible_encoded_width_derives_nothing(self):
        # WDC0436 would encode a 4" cabinet -> a -2" frame; needs a human.
        self.assertIsNone(derive_wdc_dimensions("WDC0436", None, 36.0))

    def test_the_gui_explains_with_the_same_reduction_the_parser_derives_with(self):
        # The order panel's WDC fact sheet quotes "6 inches narrower" from
        # its own constant (the session must import without pandas, so it
        # cannot use the parser's); this is the pin that stops the two
        # numbers drifting apart.
        from faceframe_cnc.gui.session import WDC_CABINET_WIDTH_REDUCTION
        from faceframe_cnc.order_parser import _WDC_FRAME_WIDTH_REDUCTION

        self.assertEqual(WDC_CABINET_WIDTH_REDUCTION, _WDC_FRAME_WIDTH_REDUCTION)


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class ResolveTests(unittest.TestCase):
    def test_resolve_a_wdc_line_that_contradicts_its_part_number(self):
        # A WDC row the auto-resolution refuses (height 35 contradicts the
        # encoded 36) still reaches the user through needs_attention and is
        # resolved by hand exactly as before.  Synthetic, because the real
        # 7-21 row now auto-resolves and never gets here.
        wdc = NeedsAttentionLine(
            row_index=12,
            part_number="WDC2436",
            qty=30,
            frame_width=None,
            frame_height=35.0,
            frame_type=FrameType.WDC,
            drawer_faces=DrawerFaces(),
            missing=("width",),
            reason="missing frame width",
        )
        completed = resolve(wdc, width=18)
        self.assertIsInstance(completed, OrderLine)
        self.assertEqual(completed.frame_type, FrameType.WDC)
        self.assertEqual(completed.frame_width, 18.0)
        self.assertEqual(completed.frame_height, 35.0)

    def test_resolve_raises_when_still_missing(self):
        result = parse_order(ORDER_7_21)
        sd = next(l for l in result.no_frame if l.part_number == "SD1212")
        with self.assertRaises(ValueError):
            resolve(sd, width=12)  # height still missing

    def test_resolve_no_frame_sd1212_yields_a_valid_order_line(self):
        # 2026-08-03 amendment: a no_frame line stays manually resolvable --
        # a user can still type dims and include the line if the order form
        # was actually wrong. The qty-0 "Sample Door" row for SD1212 in this
        # same file shows a 12 x 12 frame, so use that as the plausible
        # resolution.
        result = parse_order(ORDER_7_21)
        sd = next(l for l in result.no_frame if l.part_number == "SD1212")
        completed = resolve(sd, width=12, height=12)
        self.assertIsInstance(completed, OrderLine)
        self.assertEqual(completed.part_number, "SD1212")
        self.assertEqual(completed.qty, sd.qty)
        self.assertEqual(completed.frame_width, 12.0)
        self.assertEqual(completed.frame_height, 12.0)


if __name__ == "__main__":
    unittest.main()
