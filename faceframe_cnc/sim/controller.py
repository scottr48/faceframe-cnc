"""A deterministic cursor over a :class:`~.timeline.SimTimeline`.

Milestone 3's Qt3D view will consume this the way
:mod:`faceframe_cnc.gui.sheet_canvas` consumes
:mod:`faceframe_cnc.gui.session`: every decision about what has been cut,
what is being cut and what comes next is made here, headless, and the widget
only draws the answers.  No Qt import, no clock, no random anything — a
simulation that read the wall clock could not be stepped, diffed or tested.

The cursor sits BETWEEN steps
-----------------------------
:attr:`SimController.step_index` is a boundary, not a step: position ``k``
means "``k`` steps have fully executed", so it runs 0..``step_total``
inclusive.  That is what makes stepping reversible — ``step_back`` after
``step_forward`` is the same position and the same material state, not an
approximation of it — and it is why an occurrence's material effect lands at
its :attr:`~.timeline.CutOccurrence.end` boundary: a cut is done when its
last move is done, and the cursor is the only thing that says so.

Every mover clamps and returns whether the cursor actually moved.  Stepping
off either end of the program is a normal thing for a held-down key to do,
not an error.

Cut and section stepping are mutual inverses
--------------------------------------------
``next_cut`` seeks the END boundary of the occurrence that owns the next
step; ``prev_cut`` seeks the START boundary of the occurrence that owns the
last executed step.  Because occurrences tile the stream — occurrence i
starts where i-1 ends — those two land on each other exactly, in both
directions, from anywhere.  ``next_section``/``prev_section`` are the same
rule over :attr:`~.timeline.SimTimeline.sections`.
"""

from __future__ import annotations

from .state import MaterialState
from .timeline import CutOccurrence, SectionSpan, SimTimeline

__all__ = ["SimController"]


class SimController:
    """Playback cursor over one emitted program.

    Deterministic: two controllers over the same timeline, driven by the same
    calls, hold the same cursor, the same readouts and the same material
    state.  The only mutable thing in the object is the cursor and a cache of
    the material fold; the cache is proved against
    :meth:`~.timeline.SimTimeline.state_at`, which recomputes from uncut
    stock.
    """

    def __init__(self, timeline: SimTimeline):
        self._timeline = timeline
        self._position = 0
        self._state = MaterialState.empty(timeline.part_count)
        #: How many occurrences ``_state`` has folded in, i.e. which cursor
        #: position it describes.  Kept so a forward walk applies one cut once
        #: instead of refolding the whole program on every readout.
        self._state_cuts = 0

    @property
    def timeline(self) -> SimTimeline:
        return self._timeline

    # -- movement ----------------------------------------------------------

    def seek(self, position: int) -> bool:
        """Put the cursor at ``position``, clamped to the program.

        Returns whether it moved.
        """
        target = max(0, min(int(position), self._timeline.step_total))
        if target == self._position:
            return False
        self._position = target
        return True

    def step_forward(self) -> bool:
        """Execute the next move."""
        return self.seek(self._position + 1)

    def step_back(self) -> bool:
        """Un-execute the last move."""
        return self.seek(self._position - 1)

    def reset(self) -> bool:
        return self.seek(0)

    def to_end(self) -> bool:
        return self.seek(self._timeline.step_total)

    def next_cut(self) -> bool:
        """Finish the cut in progress (or the one about to start)."""
        cut = self.current_cut
        if cut is None:
            return False
        return self.seek(cut.end)

    def prev_cut(self) -> bool:
        """Rewind to the start of the cut whose last move has just executed."""
        if self._position <= 0:
            return False
        timeline = self._timeline
        cut = timeline.cut_at_step(self._position - 1)
        return self.seek(cut.start)

    def seek_cut(self, cut_index: int) -> bool:
        """Put the cursor at occurrence ``cut_index``'s first move.

        A cut index outside the program clamps to the first or last
        occurrence, for the same reason the step movers clamp.
        """
        timeline = self._timeline
        if not timeline.cuts:
            return self.seek(0)
        index = max(0, min(int(cut_index), timeline.cut_total - 1))
        return self.seek(timeline.cuts[index].start)

    def next_section(self) -> bool:
        """Finish the section in progress (or the one about to start)."""
        span = self.current_section
        if span is None:
            return False
        return self.seek(span.end)

    def prev_section(self) -> bool:
        """Rewind to the start of the section whose last move has executed."""
        if self._position <= 0:
            return False
        span = self._timeline.section_at_step(self._position - 1)
        return self.seek(span.start)

    def seek_section(self, section_index: int) -> bool:
        """Put the cursor at section ``section_index``'s first move."""
        timeline = self._timeline
        if not timeline.sections:
            return self.seek(0)
        index = max(0, min(int(section_index), len(timeline.sections) - 1))
        return self.seek(timeline.sections[index].start)

    # -- readouts ----------------------------------------------------------

    @property
    def step_index(self) -> int:
        """Steps fully executed: the cursor itself, 0..:attr:`step_total`."""
        return self._position

    @property
    def step_total(self) -> int:
        return self._timeline.step_total

    @property
    def progress(self) -> float:
        """Fraction of the program's moves executed, 0.0..1.0."""
        total = self._timeline.step_total
        if total == 0:
            return 1.0
        return self._position / total

    @property
    def at_start(self) -> bool:
        return self._position == 0

    @property
    def at_end(self) -> bool:
        return self._position == self._timeline.step_total

    @property
    def current_motion(self):
        """The move that has NOT run yet; ``None`` at the end of the program."""
        if self._position >= self._timeline.step_total:
            return None
        return self._timeline.steps[self._position]

    @property
    def last_motion(self):
        """The move that just ran; ``None`` at the start of the program."""
        if self._position == 0:
            return None
        return self._timeline.steps[self._position - 1]

    @property
    def position(self) -> tuple[float, float, float | None]:
        """Tool centre XYZ: the endpoint of the last executed move.

        Before anything has run the tool is where the emitter's modal state
        starts it — X0 Y0 with work Z UNKNOWN, because a section's Z is only
        established by its ``G43`` (:class:`~faceframe_cnc.post.motion.Motion`),
        so Z is ``None`` there and nowhere else after the first ``G43``.
        """
        motion = self.last_motion
        if motion is None:
            return (0.0, 0.0, None)
        return (motion.to_x, motion.to_y, motion.to_z)

    @property
    def tool(self):
        """The tool in the spindle: the one the next move uses, or at the end
        of the program the one the last move used."""
        motion = self.current_motion or self.last_motion
        return None if motion is None else motion.tool

    @property
    def feed(self) -> float | None:
        """The feed the next move commands; ``None`` for a rapid, which has
        none, and ``None`` at the end of the program."""
        motion = self.current_motion
        return None if motion is None else motion.feed

    @property
    def section(self) -> str | None:
        """The section being cut, or the last one at the end of the program."""
        motion = self.current_motion or self.last_motion
        return None if motion is None else motion.section

    @property
    def current_cut(self) -> CutOccurrence | None:
        """The occurrence in progress or about to start; ``None`` at the end."""
        if self._position >= self._timeline.step_total:
            return None
        return self._timeline.cut_at_step(self._position)

    @property
    def current_section(self) -> SectionSpan | None:
        if self._position >= self._timeline.step_total:
            return None
        return self._timeline.section_at_step(self._position)

    @property
    def cut_index(self) -> int:
        """Which cut is in progress, 0-based.

        :attr:`cut_total` once the program is finished — one past the end, the
        same convention :attr:`step_index` uses, so "cut i of N" is
        ``cut_index + 1`` while there is a cut to name.
        """
        cut = self.current_cut
        return self._timeline.cut_total if cut is None else cut.index

    @property
    def cut_total(self) -> int:
        return self._timeline.cut_total

    @property
    def completed_cuts(self) -> int:
        """Occurrences whose last move has executed."""
        return self._timeline.completed_cuts(self._position)

    # -- material ----------------------------------------------------------

    @property
    def state(self) -> MaterialState:
        """Material state at the cursor.

        Maintained incrementally forward (a step that completes a cut applies
        that one cut) and refolded from uncut stock on any backward move, so
        the answer never depends on the route taken to get here — which is
        what :meth:`~.timeline.SimTimeline.state_at` is the definition of.
        """
        timeline = self._timeline
        target = timeline.completed_cuts(self._position)
        if target < self._state_cuts:
            self._state = MaterialState.empty(timeline.part_count)
            self._state_cuts = 0
        while self._state_cuts < target:
            self._state = self._state.apply(
                timeline.cuts[self._state_cuts], timeline.last_perimeter_pass
            )
            self._state_cuts += 1
        return self._state

    def state_at(self, position: int) -> MaterialState:
        """Material state at an arbitrary cursor position, recomputed."""
        return self._timeline.state_at(position)
