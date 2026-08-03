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
        NeedsAttentionLine,
        OrderLine,
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

    def test_wdc2436_needs_attention_missing_width(self):
        wdc = next(
            (l for l in self.result.needs_attention if l.part_number == "WDC2436"), None
        )
        self.assertIsNotNone(wdc, "WDC2436 should be in needs_attention (missing width)")
        self.assertIsNone(wdc.frame_width)
        self.assertEqual(wdc.frame_height, 36.0)
        self.assertEqual(wdc.missing, ("width",))
        # Must not also appear among the good lines.
        self.assertIsNone(_line_by_part(self.result.lines, "WDC2436"))

    def test_sd1212_needs_attention_missing_both(self):
        sd = next(
            (l for l in self.result.needs_attention if l.part_number == "SD1212"), None
        )
        self.assertIsNotNone(sd, "SD1212 should be in needs_attention (missing both dims)")
        self.assertIsNone(sd.frame_width)
        self.assertIsNone(sd.frame_height)
        self.assertEqual(sd.missing, ("width", "height"))
        self.assertIsNone(_line_by_part(self.result.lines, "SD1212"))

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


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class ResolveTests(unittest.TestCase):
    def test_resolve_wdc2436_with_supplied_width(self):
        result = parse_order(ORDER_7_21)
        wdc = next(l for l in result.needs_attention if l.part_number == "WDC2436")
        completed = resolve(wdc, width=24)
        self.assertIsInstance(completed, OrderLine)
        self.assertEqual(completed.frame_type, FrameType.WDC)
        self.assertEqual(completed.frame_width, 24.0)
        self.assertEqual(completed.frame_height, wdc.frame_height)
        self.assertEqual(completed.frame_height, 36.0)

    def test_resolve_raises_when_still_missing(self):
        result = parse_order(ORDER_7_21)
        sd = next(l for l in result.needs_attention if l.part_number == "SD1212")
        with self.assertRaises(ValueError):
            resolve(sd, width=12)  # height still missing


if __name__ == "__main__":
    unittest.main()
