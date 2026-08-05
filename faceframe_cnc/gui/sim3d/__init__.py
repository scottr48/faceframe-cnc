"""The 3D cut simulation view (Milestone 3 of the simulation).

Milestone 1 gave the post a typed motion stream, Milestone 2 a headless
playback cursor (:mod:`faceframe_cnc.sim`); this package is the window an
operator watches.  It keeps the project's split inside itself:

``viewmodel``
    every decision the view makes — the current-tool field's text, the readout
    strings, the reveal model (what material is GONE at a cursor), the
    animation maths and the cut-list rows.  No Qt, no clock, and a test that
    walks this file's syntax tree to prove it.
``scene``
    the Qt3D entity tree: slabs, one group per part, one entity per revealed
    feature, and the spindle with a bit sized from the ToolSpec in hand.
    Buildable and updatable with no render surface, which is how it is tested.
``window``
    the widgets: the readout strip (with the big current-tool field), the cut
    list, the findings panel and its banner, the transport bar with its scrub
    and speed sliders and the two envelope toggles, and the one QTimer in the
    feature.  The 3D viewport is injected through a hook so the wiring can be
    tested with no GL context.
``refusal``
    the same sheet with no playback, for a sheet the planner would not turn
    into a program at all: the refusal's own words and the part it names.

Milestone 4 added the findings half of all three: the verifier judges
(:mod:`faceframe_cnc.post.verifier`), :mod:`faceframe_cnc.sim.findings` says
where each finding lands, and these three modules draw exactly that and
nothing else in red.

Importing this package does NOT import Qt: ``viewmodel`` is re-exported
eagerly, and :class:`~.scene.SimScene` / :class:`~.window.Sim3DWindow` are
resolved on first attribute access, so the pure half stays usable (and
testable) on a machine with no PySide6 — the same promise
:mod:`faceframe_cnc.gui` makes.

Eyeball check: ``python -m faceframe_cnc.gui.sim3d --demo wdc``.
"""

from . import viewmodel
from .viewmodel import (
    DangerModel,
    Overlay,
    OverlayKind,
    Readouts,
    Reveal,
    RevealKind,
    cut_rows,
    overlays,
    reveals,
    tool_display,
)

__all__ = [
    "viewmodel",
    "DangerModel",
    "Overlay",
    "OverlayKind",
    "Readouts",
    "Reveal",
    "RevealKind",
    "cut_rows",
    "overlays",
    "reveals",
    "tool_display",
    "SimScene",
    "Sim3DWindow",
    "RefusalView",
    "Viewport",
    "create_qt3d_viewport",
]

_LAZY = {
    "SimScene": ("scene", "SimScene"),
    "Sim3DWindow": ("window", "Sim3DWindow"),
    "RefusalView": ("refusal", "RefusalView"),
    "Viewport": ("window", "Viewport"),
    "create_qt3d_viewport": ("window", "create_qt3d_viewport"),
}


def __getattr__(name: str):
    """Resolve the Qt-bearing names on demand (see the module docstring)."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    from importlib import import_module

    return getattr(import_module(f".{module_name}", __name__), attribute)
