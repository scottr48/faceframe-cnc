"""Settings dialog (spec section 5): sheet size, spacing, inside nesting.

Reads and writes an :class:`~faceframe_cnc.gui.session.AppSettings`; the
main window persists it to the local JSON file and re-optimizes.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .session import AppSettings


def _spin(value: float, minimum: float, maximum: float, step: float) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setDecimals(4)
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setValue(value)
    box.setSuffix(" in")
    return box


class SettingsDialog(QDialog):
    """Edit the optimizer settings.  ``result_settings`` holds the outcome."""

    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self._settings = settings

        self.sheet_width = _spin(settings.sheet_width, 1.0, 240.0, 1.0)
        self.sheet_height = _spin(settings.sheet_height, 1.0, 480.0, 1.0)
        self.part_gap = _spin(settings.part_gap, 0.0, 12.0, 0.0625)
        self.edge_cushion = _spin(settings.edge_cushion, 0.0, 12.0, 0.125)
        self.front_margin = _spin(settings.front_margin, 0.0, 12.0, 0.125)
        self.inside_nesting = QCheckBox("Nest small frames inside larger frames' openings")
        self.inside_nesting.setChecked(settings.inside_nesting)
        self.inside_recursion = QCheckBox(
            "Allow a nested frame to host a frame of its own (depth 2)"
        )
        self.inside_recursion.setChecked(settings.inside_recursion)

        form = QFormLayout()
        form.addRow("Sheet width", self.sheet_width)
        form.addRow("Sheet height", self.sheet_height)
        form.addRow("Gap between parts", self.part_gap)
        form.addRow("Edge cushion (soft)", self.edge_cushion)
        form.addRow("Front edge margin (in)", self.front_margin)
        form.addRow(self.inside_nesting)
        form.addRow(self.inside_recursion)

        note = QLabel(
            "The gap is a hard minimum between any two parts, and between a nested "
            "frame and the opening it sits in. The edge cushion is a preference: "
            "parts may reach the sheet edge when the packing needs it. The front "
            "edge margin is also a preference: when a sheet has vertical slack, "
            "parts start this far off the front edge (Y=0); any slack beyond "
            "that goes to the back edge instead."
        )
        note.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def result_settings(self) -> AppSettings:
        """The edited settings (the original is never mutated in place)."""
        return AppSettings(
            sheet_width=self.sheet_width.value(),
            sheet_height=self.sheet_height.value(),
            part_gap=self.part_gap.value(),
            edge_cushion=self.edge_cushion.value(),
            front_margin=self.front_margin.value(),
            inside_nesting=self.inside_nesting.isChecked(),
            inside_recursion=self.inside_recursion.isChecked(),
            last_order_path=self._settings.last_order_path,
        )
