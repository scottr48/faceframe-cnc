"""Entry point: ``python -m faceframe_cnc.gui``.

Fully offline -- nothing here (or anywhere else in the app) opens a socket.

Headless verification::

    QT_QPA_PLATFORM=offscreen python -m faceframe_cnc.gui --self-test 2

``--self-test SECONDS`` (or ``FACEFRAME_GUI_SELFTEST=SECONDS``) builds the
window, paints one frame, and closes itself, so a shop PC -- or CI -- can
prove the app starts without anyone sitting in front of it.

``--self-test-sim`` does the same job for the 3D cut simulation::

    QT_QPA_PLATFORM=offscreen python -m faceframe_cnc.gui --self-test-sim

It optimizes a small built-in order (or the order named on the command line),
opens the simulation for sheet 1 through the real button path, steps and plays
a few cuts, prints one summary line and exits.  No GL is needed: the viewport
hook is injected as "no viewport", which is exactly what
:class:`~faceframe_cnc.gui.sim3d.window.Sim3DWindow` is built to accept, and
no wall clock is needed either -- playback is advanced by handing the window a
number of seconds, the same thing its timer does.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m faceframe_cnc.gui",
        description="Faceframe nesting optimizer (Eagle Woodworking).",
    )
    parser.add_argument("order", nargs="?", help="order .xls to load at startup")
    parser.add_argument(
        "--settings", help="path to the JSON settings file (default: next to the app)"
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="run the optimizer immediately after loading the order",
    )
    parser.add_argument(
        "--self-test",
        nargs="?",
        type=float,
        const=2.0,
        default=None,
        metavar="SECONDS",
        help="build the window, paint one frame, then close (headless check)",
    )
    parser.add_argument(
        "--self-test-sim",
        action="store_true",
        help=(
            "optimize a small built-in order, open the 3D cut simulation for "
            "sheet 1, play a few cuts and exit (headless check, needs no GL)"
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


#: The built-in order ``--self-test-sim`` uses when it was not given one:
#: ``(part number, width, height, qty)``.  Two ordinary wall frames, small
#: enough to nest and emit in milliseconds and chosen so the sheet is CLEAN --
#: the check is that a good sheet plays, and a refusal would be a failure of
#: it.  Not a WDC: that sheet's refusals are what ``tests/test_sim3d.py`` and
#: the demo launcher are for.
SELF_TEST_SIM_ORDER = (
    ("W3036", 30.0, 36.0, 1),
    ("W3012", 30.0, 12.0, 1),
)

#: How many cuts the unattended run steps through, and how many seconds of
#: playback it then animates.  Both small: this proves the transport and the
#: animation maths are wired to a real program, which the first few cuts of
#: any sheet show as well as all of them.
SELF_TEST_SIM_CUTS = 3
SELF_TEST_SIM_SECONDS = 2.0


def _self_test_seconds(args: argparse.Namespace) -> Optional[float]:
    if args.self_test is not None:
        return args.self_test
    env = os.environ.get("FACEFRAME_GUI_SELFTEST")
    if not env:
        return None
    try:
        return float(env)
    except ValueError:
        return 2.0


def _self_test_sim(app, window) -> int:
    """Drive the 3D simulation for sheet 1 with nobody watching.  0 = good.

    Everything goes through the real wiring -- the session builds the inputs,
    the window's own handler opens the simulation, the transport moves the
    cursor -- with two injections, both of which exist for exactly this:

    *   ``sim_viewport_hook`` returns ``None``, the documented "no viewport"
        case, so no render surface is asked for;
    *   ``unattended``, so a refusal is printed instead of waiting behind a
        modal box for a click nobody is there to make.

    Nothing sleeps and the event loop is never entered: playback is advanced
    by handing the window seconds directly, which is all its timer does.
    """
    from .session import OrderRow, SessionError
    from .sim3d.window import Sim3DWindow

    session = window.session
    window.unattended = True
    window.sim_viewport_hook = lambda root: None

    if not session.included_rows():
        session.set_rows(
            [
                OrderRow(
                    key=f"selftest-{part}",
                    part_number=part,
                    qty=qty,
                    frame_width=width,
                    frame_height=height,
                )
                for part, width, height, qty in SELF_TEST_SIM_ORDER
            ]
        )
        window.order_panel.reload()

    try:
        session.optimize()
    except SessionError as exc:
        print(f"self-test-sim: the optimizer refused the order ({exc})", file=sys.stderr)
        return 1
    window.canvas.show_sheet(0)
    window.refresh()
    if not window.simulate_button.isEnabled():
        print(
            "self-test-sim: 'Simulate cut' is still disabled after optimizing",
            file=sys.stderr,
        )
        return 1

    window.simulate_cut()
    sim = window.sim_window
    if not isinstance(sim, Sim3DWindow):
        # A RefusalView, or nothing at all: either way this sheet did not play.
        print(
            f"self-test-sim: sheet 1 did not play - "
            f"{window.last_warning or 'the post refused it'}",
            file=sys.stderr,
        )
        return 1

    app.processEvents()
    for _ in range(SELF_TEST_SIM_CUTS):
        sim.next_cut()
    stepped = sim.controller.step_index
    sim.play()
    sim.advance(SELF_TEST_SIM_SECONDS)
    sim.pause()
    app.processEvents()

    controller = sim.controller
    print(
        f"self-test-sim: {window.session.unique_sheet_count} unique sheet(s), "
        f"sheet 1 = {sim.timeline.program.header.name}: {controller.step_total} "
        f"moves, {sim.timeline.cut_total} cuts, "
        f"{0 if sim.findings is None else sim.findings.count} verifier finding(s); "
        f"stepped {SELF_TEST_SIM_CUTS} cuts to move {stepped}, then played "
        f"{SELF_TEST_SIM_SECONDS:g}s to move {controller.step_index} "
        f"({controller.completed_cuts} cuts complete, "
        f"{len(controller.state.freed_parts)} parts freed); tool field reads "
        f"{sim.tool_field.text()!r}"
    )
    window.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - depends on the install
        print(
            f"PySide6 is required to run the GUI ({exc}).\n"
            "Install it into this Python, then run "
            "'python -m faceframe_cnc.gui' again.",
            file=sys.stderr,
        )
        return 2

    from .main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Faceframe Nesting Optimizer")
    app.setOrganizationName("Eagle Woodworking")

    window = MainWindow(settings_path=args.settings)
    seconds = _self_test_seconds(args)
    unattended = seconds is not None or bool(args.self_test_sim)
    # A self test loads only what it was explicitly given, so its result
    # does not depend on whatever the last user happened to open.
    order = args.order or (
        None if unattended else window.session.settings.last_order_path
    )
    if order and os.path.exists(order):
        window.load_order(order)
        if args.optimize and window.session.included_rows():
            window.optimize()
    window.show()

    if args.self_test_sim:
        # Returns without entering the event loop: the whole run is driven by
        # hand, so "the check finished" and "the app exited" are one thing.
        return _self_test_sim(app, window)

    if seconds is not None:
        # Paint once so a rendering fault surfaces as a non-zero exit code
        # rather than an app that merely started and did nothing.
        app.processEvents()
        window.grab()
        window.schedule_self_close(int(max(0.0, seconds) * 1000))
        print(
            f"self-test: window built, {len(window.session.rows)} order rows, "
            f"{window.session.unique_sheet_count} unique sheets; "
            f"closing in {seconds:g}s"
        )

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
