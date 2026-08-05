"""Headless playback of an emitted NC program: the 3D cut simulation's model.

Milestone 1 made the post emit a typed motion stream beside its ``.anc`` text
(:mod:`faceframe_cnc.post.motion`).  This package turns that stream into
something an operator can step through — a timeline of moves grouped into
CUT OCCURRENCES, and a cursor over it that knows what has been cut, what is
being cut, and which parts are still held to the sheet.

No Qt, no OpenGL, no clock.  Everything a future 3D view needs to draw is
decided here and pinned by ``tests/test_sim.py``, including a test that walks
this package's own syntax tree to prove it imports neither a GUI toolkit nor
a source of wall-clock time or randomness.  The split is the one
:mod:`faceframe_cnc.gui.session` and :mod:`faceframe_cnc.gui.sheet_canvas`
already use: all logic testable without a display, the widget thin.

Modules
-------
``timeline``
    :class:`~.timeline.SimTimeline` — the emitted program indexed for
    playback: steps, cut occurrences, section spans, per-step travel
    distances, and the line-number hop a verifier finding takes to reach a
    step.
``state``
    :class:`~.state.MaterialState` — what has been cut, per part: grooves,
    openings, slots, and the skinned/freed pair that says whether a part is
    still attached to the sheet.
``controller``
    :class:`~.controller.SimController` — the cursor: step, cut, and section
    stepping in both directions, with readouts at the cursor.
``findings``
    :class:`~.findings.FindingSet` — where each of
    :func:`faceframe_cnc.post.verifier.verify`'s findings lands on the
    timeline.  The verifier judges; this only locates, in full and verbatim.
"""

from .controller import SimController
from .findings import Finding, FindingSet, run_verifier
from .state import MaterialState, PartState
from .timeline import (
    GROOVE_NAMES,
    SLOT_NAMES,
    CutOccurrence,
    SectionSpan,
    SimTimeline,
    cut_label,
    pass_phase,
)

__all__ = [
    "SimController",
    "SimTimeline",
    "CutOccurrence",
    "SectionSpan",
    "MaterialState",
    "PartState",
    "Finding",
    "FindingSet",
    "run_verifier",
    "cut_label",
    "pass_phase",
    "GROOVE_NAMES",
    "SLOT_NAMES",
]
