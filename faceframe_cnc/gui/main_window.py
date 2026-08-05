"""Main window: order panel, sheet preview, summary (spec section 5).

Assembly only.  Every decision -- what may be included, whether a drop is
legal, what a sheet contains -- comes from
:class:`~faceframe_cnc.gui.session.Session`.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .generate_dialog import GenerateDialog
from .order_panel import OrderPanel
from .session import (
    AppSettings,
    Session,
    SessionError,
    default_settings_path,
    load_settings,
    save_settings,
)
from .settings_dialog import SettingsDialog
from .sheet_canvas import SheetCanvas
from .summary_panel import SummaryPanel

#: Shown when the Generate button is live.  The PDF report is Milestone 6;
#: the button writes NC today.
GENERATE_TOOLTIP = "Write one verified .anc program per sheet"

#: File-dialog filter for orders.  .xls ONLY, on purpose: the parser is
#: pandas + xlrd, which cannot open a modern .xlsx at all, so listing one
#: (as this filter used to) turned a perfectly good spreadsheet into a bare
#: "Could not read order" (2026-08-04 review).
ORDER_FILE_FILTER = "Excel 97-2003 order files (*.xls);;All files (*)"


class MainWindow(QMainWindow):
    """The whole app, minus the event loop."""

    def __init__(
        self,
        session: Optional[Session] = None,
        settings_path: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.settings_path = settings_path or str(default_settings_path())
        if session is None:
            session = Session(load_settings(self.settings_path))
        self.session = session
        self.setWindowTitle("Faceframe Nesting Optimizer")
        self.resize(1360, 900)

        # -- centre: the sheet preview ---------------------------------
        self.sheet_header = QLabel("No layout yet")
        self.sheet_header.setStyleSheet("font-size:16px; font-weight:600;")
        self.prev_button = QPushButton("< Prev sheet")
        self.next_button = QPushButton("Next sheet >")
        self.prev_button.clicked.connect(lambda: self._step_sheet(-1))
        self.next_button.clicked.connect(lambda: self._step_sheet(1))
        self.prev_button.setToolTip(
            "Previous unique sheet (drop a part here to move it there)"
        )
        self.next_button.setToolTip(
            "Next unique sheet (drop a part here to move it there)"
        )

        self.canvas = SheetCanvas(self.session)
        self.canvas.editApplied.connect(self._on_edit_applied)
        self.canvas.editRejected.connect(self._on_edit_rejected)
        self.canvas.statusMessage.connect(self._on_status)
        self.canvas.droppedOutside.connect(self._on_dropped_outside)

        header_row = QHBoxLayout()
        header_row.addWidget(self.sheet_header, 1)
        header_row.addWidget(self.prev_button)
        header_row.addWidget(self.next_button)

        self.hint_label = QLabel(
            "Drag a part to move it. R rotates. Drop it into a host's opening to "
            "nest it, or back onto the sheet to un-nest. Right-click for more."
        )
        self.hint_label.setStyleSheet("color:#54606b;")

        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(8, 8, 8, 8)
        centre_layout.addLayout(header_row)
        centre_layout.addWidget(self.canvas, 1)
        centre_layout.addWidget(self.hint_label)
        self.setCentralWidget(centre)

        # -- docks -----------------------------------------------------
        self.order_panel = OrderPanel(self.session)
        self.order_panel.includeChanged.connect(self._on_order_changed)
        self.order_panel.lineResolved.connect(lambda _key: self._on_order_changed())
        self.order_panel.statusMessage.connect(self._on_status)
        order_dock = QDockWidget("Order", self)
        order_dock.setObjectName("orderDock")
        order_dock.setWidget(self.order_panel)
        order_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, order_dock)
        self.order_dock = order_dock

        self.summary = SummaryPanel(self.session)
        self.summary.sheetSelected.connect(self._show_sheet)
        summary_dock = QDockWidget("Summary", self)
        summary_dock.setObjectName("summaryDock")
        summary_dock.setWidget(self.summary)
        summary_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, summary_dock)
        self.summary_dock = summary_dock

        self._build_toolbar()
        # A stale settings file the session had to correct on load (e.g. a
        # part gap below the NC post's floor) is announced here, on startup,
        # instead of silently running with a value the user never chose.
        notes = list(getattr(self.session.settings, "migration_notes", ()) or ())
        if notes:
            self.statusBar().showMessage("Settings updated: " + "; ".join(notes))
        else:
            self.statusBar().showMessage("Open an order spreadsheet to begin")
        self.refresh()

    # -- construction ----------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("Main")
        bar.setObjectName("mainToolBar")
        bar.setMovable(False)

        self.open_action = QAction("Open order...", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.open_order)
        bar.addAction(self.open_action)

        self.optimize_action = QAction("Optimize", self)
        self.optimize_action.setShortcut("Ctrl+R")
        self.optimize_action.triggered.connect(self.optimize)
        bar.addAction(self.optimize_action)

        self.settings_action = QAction("Settings...", self)
        self.settings_action.triggered.connect(self.edit_settings)
        bar.addAction(self.settings_action)

        bar.addSeparator()
        self.generate_button = QPushButton("Generate NC")
        self.generate_button.setEnabled(False)
        self.generate_button.setToolTip(GENERATE_TOOLTIP)
        self.generate_button.clicked.connect(self.generate_nc)
        bar.addWidget(self.generate_button)

    # -- actions ---------------------------------------------------------

    def open_order(self) -> None:
        start = self.session.settings.last_order_path or os.getcwd()
        # .xls ONLY: the parser is pandas + xlrd, which cannot read a modern
        # .xlsx at all, and offering one in the filter turned a perfectly
        # valid spreadsheet into a blank "Could not read order" (2026-08-04
        # review).  A user who picks one anyway through "All files" gets the
        # specific message the session raises.
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open order spreadsheet", start, ORDER_FILE_FILTER
        )
        if not path:
            return
        self.load_order(path)

    def load_order(self, path: str) -> None:
        try:
            self.session.load_order(path)
        except SessionError as exc:
            QMessageBox.warning(self, "Could not read order", str(exc))
            return
        save_settings(self.session.settings, self.settings_path)
        self.order_panel.reload()
        self.refresh()
        attention = len(self.session.needs_attention_rows())
        invalid = len(self.session.invalid_rows())
        self.statusBar().showMessage(
            f"Loaded {len(self.session.rows)} lines from {os.path.basename(path)}"
            + (f" - {attention} need attention" if attention else "")
            + (f" - {invalid} cannot be cut (red)" if invalid else "")
        )

    def optimize(self) -> None:
        if self.session.edited and not self._confirm_discard_edits():
            return
        self._run_optimize()

    def _confirm_discard_edits(self) -> bool:
        """Ask before throwing away hand edits.  ``True`` = go ahead."""
        answer = QMessageBox.question(
            self,
            "Discard manual edits?",
            "Re-optimizing throws away the layout changes you made by hand. "
            "Continue?",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _run_optimize(self, *, layout_already_invalid: bool = False) -> bool:
        """Re-nest and redraw.  ``False`` when the optimizer refused.

        A refusal ALWAYS ends with a refresh (2026-08-04 review): the button
        states are only recomputed there, and after a failed re-nest the
        session has no layout, so Generate has to go grey with it.  When the
        caller had already invalidated the layout on purpose (a settings
        change), the message says so, because "cannot optimize" plus a blank
        preview otherwise looks like the app lost the work for no reason.
        """
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.session.optimize()
            error = None
        except SessionError as exc:
            error = str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if error is not None:
            self.refresh()
            if layout_already_invalid:
                error += (
                    "\n\nThe layout that was on screen was packed with the "
                    "previous settings, so it has been cleared and Generate NC "
                    "is switched off. Fix this and press Optimize."
                )
            QMessageBox.warning(self, "Cannot optimize", error)
            return False
        self.canvas.show_sheet(0)
        self.refresh()
        summary = self.session.summary()
        saved = summary["sheets_saved"]
        self.statusBar().showMessage(
            f"{summary['total_sheets']} sheets, {summary['unique_sheets']} unique"
            + (f", {saved} saved by inside nesting" if saved is not None else "")
        )
        return True

    def generate_nc(self) -> None:
        """Write one verified .anc per sheet (spec sections 5 and 6)."""
        blocker = self.session.generate_blocker()
        if blocker is not None:
            QMessageBox.warning(self, "Cannot generate", blocker)
            return
        dialog = GenerateDialog(self.session, self)
        if dialog.exec() != int(GenerateDialog.DialogCode.Accepted):
            return
        choices = dialog.choices()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            job = self.session.generate_nc(
                choices.output_dir,
                prefix=choices.prefix,
                dry_run=choices.dry_run,
                per_physical_sheet=choices.per_physical_sheet,
                pdf_report=choices.pdf_report,
            )
        except SessionError as exc:
            QMessageBox.warning(self, "Cannot generate", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        save_settings(self.session.settings, self.settings_path)
        self._report_job(job)

    def _report_job(self, job) -> None:
        written = len(job.written)
        refused = job.refused
        head = f"{written} program{'' if written == 1 else 's'} written to {job.output_dir}"
        if job.dry_run:
            head += "\n\nDRY RUN: these are AIR CUTS, not production programs."
        box = QMessageBox(self)
        box.setWindowTitle("Generate NC")
        box.setIcon(
            QMessageBox.Icon.Warning if refused else QMessageBox.Icon.Information
        )
        if refused:
            head += (
                f"\n\n{len(refused)} sheet{'' if len(refused) == 1 else 's'} refused - "
                f"nothing was written for those."
            )
        # 2026-08-04 review: stale programs from an earlier run of this
        # prefix are quarantined, never deleted, and never silently -- the
        # moves go in the headline (the details list each file), and a move
        # that FAILED means the folder still holds a program that is not
        # part of this job, which the operator must hear about louder than
        # a details pane.
        superseded = getattr(job, "superseded", [])
        if superseded:
            head += (
                f"\n\n{len(superseded)} stale file{'' if len(superseded) == 1 else 's'} "
                f"from an earlier run moved to {job.quarantine_dir} "
                f"(nothing deleted - see details)."
            )
        if getattr(job, "quarantine_problems", []):
            head += (
                "\n\nSTALE FILES COULD NOT BE MOVED OUT OF THE OUTPUT FOLDER - "
                "do not take this folder to the machine until that is sorted "
                "(see details)."
            )
            box.setIcon(QMessageBox.Icon.Warning)
        # Milestone 6: the report is paperwork, so its failure is reported
        # beside the job rather than as part of it (the programs went out).
        report_path = getattr(job, "report_path", None)
        report_problem = getattr(job, "report_problem", None)
        if report_path:
            head += f"\n\nCut-sheet report: {os.path.basename(report_path)}"
        elif report_problem:
            head += f"\n\n{report_problem}"
            box.setIcon(QMessageBox.Icon.Warning)
        box.setText(head)
        details = job.summary()
        if report_problem:
            details += f"\n\n{report_problem}"
        box.setDetailedText(details)
        box.exec()
        self.statusBar().showMessage(
            f"{written} NC program(s) written"
            + (f", {len(refused)} sheet(s) refused" if refused else "")
            + (f", {len(superseded)} stale file(s) quarantined" if superseded else "")
            + (" + PDF report" if report_path else "")
            + (" [dry run]" if job.dry_run else "")
        )

    def edit_settings(self) -> None:
        """Change the optimizer settings — all of it, or none of it.

        2026-08-04 review: this used to store the new settings and then lean
        on :meth:`optimize` to rebuild the layout, which had two ways out —
        the "Discard manual edits?" prompt answered No, and a re-nest that
        failed — and both left the OLD layout on screen, generate-able, under
        the NEW settings (a 49x97 layout still writable after switching to
        40x60 stock).  So the order is now: ask about the hand edits FIRST,
        then hand the settings to the session, which invalidates the layout
        itself; a failed re-nest can no longer leave anything behind to
        generate.
        """
        dialog = SettingsDialog(self.session.settings, self)
        if dialog.exec() != int(SettingsDialog.DialogCode.Accepted):
            return
        new_settings = dialog.result_settings()
        problems = new_settings.validate()
        if problems:
            QMessageBox.warning(self, "Invalid settings", "; ".join(problems))
            return
        if self.session.edited and not self._confirm_discard_edits():
            # Nothing has been touched yet: not the session, not the disk.
            self.statusBar().showMessage(
                "Settings unchanged - your manual layout edits are still there"
            )
            return
        try:
            self.session.set_settings(new_settings)
        except SessionError as exc:
            QMessageBox.warning(self, "Invalid settings", str(exc))
            return
        if not save_settings(new_settings, self.settings_path):
            # The session is running on the new numbers either way; the user
            # needs to know they will not survive a restart.
            QMessageBox.warning(
                self,
                "Settings not saved",
                f"The new settings are in use now, but they could not be "
                f"written to {self.settings_path}, so they will be back to the "
                f"old values next time the program starts.",
            )
        if self.session.included_rows():
            self._run_optimize(layout_already_invalid=True)
        else:
            self.refresh()
            self.statusBar().showMessage("Settings updated - press Optimize (Ctrl+R)")

    # -- navigation ------------------------------------------------------

    def _step_sheet(self, delta: int) -> None:
        count = self.session.unique_sheet_count
        if not count:
            return
        self._show_sheet((self.canvas.sheet_index + delta) % count)

    def _show_sheet(self, index: int) -> None:
        if 0 <= index < self.session.unique_sheet_count:
            self.canvas.show_sheet(index)
            self.refresh_header()
            self.summary.set_current(index)

    # -- signals ---------------------------------------------------------

    def _on_order_changed(self) -> None:
        # Button enabled states (Optimize, Generate) are only recomputed in
        # refresh() -- a status-bar message alone left them stale (2026-08-04
        # owner report: "Cut none" then re-checking rows never re-enabled
        # Optimize because nothing on this path ever called refresh()).
        self.refresh()
        self.statusBar().showMessage(
            f"{self.session.total_frames} frames selected - press Optimize (Ctrl+R)"
        )

    def _on_edit_applied(self, result) -> None:
        self.refresh()
        notes = []
        if result.split:
            notes.append("split one sheet out of its run")
        if result.merged:
            notes.append("merged with an identical sheet")
        suffix = f" ({'; '.join(notes)})" if notes else ""
        self.statusBar().showMessage(f"{result.message}{suffix}")

    def _on_edit_rejected(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _on_status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _on_dropped_outside(self, path, global_pos: QPoint) -> None:
        """A part was dragged off the canvas: did it land on a sheet control?"""
        destination = self._sheet_target_at(global_pos)
        if destination is None or destination == self.canvas.sheet_index:
            self.canvas.update()
            self.statusBar().showMessage(
                "Dropped outside the sheet - drop on a sheet in the list, or on "
                "Prev/Next, to move the part to that sheet"
            )
            return
        result = self.session.move_part_to_sheet(
            self.canvas.sheet_index, path, destination
        )
        if result:
            self.canvas.sheet_index = result.sheet_index
            self.canvas.selected_path = result.path or None
            self._on_edit_applied(result)
        else:
            self._on_edit_rejected(result.message)
        self.canvas.update()

    def _sheet_target_at(self, global_pos: QPoint) -> Optional[int]:
        count = self.session.unique_sheet_count
        if not count:
            return None
        for button, delta in ((self.prev_button, -1), (self.next_button, 1)):
            if button.rect().contains(button.mapFromGlobal(global_pos)):
                return (self.canvas.sheet_index + delta) % count
        listing = self.summary.sheet_list
        local = listing.viewport().mapFromGlobal(global_pos)
        if listing.viewport().rect().contains(local):
            item = listing.itemAt(local)
            if item is not None:
                return int(item.data(Qt.ItemDataRole.UserRole))
        return None

    # -- rendering -------------------------------------------------------

    def refresh_header(self) -> None:
        self.sheet_header.setText(self.session.sheet_title(self.canvas.sheet_index))

    def refresh(self) -> None:
        self.canvas.refresh()
        self.refresh_header()
        self.summary.reload(self.canvas.sheet_index)
        has_sheets = self.session.unique_sheet_count > 1
        self.prev_button.setEnabled(has_sheets)
        self.next_button.setEnabled(has_sheets)
        self.optimize_action.setEnabled(bool(self.session.included_rows()))
        self.generate_button.setEnabled(self.session.can_generate())
        self.generate_button.setToolTip(GENERATE_TOOLTIP)

    # -- self test -------------------------------------------------------

    def schedule_self_close(self, milliseconds: int = 2000) -> None:
        """Close the window after a delay (headless launch verification)."""
        QTimer.singleShot(milliseconds, self.close)
