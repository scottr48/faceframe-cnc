"""Entry point: ``python -m faceframe_cnc.gui``.

Fully offline -- nothing here (or anywhere else in the app) opens a socket.

Headless verification::

    QT_QPA_PLATFORM=offscreen python -m faceframe_cnc.gui --self-test 2

``--self-test SECONDS`` (or ``FACEFRAME_GUI_SELFTEST=SECONDS``) builds the
window, paints one frame, and closes itself, so a shop PC -- or CI -- can
prove the app starts without anyone sitting in front of it.
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
    return parser.parse_args(list(argv) if argv is not None else None)


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
    # A self test loads only what it was explicitly given, so its result
    # does not depend on whatever the last user happened to open.
    order = args.order or (
        window.session.settings.last_order_path if seconds is None else None
    )
    if order and os.path.exists(order):
        window.load_order(order)
        if args.optimize and window.session.included_rows():
            window.optimize()
    window.show()

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
