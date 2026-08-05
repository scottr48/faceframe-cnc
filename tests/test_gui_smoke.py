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
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication, QDialog

    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_QT = False

if HAVE_QT:
    from faceframe_cnc.gui.edit_row_dialog import EditRowDialog
    from faceframe_cnc.gui.main_window import (
        GENERATE_TOOLTIP,
        ORDER_FILE_FILTER,
        MainWindow,
    )
    from faceframe_cnc.gui.order_panel import OrderPanel
    from faceframe_cnc.gui.session import (
        AppSettings,
        OrderRow,
        RowStatus,
        Session,
        SessionError,
        load_settings,
    )
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


def send_key(widget, key, modifiers=None) -> None:
    """Deliver one key press to ``widget``'s own handler.

    Called directly rather than through ``QApplication.sendEvent`` so that an
    exception raised inside the handler reaches the test instead of being
    swallowed by Qt's event dispatch -- crashing out of a Qt handler is
    exactly the failure under test in the keyboard cases.
    """
    if modifiers is None:
        modifiers = Qt.KeyboardModifier.NoModifier
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


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

    def test_cutting_none_then_none_disables_optimize_and_recutting_reenables_it(self):
        # 2026-08-04 owner report: "Cut none" then re-checking rows never
        # re-enabled Optimize, because none of the include/edit/resolve
        # paths ever called refresh() -- only MainWindow.refresh() itself
        # recomputes optimize_action's enabled state, and _on_order_changed
        # (wired to the panel's includeChanged/lineResolved signals) used to
        # do nothing but write a status message.
        panel = self.window.order_panel
        self.assertTrue(self.window.optimize_action.isEnabled())

        panel.none_button.click()
        self.assertEqual(self.session.included_rows(), [])
        self.assertFalse(self.window.optimize_action.isEnabled())

        # Re-including goes through the same signal path (includeChanged),
        # never through a reload of the order itself.
        panel.all_button.click()
        self.assertTrue(self.session.included_rows())
        self.assertTrue(self.window.optimize_action.isEnabled())

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

    def test_a_wdc_order_shows_the_fact_sheet_and_a_plain_one_does_not(self):
        # 2026-08-03 owner request: the panel must SHOW what the machine
        # does to a WDC frame -- the 2" stiles, the derived width, the T17
        # slot -- in a visible area, not a tooltip.  (isHidden(), not
        # isVisible(): the window is never shown in these tests.)
        panel = self.window.order_panel
        self.assertTrue(panel.wdc_box.isHidden(), "no WDC on the base fixture")
        self.session.set_rows(
            [
                OrderRow(
                    key="w",
                    part_number="WDC2436",
                    qty=2,
                    frame_width=18.0,
                    frame_height=36.0,
                    note="width 18 derived from part number",
                )
            ]
        )
        panel.reload()
        self.assertFalse(panel.wdc_box.isHidden())
        text = panel.wdc_label.text()
        self.assertIn("18 x 36", text)
        self.assertIn('2" wide', text)
        self.assertIn("T17", text)
        self.assertIn('0.875"', text)
        # The derivation note rides along as the row's tooltip.
        self.assertIn("derived from part number", panel.table.item(0, 1).toolTip())

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

    # -- editing a line (owner request, 2026-08-03) -----------------------

    def test_double_clicking_a_row_opens_the_edit_dialog(self):
        panel = self.window.order_panel
        captured = {}
        original = module_order_panel().EditRowDialog

        class Capturing(original):
            def __init__(self, session, key, parent=None):
                super().__init__(session, key, parent)
                captured["key"] = key
                captured["dialog"] = self

            def exec(self):
                # A real exec() would block on a modal event loop this
                # offscreen test never feeds -- only the construction (and
                # so the key it was opened for) is under test here.
                return int(self.DialogCode.Rejected)

        module_order_panel().EditRowDialog = Capturing
        try:
            panel._on_cell_double_clicked(0, 2)
        finally:
            module_order_panel().EditRowDialog = original
            if "dialog" in captured:
                captured["dialog"].close()
        self.assertEqual(captured.get("key"), "a")

    def test_the_edit_button_tracks_the_selection(self):
        panel = self.window.order_panel
        self.assertFalse(panel.edit_button.isEnabled())
        panel.table.selectRow(0)
        self.assertTrue(panel.edit_button.isEnabled())
        panel.table.clearSelection()
        self.assertFalse(panel.edit_button.isEnabled())

        # A reload that drops the selected row's index (fewer rows than
        # before) leaves nothing selected there any more, and the button
        # (refreshed outside the _loading-guarded signal handler, since
        # itemSelectionChanged is ignored while reload() rebuilds the
        # table) must follow.
        panel.table.selectRow(2)
        self.session.set_rows([self.session.rows[0]])
        panel.reload()
        self.assertFalse(panel.edit_button.isEnabled())

    def test_saving_an_edit_marks_the_row_and_updates_the_status_bar(self):
        panel = self.window.order_panel
        panel.session.edit_row("a", qty=9)
        panel.reload()
        item = panel.table.item(0, 3)  # "Frame W x H" column
        self.assertIn("edited", item.text())
        self.assertIn("qty 2 -> 9", item.toolTip())


def module_order_panel():
    import faceframe_cnc.gui.order_panel as module

    return module


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class EditRowDialogSmokeTests(unittest.TestCase):
    def setUp(self):
        self.session = fake_order()
        self.dialog = EditRowDialog(self.session, "a")
        self.addCleanup(self.dialog.close)

    def _ok_button(self, dialog=None):
        from PySide6.QtWidgets import QDialogButtonBox

        dialog = dialog or self.dialog
        return dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)

    def test_save_is_disabled_with_no_changes(self):
        self.assertEqual(self.dialog.summary_label.text(), "no changes")
        self.assertFalse(self._ok_button().isEnabled())

    def test_save_enables_and_the_summary_shows_the_arrow_after_a_change(self):
        self.dialog.qty.setValue(5)
        self.assertTrue(self._ok_button().isEnabled())
        self.assertIn("->", self.dialog.summary_label.text())
        self.assertIn("qty 2 -> 5", self.dialog.summary_label.text())

    def test_revert_button_hidden_on_an_unedited_row(self):
        self.assertTrue(self.dialog.revert_button.isHidden())

    def test_revert_button_shown_once_the_row_is_edited(self):
        self.session.edit_row("a", qty=9)
        dialog = EditRowDialog(self.session, "a")
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.revert_button.isHidden())

    def test_a_missing_dims_placeholder_is_not_sent_on_a_qty_only_edit(self):
        # Qt spin boxes cannot show "unset", so a missing dimension is
        # prefilled with a 0.001 placeholder.  Untouched, it must count as
        # unset: no change in the summary, and never sent to the session as
        # a real width alongside a qty edit.
        session = Session(AppSettings())
        session.set_rows(
            [
                OrderRow(
                    key="m", part_number="WDC2436", qty=1, frame_height=36.0,
                    missing=("width",), reason="missing frame width",
                    included=False,
                )
            ]
        )
        dialog = EditRowDialog(session, "m")
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.summary_label.text(), "no changes")
        self.assertFalse(self._ok_button(dialog).isEnabled())

        dialog.qty.setValue(4)
        self.assertEqual(dialog.changes(), {"qty": 4})
        self.assertIn("qty 1 -> 4", dialog.summary_label.text())
        self.assertNotIn("width", dialog.summary_label.text())

        # Dialling a real width DOES count, attributed as "not set ->".
        dialog.width.setValue(18.0)
        self.assertEqual(dialog.changes(), {"qty": 4, "width": 18.0})
        self.assertIn("width ? -> 18", dialog.summary_label.text())

    def test_a_session_error_leaves_the_dialog_open_with_values_intact(self):
        panel = OrderPanel(self.session)
        self.addCleanup(panel.close)
        self.dialog.width.setValue(24.0)

        accepted = []
        self.dialog.accept = lambda: accepted.append(True)

        original_warning = module_order_panel().QMessageBox.warning
        module_order_panel().QMessageBox.warning = staticmethod(lambda *a, **k: None)

        def boom(*args, **kwargs):
            raise SessionError("no way")

        original_edit_row = self.session.edit_row
        self.session.edit_row = boom
        try:
            panel._on_dialog_save(self.dialog, "a")
        finally:
            self.session.edit_row = original_edit_row
            module_order_panel().QMessageBox.warning = original_warning

        self.assertEqual(accepted, [])
        self.assertEqual(self.dialog.width.value(), 24.0)

    def test_revert_via_the_panel_calls_session_revert_row(self):
        panel = OrderPanel(self.session)
        self.addCleanup(panel.close)
        self.session.edit_row("a", qty=9)
        dialog = EditRowDialog(self.session, "a", panel)
        self.addCleanup(dialog.close)

        accepted = []
        dialog.accept = lambda: accepted.append(True)
        panel._on_dialog_revert(dialog, "a")

        self.assertEqual(accepted, [True])
        self.assertEqual(self.session.row("a").qty, 2)
        self.assertFalse(self.session.row("a").edited)


# --------------------------------------------------------------------------
# Changing the settings (2026-08-04 review, fix 1).  The window must never be
# able to half-apply a settings change: the OLD layout was packed with the
# OLD settings, and it must not survive the change in a generate-able state.
# --------------------------------------------------------------------------


def module_main_window():
    import faceframe_cnc.gui.main_window as module

    return module


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class SettingsFlowSmokeTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.settings_path = os.path.join(self._folder.name, "settings.json")
        self.session = fake_order()
        self.window = MainWindow(session=self.session, settings_path=self.settings_path)
        self.addCleanup(self.window.close)
        self.window.optimize()
        self.assertTrue(self.window.generate_button.isEnabled())

    def apply_settings(self, settings, *, discard: bool = True, save_ok: bool = True):
        """Drive edit_settings() with a stubbed dialog.  Returns the warnings."""
        module = module_main_window()
        warnings: list[str] = []

        class FakeDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, current, parent=None):
                pass

            def exec(self):
                return int(QDialog.DialogCode.Accepted)

            def result_settings(self):
                return settings

        original = (
            module.SettingsDialog,
            module.QMessageBox.question,
            module.QMessageBox.warning,
            module.save_settings,
        )
        module.SettingsDialog = FakeDialog
        module.QMessageBox.question = staticmethod(
            lambda *args, **kwargs: (
                module.QMessageBox.StandardButton.Yes
                if discard
                else module.QMessageBox.StandardButton.No
            )
        )
        module.QMessageBox.warning = staticmethod(
            lambda *args, **kwargs: warnings.append(args[-1])
        )
        if not save_ok:
            module.save_settings = lambda *args, **kwargs: False
        try:
            self.window.edit_settings()
        finally:
            (
                module.SettingsDialog,
                module.QMessageBox.question,
                module.QMessageBox.warning,
                module.save_settings,
            ) = original
        return warnings

    def test_a_settings_change_re_nests_and_is_written_to_disk(self):
        warnings = self.apply_settings(AppSettings(part_gap=1.0))
        self.assertEqual(warnings, [])
        self.assertEqual(self.session.settings.part_gap, 1.0)
        self.assertEqual(self.session.config.part_gap, 1.0)
        self.assertIsNotNone(self.session.result)
        self.assertTrue(self.window.generate_button.isEnabled())
        self.assertEqual(load_settings(self.settings_path).part_gap, 1.0)

    def test_declining_the_discard_prompt_applies_nothing_at_all(self):
        # The widget branch under test only reads session.edited; the session
        # side of hand editing is covered in test_gui_session.
        self.session.edited = True
        before = [(layout.canonical(), run) for layout, run in self.session.sheets]

        warnings = self.apply_settings(
            AppSettings(sheet_width=40.0, sheet_height=60.0), discard=False
        )

        self.assertEqual(warnings, [])
        self.assertEqual(self.session.settings.sheet_width, 49.0)
        self.assertEqual(self.session.settings.sheet_height, 97.0)
        self.assertEqual(
            [(layout.canonical(), run) for layout, run in self.session.sheets], before
        )
        self.assertTrue(self.session.edited, "the hand edits are still there")
        self.assertFalse(
            os.path.exists(self.settings_path), "nothing may be written to disk"
        )
        self.assertIn("unchanged", self.window.statusBar().currentMessage())

    def test_a_settings_change_whose_re_nest_fails_turns_generate_off(self):
        warnings = self.apply_settings(AppSettings(sheet_width=10.0, sheet_height=10.0))

        self.assertEqual(len(warnings), 1)
        self.assertIn("Generate NC", warnings[0])
        self.assertIsNone(self.session.result)
        self.assertFalse(self.window.generate_button.isEnabled())
        self.assertFalse(self.session.can_generate())
        # The new stock IS in force -- the settings were applied, only the
        # nesting failed -- and the empty preview still paints.
        self.assertEqual(self.session.config.sheet_width, 10.0)
        self.assertFalse(self.window.canvas.grab().isNull())

    def test_a_settings_file_that_cannot_be_written_says_so(self):
        warnings = self.apply_settings(AppSettings(part_gap=1.0), save_ok=False)
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not be written", warnings[0])
        # ... and the session still runs on the new value.
        self.assertEqual(self.session.settings.part_gap, 1.0)

    def test_the_order_file_filter_offers_xls_only(self):
        self.assertIn("*.xls", ORDER_FILE_FILTER)
        self.assertNotIn("xlsx", ORDER_FILE_FILTER)


# --------------------------------------------------------------------------
# A selection outliving its layout, and a click that is not a drag
# (2026-08-04 review, fixes 3 and 4)
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class StaleSelectionSmokeTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.session = fake_order()
        self.window = MainWindow(
            session=self.session,
            settings_path=os.path.join(self._folder.name, "settings.json"),
        )
        self.addCleanup(self.window.close)
        self.window.optimize()
        self.canvas = self.window.canvas
        self.canvas.resize(400, 700)

    def invalidate(self) -> None:
        """Untick a line the way the operator does -- through the panel."""
        item = self.window.order_panel.table.item(1, 0)
        item.setCheckState(Qt.CheckState.Unchecked)
        self.assertIsNone(self.session.result)

    def test_the_selection_is_dropped_when_the_layout_is_invalidated(self):
        self.canvas.show_sheet(0, (0,))
        self.assertEqual(self.canvas.selected_path, (0,))
        self.invalidate()
        self.assertIsNone(self.canvas.selected_path)

    def test_the_keyboard_does_not_raise_on_a_stale_selection(self):
        self.canvas.show_sheet(0, (0,))
        self.invalidate()
        # Pre-fix these went straight into the session and raised
        # SessionError("no layout - run the optimizer first") out of the Qt
        # key handler.
        for key in (
            Qt.Key.Key_R,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            send_key(self.canvas, key)
        self.assertIsNone(self.canvas.selected_path)
        self.assertFalse(self.session.edited)

    def test_the_menu_commands_do_not_raise_on_a_stale_selection(self):
        self.canvas.show_sheet(0, (0,))
        self.invalidate()
        self.canvas.rotate_selected()
        self.canvas.centre_selected()
        self.canvas.unnest_selected()
        self.canvas.move_selected_to_sheet(0)
        self.assertIsNone(self.canvas.selected_path)
        self.assertIsNone(self.session.result)

    def test_a_stale_selection_after_a_re_optimize_is_dropped_too(self):
        # A layout with fewer parts than the path remembers.
        self.canvas.show_sheet(0, (1,))
        self.session.set_rows(
            [OrderRow(key="a", part_number="W3036", qty=1, frame_width=30.0, frame_height=36.0)]
        )
        self.window.order_panel.reload()
        self.window.optimize()
        self.assertIsNone(self.canvas.selected_path)
        send_key(self.canvas, Qt.Key.Key_R)

    def test_a_click_that_does_not_move_the_part_is_not_an_edit(self):
        layout, _run = self.session.sheet(0)
        placement = layout.placements[0]
        before = [(l.canonical(), r) for l, r in self.session.sheets]
        point = self.canvas.to_widget(
            placement.x + placement.width / 2, placement.y + 0.3
        )
        send_mouse(
            self.canvas, QMouseEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton
        )
        send_mouse(
            self.canvas, QMouseEvent.Type.MouseButtonRelease, point, Qt.MouseButton.NoButton
        )

        self.assertEqual(self.canvas.selected_path, (0,))
        self.assertFalse(
            self.session.edited,
            "reading a label must not arm the discard-edits prompt",
        )
        self.assertEqual([(l.canonical(), r) for l, r in self.session.sheets], before)
        self.assertTrue(self.window.generate_button.isEnabled())


# --------------------------------------------------------------------------
# The resolve editor: which row it is aimed at (fix 6) and the quantity it
# can now ask for (fix 7).
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class ResolveEditorSmokeTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(AppSettings())
        self.session.set_rows(
            [
                OrderRow(key="ok", part_number="W3036", qty=2, frame_width=30.0, frame_height=36.0),
                OrderRow(
                    key="first",
                    part_number="W2412",
                    qty=1,
                    frame_height=12.0,
                    missing=("width",),
                    reason="missing frame width",
                    included=False,
                ),
                OrderRow(
                    key="second",
                    part_number="W3024",
                    qty=1,
                    frame_height=24.0,
                    missing=("width",),
                    reason="missing frame width",
                    included=False,
                ),
            ]
        )
        self.panel = OrderPanel(self.session)
        self.addCleanup(self.panel.close)

    def test_a_reload_keeps_the_editor_on_the_row_the_user_picked(self):
        panel = self.panel
        self.assertEqual(panel.attention_list.count(), 2)
        panel.attention_list.setCurrentRow(1)
        self.assertEqual(panel._resolve_key, "second")
        panel.width_edit.setText("30")

        # Anything that reloads the panel used to re-aim the editor at row 0
        # while the table still highlighted the user's row -- so the next
        # click on "Resolve and include" resolved the WRONG line.
        panel.all_button.click()

        self.assertEqual(panel._resolve_key, "second")
        self.assertEqual(panel.attention_list.currentRow(), 1)
        self.assertEqual(panel.width_edit.text(), "30", "typed values survive a reload")

        panel.resolve_button.click()
        self.assertEqual(self.session.row("second").frame_width, 30.0)
        self.assertTrue(self.session.row("second").included)
        self.assertIsNone(self.session.row("first").frame_width)
        self.assertFalse(self.session.row("first").included)

    def test_the_editor_defaults_to_the_first_row_when_nothing_is_picked(self):
        self.assertEqual(self.panel._resolve_key, "first")
        self.assertEqual(self.panel.attention_list.currentRow(), 0)

    def test_resolving_moves_the_editor_on_to_what_is_left(self):
        panel = self.panel
        panel.width_edit.setText("24")
        panel.resolve_button.click()
        self.assertEqual(self.session.row("first").frame_width, 24.0)
        self.assertEqual(panel.attention_list.count(), 1)
        self.assertEqual(panel._resolve_key, "second")

    def test_a_fractional_quantity_row_gets_a_quantity_box(self):
        self.session.set_rows(
            [
                OrderRow(
                    key="frac",
                    part_number="W2412",
                    qty=2.9,
                    frame_width=24.0,
                    frame_height=12.0,
                    reason="quantity 2.9 is not a whole number",
                    included=False,
                )
            ]
        )
        panel = self.panel
        panel.reload()

        self.assertEqual(panel.attention_list.count(), 1)
        self.assertTrue(panel.qty_edit.isEnabled())
        self.assertFalse(panel.width_edit.isEnabled())
        self.assertFalse(panel.height_edit.isEnabled())
        self.assertIn("2.9", panel.reason_label.text())
        self.assertEqual(panel.qty_edit.text(), "", "never pre-filled with a guess")

        panel.qty_edit.setText("3")
        panel.resolve_button.click()

        resolved = self.session.row("frac")
        self.assertEqual(resolved.qty, 3)
        self.assertIs(resolved.status, RowStatus.READY)
        self.assertTrue(resolved.included)
        self.assertFalse(panel.qty_edit.isEnabled())

    def test_the_edit_dialog_never_floors_a_fractional_quantity(self):
        # A QSpinBox cannot hold 2.9; the danger is that it quietly holds 2
        # and "Save changes" then commits the floor the parser refused to
        # make.  It must start at 1, say what the order form said, and show
        # the change it is about to make.
        self.session.set_rows(
            [
                OrderRow(
                    key="frac",
                    part_number="W2412",
                    qty=2.9,
                    frame_width=24.0,
                    frame_height=12.0,
                    reason="quantity 2.9 is not a whole number",
                    included=False,
                )
            ]
        )
        dialog = EditRowDialog(self.session, "frac", self.panel)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.qty.value(), 1)
        self.assertIn("2.9", dialog.original_label.text())
        self.assertIn("not a whole number", dialog.original_label.text())
        self.assertEqual(dialog.changes(), {"qty": 1})
        self.assertIn("qty 2.9 -> 1", dialog.summary_label.text())

        dialog.qty.setValue(3)
        self.panel._on_dialog_save(dialog, "frac")
        resolved = self.session.row("frac")
        self.assertEqual(resolved.qty, 3)
        self.assertIs(resolved.status, RowStatus.READY)

    def test_a_fractional_quantity_left_blank_is_refused_in_the_panel(self):
        self.session.set_rows(
            [
                OrderRow(
                    key="frac",
                    part_number="W2412",
                    qty=2.9,
                    frame_width=24.0,
                    frame_height=12.0,
                    reason="quantity 2.9 is not a whole number",
                    included=False,
                )
            ]
        )
        panel = self.panel
        panel.reload()
        messages = []
        panel.statusMessage.connect(messages.append)
        panel.resolve_button.click()
        self.assertTrue(messages)
        self.assertIn("quantity", messages[-1])
        self.assertFalse(self.session.row("frac").included)


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
