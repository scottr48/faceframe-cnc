"""Scale drawing of one sheet, with drag-to-override (spec section 5).

A dumb view: it turns sheet inches into pixels, turns mouse gestures back
into inches, and asks :class:`~faceframe_cnc.gui.session.Session` what any
of it means.  Every rule about what may sit where lives in the session, so
the only "logic" here is hit-test dispatch and repainting.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolTip, QWidget

from .session import EditResult, PartPath, Session

#: Palette.  Muted enough to read at a glance on a shop-floor monitor, with
#: hosts and their passengers clearly different (spec 5: "distinguish
#: hosts/children visually").
SHEET_FILL = QColor("#fbfbf8")
SHEET_EDGE = QColor("#8a8a80")
CUSHION_EDGE = QColor("#d8d8cc")
FRONT_MARGIN_EDGE = QColor("#c9b8a0")
PART_FILL = QColor("#cfe0ee")
PART_EDGE = QColor("#2f4a63")
HOST_FILL = QColor("#bcd6ea")
CHILD_FILL = QColor("#f6dfae")
CHILD_EDGE = QColor("#8a6410")
OPENING_FILL = QColor("#ffffff")
OPENING_EDGE = QColor("#9aa7b1")
SELECT_EDGE = QColor("#c2410c")
GHOST_OK = QColor(34, 139, 34, 90)
GHOST_BAD = QColor(200, 40, 40, 90)
LABEL_COLOR = QColor("#12212e")


class SheetCanvas(QWidget):
    """Draws sheet ``sheet_index`` of a session and edits it by dragging."""

    #: A part was selected (or deselected): ``(sheet_index, path|None)``.
    selectionChanged = Signal(int, object)
    #: An edit was applied; carries the session's :class:`EditResult`.
    editApplied = Signal(object)
    #: An edit was refused; carries the violated rule.
    editRejected = Signal(str)
    #: A drag ended outside the sheet: ``(path, global QPoint)``.  The main
    #: window decides whether that landed on a sheet-navigation control.
    droppedOutside = Signal(object, object)
    #: Transient text for the status bar.
    statusMessage = Signal(str)

    def __init__(self, session: Session, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.session = session
        self.sheet_index = 0
        self.selected_path: Optional[PartPath] = None

        self._drag_path: Optional[PartPath] = None
        self._grab_dx = 0.0
        self._grab_dy = 0.0
        self._ghost: Optional[tuple[float, float, float, float]] = None
        self._ghost_ok = True
        self._ghost_reason = ""

        self.setMinimumSize(220, 380)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

    # -- state ----------------------------------------------------------

    def show_sheet(self, index: int, path: Optional[PartPath] = None) -> None:
        self.sheet_index = index
        self.selected_path = path
        self._cancel_drag()
        self.update()
        self.selectionChanged.emit(self.sheet_index, self.selected_path)

    def refresh(self) -> None:
        sheets = self.session.unique_sheet_count
        if self.sheet_index >= sheets:
            self.sheet_index = max(0, sheets - 1)
        self.update()

    # -- coordinate transform -------------------------------------------

    def _fit(self) -> tuple[float, float, float]:
        """``(scale px/inch, origin x, origin y)`` for the current widget size."""
        config = self.session.config
        margin = 14.0
        available_w = max(1.0, self.width() - 2 * margin)
        available_h = max(1.0, self.height() - 2 * margin)
        scale = min(available_w / config.sheet_width, available_h / config.sheet_height)
        drawn_w = config.sheet_width * scale
        drawn_h = config.sheet_height * scale
        return scale, (self.width() - drawn_w) / 2.0, (self.height() - drawn_h) / 2.0

    def to_widget(self, x: float, y: float) -> QPointF:
        scale, ox, oy = self._fit()
        config = self.session.config
        return QPointF(ox + x * scale, oy + (config.sheet_height - y) * scale)

    def _rect(self, x: float, y: float, w: float, h: float) -> QRectF:
        top_left = self.to_widget(x, y + h)
        scale, _ox, _oy = self._fit()
        return QRectF(top_left.x(), top_left.y(), w * scale, h * scale)

    def to_sheet(self, point: QPointF) -> tuple[float, float]:
        scale, ox, oy = self._fit()
        config = self.session.config
        return (point.x() - ox) / scale, config.sheet_height - (point.y() - oy) / scale

    # -- painting --------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#eceff1"))

        config = self.session.config
        sheet_rect = self._rect(0.0, 0.0, config.sheet_width, config.sheet_height)
        painter.fillRect(sheet_rect, SHEET_FILL)
        painter.setPen(QPen(SHEET_EDGE, 1.4))
        painter.drawRect(sheet_rect)

        cushion = config.edge_cushion
        if cushion > 0:
            painter.setPen(QPen(CUSHION_EDGE, 1.0, Qt.PenStyle.DashLine))
            painter.drawRect(
                self._rect(
                    cushion,
                    cushion,
                    max(0.0, config.sheet_width - 2 * cushion),
                    max(0.0, config.sheet_height - 2 * cushion),
                )
            )

        # Front-edge margin guide (2026-08-03 amendment): a subtle line at
        # Y=front_margin, the soft target for how far parts sit off Y=0.
        margin = config.front_margin
        if 0.0 < margin < config.sheet_height:
            painter.setPen(QPen(FRONT_MARGIN_EDGE, 1.0, Qt.PenStyle.DotLine))
            painter.drawLine(self.to_widget(0.0, margin), self.to_widget(config.sheet_width, margin))

        if self.session.result is None or not self.session.unique_sheet_count:
            painter.setPen(QPen(LABEL_COLOR))
            painter.drawText(
                sheet_rect,
                Qt.AlignmentFlag.AlignCenter,
                "Load an order and press Optimize",
            )
            painter.end()
            return

        try:
            layout, _run = self.session.sheet(self.sheet_index)
        except Exception:  # noqa: BLE001 - a stale index must never crash paint
            painter.end()
            return

        for index, placement in enumerate(layout.placements):
            self._draw_part(painter, placement, (index,))

        if self._ghost is not None:
            x, y, w, h = self._ghost
            painter.setBrush(QBrush(GHOST_OK if self._ghost_ok else GHOST_BAD))
            painter.setPen(
                QPen(SELECT_EDGE if self._ghost_ok else GHOST_BAD.darker(160), 1.6)
            )
            painter.drawRect(self._rect(x, y, w, h))

        painter.end()

    def _draw_part(self, painter: QPainter, placement, path: PartPath) -> None:
        rect = self._rect(placement.x, placement.y, placement.width, placement.height)
        nested = len(path) > 1
        if nested:
            fill, edge = CHILD_FILL, CHILD_EDGE
        elif placement.children:
            fill, edge = HOST_FILL, PART_EDGE
        else:
            fill, edge = PART_FILL, PART_EDGE

        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(edge, 1.2))
        painter.drawRect(rect)

        # The routed openings, so the user can see the frame members and
        # where an inner can possibly go (spec 5).
        painter.setBrush(QBrush(OPENING_FILL))
        painter.setPen(QPen(OPENING_EDGE, 0.9))
        for opening in self.session.sheet_openings(placement):
            painter.drawRect(
                self._rect(opening.x, opening.y, opening.width, opening.height)
            )

        for index, child in enumerate(placement.children):
            self._draw_part(painter, child, path + (index,))

        if path == self.selected_path:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(SELECT_EDGE, 2.4))
            painter.drawRect(rect)

        self._draw_label(painter, rect, placement.part_number, nested, bool(placement.children))

    def _draw_label(
        self, painter: QPainter, rect: QRectF, text: str, nested: bool, is_host: bool
    ) -> None:
        """Every part carries its part number (spec 5), readable at fit scale.

        A host's label goes in its top-left corner, over the frame member
        rather than over the frame nested in its opening; everything else is
        centred, where the routed opening gives it a white background.
        """
        font = QFont(painter.font())
        size = 9.5 if nested else 11.0
        font.setPointSizeF(size)
        font.setBold(not nested)
        metrics = QFontMetricsF(font)
        while size > 6.0 and (
            metrics.horizontalAdvance(text) > rect.width() - 6
            or metrics.height() > rect.height() - 4
        ):
            size -= 0.5
            font.setPointSizeF(size)
            metrics = QFontMetricsF(font)
        painter.setFont(font)

        if is_host:
            width = metrics.horizontalAdvance(text) + 6
            height = metrics.height() + 2
            chip = QRectF(rect.left() + 2, rect.top() + 2, width, height)
            painter.setBrush(QBrush(QColor(255, 255, 255, 215)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(chip)
            painter.setPen(QPen(LABEL_COLOR))
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
            return

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(LABEL_COLOR))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    # -- mouse -----------------------------------------------------------

    def _selectable_at(self, point: QPointF) -> Optional[PartPath]:
        if self.session.result is None or not self.session.unique_sheet_count:
            return None
        x, y = self.to_sheet(point)
        try:
            return self.session.hit_test(self.sheet_index, x, y)
        except Exception:  # noqa: BLE001
            return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        position = event.position()
        path = self._selectable_at(position)
        self.selected_path = path
        self.selectionChanged.emit(self.sheet_index, path)
        if path is None:
            self._cancel_drag()
            self.update()
            return
        placement = self._placement(path)
        if placement is None:
            return
        x, y = self.to_sheet(position)
        self._drag_path = path
        self._grab_dx = x - placement.x
        self._grab_dy = y - placement.y
        self._ghost = (placement.x, placement.y, placement.width, placement.height)
        self._ghost_ok = True
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_path is None:
            return
        placement = self._placement(self._drag_path)
        if placement is None:
            return
        x, y = self.to_sheet(event.position())
        new_x = round(x - self._grab_dx, 4)
        new_y = round(y - self._grab_dy, 4)
        if self._ghost is not None and (
            abs(new_x - self._ghost[0]) < 1e-4 and abs(new_y - self._ghost[1]) < 1e-4
        ):
            return
        self._ghost = (new_x, new_y, placement.width, placement.height)
        preview = self.session.preview_drop(self.sheet_index, self._drag_path, new_x, new_y)
        self._ghost_ok = bool(preview)
        self._ghost_reason = preview.message
        self.statusMessage.emit(
            f"{placement.part_number} to ({new_x:.3f}, {new_y:.3f}) - {preview.message}"
            if preview
            else preview.message
        )
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._drag_path is None:
            super().mouseReleaseEvent(event)
            return
        path = self._drag_path
        ghost = self._ghost
        self._cancel_drag()
        if ghost is None:
            self.update()
            return
        position = event.position()
        if not self.rect().contains(position.toPoint()):
            # Left the canvas: the main window checks whether it landed on a
            # sheet-navigation control (spec 5: move between sheets by drag).
            self.droppedOutside.emit(path, event.globalPosition().toPoint())
            self.update()
            return
        result = self.session.apply_drop(self.sheet_index, path, ghost[0], ghost[1])
        self._handle(result)

    def _cancel_drag(self) -> None:
        self._drag_path = None
        self._ghost = None
        self._ghost_reason = ""
        self._ghost_ok = True

    # -- keyboard --------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape and self._drag_path is not None:
            self._cancel_drag()
            self.update()
            return
        if self.selected_path is None:
            super().keyPressEvent(event)
            return
        if key in (Qt.Key.Key_R,):
            self.rotate_selected()
            return
        nudges = {
            Qt.Key.Key_Left: (-1.0, 0.0),
            Qt.Key.Key_Right: (1.0, 0.0),
            Qt.Key.Key_Up: (0.0, 1.0),
            Qt.Key.Key_Down: (0.0, -1.0),
        }
        if key in nudges:
            step = 0.0625 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0.5
            dx, dy = nudges[key]
            self._handle(
                self.session.nudge_part(
                    self.sheet_index, self.selected_path, dx * step, dy * step
                )
            )
            return
        super().keyPressEvent(event)

    # -- context menu ----------------------------------------------------

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        if self.session.result is None or not self.session.unique_sheet_count:
            return
        path = self._selectable_at(QPointF(event.pos()))
        if path is None:
            return
        self.selected_path = path
        self.selectionChanged.emit(self.sheet_index, path)
        self.update()

        self.build_context_menu(path).exec(event.globalPos())

    def build_context_menu(self, path: PartPath) -> QMenu:
        """The right-click menu for one part (built separately so it is testable)."""
        placement = self._placement(path)
        menu = QMenu(self)
        menu.addAction(
            f"Rotate {placement.part_number} 90 degrees\tR", self.rotate_selected
        )
        if len(path) > 1:
            menu.addAction("Centre in opening", self.centre_selected)
            menu.addAction("Un-nest onto the sheet", self.unnest_selected)
        else:
            hosts = self._host_choices(path)
            if hosts:
                submenu = menu.addMenu("Nest inside")
                for host_path, label in hosts:
                    submenu.addAction(
                        label, lambda p=path, h=host_path: self._nest(p, h)
                    )
        move_menu = menu.addMenu("Move to sheet")
        for index in range(self.session.unique_sheet_count):
            action = move_menu.addAction(
                f"Sheet {index + 1} ({self.session.sheet_contents(index)})",
                lambda p=path, i=index: self._move_to_sheet(p, i),
            )
            if index == self.sheet_index:
                action.setEnabled(False)
        return menu

    def _host_choices(self, path: PartPath) -> list[tuple[PartPath, str]]:
        layout, _run = self.session.sheet(self.sheet_index)
        choices: list[tuple[PartPath, str]] = []
        for index, candidate in enumerate(layout.placements):
            if (index,) == path:
                continue
            if self.session.sheet_openings(candidate):
                choices.append(
                    ((index,), f"{candidate.part_number} at ({candidate.x:.1f}, {candidate.y:.1f})")
                )
        return choices

    # -- commands (all of them go through the session) --------------------

    def _placement(self, path: PartPath):
        try:
            layout, _run = self.session.sheet(self.sheet_index)
        except Exception:  # noqa: BLE001
            return None
        items = layout.placements
        placement = None
        for index in path:
            if index >= len(items):
                return None
            placement = items[index]
            items = placement.children
        return placement

    def rotate_selected(self) -> None:
        if self.selected_path is None:
            return
        self._handle(self.session.rotate_part(self.sheet_index, self.selected_path))

    def centre_selected(self) -> None:
        if self.selected_path is None:
            return
        self._handle(self.session.centre_in_opening(self.sheet_index, self.selected_path))

    def unnest_selected(self) -> None:
        if self.selected_path is None:
            return
        self._handle(self.session.unnest_part(self.sheet_index, self.selected_path))

    def _nest(self, path: PartPath, host_path: PartPath) -> None:
        self._handle(self.session.nest_part(self.sheet_index, path, host_path))

    def _move_to_sheet(self, path: PartPath, destination: int) -> None:
        self._handle(self.session.move_part_to_sheet(self.sheet_index, path, destination))

    def move_selected_to_sheet(self, destination: int) -> None:
        if self.selected_path is None:
            return
        self._move_to_sheet(self.selected_path, destination)

    def _handle(self, result: EditResult) -> None:
        """Apply an :class:`EditResult`: follow the part, or explain the refusal."""
        if result:
            self.sheet_index = result.sheet_index
            self.selected_path = result.path or None
            self.update()
            self.editApplied.emit(result)
            self.selectionChanged.emit(self.sheet_index, self.selected_path)
        else:
            self.update()
            self.editRejected.emit(result.message)
            QToolTip.showText(QCursor.pos(), result.message, self)
