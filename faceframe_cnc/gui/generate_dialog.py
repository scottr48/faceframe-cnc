"""Generate-NC dialog (spec section 5's Generate button).

Thin, like every other widget here: it collects five choices and hands them
to :meth:`faceframe_cnc.gui.session.Session.generate_nc`, which owns all the
rules.  Nothing in this file decides whether a sheet may be cut, and nothing
in it composes the PDF report — it only ticks the box that asks for one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .session import Session


@dataclass(frozen=True)
class GenerateChoices:
    """What the user picked."""

    output_dir: str
    prefix: str
    dry_run: bool
    per_physical_sheet: bool
    #: Milestone 6: write ``R<prefix>_report.pdf`` beside the programs.  On
    #: by default — the printed cut sheets are what the operator works from,
    #: and a job that goes to the machine without them is a job somebody has
    #: to come back to the office to ask about.
    pdf_report: bool = True


class GenerateDialog(QDialog):
    """Ask where the programs go, what to call them, and how to cut them."""

    def __init__(self, session: Session, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Generate NC programs")
        self.session = session

        start = session.settings.last_output_dir or os.getcwd()
        self.output_dir = QLineEdit(start)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.output_dir, 1)
        folder_row.addWidget(browse)
        folder_widget = QWidget()
        folder_widget.setLayout(folder_row)

        self.prefix = QLineEdit(session.default_job_prefix())
        self.prefix.setToolTip(
            "Digits between the leading R and the two-digit sheet index: "
            "prefix 7201 gives R720101N.anc, R720102N.anc, ..."
        )
        self.prefix.textChanged.connect(self._refresh_preview)

        self.dry_run = QCheckBox(
            "Dry run (air cut): lift every cut above the stock for a first article"
        )
        self.per_physical = QCheckBox(
            "One file per physical sheet instead of one per unique sheet"
        )
        self.pdf_report = QCheckBox(
            "Write PDF cut-sheet report (one page per sheet, drawn to scale)"
        )
        self.pdf_report.setChecked(True)
        self.pdf_report.setToolTip(
            "Saved as R<prefix>_report.pdf in the same folder. If it cannot be "
            "written the .anc programs still go out."
        )

        self.preview = QLabel()
        self.preview.setStyleSheet("color:#54606b;")

        form = QFormLayout()
        form.addRow("Output folder", folder_widget)
        form.addRow("Job prefix", self.prefix)
        form.addRow(self.dry_run)
        form.addRow(self.per_physical)
        form.addRow(self.pdf_report)
        form.addRow("First file", self.preview)

        note = QLabel(
            "Every program is verified in memory before it is written. A sheet "
            "that fails is reported and NOT written; the rest of the job still "
            "goes out. WDC frames now get their 45-degree T17 stile slots, but "
            "that slot cuts 0.875 in past each end of the stile, so a WDC frame "
            "with a neighbour or a sheet edge closer than that is refused "
            "rather than cut into the part next to it. A dry-run file is an air "
            "cut only: it must never be treated as the production program. The "
            "PDF report gives one page per unique sheet, drawn to scale with "
            "every part labelled and the run quantity in the header; a sheet "
            "that was refused still gets a page, marked REFUSED."
        )
        note.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(self.buttons)
        self._refresh_preview()

    # -- helpers ---------------------------------------------------------

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Where should the .anc programs go?", self.output_dir.text()
        )
        if folder:
            self.output_dir.setText(folder)

    def _refresh_preview(self) -> None:
        from ..post.job import sheet_filename

        text = self.prefix.text().strip()
        if text.isdigit() and len(text) <= 8:
            self.preview.setText(sheet_filename(text, 1))
            ok = True
        else:
            self.preview.setText("the prefix must be 1-8 digits")
            ok = False
        button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if button is not None:
            button.setEnabled(ok)

    def choices(self) -> GenerateChoices:
        return GenerateChoices(
            output_dir=self.output_dir.text().strip(),
            prefix=self.prefix.text().strip(),
            dry_run=self.dry_run.isChecked(),
            per_physical_sheet=self.per_physical.isChecked(),
            pdf_report=self.pdf_report.isChecked(),
        )
