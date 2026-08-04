"""Tests for the headless GUI session model (Milestone 4, spec section 5).

No Qt anywhere in this file: the whole application model is exercised
without a display.  Only the tests that read the real order spreadsheet
need pandas, and they skip cleanly without it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from faceframe_cnc.geometry import FrameType
from faceframe_cnc.nesting import (
    MIN_PART_GAP,
    NestingConfig,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    place_inner,
    validate_layouts,
)
from faceframe_cnc.gui.session import (
    AppSettings,
    OrderRow,
    RowStatus,
    Session,
    SessionError,
    load_settings,
    save_settings,
    sheet_openings,
    suggest_dimensions,
    wdc_detail,
)

try:  # the .xls parser is the only thing in the app that needs pandas
    import pandas  # noqa: F401

    HAVE_PANDAS = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_PANDAS = False

HERE = os.path.dirname(os.path.abspath(__file__))
ORDER_XLS = os.path.join(
    HERE, os.pardir, "reference", "orders", "7-21-26_Cab_Tec_Order_with_specs.xls"
)
HAVE_ORDER = os.path.exists(ORDER_XLS)


def row(part_number, width, height, qty, **kwargs) -> OrderRow:
    return OrderRow(
        key=kwargs.pop("key", part_number),
        part_number=part_number,
        qty=qty,
        frame_width=width,
        frame_height=height,
        **kwargs,
    )


def canonical(session: Session) -> list[tuple[str, int]]:
    """A comparable snapshot of the whole layout."""
    return [(layout.canonical(), run) for layout, run in session.sheets]


def build(specs, sheets, settings=None) -> Session:
    """A session holding a hand-built, known-good layout.

    ``sheets`` is a list of ``(placements, run)``.  The fixture is checked
    with the independent validator so a test can never start from an
    already-illegal layout and mistake that for the behaviour under test.
    """
    session = Session(settings or AppSettings())
    session.set_rows(
        [row(s.part_number, s.width, s.height, s.qty) for s in specs]
    )
    config = session.settings.to_config()
    layouts = [(SheetLayout(list(placements)), run) for placements, run in sheets]
    result = NestingResult(
        unique_sheets=layouts,
        total_sheets=sum(run for _layout, run in layouts),
        demand=list(specs),
        config=config,
        inside_placements=sum(l.child_count() * r for l, r in layouts),
        baseline_sheets=None,
    )
    assert validate_layouts(result, config) == [], validate_layouts(result, config)
    session.set_result(result)
    return session


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


class SettingsTests(unittest.TestCase):
    def test_app_turns_inside_nesting_on(self):
        # The library defaults it off (Milestone 2 compatibility); the app
        # exists to use it, and always wants the baseline for the summary.
        self.assertFalse(NestingConfig().inside_nesting)
        config = AppSettings().to_config()
        self.assertTrue(config.inside_nesting)
        self.assertTrue(config.inside_baseline)
        self.assertFalse(config.inside_recursion)
        self.assertEqual((config.sheet_width, config.sheet_height), (49.0, 97.0))
        # 0.455 (2026-08-03): the shop's own spacing, and the least the NC
        # post can cut -- see NestingConfig.part_gap.  The frame-inside-frame
        # clearance does NOT follow it.
        self.assertEqual(config.part_gap, 0.455)
        self.assertEqual(config.inner_clearance, 0.375)
        self.assertEqual(config.edge_cushion, 0.5)

    def test_round_trip_through_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            original = AppSettings(
                sheet_width=48.0,
                sheet_height=96.0,
                part_gap=0.5,  # above the 0.455 floor, so it round-trips
                edge_cushion=0.0,
                inside_nesting=False,
                inside_recursion=True,
                last_order_path="C:/orders/job.xls",
            )
            self.assertTrue(save_settings(original, path))
            self.assertEqual(load_settings(path), original)
            with open(path, encoding="utf-8") as handle:
                self.assertIn("sheet_width", json.load(handle))

    def test_missing_or_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = os.path.join(folder, "nope.json")
            self.assertEqual(load_settings(missing), AppSettings())
            broken = os.path.join(folder, "broken.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(load_settings(broken), AppSettings())

    def test_bad_values_in_json_are_replaced_not_trusted(self):
        settings = AppSettings.from_dict(
            {"sheet_width": "wide", "part_gap": -3, "sheet_height": None, "junk": 1}
        )
        self.assertEqual(settings.sheet_width, 49.0)
        self.assertEqual(settings.sheet_height, 97.0)
        self.assertEqual(settings.part_gap, 0.455)

    def test_validate_reports_unusable_numbers(self):
        self.assertEqual(AppSettings().validate(), [])
        self.assertTrue(AppSettings(sheet_width=0).validate())
        self.assertTrue(AppSettings(part_gap=-1).validate())
        self.assertTrue(AppSettings(front_margin=-1).validate())

    def test_validate_refuses_a_part_gap_below_the_machine_floor(self):
        # 2026-08-03: 0.455 is a hard floor -- the perimeter lead-in sweeps
        # 0.425 past the part edge, so anything tighter packs sheets the NC
        # verifier must refuse at Generate time.
        problems = AppSettings(part_gap=0.375).validate()
        self.assertTrue(problems)
        self.assertIn("0.455", "; ".join(problems))
        self.assertIn("lead-in", "; ".join(problems))
        # The floor itself is fine, as is anything above it.
        self.assertEqual(AppSettings(part_gap=MIN_PART_GAP).validate(), [])
        self.assertEqual(AppSettings(part_gap=0.5).validate(), [])

    # -- the stale-settings migration (2026-08-03) -----------------------

    def test_a_stale_part_gap_is_raised_to_the_floor_with_a_note(self):
        # The owner's repro: faceframe_settings.json persisted 0.375 from
        # before the 0.455 amendment, the optimizer packed at 0.375, and 8
        # of 17 sheets came back "[foreign-cut] ..." at Generate time.  The
        # load is where it gets fixed -- visibly, not silently.
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"part_gap": 0.375}, handle)
            settings = load_settings(path)
            self.assertEqual(settings.part_gap, MIN_PART_GAP)
            self.assertEqual(len(settings.migration_notes), 1)
            note = settings.migration_notes[0]
            self.assertIn("0.375", note)
            self.assertIn("0.455", note)
            self.assertIn("lead-in", note)

    def test_a_compliant_part_gap_loads_untouched_with_no_note(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"part_gap": 0.5}, handle)
            settings = load_settings(path)
            self.assertEqual(settings.part_gap, 0.5)
            self.assertEqual(settings.migration_notes, [])

    def test_the_migration_note_is_never_persisted(self):
        # The note describes one load; writing it back would make the NEXT
        # load report a migration that never happened.
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"part_gap": 0.2}, handle)
            migrated = load_settings(path)
            self.assertTrue(migrated.migration_notes)
            self.assertNotIn("migration_notes", migrated.to_dict())
            self.assertTrue(save_settings(migrated, path))
            self.assertEqual(load_settings(path).migration_notes, [])

    def test_optimize_refuses_a_part_gap_forced_below_the_floor(self):
        # Belt and braces: a programmatic write that dodges both the dialog
        # and the load-time migration still cannot reach the optimizer.
        session = Session(AppSettings())
        session.set_rows([row("W3036", 30.0, 36.0, 1)])
        session.settings.part_gap = 0.375
        with self.assertRaises(SessionError) as caught:
            session.optimize()
        self.assertIn("0.455", str(caught.exception))
        self.assertIn("lead-in", str(caught.exception))

    def test_front_margin_defaults_and_round_trips(self):
        # 2026-08-03 amendment: front_margin defaults to 1.0 and flows
        # through to the optimizer config.
        self.assertEqual(AppSettings().front_margin, 1.0)
        self.assertEqual(AppSettings().to_config().front_margin, 1.0)

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            original = AppSettings(front_margin=0.25)
            self.assertTrue(save_settings(original, path))
            loaded = load_settings(path)
            self.assertEqual(loaded.front_margin, 0.25)
            self.assertEqual(loaded, original)

    def test_front_margin_defaults_to_1_when_key_missing(self):
        # A settings file saved before this feature existed has no
        # front_margin key at all — that must not be treated as 0.
        settings = AppSettings.from_dict(
            {"sheet_width": 49.0, "sheet_height": 97.0, "part_gap": 0.375}
        )
        self.assertEqual(settings.front_margin, 1.0)


# --------------------------------------------------------------------------
# The WDC fact sheet (2026-08-03 owner request)
# --------------------------------------------------------------------------


class WdcDetailTests(unittest.TestCase):
    """The owner asked how he can trust a derived 18" width: this text is
    the answer, so it must actually contain the answers."""

    def test_the_fact_sheet_covers_everything_the_owner_asked_about(self):
        detail = wdc_detail("WDC2436", 18.0, 36.0)
        self.assertIn("18 x 36", detail)  # the derived frame size
        self.assertIn("24 x 36", detail)  # ...and the cabinet it came from
        self.assertIn('2" wide', detail)  # the special stiles
        self.assertIn('1.5"', detail)  # vs the standard member
        self.assertIn("14 x 33", detail)  # the opening those stiles produce
        self.assertIn("T17", detail)  # the special routing
        self.assertIn('0.4375"', detail)  # slot depth
        self.assertIn('1.3386"', detail)  # centreline off the inside edge
        self.assertIn("34 mm", detail)
        self.assertIn("NO standard T13", detail)
        self.assertIn('0.875"', detail)  # the end reach the packer reserves

    def test_missing_dimensions_fall_back_to_the_part_number(self):
        # An unresolved WDC row still gets a truthful fact sheet: the size
        # comes from the name and is attributed to it.
        detail = wdc_detail("WDC2436")
        self.assertIn("18 x 36", detail)
        self.assertIn("diagonal-corner", detail)
        self.assertIn("T17", detail)

    def test_non_wdc_parts_have_no_detail(self):
        self.assertEqual(wdc_detail("W3036", 30.0, 36.0), "")
        self.assertEqual(wdc_detail("B18", 18.0, 30.0), "")
        self.assertEqual(wdc_detail("3DB24", 24.0, 30.0), "")

    def test_every_number_is_derived_not_typed(self):
        # The trust bar: the text is built FROM the geometry constants, so
        # check it against them rather than against literals where we can.
        from faceframe_cnc.geometry import (
            WDC_SLOT_DEPTH,
            WDC_SLOT_END_REACH,
            WDC_SLOT_INSET_FROM_INSIDE_EDGE,
            WDC_STILE_INSET,
        )

        detail = wdc_detail("WDC2436", 18.0, 36.0)
        self.assertIn(f'{WDC_STILE_INSET:g}" wide', detail)
        self.assertIn(f'{WDC_SLOT_DEPTH:g}" deep', detail)
        self.assertIn(f'{WDC_SLOT_INSET_FROM_INSIDE_EDGE:g}"', detail)
        self.assertIn(f'{WDC_SLOT_END_REACH:g}" past each stile end', detail)


# --------------------------------------------------------------------------
# Order rows, include/exclude and the needs-attention flow
# --------------------------------------------------------------------------


class OrderRowTests(unittest.TestCase):
    def test_status_and_frame_type(self):
        good = row("W3036", 30.0, 36.0, 2)
        self.assertIs(good.status, RowStatus.READY)
        self.assertIs(good.frame_type, FrameType.WALL)
        self.assertTrue(good.can_include)
        self.assertEqual(good.size_text, "30 x 36")

        # 2026-08-03 amendment: missing exactly ONE dim is NEEDS_ATTENTION...
        one_missing = row("WDC2436", None, 36.0, 2, missing=("width",))
        self.assertIs(one_missing.status, RowStatus.NEEDS_ATTENTION)
        self.assertFalse(one_missing.can_include)
        self.assertEqual(one_missing.size_text, "? x 36")

        # ...but missing BOTH dims is NO_FRAME ("no faceframe required",
        # e.g. SD1212, a sample door whose order form shows N/A) -- shown
        # informationally, never prompted for.
        no_frame = row("SD1212", None, None, 40, missing=("width", "height"))
        self.assertIs(no_frame.status, RowStatus.NO_FRAME)
        self.assertFalse(no_frame.can_include)
        self.assertEqual(no_frame.size_text, "n/a")
        self.assertIn("no faceframe", no_frame.hint.lower())

        # Spec section 3: too short for its pattern is flagged, not nested.
        broken = row("3DB24", 24.0, 12.0, 1)
        self.assertIs(broken.status, RowStatus.INVALID)
        self.assertFalse(broken.can_include)
        self.assertIsNotNone(broken.geometry_error)

    def test_suggest_dimensions_never_guesses_a_wdc(self):
        self.assertEqual(suggest_dimensions("W3036"), (30.0, 36.0))
        self.assertEqual(suggest_dimensions("3DB24"), (None, None))
        # The 2026-08-03 amendment: WDC2436 is an 18x36 frame, so the name
        # must never be used as a prefill.
        self.assertEqual(suggest_dimensions("WDC2436"), (None, None))
        self.assertIn("cabinet size", OrderRow("k", "WDC2436", 1, missing=("width",)).hint.lower())


class IncludeExcludeTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(AppSettings())
        self.session.set_rows(
            [
                row("W3036", 30.0, 36.0, 2, key="a"),
                row("W3012", 30.0, 12.0, 2, key="b"),
                row("SD1212", None, None, 40, key="c", missing=("width", "height"),
                    reason="missing frame width and height", included=False),
            ]
        )

    def test_demand_only_covers_included_ready_rows(self):
        demand = self.session.demand()
        self.assertEqual([s.part_number for s in demand], ["W3012", "W3036"])
        self.assertEqual(self.session.total_frames, 4)

    def test_excluding_a_line_removes_it_from_the_demand(self):
        self.session.set_included("b", False)
        self.assertEqual([s.part_number for s in self.session.demand()], ["W3036"])
        self.assertEqual(self.session.total_frames, 2)
        self.assertTrue(self.session.dirty)

    def test_a_needs_attention_line_cannot_be_included(self):
        with self.assertRaises(SessionError) as caught:
            self.session.set_included("c", True)
        self.assertIn("SD1212", str(caught.exception))
        self.assertNotIn("SD1212", [s.part_number for s in self.session.demand()])

    def test_set_all_skips_lines_that_cannot_be_included(self):
        self.session.set_all_included(True)
        self.assertFalse(self.session.row("c").included)
        self.session.set_all_included(False)
        self.assertEqual(self.session.demand(), [])

    def test_same_part_number_with_two_sizes_is_refused(self):
        self.session.set_rows(
            [row("W3036", 30.0, 36.0, 1, key="a"), row("W3036", 30.0, 30.0, 1, key="b")]
        )
        with self.assertRaises(SessionError) as caught:
            self.session.demand()
        self.assertIn("different sizes", str(caught.exception))

    def test_include_toggle_then_optimize(self):
        first = self.session.optimize()
        self.assertFalse(self.session.dirty)
        self.session.set_included("a", False)
        self.assertTrue(self.session.dirty)
        second = self.session.optimize()
        self.assertLess(second.total_sheets, first.total_sheets + 1)
        self.assertEqual(
            sorted(s.part_number for s in second.demand), ["W3012"]
        )
        self.assertEqual(validate_layouts(second, self.session.config), [])


class NoFrameTests(unittest.TestCase):
    """2026-08-03 amendment ("SD1212 / no-faceframe lines"): a row missing
    BOTH frame dims is "no faceframe required" -- auto-excluded, shown
    informationally, never prompted for, but still manually resolvable.
    """

    def setUp(self):
        self.session = Session(AppSettings())
        self.session.set_rows(
            [
                row("W3012", 30.0, 12.0, 4, key="ok"),
                row(
                    "SD1212", None, None, 40, key="sd", missing=("width", "height"),
                    reason="missing frame width and height", included=False,
                ),
            ]
        )

    def test_status_is_no_frame_not_needs_attention(self):
        sd = self.session.row("sd")
        self.assertIs(sd.status, RowStatus.NO_FRAME)
        self.assertFalse(sd.can_include)

    def test_excluded_from_needs_attention_rows(self):
        self.assertEqual(self.session.needs_attention_rows(), [])
        self.assertEqual(
            [r.part_number for r in self.session.no_frame_rows()], ["SD1212"]
        )

    def test_demand_omits_it_while_unresolved(self):
        self.assertEqual(
            [s.part_number for s in self.session.demand()], ["W3012"]
        )
        self.assertEqual(self.session.total_frames, 4)

    def test_cannot_be_included_while_unresolved(self):
        with self.assertRaises(SessionError):
            self.session.set_included("sd", True)

    def test_resolve_flow_converts_it_to_ready_and_included(self):
        resolved = self.session.resolve_row("sd", width=12.0, height=12.0)
        self.assertEqual((resolved.frame_width, resolved.frame_height), (12.0, 12.0))
        self.assertEqual(resolved.missing, ())
        self.assertTrue(resolved.resolved)
        self.assertTrue(resolved.included)
        self.assertIs(resolved.status, RowStatus.READY)
        self.assertIn("SD1212", [s.part_number for s in self.session.demand()])
        self.assertEqual(self.session.no_frame_rows(), [])

    def test_resolve_defaults_to_included_like_needs_attention_rows_do(self):
        # Same default as the needs-attention resolve flow: include=True
        # unless the caller says otherwise, for consistency between the
        # two kinds of incomplete rows.
        resolved = self.session.resolve_row("sd", width=12.0, height=12.0, include=False)
        self.assertFalse(resolved.included)
        self.assertIs(resolved.status, RowStatus.READY)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(AppSettings())
        self.session.set_rows(
            [
                row("W3012", 30.0, 12.0, 4, key="ok"),
                row("WDC2436", None, 36.0, 2, key="wdc", missing=("width",),
                    reason="missing frame width", included=False),
                row("SD1212", None, None, 3, key="sd", missing=("width", "height"),
                    reason="missing frame width and height", included=False),
            ]
        )

    def test_resolving_the_wdc_width_puts_it_in_the_cut_list(self):
        resolved = self.session.resolve_row("wdc", width=18.0)
        self.assertEqual(resolved.frame_width, 18.0)
        self.assertEqual(resolved.missing, ())
        self.assertTrue(resolved.resolved)
        self.assertTrue(resolved.included)
        self.assertIs(resolved.status, RowStatus.READY)
        self.assertIn("WDC2436", [s.part_number for s in self.session.demand()])
        # SD1212 is missing BOTH dims -- 2026-08-03 amendment puts it in
        # no_frame, not needs_attention.
        self.assertEqual(self.session.needs_attention_rows(), [])
        self.assertEqual(self.session.no_frame_rows()[0].part_number, "SD1212")

    def test_a_resolved_wdc_gets_the_amended_2_inch_stiles(self):
        self.session.resolve_row("wdc", width=18.0)
        result = self.session.optimize()
        host = None
        for layout, _run in result.unique_sheets:
            for placement in layout.placements:
                if placement.part_number == "WDC2436":
                    host = placement
        self.assertIsNotNone(host)
        opening = sheet_openings(host, self.session.ordered_specs())[0]
        self.assertAlmostEqual(min(opening.width, opening.height), 14.0)
        self.assertAlmostEqual(max(opening.width, opening.height), 33.0)

    def test_partial_resolution_is_refused_and_changes_nothing(self):
        with self.assertRaises(SessionError):
            self.session.resolve_row("sd", width=12.0)
        row_after = self.session.row("sd")
        self.assertEqual(row_after.missing, ("width", "height"))
        self.assertIsNone(row_after.frame_width)
        self.assertFalse(row_after.included)

    def test_junk_and_non_positive_values_are_refused(self):
        for bad in ("abc", -4, 0):
            with self.assertRaises(SessionError):
                self.session.resolve_row("wdc", width=bad)
        self.assertEqual(self.session.row("wdc").missing, ("width",))

    def test_a_dimension_that_cannot_produce_openings_is_rolled_back(self):
        self.session.set_rows([row("3DB24", None, 12.0, 1, key="x", missing=("width",))])
        with self.assertRaises(SessionError) as caught:
            self.session.resolve_row("x", width=24.0)
        self.assertIn("too short", str(caught.exception))
        self.assertEqual(self.session.row("x").missing, ("width",))
        self.assertIsNone(self.session.row("x").frame_width)

    def test_resolving_a_complete_row_is_an_error(self):
        with self.assertRaises(SessionError):
            self.session.resolve_row("ok", width=30.0)


# --------------------------------------------------------------------------
# Geometry helpers shared with the canvas
# --------------------------------------------------------------------------


class OpeningTransformTests(unittest.TestCase):
    """``sheet_openings`` must use exactly ``place_inner``'s convention."""

    def _check(self, rotated: bool):
        config = AppSettings().to_config()
        host_spec = PartSpec("W3036", 30.0, 36.0, 1)
        inner_spec = PartSpec("W3012", 30.0, 12.0, 1)
        host = Placement(
            "W3036",
            4.0,
            7.0,
            36.0 if rotated else 30.0,
            30.0 if rotated else 36.0,
            rotated,
        )
        child = place_inner(host, host_spec, inner_spec, config)
        self.assertIsNotNone(child)
        rects = sheet_openings(host, {"W3036": host_spec})
        self.assertEqual(len(rects), 1)
        rect = rects[0]
        # The child that place_inner produced is centred in the opening this
        # helper computes -- same transform, same rotation convention.
        self.assertAlmostEqual(
            child.x - rect.x, rect.x + rect.width - (child.x + child.width)
        )
        self.assertAlmostEqual(
            child.y - rect.y, rect.y + rect.height - (child.y + child.height)
        )
        self.assertGreaterEqual(child.x - rect.x, config.part_gap - 1e-9)
        self.assertGreaterEqual(child.y - rect.y, config.part_gap - 1e-9)

    def test_upright_host(self):
        self._check(rotated=False)

    def test_rotated_host(self):
        self._check(rotated=True)

    def test_wdc_openings_use_the_two_inch_stiles(self):
        spec = PartSpec("WDC2436", 18.0, 36.0, 1)
        rect = sheet_openings(Placement("WDC2436", 0.0, 0.0, 18.0, 36.0), {"WDC2436": spec})[0]
        self.assertAlmostEqual(rect.x, 2.0)
        self.assertAlmostEqual(rect.width, 14.0)
        self.assertAlmostEqual(rect.height, 33.0)

    def test_three_drawer_frame_has_three_openings(self):
        spec = PartSpec("3DB30", 30.0, 30.0, 1)
        rects = sheet_openings(Placement("3DB30", 0.0, 0.0, 30.0, 30.0), {"3DB30": spec})
        self.assertEqual([r.label for r in rects], ["top", "middle", "bottom"])
        self.assertEqual([round(r.height, 4) for r in rects], [5.0, 9.875, 9.125])


class HitTestTests(unittest.TestCase):
    def setUp(self):
        specs = [PartSpec("W3036", 30.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 1)]
        host = Placement("W3036", 0.5, 0.5, 30.0, 36.0)
        host.children.append(Placement("W3012", 9.5, 3.5, 12.0, 30.0, rotated=True))
        self.session = build(specs, [([host], 1)])

    def test_the_nested_frame_wins_over_its_host(self):
        self.assertEqual(self.session.hit_test(0, 12.0, 10.0), (0, 0))

    def test_the_host_is_picked_outside_its_passenger(self):
        self.assertEqual(self.session.hit_test(0, 1.0, 1.0), (0,))

    def test_empty_sheet_area_hits_nothing(self):
        self.assertIsNone(self.session.hit_test(0, 45.0, 90.0))

    def test_opening_lookup_skips_the_dragged_part(self):
        found = self.session.opening_at(0, 12.0, 10.0)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], (0, 0))  # the inner's own opening is smallest
        found = self.session.opening_at(0, 12.0, 10.0, exclude=(0, 0))
        self.assertEqual(found[0], (0,))


# --------------------------------------------------------------------------
# Editing: moves, rotation, and spec 4c run splitting
# --------------------------------------------------------------------------


def two_alike() -> Session:
    """Two identical sheets as one picture with a run of 2."""
    specs = [PartSpec("W3030", 30.0, 30.0, 4)]
    placements = [
        Placement("W3030", 0.5, 0.5, 30.0, 30.0),
        Placement("W3030", 0.5, 40.0, 30.0, 30.0),
    ]
    return build(specs, [(placements, 2)])


class MoveTests(unittest.TestCase):
    def test_a_legal_move_splits_one_sheet_out_of_the_run(self):
        session = two_alike()
        self.assertEqual((session.unique_sheet_count, session.total_sheets), (1, 2))
        result = session.move_part(0, (0,), 2.0, 2.0)
        self.assertTrue(result, result.message)
        self.assertTrue(result.split)
        # Spec 4c: ONE physical sheet became its own picture; the total is
        # unchanged because the shop still cuts two sheets.
        self.assertEqual(session.unique_sheet_count, 2)
        self.assertEqual(session.total_sheets, 2)
        self.assertEqual([run for _layout, run in session.sheets], [1, 1])
        self.assertEqual(result.sheet_index, 1)
        self.assertEqual(result.path, (0,))
        moved = session.sheet(1)[0].placements[0]
        self.assertEqual((moved.x, moved.y), (2.0, 2.0))
        # ... and the untouched sheet still holds the original position.
        self.assertEqual(session.sheet(0)[0].placements[0].x, 0.5)

    def test_a_run_of_three_keeps_the_other_two_together(self):
        specs = [PartSpec("W3030", 30.0, 30.0, 3)]
        session = build(specs, [([Placement("W3030", 0.5, 0.5, 30.0, 30.0)], 3)])
        self.assertTrue(session.move_part(0, (0,), 4.0, 4.0))
        self.assertEqual([run for _layout, run in session.sheets], [2, 1])
        self.assertEqual(session.total_sheets, 3)

    def test_an_overlapping_move_snaps_back_and_names_the_rule(self):
        session = two_alike()
        before = canonical(session)
        result = session.move_part(0, (0,), 0.5, 40.0)
        self.assertFalse(result)
        self.assertIn("gap violation", result.message)
        self.assertIn("W3030", result.message)
        self.assertEqual(canonical(session), before)
        self.assertEqual(session.unique_sheet_count, 1)
        self.assertFalse(session.edited)

    def test_a_move_off_the_sheet_snaps_back(self):
        session = two_alike()
        before = canonical(session)
        result = session.move_part(0, (0,), 20.0, 0.5)
        self.assertFalse(result)
        self.assertIn("off the sheet", result.message)
        self.assertEqual(canonical(session), before)

    def test_a_move_that_is_only_barely_legal_is_allowed(self):
        session = two_alike()
        # Exactly the 0.455 gap: legal, the parts may touch that closely.
        result = session.move_part(0, (1,), 0.5, 30.955)
        self.assertTrue(result, result.message)
        # A thousandth closer is not.
        result = session.move_part(result.sheet_index, (1,), 0.5, 30.9549)
        self.assertFalse(result)
        self.assertIn("0.4549", result.message)

    def test_preview_never_changes_anything(self):
        session = two_alike()
        before = canonical(session)
        good = session.preview_drop(0, (0,), 2.0, 2.0)
        bad = session.preview_drop(0, (0,), 0.5, 40.0)
        self.assertTrue(good)
        self.assertFalse(bad)
        self.assertEqual(canonical(session), before)
        self.assertFalse(session.edited)

    def test_nudge_moves_by_a_delta(self):
        session = two_alike()
        result = session.nudge_part(0, (0,), 0.25, 0.0)
        self.assertTrue(result, result.message)
        self.assertAlmostEqual(session.sheet(result.sheet_index)[0].placements[0].x, 0.75)


class WdcSlotClearanceTests(unittest.TestCase):
    """A WDC frame's 45-degree stile slot cuts 0.875 past each stile end,
    so a hand drag has to respect that and not just the part gap."""

    def wdc_and_neighbour(self) -> Session:
        specs = [
            PartSpec("WDC2436", 18.0, 36.0, 1),
            PartSpec("W2436", 24.0, 36.0, 1),
        ]
        return build(
            specs,
            [
                (
                    [
                        Placement("WDC2436", 1.0, 1.0, 18.0, 36.0),
                        Placement("W2436", 1.0, 50.0, 24.0, 36.0),
                    ],
                    1,
                )
            ],
        )

    def test_dragging_a_neighbour_into_the_slot_s_reach_snaps_back(self):
        session = self.wdc_and_neighbour()
        before = canonical(session)
        # 0.5 past the WDC's top stile ends: clears the 0.455 part gap and
        # is still inside the cone.
        result = session.move_part(0, (1,), 1.0, 37.5)
        self.assertFalse(result)
        self.assertIn("WDC2436", result.message)
        self.assertEqual(canonical(session), before)

    def test_the_full_reach_away_is_allowed(self):
        session = self.wdc_and_neighbour()
        result = session.move_part(0, (1,), 1.0, 37.875)
        self.assertTrue(result, result.message)

    def test_a_wdc_dragged_against_the_sheet_edge_snaps_back(self):
        session = self.wdc_and_neighbour()
        before = canonical(session)
        result = session.move_part(0, (0,), 1.0, 0.5)
        self.assertFalse(result)
        self.assertIn("T17", result.message)
        self.assertEqual(canonical(session), before)

    def test_rotating_a_wdc_turns_the_direction_the_clearance_applies_in(self):
        """Upright the room has to be above and below; turned, left and
        right.  Rotation is about the part's own centre, so both positions
        below are legal upright and only one survives the turn."""
        session = self.wdc_and_neighbour()
        # centre x 18.5 -> turned, the part runs x[0.5, 36.5]: on the sheet,
        # but 0.5 from the edge where the slot needs 0.875.
        tight = session.move_part(0, (0,), 9.5, 1.0)
        self.assertTrue(tight, tight.message)
        blocked = session.rotate_part(tight.sheet_index, (0,))
        self.assertFalse(blocked)
        self.assertIn("T17", blocked.message)

        # half an inch further in, the turn is fine
        clear = session.move_part(tight.sheet_index, (0,), 10.0, 1.0)
        self.assertTrue(clear, clear.message)
        turned = session.rotate_part(clear.sheet_index, (0,))
        self.assertTrue(turned, turned.message)
        placement = session.sheet(turned.sheet_index)[0].placements[0]
        self.assertTrue(placement.rotated)
        self.assertAlmostEqual(placement.x, 1.0)


class RotateTests(unittest.TestCase):
    def test_rotation_turns_the_part_about_its_own_centre(self):
        specs = [PartSpec("W3012", 30.0, 12.0, 1)]
        session = build(specs, [([Placement("W3012", 9.5, 20.0, 30.0, 12.0)], 1)])
        result = session.rotate_part(0, (0,))
        self.assertTrue(result, result.message)
        placement = session.sheet(0)[0].placements[0]
        self.assertEqual((placement.width, placement.height), (12.0, 30.0))
        self.assertTrue(placement.rotated)
        self.assertAlmostEqual(placement.x + placement.width / 2, 24.5)
        self.assertAlmostEqual(placement.y + placement.height / 2, 26.0)

    def test_rotating_a_host_carries_its_passenger_round_with_it(self):
        specs = [PartSpec("W3036", 30.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 1)]
        host = Placement("W3036", 9.0, 20.0, 30.0, 36.0)
        host.children.append(Placement("W3012", 18.0, 23.0, 12.0, 30.0, rotated=True))
        session = build(specs, [([host], 1)])
        result = session.rotate_part(0, (0,))
        self.assertTrue(result, result.message)
        placed_host = session.sheet(0)[0].placements[0]
        child = placed_host.children[0]
        self.assertEqual((placed_host.width, placed_host.height), (36.0, 30.0))
        self.assertEqual((child.width, child.height), (30.0, 12.0))
        self.assertFalse(child.rotated)  # turned inside a turned host: upright
        # Still centred in the (now rotated) opening.
        rect = session.sheet_openings(placed_host)[0]
        self.assertAlmostEqual(child.x - rect.x, rect.x + rect.width - (child.x + child.width))
        self.assertAlmostEqual(child.y - rect.y, rect.y + rect.height - (child.y + child.height))
        self.assertEqual(session.problems(), [])

    def test_a_rotation_that_would_leave_the_sheet_snaps_back(self):
        specs = [PartSpec("W3012", 30.0, 12.0, 1)]
        session = build(specs, [([Placement("W3012", 0.5, 0.5, 30.0, 12.0)], 1)])
        before = canonical(session)
        result = session.rotate_part(0, (0,))
        self.assertFalse(result)
        self.assertIn("off the sheet", result.message)
        self.assertEqual(canonical(session), before)

    def test_rotation_is_a_two_state_toggle(self):
        # The layout model (and the NC post behind it) knows only "upright"
        # and "turned 90 degrees"; a second quarter turn therefore lands
        # back on upright rather than on an unrepresentable 180.
        specs = [PartSpec("W3012", 30.0, 12.0, 1)]
        session = build(specs, [([Placement("W3012", 9.5, 20.0, 30.0, 12.0)], 1)])
        first = session.rotate_part(0, (0,))
        self.assertTrue(session.sheet(0)[0].placements[0].rotated)
        second = session.rotate_part(first.sheet_index, first.path)
        self.assertTrue(second, second.message)
        placement = session.sheet(0)[0].placements[0]
        self.assertFalse(placement.rotated)
        self.assertEqual((placement.x, placement.y), (9.5, 20.0))

    def test_undoing_an_edit_re_groups_the_two_pictures(self):
        session = two_alike()
        first = session.rotate_part(0, (0,))
        self.assertTrue(first, first.message)
        self.assertTrue(first.split)
        self.assertFalse(first.merged)
        self.assertEqual(session.unique_sheet_count, 2)
        self.assertEqual([run for _layout, run in session.sheets], [1, 1])

        second = session.rotate_part(first.sheet_index, first.path)
        self.assertTrue(second, second.message)
        # The edited sheet looks exactly like the other one again, so spec
        # 4c folds them back into a single picture with a run of 2.
        self.assertTrue(second.merged)
        self.assertEqual(session.unique_sheet_count, 1)
        self.assertEqual(session.total_sheets, 2)
        self.assertEqual([run for _layout, run in session.sheets], [2])
        self.assertEqual(second.sheet_index, 0)
        self.assertEqual(second.path, (0,))


class CrossSheetTests(unittest.TestCase):
    def make(self) -> Session:
        specs = [PartSpec("W3012", 30.0, 12.0, 2)]
        return build(
            specs,
            [
                ([Placement("W3012", 0.5, 0.5, 30.0, 12.0)], 1),
                ([Placement("W3012", 0.5, 30.0, 30.0, 12.0)], 1),
            ],
        )

    def test_moving_the_last_part_off_a_sheet_retires_the_sheet(self):
        session = self.make()
        self.assertEqual(session.total_sheets, 2)
        result = session.move_part_to_sheet(0, (0,), 1)
        self.assertTrue(result, result.message)
        self.assertEqual(session.unique_sheet_count, 1)
        self.assertEqual(session.total_sheets, 1)
        layout, run = session.sheet(0)
        self.assertEqual(len(layout.placements), 2)
        self.assertEqual(run, 1)
        self.assertEqual(session.problems(), [])

    def test_the_part_lands_clear_of_everything_already_there(self):
        session = self.make()
        result = session.move_part_to_sheet(0, (0,), 1)
        moved = session.sheet(result.sheet_index)[0].placements[result.path[0]]
        other = [p for p in session.sheet(0)[0].placements if p is not moved][0]
        self.assertGreaterEqual(
            min(
                abs(moved.y - (other.y + other.height)),
                abs(other.y - (moved.y + moved.height)),
            ),
            session.config.part_gap - 1e-9,
        )

    def test_a_destination_with_no_room_is_refused(self):
        specs = [PartSpec("W4048", 48.0, 48.0, 3)]
        session = build(
            specs,
            [
                ([Placement("W4048", 0.5, 0.5, 48.0, 48.0)], 1),
                (
                    [
                        Placement("W4048", 0.5, 0.5, 48.0, 48.0),
                        Placement("W4048", 0.5, 48.955, 48.0, 48.0),
                    ],
                    1,
                ),
            ],
        )
        before = canonical(session)
        result = session.move_part_to_sheet(0, (0,), 1)
        self.assertFalse(result)
        self.assertIn("no free space", result.message)
        self.assertEqual(canonical(session), before)

    def test_both_sheets_split_out_of_their_runs(self):
        specs = [PartSpec("W3012", 30.0, 12.0, 6)]
        session = build(
            specs,
            [
                (
                    [
                        Placement("W3012", 0.5, 0.5, 30.0, 12.0),
                        Placement("W3012", 0.5, 20.0, 30.0, 12.0),
                    ],
                    2,
                ),
                ([Placement("W3012", 0.5, 60.0, 30.0, 12.0)], 2),
            ],
        )
        result = session.move_part_to_sheet(0, (0,), 1)
        self.assertTrue(result, result.message)
        self.assertTrue(result.split)
        # Both sheets were physically changed, so both owe spec 4c a picture
        # of their own: 2+2 sheets become 1+1 (untouched) and 1+1 (edited).
        self.assertEqual(session.total_sheets, 4)
        self.assertEqual(sorted(run for _layout, run in session.sheets), [1, 1, 1, 1])
        self.assertEqual(session.unique_sheet_count, 4)
        self.assertEqual(session.problems(), [])

    def test_emptying_a_sheet_mid_run_retires_one_physical_sheet(self):
        specs = [PartSpec("W3012", 30.0, 12.0, 4)]
        session = build(
            specs,
            [
                ([Placement("W3012", 0.5, 0.5, 30.0, 12.0)], 2),
                ([Placement("W3012", 0.5, 60.0, 30.0, 12.0)], 2),
            ],
        )
        result = session.move_part_to_sheet(0, (0,), 1)
        self.assertTrue(result, result.message)
        # The edited source sheet has nothing left on it, so it is not cut.
        self.assertEqual(session.total_sheets, 3)
        self.assertEqual(sorted(run for _layout, run in session.sheets), [1, 1, 1])
        self.assertEqual(session.problems(), [])

    def test_moving_to_the_same_sheet_is_rejected(self):
        session = self.make()
        with self.assertRaises(SessionError):
            session.move_part_to_sheet(0, (0,), 0)


# --------------------------------------------------------------------------
# Editing: frame-inside-frame by hand (spec 4b)
# --------------------------------------------------------------------------


def host_and_loose_inners(count: int = 2) -> Session:
    """A W3036 host plus ``count`` loose W3012s, already turned to fit.

    The W3012s arrive rotated because 30 x 12 upright cannot enter a 27 x 33
    opening -- and the session never turns a part behind the user's back.
    """
    specs = [PartSpec("W3036", 30.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, count)]
    placements = [Placement("W3036", 0.5, 0.5, 30.0, 36.0)]
    x = 0.5
    for _ in range(count):
        placements.append(Placement("W3012", x, 45.0, 12.0, 30.0, rotated=True))
        x += 13.0
    return build(specs, [(placements, 1)])


class NestByHandTests(unittest.TestCase):
    def test_centring_drop_makes_the_part_a_child(self):
        session = host_and_loose_inners(1)
        result = session.nest_part(0, (1,), (0,))
        self.assertTrue(result, result.message)
        self.assertEqual(result.path, (0, 0))
        layout, _run = session.sheet(result.sheet_index)
        self.assertEqual(len(layout.placements), 1)
        self.assertEqual(len(layout.placements[0].children), 1)
        self.assertEqual(session.result.inside_placements, 1)
        rect = session.sheet_openings(layout.placements[0])[0]
        child = layout.placements[0].children[0]
        self.assertAlmostEqual(child.x - rect.x, rect.x + rect.width - (child.x + child.width))
        self.assertAlmostEqual(child.y - rect.y, rect.y + rect.height - (child.y + child.height))

    def test_an_inner_that_cannot_fit_is_refused(self):
        specs = [PartSpec("W3036", 30.0, 36.0, 1), PartSpec("W2436", 24.0, 36.0, 1)]
        session = build(
            specs,
            [
                (
                    [
                        Placement("W3036", 0.5, 0.5, 30.0, 36.0),
                        Placement("W2436", 0.5, 40.0, 24.0, 36.0),
                    ],
                    1,
                )
            ],
        )
        before = canonical(session)
        result = session.nest_part(0, (1,), (0,))
        self.assertFalse(result)
        self.assertIn("does not fit any opening", result.message)
        self.assertEqual(canonical(session), before)

    def test_dropping_too_close_to_the_opening_edge_reports_the_clearance(self):
        session = host_and_loose_inners(1)
        before = canonical(session)
        # The opening is at (2.0, 2.0) 27x33; x=2.0 leaves no clearance.
        result = session.nest_part(0, (1,), (0,), x=2.0, y=3.5)
        self.assertFalse(result)
        self.assertIn("does not fit inside any single opening", result.message)
        self.assertIn("0.375 clearance", result.message)
        self.assertEqual(canonical(session), before)

    def test_a_dropped_position_inside_the_opening_is_kept(self):
        session = host_and_loose_inners(1)
        result = session.nest_part(0, (1,), (0,), x=2.375, y=3.5)
        self.assertTrue(result, result.message)
        child = session.sheet(result.sheet_index)[0].placements[0].children[0]
        self.assertAlmostEqual(child.x, 2.375)
        self.assertAlmostEqual(child.y, 3.5)

    def test_two_inners_in_one_host_are_allowed_by_hand_but_must_still_clear(self):
        session = host_and_loose_inners(2)
        first = session.nest_part(0, (1,), (0,), x=2.375, y=3.5)
        self.assertTrue(first, first.message)

        layout, _run = session.sheet(first.sheet_index)
        loose = [i for i, p in enumerate(layout.placements) if not p.children][0]

        # Spec 4b: the optimizer prefers one inner per host, but a manual
        # drag may add more -- so long as the clearances still hold.
        overlapping = session.nest_part(
            first.sheet_index, (loose,), (0,), x=14.0, y=3.5
        )
        self.assertFalse(overlapping)
        self.assertIn("gap violation", overlapping.message)

        ok = session.nest_part(first.sheet_index, (loose,), (0,), x=14.83, y=3.5)
        self.assertTrue(ok, ok.message)
        host = session.sheet(ok.sheet_index)[0].placements[0]
        self.assertEqual(len(host.children), 2)
        self.assertEqual(session.result.inside_placements, 2)
        self.assertEqual(session.problems(), [])

    def test_depth_two_is_refused_unless_recursion_is_enabled(self):
        specs = [
            PartSpec("W3036", 30.0, 36.0, 1),
            PartSpec("W2430", 24.0, 30.0, 1),
            PartSpec("W1206", 12.0, 6.0, 1),
        ]
        placements = [
            Placement("W3036", 0.5, 0.5, 30.0, 36.0),
            Placement("W2430", 0.5, 40.0, 24.0, 30.0),
            Placement("W1206", 0.5, 75.0, 12.0, 6.0),
        ]
        session = build(specs, [(placements, 1)])
        inner = session.nest_part(0, (1,), (0,))
        self.assertTrue(inner, inner.message)
        layout, _run = session.sheet(inner.sheet_index)
        loose = [i for i, p in enumerate(layout.placements) if p.part_number == "W1206"][0]
        deep = session.nest_part(inner.sheet_index, (loose,), inner.path)
        self.assertFalse(deep)
        self.assertIn("inside_recursion=False", deep.message)

        session.settings.inside_recursion = True
        session.result.config.inside_recursion = True
        deep = session.nest_part(inner.sheet_index, (loose,), inner.path)
        self.assertTrue(deep, deep.message)
        self.assertEqual(len(deep.path), 3)

    def test_a_part_cannot_be_nested_inside_itself(self):
        session = host_and_loose_inners(1)
        with self.assertRaises(SessionError):
            session.nest_part(0, (0,), (0,))

    def test_centre_in_opening_re_centres_a_dropped_child(self):
        session = host_and_loose_inners(1)
        dropped = session.nest_part(0, (1,), (0,), x=2.375, y=3.5)
        self.assertTrue(dropped, dropped.message)
        centred = session.centre_in_opening(dropped.sheet_index, dropped.path)
        self.assertTrue(centred, centred.message)
        host = session.sheet(centred.sheet_index)[0].placements[0]
        child = host.children[0]
        rect = session.sheet_openings(host)[0]
        self.assertAlmostEqual(child.x - rect.x, rect.x + rect.width - (child.x + child.width))
        self.assertAlmostEqual(child.y - rect.y, rect.y + rect.height - (child.y + child.height))

    def test_centre_in_opening_needs_a_nested_part(self):
        session = host_and_loose_inners(1)
        with self.assertRaises(SessionError):
            session.centre_in_opening(0, (0,))


class UnNestTests(unittest.TestCase):
    def make(self) -> Session:
        specs = [PartSpec("W3036", 30.0, 36.0, 1), PartSpec("W3012", 30.0, 12.0, 1)]
        host = Placement("W3036", 0.5, 0.5, 30.0, 36.0)
        host.children.append(Placement("W3012", 9.5, 3.5, 12.0, 30.0, rotated=True))
        return build(specs, [([host], 1)])

    def test_un_nesting_puts_the_frame_back_on_the_sheet(self):
        session = self.make()
        self.assertEqual(session.result.inside_placements, 1)
        result = session.unnest_part(0, (0, 0))
        self.assertTrue(result, result.message)
        layout, _run = session.sheet(result.sheet_index)
        self.assertEqual(len(layout.placements), 2)
        self.assertEqual(layout.child_count(), 0)
        self.assertEqual(session.result.inside_placements, 0)
        self.assertEqual(session.problems(), [])

    def test_un_nesting_to_an_explicit_spot_keeps_it(self):
        session = self.make()
        result = session.unnest_part(0, (0, 0), x=4.0, y=50.0)
        self.assertTrue(result, result.message)
        freed = session.sheet(result.sheet_index)[0].placements[result.path[0]]
        self.assertEqual((freed.x, freed.y), (4.0, 50.0))

    def test_un_nesting_onto_its_own_host_is_refused(self):
        session = self.make()
        before = canonical(session)
        result = session.unnest_part(0, (0, 0), x=9.5, y=3.5)
        self.assertFalse(result)
        self.assertIn("gap violation", result.message)
        self.assertEqual(canonical(session), before)

    def test_un_nesting_a_top_level_part_is_an_error(self):
        session = self.make()
        with self.assertRaises(SessionError):
            session.unnest_part(0, (0,))


class DropPlanningTests(unittest.TestCase):
    def setUp(self):
        self.session = host_and_loose_inners(1)
        self.loose_path = (1,)

    def test_a_drop_over_an_opening_nests(self):
        action, host = self.session.plan_drop(0, self.loose_path, 9.5, 3.5)
        self.assertEqual((action, host), ("nest", (0,)))

    def test_a_drop_on_bare_sheet_moves(self):
        action, host = self.session.plan_drop(0, self.loose_path, 34.0, 60.0)
        self.assertEqual((action, host), ("move", None))

    def test_a_child_dropped_outside_its_host_un_nests(self):
        nested = self.session.nest_part(0, self.loose_path, (0,))
        self.assertTrue(nested, nested.message)
        action, host = self.session.plan_drop(
            nested.sheet_index, nested.path, 34.0, 60.0
        )
        self.assertEqual((action, host), ("unnest", None))

    def test_a_child_dropped_inside_its_own_host_just_moves(self):
        nested = self.session.nest_part(0, self.loose_path, (0,))
        action, host = self.session.plan_drop(
            nested.sheet_index, nested.path, 3.0, 4.0
        )
        self.assertEqual((action, host), ("move", (0,)))

    def test_apply_drop_runs_the_planned_gesture(self):
        result = self.session.apply_drop(0, self.loose_path, 9.5, 3.5)
        self.assertTrue(result, result.message)
        self.assertEqual(len(result.path), 2)
        out = self.session.apply_drop(result.sheet_index, result.path, 34.0, 60.0)
        self.assertTrue(out, out.message)
        self.assertEqual(len(out.path), 1)


class EditGuardTests(unittest.TestCase):
    def test_editing_without_a_layout_is_an_error(self):
        session = Session(AppSettings())
        session.set_rows([row("W3012", 30.0, 12.0, 1)])
        with self.assertRaises(SessionError):
            session.move_part(0, (0,), 1.0, 1.0)
        with self.assertRaises(SessionError):
            session.sheet(0)

    def test_a_bad_path_or_sheet_index_is_an_error(self):
        session = two_alike()
        with self.assertRaises(SessionError):
            session.move_part(9, (0,), 1.0, 1.0)
        with self.assertRaises(SessionError):
            session.move_part(0, (7,), 1.0, 1.0)


class SheetReportingTests(unittest.TestCase):
    def test_titles_and_contents(self):
        session = host_and_loose_inners(1)
        title = session.sheet_title(0)
        self.assertIn("Sheet 1 of 1", title)
        self.assertIn("run quantity 1", title)
        self.assertIn("1xW3036", session.sheet_contents(0))
        summary = session.summary()
        self.assertEqual(summary["total_sheets"], 1)
        self.assertEqual(summary["unique_sheets"], 1)

    def test_title_when_nothing_is_loaded(self):
        session = Session(AppSettings())
        self.assertEqual(session.sheet_title(0), "No layout yet")


# --------------------------------------------------------------------------
# End to end against the real order (needs pandas + the sample file)
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_PANDAS, "pandas/xlrd are needed to read .xls orders")
@unittest.skipUnless(HAVE_ORDER, "the sample order spreadsheet is not present")
class RealOrderTests(unittest.TestCase):
    def load(self) -> Session:
        session = Session(AppSettings())
        session.load_order(ORDER_XLS)
        return session

    def test_the_acceptance_order_parses_into_rows_and_attention_lines(self):
        session = self.load()
        self.assertEqual(len(session.rows), 14)
        # 2026-08-03 amendments: WDC2436 (missing exactly one dim, and the
        # dim it has matches its part number) is AUTO-RESOLVED to 18 x 36 --
        # nothing needs attention in this file any more; SD1212 (missing
        # both) is no_frame and is never prompted for.
        self.assertEqual(session.needs_attention_rows(), [])
        wdc = next(r for r in session.rows if r.part_number == "WDC2436")
        self.assertIs(wdc.status, RowStatus.READY)
        self.assertTrue(wdc.included)
        self.assertEqual((wdc.frame_width, wdc.frame_height), (18.0, 36.0))
        self.assertIn("width 18 derived from part number", wdc.note)
        no_frame = {r.part_number: r.missing for r in session.no_frame_rows()}
        self.assertEqual(no_frame, {"SD1212": ("width", "height")})
        self.assertTrue(all(not r.included for r in session.no_frame_rows()))
        self.assertEqual(session.total_frames, 245)  # the 30 WDC2436 included

    def test_load_optimize_with_no_manual_resolution(self):
        session = self.load()
        # SD1212 has no frame dimensions at all, so it stays out of the cut;
        # everything else -- WDC2436 included -- is ready as loaded.
        self.assertEqual(session.total_frames, 245)

        result = session.optimize()
        self.assertEqual(result.total_parts, 245)
        self.assertEqual(validate_layouts(result, session.config), [])
        self.assertGreater(result.inside_placements, 0)
        self.assertIsNotNone(result.baseline_sheets)
        self.assertGreater(result.sheets_saved, 0)
        self.assertLess(result.total_sheets, result.baseline_sheets)
        self.assertGreaterEqual(result.total_sheets, result.area_lower_bound_sheets)
        self.assertEqual(
            sum(run for _layout, run in result.unique_sheets), result.total_sheets
        )

    def test_re_optimizing_is_deterministic(self):
        session = self.load()
        first = canonical(session) if session.result else None
        self.assertIsNone(first)
        session.optimize()
        first = canonical(session)
        session.optimize()
        self.assertEqual(canonical(session), first)

    def test_excluding_a_line_reduces_the_sheet_count(self):
        session = self.load()
        full = session.optimize().total_sheets
        for candidate in session.rows:
            if candidate.part_number == "3DB30":
                session.set_included(candidate.key, False)
        self.assertTrue(session.dirty)
        fewer = session.optimize().total_sheets
        self.assertLess(fewer, full)
        self.assertFalse(session.dirty)

    def test_an_edit_on_the_real_layout_keeps_the_totals_straight(self):
        session = self.load()
        result = session.optimize()
        sheets_before = result.total_sheets

        # Nudge a part on the first multi-sheet run and make sure spec 4c
        # accounting survives contact with a real 16-picture layout.
        index = next(
            i for i, (_layout, run) in enumerate(session.sheets) if run > 1
        )
        run_before = session.sheet(index)[1]
        edit = None
        for dx, dy in ((-0.0625, 0.0), (0.0625, 0.0), (0.0, -0.0625), (0.0, 0.0625)):
            edit = session.nudge_part(index, (0,), dx, dy)
            if edit:
                break
        self.assertTrue(edit, edit.message)
        self.assertTrue(edit.split)
        self.assertEqual(session.total_sheets, sheets_before)
        self.assertEqual(session.sheet(index)[1], run_before - 1)
        self.assertEqual(session.problems(), [])
        self.assertTrue(session.edited)


if __name__ == "__main__":
    unittest.main()
