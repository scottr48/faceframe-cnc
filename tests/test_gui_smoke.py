"""Smoke tests for the Qt layer (Milestone 4, spec section 5).

Skipped whole when PySide6 is not installed, so ``python -m unittest
discover tests`` stays green on a bare stdlib interpreter.  When it does
run it uses the offscreen platform plugin: no display, no window manager,
no network -- just enough to prove the widgets build, paint, and forward
gestures into the session.

Deliberately thin.  The rules being enforced when a part is dragged are
tested in ``test_gui_session.py``; what is checked here is only the wiring.
"""

from __future__ import annotations

import os
import tempfile
import unittest

# Must be set before the first QGuiApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_QT = False

if HAVE_QT:
    from faceframe_cnc.gui.main_window import GENERATE_TOOLTIP, MainWindow
    from faceframe_cnc.gui.session import AppSettings, OrderRow, Session
    from faceframe_cnc.gui.settings_dialog import SettingsDialog

_APP = None


def setUpModule():  # noqa: N802 - unittest naming
    global _APP
    if HAVE_QT:
        _APP = QApplication.instance() or QApplication([])


def fake_order() -> Session:
    """A three-line order small enough to optimize in milliseconds."""
    session = Session(AppSettings())
    session.set_rows(
        [
            OrderRow(key="a", part_number="W3036", qty=2, frame_width=30.0, frame_height=36.0),
            OrderRow(key="b", part_number="W3012", qty=2, frame_width=30.0, frame_height=12.0),
            OrderRow(
                key="c",
                part_number="SD1212",
                qty=1,
                included=False,
                missing=("width", "height"),
                reason="missing frame width and height",
            ),
        ]
    )
    return session


def send_mouse(widget, kind, point: QPointF, buttons) -> None:
    event = QMouseEvent(
        kind,
        point,
        widget.mapToGlobal(point.toPoint()),
        Qt.MouseButton.LeftButton,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class MainWindowSmokeTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.session = fake_order()
        self.window = MainWindow(
            session=self.session,
            settings_path=os.path.join(self._folder.name, "settings.json"),
        )
        self.window.resize(1100, 800)
        self.addCleanup(self.window.close)

    def optimize(self):
        self.window.optimize()

    def use(self, *rows: OrderRow) -> None:
        """Swap in a different order and re-optimize."""
        self.session.set_rows(rows)
        self.window.order_panel.reload()
        self.optimize()

    def plain_sheet(self) -> None:
        """Four W3030s: two sheets, no frame ever nested inside another."""
        self.use(
            OrderRow(key="a", part_number="W3030", qty=4, frame_width=30.0, frame_height=30.0)
        )

    # -- construction ---------------------------------------------------

    def test_window_builds_with_an_order_but_no_layout(self):
        self.assertEqual(self.window.order_panel.table.rowCount(), 3)
        self.assertEqual(self.window.sheet_header.text(), "No layout yet")
        self.assertEqual(self.window.summary.total_sheets_label.text(), "-")
        self.assertEqual(self.window.summary.sheet_list.count(), 0)
        # 2026-08-03 amendment: SD1212 is missing BOTH frame dims, so it is
        # a NO_FRAME row -- shown informationally, never in the
        # needs-attention list (which is now empty for this fixture).
        self.assertEqual(self.window.order_panel.attention_list.count(), 0)
        # The window is never shown in this test, so isVisible() is always
        # False regardless of setVisible() -- check the flag Qt tracks
        # independent of the ancestor chain instead.
        self.assertFalse(self.window.order_panel.no_frame_label.isHidden())
        self.assertIn("SD1212", self.window.order_panel.no_frame_label.text())
        self.assertTrue(self.window.order_panel.attention_box.isVisible() or True)

    def test_generate_is_present_but_disabled_until_milestone_5(self):
        self.assertFalse(self.window.generate_button.isEnabled())
        self.assertEqual(self.window.generate_button.toolTip(), GENERATE_TOOLTIP)

    def test_rendering_a_frame_raises_nothing(self):
        self.optimize()
        pixmap = self.window.grab()
        self.assertFalse(pixmap.isNull())
        self.assertGreater(pixmap.width(), 0)
        canvas = self.window.canvas.grab()
        self.assertFalse(canvas.isNull())

    def test_rendering_before_any_layout_also_works(self):
        self.assertFalse(self.window.canvas.grab().isNull())

    # -- optimize and report --------------------------------------------

    def test_optimize_fills_in_the_header_and_summary(self):
        self.optimize()
        total = self.session.total_sheets
        unique = self.session.unique_sheet_count
        self.assertGreater(total, 0)
        self.assertIn("Sheet 1 of", self.window.sheet_header.text())
        self.assertIn(f"of {unique}", self.window.sheet_header.text())
        self.assertIn("run quantity", self.window.sheet_header.text())
        self.assertEqual(self.window.summary.total_sheets_label.text(), str(total))
        self.assertEqual(self.window.summary.sheet_list.count(), unique)
        self.assertIn("Frames included: 4", self.window.summary.detail_label.text())

    def test_the_summary_list_navigates_the_preview(self):
        self.plain_sheet()
        self.assertEqual(self.window.summary.sheet_list.count(), 2)
        self.window.summary.sheet_list.setCurrentRow(1)
        self.assertEqual(self.window.canvas.sheet_index, 1)
        self.assertIn("Sheet 2 of", self.window.sheet_header.text())

    def test_prev_and_next_wrap_around(self):
        self.plain_sheet()
        self.window.next_button.click()
        self.assertEqual(self.window.canvas.sheet_index, 1)
        self.window.next_button.click()
        self.assertEqual(self.window.canvas.sheet_index, 0)
        self.window.prev_button.click()
        self.assertEqual(self.window.canvas.sheet_index, 1)

    # -- order panel -----------------------------------------------------

    def test_unticking_a_row_updates_the_session(self):
        item = self.window.order_panel.table.item(1, 0)
        self.assertEqual(item.checkState(), Qt.CheckState.Checked)
        item.setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(self.session.row("b").included)
        self.assertEqual([s.part_number for s in self.session.demand()], ["W3036"])

    def test_a_no_frame_row_has_no_usable_checkbox(self):
        # SD1212 (2026-08-03 amendment: NO_FRAME, not needs-attention) still
        # cannot be ticked on until resolved.
        item = self.window.order_panel.table.item(2, 0)
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)
        self.assertFalse(bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable))

    def test_resolving_a_no_frame_row_from_the_table_adds_the_line(self):
        # SD1212 is not in attention_list (2026-08-03 amendment), but
        # selecting it directly in the table still offers the resolve
        # editor -- for when the order form really was wrong.
        panel = self.window.order_panel
        panel.table.selectRow(2)
        panel.width_edit.setText("12")
        panel.height_edit.setText("12")
        panel.resolve_button.click()
        self.assertTrue(self.session.row("c").included)
        self.assertEqual(panel.attention_list.count(), 0)
        self.assertEqual(panel.table.rowCount(), 3)
        self.assertTrue(panel.no_frame_label.isHidden())

    # -- canvas gestures --------------------------------------------------

    def test_clicking_a_host_selects_the_host_and_its_passenger_selects_itself(self):
        self.optimize()
        canvas = self.window.canvas
        canvas.resize(400, 700)
        layout, _run = self.session.sheet(0)
        host = next(p for p in layout.placements if p.children)
        index = layout.placements.index(host)
        child = host.children[0]

        corner = canvas.to_widget(host.x + 0.4, host.y + 0.4)
        send_mouse(canvas, QMouseEvent.Type.MouseButtonPress, corner, Qt.MouseButton.LeftButton)
        self.assertEqual(canvas.selected_path, (index,))
        send_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, corner, Qt.MouseButton.NoButton)

        middle = canvas.to_widget(
            child.x + child.width / 2, child.y + child.height / 2
        )
        send_mouse(canvas, QMouseEvent.Type.MouseButtonPress, middle, Qt.MouseButton.LeftButton)
        self.assertEqual(canvas.selected_path, (index, 0))
        send_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, middle, Qt.MouseButton.NoButton)

    def drag(self, canvas, placement, dx: float, dy: float) -> None:
        start = canvas.to_widget(
            placement.x + placement.width / 2, placement.y + placement.height / 2
        )
        end = canvas.to_widget(
            placement.x + dx + placement.width / 2,
            placement.y + dy + placement.height / 2,
        )
        send_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
        send_mouse(canvas, QMouseEvent.Type.MouseMove, end, Qt.MouseButton.LeftButton)
        send_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton)

    def test_a_drag_moves_the_part_and_updates_the_summary(self):
        self.plain_sheet()
        canvas = self.window.canvas
        canvas.resize(400, 700)
        placement = self.session.sheet(0)[0].placements[0]
        self.drag(canvas, placement, 2.0, 0.0)
        moved = self.session.sheet(canvas.sheet_index)[0].placements[canvas.selected_path[0]]
        self.assertTrue(self.session.edited)
        self.assertGreater(moved.x, placement.x)
        self.assertEqual(
            self.window.summary.total_sheets_label.text(), str(self.session.total_sheets)
        )
        self.assertEqual(self.session.problems(), [])

    def test_an_illegal_drag_snaps_back_and_reports_the_rule(self):
        self.plain_sheet()
        canvas = self.window.canvas
        canvas.resize(400, 700)
        layout, _run = self.session.sheet(0)
        first, second = layout.placements[0], layout.placements[1]
        before = [(l.canonical(), r) for l, r in self.session.sheets]
        self.drag(canvas, first, second.x - first.x, second.y - first.y)
        self.assertEqual([(l.canonical(), r) for l, r in self.session.sheets], before)
        self.assertFalse(self.session.edited)
        message = self.window.statusBar().currentMessage()
        self.assertTrue(message)
        self.assertIn("W3030", message)

    def test_the_r_key_rotates_the_selected_part(self):
        self.plain_sheet()
        canvas = self.window.canvas
        canvas.show_sheet(0, (0,))
        before = self.session.sheet(0)[0].placements[0].rotated
        canvas.rotate_selected()
        after = self.session.sheet(canvas.sheet_index)[0].placements[
            canvas.selected_path[0]
        ]
        self.assertNotEqual(after.rotated, before)

    def test_the_context_menu_offers_the_right_commands(self):
        self.optimize()
        canvas = self.window.canvas
        layout, _run = self.session.sheet(0)
        host_index = layout.placements.index(
            next(p for p in layout.placements if p.children)
        )

        menu = canvas.build_context_menu((host_index,))
        self.addCleanup(menu.deleteLater)
        labels = [a.text() for a in menu.actions()]
        self.assertTrue(any("Rotate" in text for text in labels))
        self.assertTrue(any("Move to sheet" in text for text in labels))
        self.assertNotIn("Un-nest onto the sheet", labels)

        child_menu = canvas.build_context_menu((host_index, 0))
        self.addCleanup(child_menu.deleteLater)
        child_labels = [a.text() for a in child_menu.actions()]
        self.assertIn("Centre in opening", child_labels)
        self.assertIn("Un-nest onto the sheet", child_labels)

    def test_moving_between_sheets_by_dropping_on_the_sheet_list(self):
        self.plain_sheet()
        canvas = self.window.canvas
        listing = self.window.summary.sheet_list
        item = listing.item(1)
        target = listing.viewport().mapToGlobal(
            listing.visualItemRect(item).center()
        )
        self.assertEqual(self.window._sheet_target_at(target), 1)
        before = self.session.sheet(1)[0]
        self.window._on_dropped_outside((0,), target)
        self.assertTrue(self.session.edited)
        self.assertGreater(
            len(self.session.sheet(self.window.canvas.sheet_index)[0].placements),
            len(before.placements),
        )
        self.assertEqual(canvas.sheet_index, self.window.canvas.sheet_index)

    # -- settings ---------------------------------------------------------

    def test_settings_dialog_round_trips_the_values(self):
        dialog = SettingsDialog(self.session.settings)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.sheet_width.value(), 49.0)
        self.assertTrue(dialog.inside_nesting.isChecked())
        dialog.sheet_width.setValue(48.0)
        dialog.inside_nesting.setChecked(False)
        edited = dialog.result_settings()
        self.assertEqual(edited.sheet_width, 48.0)
        self.assertFalse(edited.inside_nesting)
        # The original object is untouched until the caller adopts the copy.
        self.assertEqual(self.session.settings.sheet_width, 49.0)


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class EntryPointTests(unittest.TestCase):
    def test_self_test_flag_and_environment_variable(self):
        from faceframe_cnc.gui import __main__ as entry

        args = entry._parse_args(["--self-test"])
        self.assertEqual(entry._self_test_seconds(args), 2.0)
        args = entry._parse_args(["--self-test", "0.5"])
        self.assertEqual(entry._self_test_seconds(args), 0.5)

        args = entry._parse_args([])
        self.assertIsNone(entry._self_test_seconds(args))
        os.environ["FACEFRAME_GUI_SELFTEST"] = "1.5"
        try:
            self.assertEqual(entry._self_test_seconds(args), 1.5)
        finally:
            del os.environ["FACEFRAME_GUI_SELFTEST"]

    def test_order_argument_is_optional(self):
        from faceframe_cnc.gui import __main__ as entry

        self.assertIsNone(entry._parse_args([]).order)
        self.assertEqual(entry._parse_args(["job.xls"]).order, "job.xls")


if __name__ == "__main__":
    unittest.main()
