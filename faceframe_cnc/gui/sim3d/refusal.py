"""A REFUSED sheet, shown instead of merely reported.

Some sheets never become a program at all: the planner raises
:class:`~faceframe_cnc.post.from_layout.SheetPlanError` (or its
:class:`~faceframe_cnc.post.from_layout.WdcNotSupportedError` subclass) and
there is no ``.anc``, no plan and no timeline, so there is nothing for
:class:`~faceframe_cnc.gui.sim3d.window.Sim3DWindow` to play.  The operator
still has to SEE what is wrong — a refusal is usually about where two parts
are, and where two parts are is exactly the thing a sentence is bad at.

So this is the same 3D sheet with the playback taken out:

*   the slabs and the parts as :mod:`~faceframe_cnc.gui.sim3d.scene` draws
    them at the start of a program, with no cursor and no spindle motion;
*   the refusal's own message, verbatim, in the banner.  It is the reason the
    planner gave and it is not paraphrased, shortened or re-punctuated here;
*   the part the refusal names outlined in the error red, when the error
    carries one (:attr:`SheetPlanError.part_number` /
    :attr:`SheetPlanError.box`).  Older refusals carry neither and simply get
    no outline — a view that guessed at which part was meant would be
    pointing the operator at the wrong frame;
*   the two envelope toggles, which is the whole reason this view is worth
    building: a WDC refusal is a cone-reach fact, and switching the cone
    reach on shows the room the slot needs against the neighbour that took
    it.  There is no plan behind a refused sheet, so
    :meth:`~faceframe_cnc.gui.sim3d.viewmodel.DangerModel.for_sheet` supplies
    the two envelopes that do not need one — the cone reaches and the fence.
    Nothing here is red except the outline: an envelope is machine reach, not
    a complaint.

No program at all
-----------------
If even the :class:`~faceframe_cnc.post.model.SheetProgram` could not be built
(a frame the geometry engine rejects, an empty sheet), pass ``program=None``:
the view is then the banner alone, which is the plain message path.  What must
never happen is a traceback in front of an operator.
"""

from __future__ import annotations

from typing import Callable

from PySide6.Qt3DCore import Qt3DCore
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...post.model import Box, PostConfig, SheetProgram, default_config
from .scene import ERROR_FILL, SimScene
from .viewmodel import DangerModel
from .window import (
    CONE_KINDS,
    CONE_TOGGLE_TEXT,
    ENVELOPE_KINDS,
    ENVELOPE_TOGGLE_TEXT,
    NO_VIEWPORT_TEXT,
    Viewport,
    create_qt3d_viewport,
)

__all__ = ["RefusalView", "REFUSAL_TITLE", "NO_PROGRAM_TEXT"]

#: What the window is called.  "Refused" and not "failed": the post declined
#: to cut this sheet, which is a decision it made on purpose.
REFUSAL_TITLE = "Sheet refused"

#: Shown in place of the sheet when there was not even a program to draw.
NO_PROGRAM_TEXT = "This sheet could not be built at all, so there is nothing to draw."

#: The name the refused part's outline is built under.
ERROR_MARK_NAME = "refusal-part"


class RefusalView(QWidget):
    """The sheet that will not be cut, and the reason, on one screen.

    ``error`` is any exception; a
    :class:`~faceframe_cnc.post.from_layout.SheetPlanError` additionally
    carries the part it is about, and that part gets the outline.  Nothing
    here judges the sheet: the refusal already did.
    """

    def __init__(
        self,
        error: BaseException,
        program: SheetProgram | None = None,
        config: PostConfig | None = None,
        parent: QWidget | None = None,
        create_viewport: Callable[[Qt3DCore.QEntity], Viewport | None] | None = None,
    ):
        super().__init__(parent)
        self.error = error
        self.program = program
        self.config = config if config is not None else default_config()
        #: The message exactly as the refusal worded it.
        self.message = str(error)
        #: Which part the refusal named, if it named one.
        self.part_number: str | None = getattr(error, "part_number", None)
        self.box: Box | None = getattr(error, "box", None)

        name = "" if program is None else program.header.name
        self.setWindowTitle(f"{REFUSAL_TITLE} - {name}" if name else REFUSAL_TITLE)

        self.scene: SimScene | None = None
        self.viewport: Viewport | None = None
        #: The footprint that got the red outline, if any.
        self.marked_box: Box | None = None
        if program is not None:
            self.scene = SimScene(
                program,
                self.config,
                danger=DangerModel.for_sheet(program, self.config),
            )
            self._mark_the_part()
            hook = create_qt3d_viewport if create_viewport is None else create_viewport
            self.viewport = hook(self.scene.root)

        self._build_ui()

    # -- construction ------------------------------------------------------

    def _mark_the_part(self) -> None:
        """Outline the refused part, from the error's own box or its number.

        The box is preferred because it is the footprint the refusal was
        computed against; the number is the fallback for a refusal that names
        a part on a sheet whose geometry it did not carry.  Neither: no mark.
        """
        assert self.scene is not None
        box = self.box
        if box is None and self.part_number and self.program is not None:
            for part in self.program.flat_parts():
                if part.part_number == self.part_number:
                    box = part.box
                    break
        if box is not None:
            self.marked_box = box
            self.scene.add_error_mark(box, ERROR_MARK_NAME)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        self.banner = QLabel(self.message, self)
        self.banner.setObjectName("refusalBanner")
        self.banner.setWordWrap(True)
        font = QFont(self.banner.font())
        font.setBold(True)
        self.banner.setFont(font)
        self.banner.setStyleSheet(
            f"background-color: {ERROR_FILL.name()}; color: white; padding: 6px;"
        )
        outer.addWidget(self.banner)

        widget: QWidget
        if self.scene is None:
            widget = QLabel(NO_PROGRAM_TEXT, self)
        else:
            found = None if self.viewport is None else self.viewport.widget
            widget = found if found is not None else QLabel(NO_VIEWPORT_TEXT, self)
        if isinstance(widget, QLabel):
            widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            widget.setWordWrap(True)
            widget.setMinimumSize(320, 240)
            widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
        self.viewport_widget = widget
        outer.addWidget(widget, 1)

        outer.addWidget(self._build_toggles())

    def _build_toggles(self) -> QWidget:
        """The two envelope toggles, both off, as in the playback window.

        Disabled when there is no scene to draw them in, rather than absent:
        the operator should see that the tool exists even on the one refusal
        that cannot use it.
        """
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(frame)

        def toggle(text: str, kinds) -> QCheckBox:
            box = QCheckBox(text, frame)
            box.setChecked(False)
            box.setEnabled(self.scene is not None)
            box.toggled.connect(
                lambda on, group=kinds: self.show_overlays(group, on)
            )
            row.addWidget(box)
            return box

        self.cone_toggle = toggle(CONE_TOGGLE_TEXT, CONE_KINDS)
        self.envelope_toggle = toggle(ENVELOPE_TOGGLE_TEXT, ENVELOPE_KINDS)
        row.addStretch(1)
        return frame

    # -- what little there is to do ----------------------------------------

    def show_overlays(self, kinds, visible: bool) -> None:
        """Switch one envelope family on or off, as the playback window does.

        A refused sheet carries no lead-in envelopes (there are no loops), so
        that half of the second toggle has nothing to show and the fence has;
        asking for either is harmless.
        """
        if self.scene is None:
            return
        for kind in kinds:
            self.scene.set_overlay_visible(kind, bool(visible))

    @property
    def marked_part(self) -> Box | None:
        """The footprint this view outlined in red, or ``None``."""
        return self.marked_box if self.scene is not None else None
