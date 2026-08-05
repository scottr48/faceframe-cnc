"""Unit tests for faceframe_cnc.order_parser (Milestone 1b).

Requires pandas + xlrd (not part of the bare stdlib environment). Skips
cleanly when either is unavailable so ``python -m unittest discover tests``
keeps passing the Milestone 1a geometry tests on bare Python. Run the real
thing with the project venv:

    .\\.venv\\Scripts\\python.exe -m unittest discover tests -v
"""

import math
import os
import unittest
from unittest.mock import patch

try:
    import pandas as pd  # noqa: F401
    import xlrd  # noqa: F401

    from faceframe_cnc import order_parser as op
    from faceframe_cnc.order_parser import (
        DrawerFaces,
        NeedsAttentionLine,
        OrderLine,
        check_wdc_dimensions,
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


def _by_part(entries, part_number):
    for entry in entries:
        if entry.part_number == part_number:
            return entry
    return None


def _build_df(rows, ncols=21):
    """Build a bare-index DataFrame like ``pd.read_excel(header=None)`` would.

    ``rows`` is a list of ``{col_index: value}`` dicts; every column not
    given a value is ``None`` (pandas reads a genuinely blank cell as
    ``NaN``, but ``None`` round-trips the same way through
    :func:`safe_float`/:func:`safe_int`/``_safe_str`` -- see
    ``order_parser.safe_float``'s own ``None`` handling).
    """
    data = []
    for row in rows:
        values = [None] * ncols
        for idx, value in row.items():
            values[idx] = value
        data.append(values)
    return pd.DataFrame(data)


def _row(part, qty=None, w=None, h=None):
    return {op.COL_PART: part, op.COL_QTY: qty, op.COL_FRAME_W: w, op.COL_FRAME_H: h}


def _parse_rows(rows):
    """Run :func:`parse_order` against synthetic rows via a mocked read_excel.

    The real order files never exercise every corner this session's
    review fixes touch (a fractional QTY, an actually-ordered drawer-base
    family, ...), so these tests build minimal synthetic sheets instead of
    depending on a real file having the right junk in it.
    """
    df = _build_df(rows)
    with patch("faceframe_cnc.order_parser.pd.read_excel", return_value=df):
        return parse_order("synthetic.xls")


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

    # 2026-08-04 review fix 2: neither branch may let a non-finite value
    # through -- the numeric branch used to filter NaN but not inf, and
    # the string branch filtered NEITHER.
    def test_inf_float(self):
        self.assertIsNone(safe_float(float("inf")))
        self.assertIsNone(safe_float(float("-inf")))

    def test_inf_string(self):
        self.assertIsNone(safe_float("inf"))
        self.assertIsNone(safe_float("-inf"))
        self.assertIsNone(safe_float("Infinity"))

    def test_overflow_string(self):
        # float("1e999") overflows to inf -- still not finite.
        self.assertIsNone(safe_float("1e999"))

    def test_nan_string(self):
        self.assertIsNone(safe_float("nan"))


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class SafeIntTests(unittest.TestCase):
    def test_junk(self):
        self.assertIsNone(safe_int("?"))

    def test_star(self):
        self.assertIsNone(safe_int("*"))

    def test_valid(self):
        self.assertEqual(safe_int("25"), 25)
        self.assertEqual(safe_int(25.0), 25)

    # 2026-08-04 review fix 3: an exactly-integral value still parses
    # (even when it arrives as a float or a numeric string), but anything
    # fractional or non-finite is refused rather than floored.
    def test_exact_integral_float_still_parses(self):
        self.assertEqual(safe_int(2.0), 2)
        self.assertEqual(safe_int("2"), 2)
        self.assertEqual(safe_int("2.0"), 2)

    def test_fractional_qty_is_not_floored(self):
        self.assertIsNone(safe_int(2.9))
        self.assertIsNone(safe_int("2.9"))
        self.assertIsNone(safe_int(0.9))
        self.assertIsNone(safe_int("0.9"))

    def test_non_finite_qty_is_refused(self):
        self.assertIsNone(safe_int(float("inf")))
        self.assertIsNone(safe_int("inf"))
        self.assertIsNone(safe_int("1e999"))

    def test_blank_and_nan_are_not_a_number_at_all(self):
        # A blank/NaN cell means "no qty entered" -- distinct from a
        # fractional/non-finite entered value, and both still route to
        # ``None`` out of safe_int (parse_order tells them apart itself
        # via ``_raw_number`` when it needs to).
        self.assertIsNone(safe_int(float("nan")))
        self.assertIsNone(safe_int(None))


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

    def test_wdc2436_template_prefill_is_auto_corrected(self):
        # 2026-08-04 review fix 1 -- the verified reproduction: this real
        # file's WDC2436 row is QTY 30, W=24, H=36 -- both dims present,
        # but 24 is the CABINET width the order template prefilled, not
        # the true 18" frame width.  Must auto-correct to 18x36 with a
        # visible provenance note, not parse READY at the wrong 24x36.
        result = parse_order(ORDER_7_7)
        wdc = _line_by_part(result.lines, "WDC2436")
        self.assertIsNotNone(wdc, "WDC2436 should parse as a READY line")
        self.assertEqual(wdc.qty, 30)
        self.assertEqual(wdc.frame_width, 18.0)
        self.assertEqual(wdc.frame_height, 36.0)
        self.assertIn("width 18", wdc.note)
        self.assertIn("prefilled by the order template", wdc.note)
        self.assertIsNone(
            next((l for l in result.needs_attention if l.part_number == "WDC2436"), None)
        )
        # needs_attention is otherwise unchanged from before this fix --
        # still empty for this file (only SD1212 is incomplete, and it's
        # a no_frame row, not needs_attention).
        self.assertEqual(result.needs_attention, [])


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
class CheckWdcDimensionsTests(unittest.TestCase):
    """2026-08-04 review fix 1: the both-dims-present WDC contradiction check.

    Direct unit tests of :func:`check_wdc_dimensions`; the real-file
    reproduction lives in ``Parse7_7Tests.test_wdc2436_template_prefill_is_auto_corrected``.
    """

    def test_both_dims_correct_is_unchanged_with_no_note(self):
        width, height, note = check_wdc_dimensions("WDC2436", 18.0, 36.0)
        self.assertEqual((width, height), (18.0, 36.0))
        self.assertEqual(note, "")

    def test_template_prefill_is_auto_corrected_with_a_note(self):
        # The verified bug: the order template puts the CABINET width (24)
        # in the FRAME W column instead of the true 18" frame width.
        width, height, note = check_wdc_dimensions("WDC2436", 24.0, 36.0)
        self.assertEqual((width, height), (18.0, 36.0))
        self.assertIn("width 18", note)
        self.assertIn("prefilled by the order template", note)
        self.assertIn("24x36", note)

    def test_contradicting_width_raises(self):
        # 20 matches neither the encoded frame width (18) nor the raw
        # cabinet width (24) -- a real contradiction, never guessed over.
        with self.assertRaises(ValueError) as ctx:
            check_wdc_dimensions("WDC2436", 20.0, 36.0)
        message = str(ctx.exception)
        self.assertIn("20", message)
        self.assertIn("36", message)
        self.assertIn("24x36", message)
        self.assertIn("18x36", message)

    def test_contradicting_height_raises_even_if_width_matches(self):
        with self.assertRaises(ValueError):
            check_wdc_dimensions("WDC2436", 18.0, 35.0)
        with self.assertRaises(ValueError):
            check_wdc_dimensions("WDC2436", 24.0, 35.0)

    def test_non_decodable_name_is_left_unchanged(self):
        # Not a plain WDC<ww><hh> name -- leave current behavior, per the
        # fix's own spec ("A WDC part number that doesn't decode -> leave
        # current behavior").
        width, height, note = check_wdc_dimensions("WDCX436", 24.0, 36.0)
        self.assertEqual((width, height), (24.0, 36.0))
        self.assertEqual(note, "")


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class QtyNeedsAttentionTests(unittest.TestCase):
    """2026-08-04 review fix 3: a non-integral/non-finite QTY is never floored."""

    def test_fractional_qty_routes_to_needs_attention(self):
        result = _parse_rows([_row("W3036", qty=2.9, w=30, h=36)])
        self.assertIsNone(_line_by_part(result.lines, "W3036"))
        entry = _by_part(result.needs_attention, "W3036")
        self.assertIsNotNone(entry, "a fractional qty must reach needs_attention")
        self.assertEqual(entry.qty, 2.9)
        self.assertEqual(entry.missing, ())
        self.assertIn("2.9", entry.reason)
        self.assertIn("not a whole number", entry.reason)
        # The dims are still carried through even though qty is the
        # problem -- a UI can show the rest of the row's data.
        self.assertEqual(entry.frame_width, 30.0)
        self.assertEqual(entry.frame_height, 36.0)

    def test_qty_below_one_is_not_silently_dropped(self):
        # The exact bug: floor(0.9) == 0 -> used to vanish as "qty <= 0"
        # with zero record the row ever existed.
        result = _parse_rows([_row("W3036", qty=0.9, w=30, h=36)])
        self.assertIsNone(_line_by_part(result.lines, "W3036"))
        entry = _by_part(result.needs_attention, "W3036")
        self.assertIsNotNone(entry, "0.9 must not be silently skipped")
        self.assertEqual(entry.qty, 0.9)

    def test_infinite_qty_routes_to_needs_attention(self):
        result = _parse_rows([_row("W3036", qty=float("inf"), w=30, h=36)])
        entry = _by_part(result.needs_attention, "W3036")
        self.assertIsNotNone(entry, "an infinite qty must reach needs_attention")
        self.assertTrue(math.isinf(entry.qty))

    def test_exact_integral_qty_still_parses_as_a_ready_line(self):
        result = _parse_rows([_row("W3036", qty=3.0, w=30, h=36)])
        line = _line_by_part(result.lines, "W3036")
        self.assertIsNotNone(line)
        self.assertEqual(line.qty, 3)
        self.assertEqual(result.needs_attention, [])

    def test_blank_qty_is_still_silently_skipped(self):
        # A blank/NaN qty cell means "not ordering this line" -- unchanged
        # business-as-usual behavior, NOT a needs_attention case, even
        # though it is also technically "not a whole number".
        result = _parse_rows([_row("W3036", qty=None, w=30, h=36)])
        self.assertEqual(result.lines, [])
        self.assertEqual(result.needs_attention, [])
        self.assertEqual(result.skipped_rows, 1)

    def test_zero_and_negative_integral_qty_still_silently_skipped(self):
        result = _parse_rows(
            [_row("W3036", qty=0, w=30, h=36), _row("W2436", qty=-3, w=24, h=36)]
        )
        self.assertEqual(result.lines, [])
        self.assertEqual(result.needs_attention, [])
        self.assertEqual(result.skipped_rows, 2)


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class UnsupportedDrawerBaseRowTests(unittest.TestCase):
    """2026-08-04 review fix 4: drawer-base families this app can't lay out."""

    def test_each_family_routes_to_needs_attention_not_wall(self):
        families = [
            ("2DB24", 24, 30),
            ("2DB30", 30, 30),
            ("2DB33", 33, 30),
            ("2DB36", 36, 30),
            ("4DB18", 18, 30),
            ("MICRO3DB24", 24, 30),
            ("MICRO3DB27", 27, 30),
            ("MICRO3DB30", 30, 30),
        ]
        for part, w, h in families:
            with self.subTest(part=part):
                result = _parse_rows([_row(part, qty=5, w=w, h=h)])
                self.assertIsNone(
                    _line_by_part(result.lines, part),
                    f"{part} must never become a fabricated line",
                )
                entry = _by_part(result.needs_attention, part)
                self.assertIsNotNone(entry, f"{part} must reach needs_attention")
                self.assertEqual(entry.frame_type, FrameType.UNSUPPORTED_DRAWER_BASE)
                self.assertIn("drawer-base", entry.reason)
                self.assertIn(part, entry.reason)

    def test_unsupported_family_with_missing_dims_still_routes_there(self):
        # Even a row ALSO missing a dimension goes to needs_attention for
        # the family reason, not silently to no_frame or a WALL guess.
        result = _parse_rows([_row("2DB24", qty=5, w=None, h=30)])
        entry = _by_part(result.needs_attention, "2DB24")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.missing, ("width",))

    def test_qty_zero_is_still_silently_skipped(self):
        # Fix 4 only fires "when ordered, qty>0" -- a catalogue row nobody
        # ordered stays exactly as unremarkable as before.
        result = _parse_rows([_row("2DB24", qty=0, w=24, h=30)])
        self.assertEqual(result.needs_attention, [])
        self.assertEqual(result.skipped_rows, 1)


@unittest.skipUnless(_IMPORT_ERROR is None, f"pandas/xlrd unavailable: {_IMPORT_ERROR}")
class AccessoryRowTests(unittest.TestCase):
    """2026-08-04 review investigation: "B"-prefixed catalogue accessories.

    Neither reference order file ever orders BSK/BFD24/BPP9/BPP12/BES12/
    BF3 (every catalogue row for all of them is qty 0 or "*" in both
    files) -- verified by scanning both files directly, not assumed. This
    is a defensive fix for a hypothetical future order, exercised here
    with synthetic rows since no real file has one at qty > 0.
    """

    def test_each_known_accessory_routes_to_needs_attention_not_base(self):
        accessories = [
            ("BSK", 24, 34.5),
            ("BFD24", 24, 34.5),
            ("BPP9", 9, 30),
            ("BPP12", 12, 30),
            ("BES12", 12, 34.5),
            ("BF3", 3, 34.5),
        ]
        for part, w, h in accessories:
            with self.subTest(part=part):
                result = _parse_rows([_row(part, qty=4, w=w, h=h)])
                self.assertIsNone(
                    _line_by_part(result.lines, part),
                    f"{part} must never become a fabricated base-frame line",
                )
                entry = _by_part(result.needs_attention, part)
                self.assertIsNotNone(entry, f"{part} must reach needs_attention")
                self.assertIn("not a standard faceframe part", entry.reason)

    def test_real_base_parts_are_unaffected(self):
        # A genuine base frame (bare digits after "B") must still parse
        # normally -- the accessory check must not overreach.
        result = _parse_rows([_row("B18", qty=25, w=18, h=30)])
        line = _line_by_part(result.lines, "B18")
        self.assertIsNotNone(line)
        self.assertEqual(line.frame_type, FrameType.BASE)
        self.assertEqual(result.needs_attention, [])


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

    # -- 2026-08-04 review: what the caller supplies wins ----------------

    def wdc_contradiction(self) -> NeedsAttentionLine:
        """A WDC row whose entered dims contradict its part number (fix 1).

        The whole reason this row is here is that its 24 is NOT to be
        trusted -- so a caller correcting it to the encoded 18 must not have
        that correction silently discarded, which is what resolve() used to
        do with any dimension the line already had.
        """
        return NeedsAttentionLine(
            row_index=7,
            part_number="WDC2436",
            qty=30,
            frame_width=24.0,
            frame_height=36.0,
            frame_type=FrameType.WDC,
            drawer_faces=DrawerFaces(),
            missing=(),
            reason="WDC2436 entered as 24 x 36 but the part number encodes 18 x 36",
        )

    def test_a_supplied_dimension_overrides_a_present_one(self):
        completed = resolve(self.wdc_contradiction(), width=18)
        self.assertEqual(completed.frame_width, 18.0)
        self.assertEqual(completed.frame_height, 36.0)

    def test_omitted_dimensions_still_come_from_the_line(self):
        completed = resolve(self.wdc_contradiction())
        self.assertEqual((completed.frame_width, completed.frame_height), (24.0, 36.0))

    def fractional_qty_line(self) -> NeedsAttentionLine:
        """A row held back because its QTY cell was 2.9 (fix 3)."""
        return NeedsAttentionLine(
            row_index=9,
            part_number="W2412",
            qty=2.9,
            frame_width=24.0,
            frame_height=12.0,
            frame_type=FrameType.WALL,
            drawer_faces=DrawerFaces(),
            missing=(),
            reason="quantity 2.9 is not a whole number",
        )

    def test_a_fractional_quantity_must_be_supplied_to_resolve(self):
        with self.assertRaises(ValueError) as caught:
            resolve(self.fractional_qty_line())
        self.assertIn("quantity", str(caught.exception))
        self.assertIn("2.9", str(caught.exception))

    def test_a_supplied_whole_quantity_completes_the_line(self):
        completed = resolve(self.fractional_qty_line(), qty=3)
        self.assertIsInstance(completed, OrderLine)
        self.assertEqual(completed.qty, 3)
        self.assertIsInstance(completed.qty, int)

    def test_a_supplied_quantity_that_is_not_whole_is_refused(self):
        for bad in (2.9, 0, -2, "three"):
            with self.assertRaises(ValueError):
                resolve(self.fractional_qty_line(), qty=bad)

    def test_a_quantity_can_be_corrected_on_any_line(self):
        completed = resolve(self.wdc_contradiction(), width=18, qty=12)
        self.assertEqual(completed.qty, 12)
        self.assertEqual(completed.frame_width, 18.0)


if __name__ == "__main__":
    unittest.main()
