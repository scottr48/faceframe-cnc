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

GENERATE_TOOLTIP = "NC generation arrives in Milestone 5"


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
        self.generate_button = QPushButton("Generate NC + PDF")
        self.generate_button.setEnabled(False)
        self.generate_button.setToolTip(GENERATE_TOOLTIP)
        bar.addWidget(self.generate_button)

    # -- actions ---------------------------------------------------------

    def open_order(self) -> None:
        start = self.session.settings.last_order_path or os.getcwd()
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open order spreadsheet", start, "Excel files (*.xls *.xlsx);;All files (*)"
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
        self.statusBar().showMessage(
            f"Loaded {len(self.session.rows)} lines from {os.path.basename(path)}"
            + (f" - {attention} need attention" if attention else "")
        )

    def optimize(self) -> None:
        if self.session.edited:
            answer = QMessageBox.question(
                self,
                "Discard manual edits?",
                "Re-optimizing throws away the layout changes you made by hand. "
                "Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.session.optimize()
        except SessionError as exc:
            QMessageBox.warning(self, "Cannot optimize", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.canvas.show_sheet(0)
        self.refresh()
        summary = self.session.summary()
        saved = summary["sheets_saved"]
        self.statusBar().showMessage(
            f"{summary['total_sheets']} sheets, {summary['unique_sheets']} unique"
            + (f", {saved} saved by inside nesting" if saved is not None else "")
        )

    def edit_settings(self) -> None:
        dialog = SettingsDialog(self.session.settings, self)
        if dialog.exec() != int(SettingsDialog.DialogCode.Accepted):
            return
        new_settings = dialog.result_settings()
        problems = new_settings.validate()
        if problems:
            QMessageBox.warning(self, "Invalid settings", "; ".join(problems))
            return
        self.session.settings = new_settings
        save_settings(new_settings, self.settings_path)
        if self.session.result is not None or self.session.included_rows():
            self.optimize()
        else:
            self.refresh()

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
        self.generate_button.setToolTip(GENERATE_TOOLTIP)

    # -- self test -------------------------------------------------------

    def schedule_self_close(self, milliseconds: int = 2000) -> None:
        """Close the window after a delay (headless launch verification)."""
        QTimer.singleShot(milliseconds, self.close)
