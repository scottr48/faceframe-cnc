"""Desktop GUI for the faceframe optimizer (spec section 5) — Milestone 4.

The package is split in two, deliberately:

*   :mod:`faceframe_cnc.gui.session` — the whole application MODEL: order
    rows, include/exclude, needs-attention resolution, running the
    optimizer, and every manual layout edit.  It imports no Qt and is unit
    tested headlessly (``tests/test_gui_session.py``).
*   :mod:`faceframe_cnc.gui.main_window` and friends — Qt widgets that
    render the model and forward gestures back to it.  They hold no
    business rules; a widget that needs to know whether a drop is legal
    asks the session.

Launch with ``python -m faceframe_cnc.gui``.  Importing this package does
NOT import Qt, so the session model stays usable (and testable) on a
machine with no PySide6 installed.
"""

__all__ = ["session"]
