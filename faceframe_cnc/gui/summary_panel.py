"""Summary panel (spec section 5): the headline sheet count, the saving
against the no-inside-nesting baseline, and the unique-sheet list.

Clicking a sheet in the list navigates the preview to it; the list also
accepts a part dragged off the canvas (the main window wires that up).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .session import Session


class SummaryPanel(QWidget):
    """Totals plus the clickable unique-sheet list."""

    sheetSelected = Signal(int)

    def __init__(self, session: Session, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.session = session

        self.headline_caption = QLabel("TOTAL SHEETS")
        self.headline_caption.setStyleSheet("color:#54606b; letter-spacing:1px;")
        self.total_sheets_label = QLabel("-")
        self.total_sheets_label.setStyleSheet(
            "font-size:46px; font-weight:700; color:#12212e;"
        )
        self.total_sheets_label.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.saved_label = QLabel("")
        self.saved_label.setWordWrap(True)
        self.saved_label.setStyleSheet("color:#166534; font-weight:600;")
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.problem_label = QLabel("")
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color:#b91c1c;")

        self.sheet_list = QListWidget()
        self.sheet_list.currentRowChanged.connect(self._on_row_changed)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFrameShadow(QFrame.Shadow.Sunken)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.headline_caption)
        layout.addWidget(self.total_sheets_label)
        layout.addWidget(self.saved_label)
        layout.addWidget(rule)
        layout.addWidget(self.detail_label)
        layout.addWidget(QLabel("Unique sheets"))
        layout.addWidget(self.sheet_list, 1)
        layout.addWidget(self.problem_label)

        self.reload()

    def reload(self, current: int = -1) -> None:
        session = self.session
        summary = session.summary()
        self.total_sheets_label.setText(
            str(summary["total_sheets"]) if session.result is not None else "-"
        )

        saved = summary["sheets_saved"]
        baseline = summary["baseline_sheets"]
        if session.result is None:
            self.saved_label.setText("")
        elif saved is None:
            self.saved_label.setText("no inside-nesting baseline was computed")
        else:
            self.saved_label.setText(
                f"{saved} sheet{'s' if saved != 1 else ''} saved vs the "
                f"{baseline}-sheet no-inside-nesting baseline"
            )

        self.detail_label.setText(
            "\n".join(
                (
                    f"Frames included: {summary['frames']} "
                    f"(on {summary['lines']} order lines)",
                    f"Unique sheet pictures: {summary['unique_sheets']}",
                    f"Frames nested inside another frame: {summary['inside_placements']}",
                    f"Sheet fill: {summary['fill'] * 100:.1f}% "
                    f"(area floor {summary['area_floor']} sheets)",
                )
            )
        )

        self.sheet_list.blockSignals(True)
        self.sheet_list.clear()
        for index in range(session.unique_sheet_count):
            _layout, run = session.sheet(index)
            item = QListWidgetItem(
                f"Sheet {index + 1}  x{run}\n    {session.sheet_contents(index)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.sheet_list.addItem(item)
        if 0 <= current < self.sheet_list.count():
            self.sheet_list.setCurrentRow(current)
        self.sheet_list.blockSignals(False)

        problems = session.problems()
        self.problem_label.setText(
            "" if not problems else f"{len(problems)} layout problem(s): {problems[0]}"
        )

    def set_current(self, index: int) -> None:
        if 0 <= index < self.sheet_list.count() and index != self.sheet_list.currentRow():
            self.sheet_list.blockSignals(True)
            self.sheet_list.setCurrentRow(index)
            self.sheet_list.blockSignals(False)

    def _on_row_changed(self, index: int) -> None:
        if index >= 0:
            self.sheetSelected.emit(index)
