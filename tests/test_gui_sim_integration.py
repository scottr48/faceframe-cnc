"""Milestone 5 of the 3D cut simulation: the simulation wired into the app.

Three layers, each tested where it lives:

  (a) the SESSION helper (:meth:`faceframe_cnc.gui.session.Session.simulation_inputs`),
      headless and Qt-free: it must plan, emit and judge the sheet the user is
      looking at with exactly what the Generate path uses -- the same planner
      call, the same post table, and the same expected-work manifest
      :func:`faceframe_cnc.post.job.build_job` hands the verifier -- and it
      must refuse a stale, invalid or non-existent sheet with the same
      predicate Generate refuses on, never a parallel one of its own;
  (b) the WINDOW wiring, offscreen with the 3D viewport injected as ``None``:
      the button exists, its enabled state is recomputed in ``refresh()`` and
      so cannot go stale, a click opens the playback window, a second click
      replaces it, a refused sheet opens the refusal view instead, and a
      session refusal is a message with no window;
  (c) the ``--self-test-sim`` entry point, run as a subprocess on the
      offscreen platform: the whole path, unattended, exit 0.

Nothing here instantiates a Qt3D render surface (see ``tests/test_sim3d.py``
for why) and nothing here re-tests the post, the verifier or the sim package:
what is under test is only that the app hands them the right things and shows
what they say.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

# Must be set before the first QGuiApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from faceframe_cnc.gui.session import (
    SIM_CREATED,
    SIM_JOB_PREFIX,
    AppSettings,
    OrderRow,
    Session,
    SessionError,
    SimulationRefused,
)
from faceframe_cnc.nesting import (
    NestingConfig,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    validate_layouts,
)
from faceframe_cnc.post.from_layout import (
    WdcNotSupportedError,
    post_config_for,
)
from faceframe_cnc.post.job import JobOptions, build_job, sheet_filename
from faceframe_cnc.post.verifier import ExpectedWork, expected_work, verify
from faceframe_cnc.sim import FindingSet, SimTimeline

try:
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_QT = False

if HAVE_QT:
    from faceframe_cnc.gui.main_window import (
        SIMULATE_ERROR_TITLE,
        SIMULATE_TOOLTIP,
        MainWindow,
    )
    from faceframe_cnc.gui.sim3d.refusal import RefusalView
    from faceframe_cnc.gui.sim3d.window import Sim3DWindow

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

_APP = None


def setUpModule():  # noqa: N802 - unittest naming
    global _APP
    if HAVE_QT:
        _APP = QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------
# Fixtures.  Built here rather than imported from another test module, the
# rule the sim tests already follow: one file's failure must not be another's.
# --------------------------------------------------------------------------


def walk_placements(placements):
    """Every placement, hosts before their passengers.

    The order :meth:`faceframe_cnc.post.model.SheetProgram.flat_parts` walks in,
    reproduced here so the comparison below is against the layout and not
    against the program's own idea of it.
    """
    for placement in placements:
        yield placement
        yield from walk_placements(placement.children)


def plain_session(settings: AppSettings | None = None) -> Session:
    """Two ordinary wall frames, optimized: one clean sheet, no WDC."""
    session = Session(settings or AppSettings())
    session.set_rows(
        [
            OrderRow(
                key="a", part_number="W3036", qty=1, frame_width=30.0, frame_height=36.0
            ),
            OrderRow(
                key="b", part_number="W3012", qty=1, frame_width=30.0, frame_height=12.0
            ),
        ]
    )
    session.optimize()
    return session


def wdc_session() -> Session:
    """A hand-built sheet with a WDC2436 on it, checked by the validator.

    The placement is the demo's: the WDC clear of the sheet edge by more than
    its slot's reach, so the layout is legal and the post cuts it.  Tests that
    want a REFUSAL move the frame afterwards (see
    :meth:`SimulationRefusalTests.push_the_wdc_to_the_edge`).
    """
    session = Session(AppSettings())
    session.set_rows(
        [
            OrderRow(
                key="w",
                part_number="WDC2436",
                qty=1,
                frame_width=18.0,
                frame_height=36.0,
            ),
            OrderRow(
                key="p",
                part_number="W2436",
                qty=1,
                frame_width=24.0,
                frame_height=36.0,
            ),
        ]
    )
    layout = SheetLayout(
        [
            Placement("WDC2436", 4.0, 4.0, 18.0, 36.0),
            Placement("W2436", 4.0, 44.0, 24.0, 36.0),
        ]
    )
    specs = [PartSpec("WDC2436", 18.0, 36.0, 1), PartSpec("W2436", 24.0, 36.0, 1)]
    config = session.settings.to_config()
    result = NestingResult(
        unique_sheets=[(layout, 1)],
        total_sheets=1,
        demand=specs,
        config=config,
    )
    assert validate_layouts(result, config) == [], validate_layouts(result, config)
    session.set_result(result)
    return session


def overlapping_session() -> Session:
    """A layout its own validator condemns (two frames on top of each other)."""
    session = Session(AppSettings())
    session.set_rows(
        [
            OrderRow(
                key="a", part_number="W3030", qty=2, frame_width=30.0, frame_height=30.0
            )
        ]
    )
    layout = SheetLayout(
        [
            Placement("W3030", 1.0, 1.0, 30.0, 30.0),
            Placement("W3030", 1.0, 1.2, 30.0, 30.0),
        ]
    )
    config = session.settings.to_config()
    result = NestingResult(
        unique_sheets=[(layout, 1)],
        total_sheets=1,
        demand=[PartSpec("W3030", 30.0, 30.0, 2)],
        config=config,
    )
    assert validate_layouts(result, config), "this fixture must be invalid"
    session.set_result(result)
    return session


# --------------------------------------------------------------------------
# (a) The session helper
# --------------------------------------------------------------------------


class SimulationInputsTests(unittest.TestCase):
    def setUp(self):
        self.session = plain_session()

    def test_the_timeline_is_the_sheet_on_screen(self):
        layout, run = self.session.sheet(0)
        inputs = self.session.simulation_inputs(0)

        self.assertEqual(inputs.sheet_index, 0)
        self.assertIs(inputs.layout, layout)
        self.assertEqual(inputs.run_quantity, run)
        self.assertIs(inputs.timeline.program, inputs.program)
        self.assertIs(inputs.timeline.plan, inputs.plan)

        # Every placement on the sheet -- hosts before the frames nested in
        # their openings, which is the order flat_parts() walks -- is a part of
        # the program the timeline plays, footprint included, so a program built
        # for some other sheet could not pass this.
        placed = [
            (p.part_number, round(p.x, 6), round(p.y, 6), round(p.width, 6))
            for p in walk_placements(layout.placements)
        ]
        played = [
            (
                part.part_number,
                round(part.box.x0, 6),
                round(part.box.y0, 6),
                round(part.box.width, 6),
            )
            for part in inputs.program.flat_parts()
        ]
        self.assertEqual(played, placed)
        self.assertEqual(inputs.timeline.part_count, len(placed))

    def test_a_clean_optimized_sheet_has_no_findings(self):
        inputs = self.session.simulation_inputs(0)
        self.assertEqual(inputs.findings.count, 0)
        self.assertEqual(list(inputs.findings.all), [])
        self.assertTrue(inputs.clean)
        # And the sheet really is one Generate would write, judged the same way.
        job = build_job(
            self.session.result,
            JobOptions(output_dir="unused", prefix="0000", created="01 JAN 27 - 08:00"),
        )
        self.assertEqual([o.problems for o in job.outcomes], [[]])

    def test_the_post_table_is_the_generate_paths(self):
        inputs = self.session.simulation_inputs(0)
        self.assertEqual(inputs.post_config, post_config_for(self.session.result.config))
        self.assertIs(inputs.timeline.config, inputs.post_config)
        # The optimizer's sheet size reached the post table (the emitter
        # refuses a program whose sheet differs from its own).
        self.assertAlmostEqual(
            inputs.post_config.sheet_width, self.session.config.sheet_width
        )
        self.assertAlmostEqual(
            inputs.post_config.sheet_length, self.session.config.sheet_height
        )

    def test_the_expected_manifest_is_the_job_builders(self):
        """The sheet is judged against what it OWES, stated from the layout.

        Same function, same layout, same post table as
        :func:`faceframe_cnc.post.job.build_job` uses for this sheet -- the
        manifest is what lets the verifier see a cut that is missing, and
        without it the simulation could run a program clean that Generate then
        refuses.
        """
        layout, _run = self.session.sheet(0)
        inputs = self.session.simulation_inputs(0)
        self.assertEqual(inputs.expected, expected_work(layout, inputs.post_config))
        self.assertGreater(len(inputs.expected), 0)

    def test_the_manifest_actually_reaches_the_verifier(self):
        """Proved twice over: the object passed, and that it is load bearing."""
        import faceframe_cnc.sim.findings as findings_module

        seen: list = []
        original = findings_module.verify

        def spy(text, config=None, expected=None):
            seen.append((text, config, expected))
            return original(text, config, expected)

        findings_module.verify = spy
        try:
            inputs = self.session.simulation_inputs(0)
        finally:
            findings_module.verify = original

        self.assertEqual(len(seen), 1, "the verifier is called once, by the session")
        text, config, expected = seen[0]
        self.assertIs(expected, inputs.expected)
        self.assertIs(config, inputs.post_config)
        self.assertEqual(text, inputs.timeline.emitted.text)

        # ... and a manifest is not decoration: drop one owed cut from it and
        # the same program stops being clean.  So passing None (which is what
        # "the manifest is not in play" would mean) would be a weaker verdict
        # than the one the operator was shown.
        short = ExpectedWork(inputs.expected.cuts[1:])
        self.assertTrue(verify(inputs.timeline.emitted.text, inputs.post_config, short))

    def test_the_findings_are_the_verifiers_own_verdict_located(self):
        inputs = self.session.simulation_inputs(0)
        expected_set = FindingSet.build(
            inputs.timeline,
            verify(inputs.timeline.emitted.text, inputs.post_config, inputs.expected),
        )
        self.assertEqual(inputs.findings, expected_set)

    def test_the_header_is_a_fixed_simulation_identity(self):
        inputs = self.session.simulation_inputs(0)
        self.assertEqual(inputs.header.created, SIM_CREATED)
        self.assertEqual(inputs.header.o_number, 1)
        self.assertEqual(
            inputs.header.name, sheet_filename(SIM_JOB_PREFIX, 1)[: -len(".anc")]
        )
        self.assertEqual(inputs.program_name, inputs.header.name)
        # Fixed means the same bytes twice: a simulation that judged a
        # different program every minute could not be reasoned about.
        again = self.session.simulation_inputs(0)
        self.assertEqual(again.timeline.emitted.text, inputs.timeline.emitted.text)

    def test_a_saved_job_prefix_names_the_simulated_program(self):
        self.session.settings.job_prefix = "7201"
        inputs = self.session.simulation_inputs(0)
        self.assertEqual(inputs.header.name, "R720101N")

    def test_every_unique_sheet_can_be_simulated(self):
        session = Session(AppSettings())
        session.set_rows(
            [
                OrderRow(
                    key="a",
                    part_number="W3030",
                    qty=4,
                    frame_width=30.0,
                    frame_height=30.0,
                )
            ]
        )
        session.optimize()
        self.assertGreater(session.unique_sheet_count, 1)
        for index in range(session.unique_sheet_count):
            inputs = session.simulation_inputs(index)
            self.assertEqual(inputs.sheet_index, index)
            self.assertGreater(inputs.timeline.cut_total, 0)
            self.assertEqual(inputs.findings.count, 0)


class SimulationGateTests(unittest.TestCase):
    """The gate is Generate's gate.  Not a copy of it -- the same call."""

    def test_no_layout_refuses_and_returns_nothing(self):
        session = Session(AppSettings())
        session.set_rows(
            [
                OrderRow(
                    key="a",
                    part_number="W3036",
                    qty=1,
                    frame_width=30.0,
                    frame_height=36.0,
                )
            ]
        )
        self.assertFalse(session.can_simulate(0))
        with self.assertRaises(SessionError) as caught:
            session.simulation_inputs(0)
        self.assertNotIsInstance(caught.exception, SimulationRefused)
        self.assertEqual(str(caught.exception), session.generate_blocker())
        self.assertIn("Optimize", str(caught.exception))

    def test_an_index_that_is_not_on_screen_refuses(self):
        session = plain_session()
        for index in (-1, session.unique_sheet_count, 99):
            self.assertFalse(session.can_simulate(index))
            with self.assertRaises(SessionError):
                session.simulation_inputs(index)
        self.assertIn("does not exist", str(self._raise(session, 99)))

    @staticmethod
    def _raise(session: Session, index: int) -> SessionError:
        try:
            session.simulation_inputs(index)
        except SessionError as exc:
            return exc
        raise AssertionError("expected a refusal")

    def test_editing_a_row_invalidates_the_simulation_exactly_like_generate(self):
        """The 2026-08-04 rule: a stale layout may not reach Generate -- and a
        stale layout the operator is WATCHING BEING CUT would be worse."""
        session = plain_session()
        self.assertTrue(session.can_generate())
        self.assertTrue(session.can_simulate(0))
        session.simulation_inputs(0)  # works before the edit

        session.edit_row("a", qty=3)

        self.assertFalse(session.can_generate())
        self.assertFalse(session.can_simulate(0))
        generate_error = ""
        try:
            session.generate_nc(tempfile.gettempdir(), prefix="0000")
        except SessionError as exc:
            generate_error = str(exc)
        with self.assertRaises(SessionError) as caught:
            session.simulation_inputs(0)
        self.assertEqual(str(caught.exception), generate_error)

    def test_a_settings_change_invalidates_the_simulation_too(self):
        session = plain_session()
        session.set_settings(AppSettings(part_gap=1.0))
        self.assertFalse(session.can_simulate(0))
        with self.assertRaises(SessionError):
            session.simulation_inputs(0)

    def test_resolving_and_reoptimizing_brings_it_back(self):
        session = plain_session()
        session.edit_row("a", qty=2)
        self.assertFalse(session.can_simulate(0))
        session.optimize()
        self.assertTrue(session.can_simulate(0))
        self.assertGreater(session.simulation_inputs(0).timeline.cut_total, 0)

    def test_a_layout_its_own_validator_condemns_refuses_with_that_reason(self):
        """can_simulate is STRUCTURAL; the session still has the last word.

        The button stays live (the sheet exists and can be drawn), and the
        request is refused with the very message Generate refuses with, so the
        operator reads the same sentence in both places instead of finding a
        grey button and no explanation.
        """
        session = overlapping_session()
        self.assertTrue(session.can_simulate(0))
        self.assertFalse(session.can_generate())
        with self.assertRaises(SessionError) as caught:
            session.simulation_inputs(0)
        self.assertEqual(str(caught.exception), session.generate_blocker())
        self.assertIn("problem", str(caught.exception))


class SimulationRefusalTests(unittest.TestCase):
    """A sheet the POST refuses: the structure must survive the trip."""

    def push_the_wdc_to_the_edge(self, session: Session) -> Placement:
        """Move the WDC frame where its T17 slot runs off the sheet.

        Done by hand, past the optimizer's validator, on purpose: that is the
        state :mod:`faceframe_cnc.post.from_layout` re-checks for ("a
        hand-edited layout that slipped past cannot quietly cut a resized
        frame"), and it is the only way a refusal reaches the simulation with
        the layout still counted as current.
        """
        layout, _run = session.sheet(0)
        placement = layout.placements[0]
        placement.y = 0.2
        return placement

    def test_a_wdc_refusal_arrives_with_its_part_number_and_box(self):
        session = wdc_session()
        session.simulation_inputs(0)  # legal where the fixture put it
        placement = self.push_the_wdc_to_the_edge(session)

        with self.assertRaises(SimulationRefused) as caught:
            session.simulation_inputs(0)
        refused = caught.exception

        self.assertIsInstance(refused, SessionError)  # the Qt layer's net
        self.assertIsInstance(refused.error, WdcNotSupportedError)
        self.assertIs(refused.__cause__, refused.error)
        self.assertEqual(str(refused), str(refused.error))
        self.assertEqual(refused.part_number, "WDC2436")
        self.assertEqual(refused.part_number, refused.error.part_number)
        self.assertIsNotNone(refused.box)
        self.assertAlmostEqual(refused.box.x0, placement.x)
        self.assertAlmostEqual(refused.box.y0, placement.y)
        self.assertEqual(refused.sheet_index, 0)
        self.assertIsNotNone(refused.post_config)
        # The plan failed, not the program: the refusal view can draw the sheet.
        self.assertIsNotNone(refused.program)
        self.assertIn(
            "WDC2436", [p.part_number for p in refused.program.flat_parts()]
        )
        self.assertIn("T17 stile slot", str(refused))

    def test_a_refusal_with_no_program_at_all_says_so(self):
        """A part with no order line: there is nothing to draw, and no crash."""
        session = wdc_session()
        layout, _run = session.sheet(0)
        layout.placements[1].part_number = "W9999"

        with self.assertRaises(SimulationRefused) as caught:
            session.simulation_inputs(0)
        refused = caught.exception
        self.assertIsNone(refused.program)
        self.assertEqual(refused.part_number, "W9999")
        self.assertIn("not in the order", str(refused))

    def test_a_refused_sheet_does_not_disturb_the_session(self):
        session = wdc_session()
        self.push_the_wdc_to_the_edge(session)
        with self.assertRaises(SimulationRefused):
            session.simulation_inputs(0)
        # Simulating is a read: the layout, the problems and the flags are
        # exactly as they were, so the operator can go and fix the sheet.
        self.assertIsNotNone(session.result)
        self.assertEqual(session.problems(), [])
        self.assertTrue(session.can_simulate(0))


# --------------------------------------------------------------------------
# (b) The window wiring
# --------------------------------------------------------------------------


def module_main_window():
    import faceframe_cnc.gui.main_window as module

    return module


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class SimulateButtonTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.session = Session(AppSettings())
        self.session.set_rows(
            [
                OrderRow(
                    key="a",
                    part_number="W3036",
                    qty=1,
                    frame_width=30.0,
                    frame_height=36.0,
                ),
                OrderRow(
                    key="b",
                    part_number="W3012",
                    qty=1,
                    frame_width=30.0,
                    frame_height=12.0,
                ),
            ]
        )
        self.window = MainWindow(
            session=self.session,
            settings_path=os.path.join(self._folder.name, "settings.json"),
        )
        # No GL anywhere in this suite: the sim windows' own injection point.
        self.window.sim_viewport_hook = lambda root: None
        self.addCleanup(self.window.close)

    def test_the_button_exists_and_is_off_until_there_is_a_layout(self):
        self.assertFalse(self.window.simulate_button.isEnabled())
        self.assertFalse(self.window.simulate_action.isEnabled())
        self.assertEqual(self.window.simulate_button.toolTip(), SIMULATE_TOOLTIP)
        self.assertIn("sheet on screen", SIMULATE_TOOLTIP)
        self.assertIsNone(self.window.sim_window)

    def test_optimizing_turns_it_on(self):
        self.window.optimize()
        self.assertTrue(self.window.simulate_button.isEnabled())
        self.assertTrue(self.window.simulate_action.isEnabled())

    def test_an_order_change_cannot_leave_the_button_stale(self):
        """The grayed-Optimize lesson, on this button.

        The include/resolve signals land on ``_on_order_changed``, and the ONLY
        place any button's enabled state is recomputed is ``refresh()``; a
        button whose state depended on anything else would go stale here.
        """
        panel = self.window.order_panel
        self.window.optimize()
        self.assertTrue(self.window.simulate_button.isEnabled())

        panel.none_button.click()  # the layout is invalidated with the demand
        self.assertIsNone(self.session.result)
        self.assertFalse(self.window.simulate_button.isEnabled())
        self.assertFalse(self.window.simulate_action.isEnabled())

        panel.all_button.click()  # still no layout: the rows are back, not the nest
        self.assertFalse(self.window.simulate_button.isEnabled())

        self.window.optimize()
        self.assertTrue(self.window.simulate_button.isEnabled())

    def test_a_settings_change_that_clears_the_layout_greys_it_out(self):
        self.window.optimize()
        self.session.set_settings(AppSettings(sheet_width=40.0, sheet_height=60.0))
        self.window.refresh()
        self.assertFalse(self.window.simulate_button.isEnabled())

    def test_clicking_opens_a_playback_window_for_the_sheet_on_screen(self):
        self.window.optimize()
        self.window.simulate_button.click()

        sim = self.window.sim_window
        self.assertIsInstance(sim, Sim3DWindow)
        self.assertIsNone(sim.parent(), "a separate, non-modal top level")
        self.assertFalse(sim.isModal())
        self.assertIsNotNone(sim.findings)
        self.assertEqual(sim.findings.count, 0)
        self.assertEqual(
            sim.timeline.program.header.name,
            self.session.simulation_inputs(0).header.name,
        )
        self.assertIn("Simulating sheet 1", self.window.statusBar().currentMessage())
        self.assertIsNone(self.window.last_warning)

    def test_the_second_sheet_is_the_one_the_preview_is_showing(self):
        self.session.set_rows(
            [
                OrderRow(
                    key="a",
                    part_number="W3030",
                    qty=4,
                    frame_width=30.0,
                    frame_height=30.0,
                )
            ]
        )
        self.window.order_panel.reload()
        self.window.optimize()
        self.assertGreater(self.session.unique_sheet_count, 1)

        self.window.next_button.click()
        self.assertEqual(self.window.canvas.sheet_index, 1)
        self.window.simulate_button.click()
        sim = self.window.sim_window
        self.assertIsInstance(sim, Sim3DWindow)
        self.assertEqual(
            sim.timeline.program.header.name,
            self.session.simulation_inputs(1).header.name,
        )
        self.assertIn("sheet 2", self.window.statusBar().currentMessage())

    def test_a_second_click_replaces_the_first_window(self):
        self.window.optimize()
        self.window.simulate_button.click()
        first = self.window.sim_window
        self.window.simulate_button.click()
        second = self.window.sim_window

        self.assertIsInstance(second, Sim3DWindow)
        self.assertIsNot(second, first)
        self.assertFalse(first.isVisible(), "one simulation window at a time")
        self.assertFalse(first.timer.isActive(), "and its clock is stopped")

    def test_closing_the_main_window_takes_the_simulation_with_it(self):
        self.window.optimize()
        self.window.simulate_button.click()
        sim = self.window.sim_window
        self.window.close()
        self.assertIsNone(self.window.sim_window)
        self.assertFalse(sim.isVisible())

    def test_the_action_opens_the_same_window(self):
        self.window.optimize()
        self.window.simulate_action.trigger()
        self.assertIsInstance(self.window.sim_window, Sim3DWindow)

    def test_a_refused_sheet_opens_the_refusal_view_with_the_message(self):
        self.session.set_rows(
            [
                OrderRow(
                    key="w",
                    part_number="WDC2436",
                    qty=1,
                    frame_width=18.0,
                    frame_height=36.0,
                ),
                OrderRow(
                    key="p",
                    part_number="W2436",
                    qty=1,
                    frame_width=24.0,
                    frame_height=36.0,
                ),
            ]
        )
        self.window.order_panel.reload()
        source = wdc_session()
        self.session.set_result(source.result)
        self.window.canvas.show_sheet(0)
        self.window.refresh()
        layout, _run = self.session.sheet(0)
        layout.placements[0].y = 0.2

        self.assertTrue(
            self.window.simulate_button.isEnabled(),
            "a refused sheet is exactly what the refusal view is for",
        )
        self.window.simulate_button.click()

        view = self.window.sim_window
        self.assertIsInstance(view, RefusalView)
        self.assertIn("T17 stile slot", view.banner.text())
        self.assertEqual(view.part_number, "WDC2436")
        self.assertIsNotNone(view.marked_part, "the refused frame is outlined")
        self.assertIn("REFUSED", self.window.statusBar().currentMessage())
        # A refusal is a result, not an error: no message box was needed.
        self.assertIsNone(self.window.last_warning)

    def test_a_session_refusal_is_a_message_box_and_no_window(self):
        module = module_main_window()
        warnings: list = []
        original = module.QMessageBox.warning
        module.QMessageBox.warning = staticmethod(
            lambda *args, **kwargs: warnings.append(args[-1])
        )

        def boom(_index):
            raise SessionError("no layout, no simulation")

        self.window.optimize()
        self.session.simulation_inputs = boom
        try:
            self.window.simulate_button.click()
        finally:
            module.QMessageBox.warning = original
            del self.session.simulation_inputs

        self.assertEqual(warnings, ["no layout, no simulation"])
        self.assertIsNone(self.window.sim_window)
        self.assertEqual(
            self.window.last_warning, (SIMULATE_ERROR_TITLE, "no layout, no simulation")
        )

    def test_an_unexpected_failure_is_still_a_message_and_never_a_traceback(self):
        module = module_main_window()
        warnings: list = []
        original = module.QMessageBox.warning
        module.QMessageBox.warning = staticmethod(
            lambda *args, **kwargs: warnings.append(args[-1])
        )

        def boom(_index):
            raise RuntimeError("something nobody predicted")

        self.window.optimize()
        self.session.simulation_inputs = boom
        try:
            self.window.simulate_button.click()  # must not raise
        finally:
            module.QMessageBox.warning = original
            del self.session.simulation_inputs

        self.assertEqual(len(warnings), 1)
        self.assertIn("something nobody predicted", warnings[0])
        self.assertIsNone(self.window.sim_window)

    def test_the_production_viewport_hook_is_the_windows_own_default(self):
        """Nothing in the app passes a stub: the hook is None until a test or
        the unattended self test puts one there."""
        fresh = MainWindow(
            session=plain_session(),
            settings_path=os.path.join(self._folder.name, "settings2.json"),
        )
        self.addCleanup(fresh.close)
        self.assertIsNone(fresh.sim_viewport_hook)
        self.assertFalse(fresh.unattended)

    def test_the_playback_window_can_be_driven_with_no_viewport(self):
        self.window.optimize()
        self.window.simulate_button.click()
        sim = self.window.sim_window
        self.assertIsNone(sim.viewport)
        sim.next_cut()
        sim.next_cut()
        self.assertGreater(sim.controller.step_index, 0)
        sim.advance(1.0)
        self.assertGreater(sim.controller.step_index, 0)
        sim.reset()
        self.assertEqual(sim.controller.step_index, 0)


# --------------------------------------------------------------------------
# (c) --self-test-sim
# --------------------------------------------------------------------------


class SelfTestSimFlagTests(unittest.TestCase):
    def test_the_flag_is_off_by_default(self):
        from faceframe_cnc.gui import __main__ as entry

        self.assertFalse(entry._parse_args([]).self_test_sim)
        self.assertTrue(entry._parse_args(["--self-test-sim"]).self_test_sim)
        # It is its own flag, not a mode of --self-test.
        self.assertIsNone(entry._parse_args(["--self-test-sim"]).self_test)

    def test_the_built_in_order_is_two_cuttable_frames(self):
        from faceframe_cnc.gui import __main__ as entry

        session = Session(AppSettings())
        session.set_rows(
            [
                OrderRow(
                    key=part,
                    part_number=part,
                    qty=qty,
                    frame_width=width,
                    frame_height=height,
                )
                for part, width, height, qty in entry.SELF_TEST_SIM_ORDER
            ]
        )
        self.assertEqual(len(session.included_rows()), len(entry.SELF_TEST_SIM_ORDER))
        session.optimize()
        self.assertEqual(session.unique_sheet_count, 1)
        self.assertEqual(session.simulation_inputs(0).findings.count, 0)


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class SelfTestSimRunTests(unittest.TestCase):
    """The whole hook, in a subprocess, on the offscreen platform.

    A subprocess because the check IS an exit code and a printed line, and
    because a second QApplication in this process would be a different test
    from the one CI runs.
    """

    def test_it_runs_unattended_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            env.pop("FACEFRAME_GUI_SELFTEST", None)
            env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "faceframe_cnc.gui",
                    "--self-test-sim",
                    "--settings",
                    os.path.join(folder, "settings.json"),
                ],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
        self.assertEqual(
            completed.returncode, 0, f"{completed.stdout}\n{completed.stderr}"
        )
        self.assertIn("self-test-sim:", completed.stdout)
        self.assertIn("0 verifier finding(s)", completed.stdout)
        self.assertIn("cuts complete", completed.stdout)
        self.assertIn("unique sheet(s)", completed.stdout)


if __name__ == "__main__":
    unittest.main()
