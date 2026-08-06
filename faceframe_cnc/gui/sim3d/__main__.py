"""Manual demo launcher for the 3D cut simulation — the EYEBALL CHECK.

Nothing under ``tests/`` uses this file.  The automated tests prove the
wiring, the reveal geometry and the determinism of everything below the timer;
what they cannot tell anybody is whether the thing LOOKS like a sheet being
cut.  That is what this is for::

    python -m faceframe_cnc.gui.sim3d --demo wdc
    python -m faceframe_cnc.gui.sim3d --demo nested --play

``--self-close SECONDS`` builds the window, paints it, plays, and exits on its
own, so "does it open without a traceback" is answerable without anybody
sitting in front of it (the same flag ``python -m faceframe_cnc.gui`` has).

The two demo sheets are built through the planner
(:func:`~faceframe_cnc.post.from_layout.plan_sheet`) and the post table Generate
itself uses (:func:`~faceframe_cnc.post.from_layout.post_config_for` — one
perimeter pass since the 2026-08-05 amendment, not the measured table's two), so
what is animated is a sheet the shop could actually be handed: ``wdc`` puts a
WDC2436 (the frame with the T17 stile slots) beside an ordinary W2436, and
``nested`` puts a W3012 inside a W2742's opening (spec 4b), which is the sheet
where a host slab must not come loose before its passenger.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from ...nesting import NestingConfig, PartSpec, Placement, SheetLayout
from ...post import ProgramHeader, plan_sheet, post_config_for
from ...sim import SimTimeline

DEMO_CREATED = "01 JAN 27 - 08:00"


def wdc_sheet() -> SimTimeline:
    """A WDC2436 clear of the sheet edge, with an ordinary frame beside it."""
    layout = SheetLayout(
        [
            Placement("WDC2436", 4.0, 4.0, 18.0, 36.0),
            Placement("W2436", 4.0, 44.0, 24.0, 36.0),
        ]
    )
    demand = [PartSpec("WDC2436", 18.0, 36.0, 1), PartSpec("W2436", 24.0, 36.0, 1)]
    config = post_config_for(NestingConfig())
    program, plan = plan_sheet(
        layout,
        ProgramHeader(name="R990102N", created=DEMO_CREATED),
        demand,
        NestingConfig(),
        config,
    )
    return SimTimeline.build(program, plan, config)


def nested_sheet() -> SimTimeline:
    """A W3012 turned 90 degrees inside a W2742's opening, and one beside it."""
    layout = SheetLayout(
        [
            Placement(
                "W2742",
                0.0,
                0.0,
                27.0,
                42.0,
                False,
                [Placement("W3012", 5.0, 6.0, 12.0, 30.0, True, [])],
            ),
            Placement("W3012", 30.0, 0.0, 12.0, 30.0, True, []),
        ]
    )
    demand = [PartSpec("W2742", 27.0, 42.0, 1), PartSpec("W3012", 30.0, 12.0, 2)]
    config = post_config_for(NestingConfig())
    program, plan = plan_sheet(
        layout,
        ProgramHeader(name="R990103N", created=DEMO_CREATED),
        demand,
        NestingConfig(),
        config,
    )
    return SimTimeline.build(program, plan, config)


DEMOS = {"wdc": wdc_sheet, "nested": nested_sheet}


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m faceframe_cnc.gui.sim3d",
        description="Watch a demo sheet being cut in 3D (manual check).",
    )
    parser.add_argument(
        "--demo", choices=sorted(DEMOS), default="wdc", help="which demo sheet to cut"
    )
    parser.add_argument(
        "--play", action="store_true", help="start playing immediately"
    )
    parser.add_argument(
        "--self-close",
        nargs="?",
        type=float,
        const=5.0,
        default=None,
        metavar="SECONDS",
        help="play for SECONDS, then close (unattended check)",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - depends on the install
        print(f"PySide6 is required for the 3D simulation ({exc}).", file=sys.stderr)
        return 2

    from .window import Sim3DWindow

    timeline = DEMOS[args.demo]()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = Sim3DWindow(timeline)
    window.resize(1400, 900)
    window.show()

    if args.play or args.self_close is not None:
        window.play()

    print(
        f"demo {args.demo}: {timeline.step_total} moves, {timeline.cut_total} cuts, "
        f"{len(timeline.sections)} sections, {timeline.part_count} parts; "
        f"tool field reads {window.tool_field.text()!r} at {window.speed_label.text()}"
    )

    if args.self_close is not None:
        app.processEvents()

        def finish() -> None:
            """Say what actually happened, so the check is not just "exit 0"."""
            controller = window.controller
            print(
                f"self-test: played {controller.step_index} of "
                f"{controller.step_total} moves, {controller.completed_cuts} cuts "
                f"complete, {len(controller.state.freed_parts)} parts freed; "
                f"tool field reads {window.tool_field.text()!r}, "
                f"{window.counter_label.text()}"
            )
            window.close()

        delay = int(max(0.0, args.self_close) * 1000)
        QTimer.singleShot(delay, finish)
        QTimer.singleShot(delay + 200, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
