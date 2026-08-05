"""The 3D cut simulation window: thin widgets over
:mod:`~faceframe_cnc.gui.sim3d.viewmodel`.

Three things the shop owner asked for, and where each of them is:

1.  a PROMINENT current-tool field — :attr:`Sim3DWindow.tool_field`, the big
    bold label at the left of the readout strip, whose text is
    :func:`~faceframe_cnc.gui.sim3d.viewmodel.tool_display` of whatever
    :class:`~faceframe_cnc.post.model.ToolSpec` the cursor says is in the
    spindle.  It changes on the step that changes section, because it is
    recomputed from the cursor on every refresh and never cached;
2.  a FREE 3D camera — orbit, pan and zoom with the mouse through
    :class:`~PySide6.Qt3DExtras.Qt3DExtras.QOrbitCameraController`, plus a
    "Reset view" button that puts the camera back on
    :func:`~faceframe_cnc.gui.sim3d.viewmodel.camera_pose`;
3.  a PLAYBACK SPEED control — the speed slider over
    :data:`~faceframe_cnc.gui.sim3d.viewmodel.SPEED_CHOICES`, with the
    multiplier shown beside it and a default faster than real time.

The clock lives here and only here
---------------------------------
One :class:`~PySide6.QtCore.QTimer` drives the animation, and all it does is
hand :meth:`Sim3DWindow.advance` a number of seconds; the maths that turns
seconds into cursor movement is in the viewmodel.  So the window can be driven
step by step with the timer stopped and lands in exactly the state plain
cursor stepping lands in, which is what ``tests/test_sim3d.py`` pins.

Findings, when the caller has any
--------------------------------
``findings`` is optional and defaults to ``None``: this window never runs the
verifier itself.  Whether a program has been judged, and against what, is the
caller's decision (Milestone 5's session wiring passes a
:class:`~faceframe_cnc.sim.FindingSet` in), and a window that quietly verified
what it was handed would be a second authority.

Given a set, three things appear and nothing else changes: a banner across the
top carrying the count and the refusal, a panel listing every finding in the
verifier's own words, and the red marks in the scene
(:mod:`~faceframe_cnc.gui.sim3d.scene`).  Given ``None`` — or an empty set —
the banner and the panel are hidden and the scene is byte for byte the
Milestone 3 scene.  The two envelope toggles are always there, because an
envelope is a fact about the machine rather than a complaint about the sheet,
and both start OFF.

The viewport is injected
------------------------
``create_viewport`` is a hook: it is handed the scene's root entity and returns
a :class:`Viewport` (a widget plus the camera and camera controller behind it),
or ``None`` for no viewport at all.  The default,
:func:`create_qt3d_viewport`, builds the real
:class:`~PySide6.Qt3DExtras.Qt3DExtras.Qt3DWindow`; tests inject one that
returns ``None``, which is how every widget, readout and transport gesture in
here is tested with no GL context anywhere.  A window with no viewport shows a
placeholder and works in every other respect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.Qt3DCore import Qt3DCore
from PySide6.Qt3DExtras import Qt3DExtras
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QVector3D
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...sim import FindingSet, SimController, SimTimeline
from .scene import ERROR_FILL, SimScene
from .viewmodel import (
    CAMERA_FOV_DEGREES,
    CAMERA_MIN_ASPECT,
    DEFAULT_SPEED_INDEX,
    SPEED_CHOICES,
    DangerModel,
    OverlayKind,
    Readouts,
    banner_text,
    camera_pose,
    cut_rows,
    current_row,
    finding_rows,
    motion_duration,
    speed_text,
)

__all__ = [
    "Sim3DWindow",
    "Viewport",
    "create_qt3d_viewport",
    "TICK_MS",
    "CONE_TOGGLE_TEXT",
    "ENVELOPE_TOGGLE_TEXT",
]

#: VISUAL ONLY.  Animation tick, about 60 a second.  The tick length is the
#: only thing the wall clock contributes: what a tick DOES is
#: :func:`~faceframe_cnc.gui.sim3d.viewmodel.step_duration`'s answer.
TICK_MS = 16

#: VISUAL ONLY.  Camera controller rates, in scene units (inches) per second
#: and degrees per second.
CAMERA_LINEAR_SPEED = 120.0
CAMERA_LOOK_SPEED = 240.0

#: VISUAL ONLY.  How much bigger than the window's font the current-tool
#: field is: the owner asked for it to be prominent, and it is the one readout
#: that has to be legible from the machine.
TOOL_FIELD_SCALE = 1.9

PLAY_TEXT = "Play"
PAUSE_TEXT = "Pause"

#: The two envelope toggles, named for what they SHOW rather than for what
#: they protect against: both are legitimate machine reach.
CONE_TOGGLE_TEXT = "WDC cone reach"
ENVELOPE_TOGGLE_TEXT = "Lead-in envelopes"

#: Which overlay families each toggle owns.  The fence rides with the lead-in
#: envelopes because it is the boundary they are judged against — showing a
#: ramp that runs off the sheet without showing the sheet's edge says nothing.
CONE_KINDS = (OverlayKind.CONE_REACH,)
ENVELOPE_KINDS = (OverlayKind.LEAD_IN, OverlayKind.FENCE)

#: Shown in place of the viewport when there is none (a test, or a machine
#: whose driver cannot give Qt3D a surface).
NO_VIEWPORT_TEXT = "3D viewport unavailable"


@dataclass
class Viewport:
    """What a viewport hook hands back.

    ``widget`` is what goes in the layout (``None`` for no viewport).
    ``camera`` and ``controller`` are the Qt3D camera and its mouse
    controller, or ``None``; ``view`` is the
    :class:`~PySide6.Qt3DExtras.Qt3DExtras.Qt3DWindow` itself, held so it
    outlives the container widget.
    """

    widget: QWidget | None = None
    camera: object | None = None
    controller: object | None = None
    view: object | None = None


def create_qt3d_viewport(root: Qt3DCore.QEntity) -> Viewport:
    """The real viewport: a Qt3D window in a container, with a free camera.

    Requires a render surface, so nothing under ``tests/`` calls this.
    """
    view = Qt3DExtras.Qt3DWindow()
    camera = view.camera()
    # The same field of view camera_pose() framed the sheet against; Qt3DWindow
    # keeps the aspect in step with the container as it is resized.
    camera.lens().setPerspectiveProjection(
        CAMERA_FOV_DEGREES, 16.0 / 9.0, 0.5, 2000.0
    )
    controller = Qt3DExtras.QOrbitCameraController(root)
    controller.setCamera(camera)
    controller.setLinearSpeed(CAMERA_LINEAR_SPEED)
    controller.setLookSpeed(CAMERA_LOOK_SPEED)
    view.setRootEntity(root)
    container = QWidget.createWindowContainer(view)
    # Landscape minimum: the opening pose frames the whole sheet at
    # CAMERA_MIN_ASPECT or wider, so the viewport must not go narrower.
    container.setMinimumSize(640, int(640 / CAMERA_MIN_ASPECT))
    container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return Viewport(widget=container, camera=camera, controller=controller, view=view)


class Sim3DWindow(QWidget):
    """Playback of one emitted program in 3D: readouts, cut list, transport."""

    def __init__(
        self,
        timeline: SimTimeline,
        parent: QWidget | None = None,
        create_viewport: Callable[[Qt3DCore.QEntity], Viewport | None] | None = None,
        tick_ms: int = TICK_MS,
        findings: FindingSet | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(
            f"Cut simulation - {timeline.program.header.name} "
            f"({timeline.cut_total} cuts)"
        )
        self.timeline = timeline
        self.controller = SimController(timeline)
        #: The verifier's verdict on this program, as the caller handed it in;
        #: ``None`` means nobody has judged it here and nothing is claimed.
        self.findings = findings
        self.scene = SimScene(
            timeline.program,
            timeline.config,
            danger=DangerModel.build(timeline, findings),
        )

        #: Fraction of the move in progress that has been animated, 0..1.
        self._fraction = 0.0
        self._speed = SPEED_CHOICES[DEFAULT_SPEED_INDEX]
        #: True while a control is being written FROM the controller, so its
        #: signal does not read back as an operator gesture.
        self._syncing = False

        hook = create_qt3d_viewport if create_viewport is None else create_viewport
        self.viewport = hook(self.scene.root)

        self.timer = QTimer(self)
        self.timer.setInterval(max(1, int(tick_ms)))
        self.timer.timeout.connect(self._on_tick)

        self._build_ui()
        self.refresh()
        self.reset_view()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.addWidget(self._build_banner())
        outer.addWidget(self._build_readouts())

        split = QSplitter(Qt.Orientation.Horizontal, self)
        widget = None if self.viewport is None else self.viewport.widget
        if widget is None:
            widget = QLabel(NO_VIEWPORT_TEXT, self)
            widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            widget.setMinimumSize(320, 240)
        self.viewport_widget = widget
        split.addWidget(widget)

        lists = QSplitter(Qt.Orientation.Vertical, self)
        self.cut_list = QListWidget(self)
        self.cut_list.setObjectName("cutList")
        self.cut_list.setAlternatingRowColors(True)
        for row in cut_rows(self.timeline):
            self.cut_list.addItem(f"{row.index + 1}. {row.label}")
        self.cut_list.itemClicked.connect(self._on_row_clicked)
        self.cut_list.currentRowChanged.connect(self._on_row_changed)
        lists.addWidget(self.cut_list)
        lists.addWidget(self._build_findings())
        # The cut list is what the operator reads while watching; the findings
        # panel is short and is read once, so it gets the smaller share.
        lists.setStretchFactor(0, 2)
        lists.setStretchFactor(1, 1)
        split.addWidget(lists)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        outer.addWidget(split, 1)

        outer.addWidget(self._build_transport())
        # Visibility LAST: re-parenting a widget into a layout clears an
        # explicit hide, so a panel hidden while it was still parentless would
        # come back the moment the window is shown.
        self._show_findings_widgets()

    def _show_findings_widgets(self) -> None:
        """Banner and panel are present only when there is something to say."""
        self.banner.setVisible(bool(self.banner.text()))
        self.findings_list.setVisible(self.findings_list.count() > 0)

    def _build_banner(self) -> QWidget:
        """The verdict, across the top, or nothing at all.

        Hidden — not merely empty — when there is nothing wrong: a bar that is
        sometimes blank teaches the eye to skip it.
        """
        self.banner = QLabel(banner_text(self.findings), self)
        self.banner.setObjectName("findingsBanner")
        self.banner.setWordWrap(True)
        font = QFont(self.banner.font())
        font.setBold(True)
        self.banner.setFont(font)
        self.banner.setStyleSheet(
            f"background-color: {ERROR_FILL.name()}; color: white; padding: 4px;"
        )
        return self.banner

    def _build_findings(self) -> QWidget:
        """The findings panel: one row per finding, the verifier's own words.

        Clicking a row seeks to the move the finding names and stops there, so
        the operator is looking at the offending move with the bit on it.  A
        whole-file finding (no line, or a line that commands no move) seeks
        nowhere: there is no move to look at, and jumping somewhere arbitrary
        would suggest there is.
        """
        self.findings_list = QListWidget(self)
        self.findings_list.setObjectName("findingsList")
        self.findings_list.setAlternatingRowColors(True)
        self.findings_list.setToolTip(
            "What the independent verifier found in this program, in its own "
            "words. Click a row to go to the move it names."
        )
        for text in finding_rows(self.findings):
            self.findings_list.addItem(text)
        self.findings_list.itemClicked.connect(self._on_finding_clicked)
        return self.findings_list

    def _build_readouts(self) -> QWidget:
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        grid = QGridLayout(frame)

        self.tool_field = QLabel("-", frame)
        self.tool_field.setObjectName("toolField")
        font = QFont(self.tool_field.font())
        font.setBold(True)
        # A font is sized in points OR in pixels depending on the platform's
        # default; scaling the wrong one silently shrinks the field to nothing.
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * TOOL_FIELD_SCALE)
        elif font.pixelSize() > 0:
            font.setPixelSize(int(font.pixelSize() * TOOL_FIELD_SCALE))
        self.tool_field.setFont(font)
        self.tool_field.setToolTip(
            "The tool in the spindle right now, as its own section header "
            "names it"
        )
        grid.addWidget(self.tool_field, 0, 0, 2, 1)

        self.section_label = QLabel("-", frame)
        self.counter_label = QLabel("-", frame)
        self.feed_label = QLabel("-", frame)
        self.z_label = QLabel("-", frame)
        self.cut_label = QLabel("-", frame)
        self.cut_label.setWordWrap(True)

        grid.addWidget(self.section_label, 0, 1)
        grid.addWidget(self.counter_label, 0, 2)
        grid.addWidget(self.feed_label, 1, 1)
        grid.addWidget(self.z_label, 1, 2)
        grid.addWidget(self.cut_label, 0, 3, 2, 1)
        grid.setColumnStretch(3, 1)
        return frame

    def _build_transport(self) -> QWidget:
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(frame)

        def button(text: str, slot, tip: str) -> QPushButton:
            widget = QPushButton(text, frame)
            widget.setToolTip(tip)
            widget.clicked.connect(slot)
            row.addWidget(widget)
            return widget

        self.play_button = button(PLAY_TEXT, self.toggle_play, "Run the program")
        self.reset_button = button("|<", self.reset, "Back to the first move")
        self.prev_section_button = button(
            "<<", self.prev_section, "Back to the start of this tool's section"
        )
        self.prev_cut_button = button("<", self.prev_cut, "Back to the start of this cut")
        self.next_cut_button = button(">", self.next_cut, "Finish this cut")
        self.next_section_button = button(
            ">>", self.next_section, "Finish this tool's section"
        )
        self.end_button = button(">|", self.to_end, "Jump to the end of the program")
        self.reset_view_button = button(
            "Reset view", self.reset_view, "Frame the whole sheet again"
        )

        def toggle(text: str, kinds, tip: str) -> QCheckBox:
            """One envelope family, off until asked for (module docstring)."""
            box = QCheckBox(text, frame)
            box.setChecked(False)
            box.setToolTip(tip)
            box.toggled.connect(
                lambda on, group=kinds: self.show_overlays(group, on)
            )
            row.addWidget(box)
            return box

        self.cone_toggle = toggle(
            CONE_TOGGLE_TEXT,
            CONE_KINDS,
            "Where a WDC stile slot's deep pass really removes material: the "
            "45-degree cone ends twice its own reach past each stile end",
        )
        self.envelope_toggle = toggle(
            ENVELOPE_TOGGLE_TEXT,
            ENVELOPE_KINDS,
            "Everything each profile loop's motion touches, ramps and "
            "overshoot included, against the sheet plus its trim overhang",
        )

        self.scrub = QSlider(Qt.Orientation.Horizontal, frame)
        self.scrub.setObjectName("scrub")
        self.scrub.setMinimum(0)
        self.scrub.setMaximum(self.timeline.step_total)
        self.scrub.setToolTip("One tick per commanded move")
        self.scrub.valueChanged.connect(self._on_scrub)
        row.addWidget(self.scrub, 1)

        self.speed = QSlider(Qt.Orientation.Horizontal, frame)
        self.speed.setObjectName("speed")
        self.speed.setMinimum(0)
        self.speed.setMaximum(len(SPEED_CHOICES) - 1)
        self.speed.setValue(DEFAULT_SPEED_INDEX)
        self.speed.setToolTip("Playback speed against real machine time")
        self.speed.valueChanged.connect(self._on_speed)
        row.addWidget(self.speed)

        self.speed_label = QLabel(speed_text(self._speed), frame)
        self.speed_label.setObjectName("speedLabel")
        row.addWidget(self.speed_label)
        return frame

    # -- playback ----------------------------------------------------------

    @property
    def multiplier(self) -> float:
        """The playback speed the animation maths is running at."""
        return self._speed

    @property
    def fraction(self) -> float:
        """How far into the move in progress the animation has got."""
        return self._fraction

    @property
    def playing(self) -> bool:
        return self.timer.isActive()

    def play(self) -> None:
        if self.controller.at_end:
            return
        self.timer.start()
        self.refresh()

    def pause(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.refresh()

    def toggle_play(self) -> None:
        if self.timer.isActive():
            self.pause()
        else:
            self.play()

    def current_step_duration(self) -> float:
        """Seconds the move in progress takes at the current multiplier.

        Zero at the end of the program, where there is no move in progress.
        """
        motion = self.controller.current_motion
        if motion is None:
            return 0.0
        return motion_duration(
            motion,
            self.timeline.path_lengths[self.controller.step_index],
            self._speed,
        )

    def advance(self, seconds: float) -> None:
        """Animate ``seconds`` of playback, stepping the cursor as moves finish.

        The whole animation, and the only thing the timer calls: a tick is
        worth its own interval in seconds and nothing else, so driving this
        with the timer stopped is the same simulation.  A zero-length move (a
        retract that moves nothing, the ``M59`` marker's) has no duration and
        is stepped straight over rather than stalling playback on it.
        """
        controller = self.controller
        remaining = float(seconds)
        while remaining > 0.0 and not controller.at_end:
            duration = self.current_step_duration()
            if duration <= 0.0:
                self._fraction = 0.0
                controller.step_forward()
                continue
            left = (1.0 - self._fraction) * duration
            if remaining < left:
                self._fraction += remaining / duration
                break
            remaining -= left
            self._fraction = 0.0
            controller.step_forward()
        if controller.at_end:
            self._fraction = 0.0
            if self.timer.isActive():
                self.timer.stop()
        self.refresh()

    def _on_tick(self) -> None:
        self.advance(self.timer.interval() / 1000.0)

    # -- transport ---------------------------------------------------------

    def _gesture(self, name: str) -> None:
        """One transport gesture: playback stops, the cursor moves, all redraws.

        Stepping or jumping while the program is running pauses it — the
        operator has taken the wheel — and any gesture lands the animation on
        a move boundary rather than part way through the move it left.
        """
        if self.timer.isActive():
            self.timer.stop()
        self._fraction = 0.0
        getattr(self.controller, name)()
        self.refresh()

    def step_forward(self) -> None:
        self._gesture("step_forward")

    def step_back(self) -> None:
        self._gesture("step_back")

    def next_cut(self) -> None:
        self._gesture("next_cut")

    def prev_cut(self) -> None:
        self._gesture("prev_cut")

    def next_section(self) -> None:
        self._gesture("next_section")

    def prev_section(self) -> None:
        self._gesture("prev_section")

    def reset(self) -> None:
        self._gesture("reset")

    def to_end(self) -> None:
        self._gesture("to_end")

    def select_cut(self, cut_index: int) -> None:
        """Seek to a cut and stop there: what clicking a cut-list row does."""
        if self.timer.isActive():
            self.timer.stop()
        self._fraction = 0.0
        self.controller.seek_cut(cut_index)
        self.refresh()

    def select_finding(self, index: int) -> None:
        """Go to finding ``index``: what clicking a findings row does.

        Playback stops either way — the operator has taken the wheel — and the
        cursor lands BEFORE the offending move, so it is the move about to run
        and the bit is sitting on it in red.  A finding with no move (see
        :mod:`faceframe_cnc.sim.findings`) moves the cursor nowhere at all.
        """
        if self.findings is None:
            return
        items = self.findings.all
        if not 0 <= index < len(items):
            return
        if self.timer.isActive():
            self.timer.stop()
        self._fraction = 0.0
        step = items[index].step_index
        if step is not None:
            self.controller.seek(step)
        self.refresh()

    def show_overlays(self, kinds, visible: bool) -> None:
        """Switch one envelope family on or off in the scene."""
        for kind in kinds:
            self.scene.set_overlay_visible(kind, bool(visible))

    def reset_view(self) -> None:
        """Put the camera back where it started (owner request: a way back)."""
        if self.viewport is None or self.viewport.camera is None:
            return
        eye, centre, up = camera_pose(self.timeline.config)
        camera = self.viewport.camera
        camera.setUpVector(QVector3D(*up))
        camera.setViewCenter(QVector3D(*centre))
        camera.setPosition(QVector3D(*eye))

    # -- signals from the controls -----------------------------------------

    def _on_scrub(self, value: int) -> None:
        if self._syncing:
            return
        if self.timer.isActive():
            self.timer.stop()
        self._fraction = 0.0
        self.controller.seek(int(value))
        self.refresh()

    def _on_speed(self, index: int) -> None:
        self._speed = SPEED_CHOICES[max(0, min(int(index), len(SPEED_CHOICES) - 1))]
        self.speed_label.setText(speed_text(self._speed))

    def _on_row_clicked(self, item) -> None:
        self.select_cut(self.cut_list.row(item))

    def _on_row_changed(self, row: int) -> None:
        if self._syncing or row < 0:
            return
        self.select_cut(row)

    def _on_finding_clicked(self, item) -> None:
        self.select_finding(self.findings_list.row(item))

    # -- redraw ------------------------------------------------------------

    def refresh(self) -> None:
        """Push the cursor into every widget and into the scene."""
        readouts = Readouts.from_controller(self.controller, self._speed)
        self._syncing = True
        try:
            self.tool_field.setText(readouts.tool)
            self.section_label.setText(f"Section: {readouts.section}")
            self.counter_label.setText(readouts.counter)
            self.feed_label.setText(f"Feed: {readouts.feed}")
            self.z_label.setText(readouts.z)
            self.cut_label.setText(readouts.cut_label)
            self.speed_label.setText(readouts.speed)
            self.scrub.setValue(self.controller.step_index)
            row = current_row(self.controller)
            if row >= 0 and self.cut_list.currentRow() != row:
                self.cut_list.setCurrentRow(row)
                item = self.cut_list.item(row)
                if item is not None:
                    self.cut_list.scrollToItem(item)
        finally:
            self._syncing = False
        self.play_button.setText(PAUSE_TEXT if self.timer.isActive() else PLAY_TEXT)
        self.scene.update_from(self.controller, self._fraction)

    # -- lifecycle ---------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        self.timer.stop()
        super().closeEvent(event)
