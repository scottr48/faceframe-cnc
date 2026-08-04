"""Order panel: the parsed lines, their include checkboxes, and the
needs-attention editor (spec section 5).

Pure view: every change is pushed straight into the session, and the panel
re-reads the session to redraw itself.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .edit_row_dialog import EditRowDialog
from .session import RowStatus, Session, SessionError, suggest_dimensions, wdc_detail

_HEADERS = ("Cut", "Part #", "Qty", "Frame W x H", "Type")


class OrderPanel(QWidget):
    """One row per parsed order line, plus the needs-attention list."""

    #: A line was ticked in or out of the cut list.
    includeChanged = Signal()
    #: A needs-attention line was completed.
    lineResolved = Signal(str)
    statusMessage = Signal(str)

    def __init__(self, session: Session, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.session = session
        self._loading = False
        #: Row key the width/height editor currently targets -- set either
        #: by picking a needs-attention list entry, or (2026-08-03
        #: amendment) by selecting a NO_FRAME row directly in the table,
        #: since those rows are not listed in ``attention_list``.
        self._resolve_key: Optional[str] = None

        self.table = QTableWidget(0, len(_HEADERS), self)
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        buttons = QHBoxLayout()
        self.all_button = QPushButton("Cut all")
        self.none_button = QPushButton("Cut none")
        self.edit_button = QPushButton("Edit...")
        self.edit_button.setEnabled(False)
        self.edit_button.setToolTip("Change this line's quantity or frame dimensions")
        self.all_button.clicked.connect(lambda: self._set_all(True))
        self.none_button.clicked.connect(lambda: self._set_all(False))
        self.edit_button.clicked.connect(self._on_edit_button)
        buttons.addWidget(self.all_button)
        buttons.addWidget(self.none_button)
        buttons.addWidget(self.edit_button)
        buttons.addStretch(1)
        self.count_label = QLabel("no order loaded")
        buttons.addWidget(self.count_label)

        # -- no-faceframe lines (2026-08-03 amendment) -----------------
        # Informational only: these rows are excluded automatically and
        # never appear in the needs-attention list; the resolve editor is
        # still reachable by selecting one of them in the table above.
        self.no_frame_label = QLabel("")
        self.no_frame_label.setWordWrap(True)
        self.no_frame_label.setStyleSheet("color: #6b7280;")
        self.no_frame_label.setVisible(False)

        # -- WDC fact sheet (2026-08-03 owner request) -----------------
        # "How do I know that it has the 2 inch stiles and the special T17
        # routing?"  A visible, read-only area — deliberately NOT a tooltip
        # the user has to hunt for — showing what the machine does to every
        # WDC frame on the order.  The text comes from the session
        # (wdc_detail), which derives every number from the geometry
        # engine, so this label can never disagree with the cut.
        self.wdc_box = QGroupBox("WDC frames — what the machine does")
        self.wdc_label = QLabel("")
        self.wdc_label.setWordWrap(True)
        self.wdc_label.setStyleSheet("color: #6b7280;")
        self.wdc_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        wdc_layout = QVBoxLayout()
        wdc_layout.addWidget(self.wdc_label)
        self.wdc_box.setLayout(wdc_layout)
        self.wdc_box.setVisible(False)

        # -- needs attention ------------------------------------------
        self.attention_box = QGroupBox("Needs attention")
        self.attention_list = QListWidget()
        self.attention_list.setMaximumHeight(90)
        self.attention_list.currentRowChanged.connect(self._on_attention_selected)
        self.reason_label = QLabel("")
        self.reason_label.setWordWrap(True)
        self.width_edit = QLineEdit()
        self.height_edit = QLineEdit()
        self.width_edit.setPlaceholderText("inches")
        self.height_edit.setPlaceholderText("inches")
        self.resolve_button = QPushButton("Resolve and include")
        self.resolve_button.clicked.connect(self._on_resolve)

        form = QFormLayout()
        form.addRow("Frame width", self.width_edit)
        form.addRow("Frame height", self.height_edit)

        attention_layout = QVBoxLayout()
        attention_layout.addWidget(self.attention_list)
        attention_layout.addWidget(self.reason_label)
        attention_layout.addLayout(form)
        attention_layout.addWidget(self.resolve_button)
        self.attention_box.setLayout(attention_layout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.no_frame_label)
        layout.addWidget(self.wdc_box)
        layout.addWidget(self.attention_box)

        self.reload()

    # -- rendering -------------------------------------------------------

    def reload(self) -> None:
        session = self.session
        self._loading = True
        try:
            rows = session.rows
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                check = QTableWidgetItem(row.part_number)
                check.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | (
                        Qt.ItemFlag.ItemIsUserCheckable
                        if row.can_include
                        else Qt.ItemFlag.NoItemFlags
                    )
                )
                check.setText("")
                check.setData(Qt.ItemDataRole.UserRole, row.key)
                check.setCheckState(
                    Qt.CheckState.Checked
                    if (row.included and row.can_include)
                    else Qt.CheckState.Unchecked
                )
                self.table.setItem(index, 0, check)

                cells = (
                    row.part_number,
                    str(row.qty),
                    row.size_text + (" (edited)" if row.edited else ""),
                    row.type_text,
                )
                for column, text in enumerate(cells, start=1):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, row.key)
                    if row.status is RowStatus.NEEDS_ATTENTION:
                        item.setForeground(QBrush(QColor("#b45309")))
                        item.setToolTip(row.reason or "missing dimension")
                    elif row.status is RowStatus.NO_FRAME:
                        # 2026-08-03 amendment: greyed out, not amber -- this
                        # is informational, not a data-entry gap to chase.
                        item.setForeground(QBrush(QColor("#9ca3af")))
                        item.setToolTip(row.hint or row.reason or "no faceframe required")
                    elif row.status is RowStatus.INVALID:
                        item.setForeground(QBrush(QColor("#b91c1c")))
                        item.setToolTip(row.geometry_error or "")
                    elif row.edited:
                        # Owner request (2026-08-03): a line changed with the
                        # Edit dialog is marked the same amber used elsewhere
                        # for "look at this", with the note (what changed,
                        # from what) as the tooltip.
                        item.setForeground(QBrush(QColor("#8a6410")))
                        item.setToolTip(row.note)
                    elif row.note:
                        # A dimension the parser derived (a WDC width from
                        # the part number) says where it came from.
                        item.setToolTip(row.note)
                    self.table.setItem(index, column, item)

            attention = session.needs_attention_rows()
            no_frame = session.no_frame_rows()
            self.attention_list.clear()
            for row in attention:
                item = QListWidgetItem(f"{row.part_number} (qty {row.qty}) - {row.reason}")
                item.setData(Qt.ItemDataRole.UserRole, row.key)
                self.attention_list.addItem(item)
            # The box holds the resolve editor, which a NO_FRAME row selected
            # directly in the table (see _on_table_selection_changed) can
            # also open, so keep it available whenever either kind exists.
            self.attention_box.setVisible(bool(attention) or bool(no_frame))
            if attention:
                self.attention_list.setCurrentRow(0)

            if no_frame:
                self.no_frame_label.setText(
                    "; ".join(
                        f"{row.part_number}: no faceframe (N/A on order)" for row in no_frame
                    )
                )
            else:
                self.no_frame_label.setText("")
            self.no_frame_label.setVisible(bool(no_frame))

            # One fact sheet per distinct WDC part on the order, in a
            # visible area (not a tooltip): the owner asked to SEE the 2"
            # stiles, the derived width and the T17 slot before trusting a
            # WDC line.
            details: list[str] = []
            seen: set[str] = set()
            for row in rows:
                if row.part_number in seen:
                    continue
                detail = wdc_detail(row.part_number, row.frame_width, row.frame_height)
                if detail:
                    seen.add(row.part_number)
                    details.append(detail)
            self.wdc_label.setText("\n\n".join(details))
            self.wdc_box.setVisible(bool(details))

            frames = session.total_frames
            self.count_label.setText(
                f"{len(session.included_rows())} of {len(rows)} lines, {frames} frames"
            )
        finally:
            self._loading = False
        # itemSelectionChanged is ignored while _loading, so the Edit
        # button's enabled state needs a manual refresh against whatever
        # selection (if any) survived the rebuild.
        self.edit_button.setEnabled(bool(self.table.selectedItems()))

    # -- events ----------------------------------------------------------

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        want = item.checkState() == Qt.CheckState.Checked
        try:
            self.session.set_included(key, want)
        except SessionError as exc:
            self.statusMessage.emit(str(exc))
            self.reload()
            return
        self.count_label.setText(
            f"{len(self.session.included_rows())} of {len(self.session.rows)} lines, "
            f"{self.session.total_frames} frames"
        )
        self.includeChanged.emit()

    def _set_all(self, included: bool) -> None:
        self.session.set_all_included(included)
        self.reload()
        self.includeChanged.emit()

    def _current_attention_key(self) -> Optional[str]:
        return self._resolve_key

    def _on_attention_selected(self, _index: int) -> None:
        item = self.attention_list.currentItem()
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self._show_editor_for(key)

    def _on_table_selection_changed(self) -> None:
        """2026-08-03 amendment: a NO_FRAME row is never in ``attention_list``
        (it is informational, not a prompt), but selecting it directly in
        the table still offers the resolve editor -- for the case where the
        order form really was wrong and the line should be cut after all.
        """
        if self._loading:
            return
        items = self.table.selectedItems()
        self.edit_button.setEnabled(bool(items))
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        if key is None:
            return
        row = self.session.row(key)
        if row.status is RowStatus.NO_FRAME:
            self._show_editor_for(key)

    # -- edit dialog (owner request, 2026-08-03) --------------------------

    def _on_cell_double_clicked(self, row_index: int, _column: int) -> None:
        item = self.table.item(row_index, 0)
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is not None:
            self._open_edit_dialog(key)

    def _on_edit_button(self) -> None:
        items = self.table.selectedItems()
        if not items:
            return
        key = items[0].data(Qt.ItemDataRole.UserRole)
        if key is not None:
            self._open_edit_dialog(key)

    def _open_edit_dialog(self, key: str) -> None:
        dialog = EditRowDialog(self.session, key, self)
        dialog.saveRequested.connect(lambda: self._on_dialog_save(dialog, key))
        dialog.revertRequested.connect(lambda: self._on_dialog_revert(dialog, key))
        dialog.exec()

    def _on_dialog_save(self, dialog: EditRowDialog, key: str) -> None:
        # Only what the user actually changed is sent: an untouched
        # placeholder in a missing dimension's spin box must not ride along
        # as a real 0.001" width on a qty-only edit.
        try:
            row = self.session.edit_row(key, **dialog.changes())
        except SessionError as exc:
            QMessageBox.warning(dialog, "Cannot save", str(exc))
            return
        dialog.accept()
        self.reload()
        self.statusMessage.emit(f"{row.part_number}: {row.note or 'no changes'}")
        self.includeChanged.emit()

    def _on_dialog_revert(self, dialog: EditRowDialog, key: str) -> None:
        try:
            row = self.session.revert_row(key)
        except SessionError as exc:
            QMessageBox.warning(dialog, "Cannot revert", str(exc))
            return
        dialog.accept()
        self.reload()
        self.statusMessage.emit(f"{row.part_number} reverted to order form values")
        self.includeChanged.emit()

    def _show_editor_for(self, key: Optional[str]) -> None:
        self._resolve_key = key
        if key is None:
            self.reason_label.setText("")
            return
        row = self.session.row(key)
        self.reason_label.setText(f"{row.reason}. {row.hint}")
        self.width_edit.setEnabled("width" in row.missing)
        self.height_edit.setEnabled("height" in row.missing)
        width, height = suggest_dimensions(row.part_number)
        self.width_edit.setText(
            "" if not self.width_edit.isEnabled() or width is None else f"{width:g}"
        )
        self.height_edit.setText(
            "" if not self.height_edit.isEnabled() or height is None else f"{height:g}"
        )

    def _on_resolve(self) -> None:
        key = self._current_attention_key()
        if key is None:
            return
        try:
            row = self.session.resolve_row(
                key,
                width=self.width_edit.text().strip() or None,
                height=self.height_edit.text().strip() or None,
            )
        except SessionError as exc:
            self.statusMessage.emit(str(exc))
            return
        self.reload()
        self.statusMessage.emit(
            f"{row.part_number} resolved to {row.size_text} and added to the cut list"
        )
        self.lineResolved.emit(key)
