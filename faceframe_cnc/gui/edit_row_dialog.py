"""Edit-row dialog (owner request, 2026-08-03): change a line's quantity
and frame dimensions, with an explicit save step -- Scott's words, "after
the change a save button or some other form of 'are you sure?'".

Thin, like every other dialog here.  Every rule -- what counts as a usable
quantity or dimension, whether the result still produces valid geometry,
what the provenance note ends up saying -- lives in
:meth:`faceframe_cnc.gui.session.Session.edit_row` /
:meth:`~faceframe_cnc.gui.session.Session.revert_row`.  This dialog collects
three numbers, shows the user exactly what will change before they commit
to it (the "are you sure?"), and leaves the actual session call to its
caller: it emits :attr:`saveRequested` / :attr:`revertRequested` instead of
closing itself, so :class:`~faceframe_cnc.gui.order_panel.OrderPanel` can
call the session, and — on a :class:`~faceframe_cnc.gui.session.SessionError`
— report it and leave the dialog open with the user's values intact, rather
than the dialog closing out from under a rejected edit.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .session import Session, _format_dim


class EditRowDialog(QDialog):
    """Edit one order line's quantity and frame width/height."""

    #: The user clicked "Save changes" -- read :meth:`values` for what to
    #: pass to :meth:`Session.edit_row`.
    saveRequested = Signal()
    #: The user clicked "Revert to order form".
    revertRequested = Signal()

    def __init__(self, session: Session, key: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.session = session
        self.key = key
        row = session.row(key)
        self.setWindowTitle(f"Edit {row.part_number}")

        self.qty = QSpinBox()
        self.qty.setRange(1, 100000)
        self.qty.setValue(max(1, row.qty))

        self.width = QDoubleSpinBox()
        self.width.setDecimals(3)
        self.width.setRange(0.001, 240.0)
        self.width.setSuffix('"')
        self.width.setValue(row.frame_width if row.frame_width is not None else 0.001)

        self.height = QDoubleSpinBox()
        self.height.setDecimals(3)
        self.height.setRange(0.001, 480.0)
        self.height.setSuffix('"')
        self.height.setValue(row.frame_height if row.frame_height is not None else 0.001)

        self.qty.valueChanged.connect(self._refresh)
        self.width.valueChanged.connect(self._refresh)
        self.height.valueChanged.connect(self._refresh)

        self.original_label = QLabel(self._original_text())
        self.original_label.setWordWrap(True)
        self.original_label.setStyleSheet("color:#6b7280;")

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Quantity", self.qty)
        form.addRow("Frame width", self.width)
        form.addRow("Frame height", self.height)

        self.revert_button = QPushButton("Revert to order form")
        self.revert_button.clicked.connect(self.revertRequested)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save changes")
        self.buttons.accepted.connect(self.saveRequested)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.original_label)
        layout.addLayout(form)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.revert_button)
        layout.addWidget(self.buttons)

        self._refresh()

    # -- content -----------------------------------------------------------

    def _original_text(self) -> str:
        row = self.session.row(self.key)
        text = (
            f"order form: qty {row.original_qty}, "
            f'{_format_dim(row.original_width)}" x {_format_dim(row.original_height)}"'
        )
        if row.base_note:
            text += f"\n{row.base_note}"
        return text

    #: What a missing dimension's spin box shows until the user touches it.
    #: While the box still reads this, the dimension counts as UNSET: it is
    #: neither a change to report nor a value to send to the session — so a
    #: qty-only edit on a row that is still missing a dimension stays a
    #: qty-only edit instead of smuggling 0.001" in as a real width.
    PLACEHOLDER = 0.001

    def _pending_dim(self, spin: QDoubleSpinBox, current: Optional[float]) -> Optional[float]:
        """The value to send for one dimension, ``None`` for "leave alone"."""
        if current is None:
            untouched = abs(spin.value() - self.PLACEHOLDER) <= 1e-9
            return None if untouched else spin.value()
        return None if abs(spin.value() - current) <= 1e-9 else spin.value()

    def changes(self) -> dict:
        """Only the fields that actually differ, as ``Session.edit_row`` kwargs."""
        row = self.session.row(self.key)
        out: dict = {}
        if self.qty.value() != row.qty:
            out["qty"] = self.qty.value()
        width = self._pending_dim(self.width, row.frame_width)
        if width is not None:
            out["width"] = width
        height = self._pending_dim(self.height, row.frame_height)
        if height is not None:
            out["height"] = height
        return out

    def _refresh(self) -> None:
        row = self.session.row(self.key)
        pending = self.changes()
        changes: list[str] = []
        if "qty" in pending:
            changes.append(f"qty {row.qty} -> {pending['qty']}")
        if "width" in pending:
            changes.append(
                f"width {_format_dim(row.frame_width)} -> {_format_dim(pending['width'])}"
            )
        if "height" in pending:
            changes.append(
                f"height {_format_dim(row.frame_height)} -> {_format_dim(pending['height'])}"
            )

        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if changes:
            self.summary_label.setText(", ".join(changes))
            ok_button.setEnabled(True)
        else:
            self.summary_label.setText("no changes")
            ok_button.setEnabled(False)
        self.revert_button.setVisible(row.edited)

    # -- outcome -------------------------------------------------------

    def values(self) -> tuple[int, float, float]:
        """The quantity/width/height currently dialled in (raw spin values;
        prefer :meth:`changes` for what should actually be saved)."""
        return self.qty.value(), self.width.value(), self.height.value()
