"""Milestone 5 phase 2: the Generate button, session side and Qt side.

The session half runs anywhere (no Qt, no display, no pandas); the Qt half
skips itself when PySide6 is missing and otherwise builds the dialog on the
offscreen platform plugin.  As everywhere else in this app, the rules live
in the session and the widget only collects four choices.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
import tempfile
import unittest

# Must be set before the first QGuiApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from faceframe_cnc.gui.session import (
    AppSettings,
    OrderRow,
    Session,
    SessionError,
    load_settings,
    save_settings,
)
from faceframe_cnc.nesting import NestingConfig, nest
from tests.test_nesting import ORDER_7_21_26

try:  # the .xls parser is the only thing in the app that needs pandas
    import pandas  # noqa: F401

    HAVE_PANDAS = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_PANDAS = False

HERE = os.path.dirname(os.path.abspath(__file__))
ORDER_XLS = os.path.join(
    HERE, os.pardir, "reference", "orders", "7-21-26_Cab_Tec_Order_with_specs.xls"
)
HAVE_ORDER = os.path.exists(ORDER_XLS)

CREATED = "01 JAN 27 - 08:00"


def session_with_layout(part_gap: float = 0.455) -> Session:
    """Two wall frames and two small ones: one sheet, one nested frame."""
    session = Session(AppSettings(part_gap=part_gap, inside_nesting=True))
    session.set_rows(
        [
            OrderRow(key="a", part_number="W3036", qty=2, frame_width=30.0, frame_height=36.0),
            OrderRow(key="b", part_number="W3012", qty=2, frame_width=30.0, frame_height=12.0),
        ]
    )
    session.optimize()
    return session


class SessionGenerateTests(unittest.TestCase):
    def test_nothing_to_generate_before_the_optimizer_runs(self):
        session = Session(AppSettings())
        self.assertFalse(session.can_generate())
        self.assertEqual(session.generate_blocker(), "Optimize a layout first")
        with self.assertRaises(SessionError):
            session.generate_nc("nowhere", prefix="1234")

    def test_a_layout_with_problems_is_not_generated(self):
        session = session_with_layout()
        session._problems = ["sheet 1: something is wrong"]
        self.assertFalse(session.can_generate())
        self.assertIn("unresolved problem", session.generate_blocker() or "")
        with self.assertRaises(SessionError):
            session.generate_nc("nowhere", prefix="1234")

    def test_it_writes_one_verified_file_per_sheet(self):
        session = session_with_layout()
        self.assertTrue(session.can_generate())
        self.assertIsNone(session.generate_blocker())
        with tempfile.TemporaryDirectory() as folder:
            job = session.generate_nc(
                folder, prefix="7201", pdf_report=False, created=CREATED
            )
            self.assertEqual(len(job.outcomes), session.unique_sheet_count)
            self.assertEqual(job.refused, [], job.summary())
            names = sorted(os.listdir(folder))
            self.assertEqual(names, sorted(o.filename for o in job.written))
            for name in names:
                self.assertRegex(name, r"^R7201\d{2}N\.anc$")
            self.assertIsNone(job.report_path, "no report was asked for")
            self.assertIsNone(job.report_problem)

    def test_a_bad_prefix_never_reaches_the_disk(self):
        session = session_with_layout()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(SessionError):
                session.generate_nc(folder, prefix="7-2", created=CREATED)
            self.assertEqual(os.listdir(folder), [])

    def test_the_dry_run_toggle_reaches_the_job(self):
        session = session_with_layout()
        with tempfile.TemporaryDirectory() as folder:
            job = session.generate_nc(
                folder, prefix="7201", dry_run=True, created=CREATED
            )
            self.assertTrue(job.dry_run)
            with open(job.files[0], "r", newline="") as handle:
                self.assertIn("DRY RUN", handle.read())

    def test_one_file_per_physical_sheet(self):
        session = session_with_layout()
        with tempfile.TemporaryDirectory() as folder:
            job = session.generate_nc(
                folder, prefix="7201", per_physical_sheet=True, created=CREATED
            )
            self.assertEqual(len(job.outcomes), session.total_sheets)

    def test_a_wdc_line_comes_out_with_its_t17_slot(self):
        """WDC frames used to be refused for want of a tool; they are cut
        now, and the optimizer leaves the room the 45-degree slot needs."""
        session = Session(AppSettings(part_gap=0.455))
        session.set_rows(
            [
                OrderRow(
                    key="w", part_number="WDC2436", qty=2, frame_width=18.0, frame_height=36.0
                )
            ]
        )
        session.optimize()
        with tempfile.TemporaryDirectory() as folder:
            job = session.generate_nc(
                folder, prefix="7201", pdf_report=False, created=CREATED
            )
            self.assertEqual(job.refused, [], job.summary())
            self.assertEqual(os.listdir(folder), ["R720101N.anc"])
            with open(job.files[0], "r", newline="") as handle:
                text = handle.read()
            self.assertIn("(ROUTE TOOL #17: T17 45 VTIP 158-562SC.026-1W-A)", text)

    def test_refusals_come_back_instead_of_raising(self):
        """A sheet the verifier will not pass is reported, not raised, and
        the job still writes the sheets that are fine.

        The 0.375 gap the spec asks for is what produces one: it is 0.05
        short of the perimeter lead-in's sweep, so on a real order some
        sheets cut into their neighbours.  It needs the real order — a
        two-frame sheet has nowhere for the lead-in to reach.

        ``Session.optimize`` refuses a 0.375 gap outright now (the
        2026-08-03 floor), so the layout is packed with the library and
        installed via ``set_result`` — the verifier stays the last line of
        defence for layouts that never went through the session's guard.
        """
        session = Session(AppSettings())
        session.set_result(
            nest(ORDER_7_21_26, NestingConfig(part_gap=0.375, inside_nesting=True))
        )
        with tempfile.TemporaryDirectory() as folder:
            job = session.generate_nc(folder, prefix="7201", created=CREATED)
            self.assertTrue(job.refused, "0.375 cannot be cut")
            self.assertTrue(job.written, "the rest of the job still goes out")
            for outcome in job.refused:
                self.assertEqual(outcome.refusal_kind, "verifier")
                self.assertNotIn(outcome.filename, os.listdir(folder))

    def test_optimize_itself_now_refuses_the_gap_that_caused_those_refusals(self):
        """Bug fix 2026-08-03: a stale 0.375 used to sail through optimize
        and surface as 8 of 17 sheets refused at Generate.  The session now
        stops it at optimize time, with the reason and the fix in the
        message."""
        session = Session(AppSettings(inside_nesting=True))
        session.settings.part_gap = 0.375
        session.set_rows(
            [OrderRow(key="a", part_number="W3036", qty=2, frame_width=30.0, frame_height=36.0)]
        )
        with self.assertRaises(SessionError) as caught:
            session.optimize()
        self.assertIn("0.455", str(caught.exception))

    def test_the_output_folder_and_prefix_are_remembered(self):
        session = session_with_layout()
        with tempfile.TemporaryDirectory() as folder:
            session.generate_nc(folder, prefix="7201", created=CREATED)
            self.assertEqual(session.settings.job_prefix, "7201")
            self.assertEqual(
                os.path.normcase(session.settings.last_output_dir or ""),
                os.path.normcase(os.path.abspath(folder)),
            )
            settings_path = os.path.join(folder, "settings.json")
            self.assertTrue(save_settings(session.settings, settings_path))
            reloaded = load_settings(settings_path)
            self.assertEqual(reloaded.job_prefix, "7201")
            self.assertEqual(reloaded.last_output_dir, session.settings.last_output_dir)

    def test_the_dry_run_toggle_is_deliberately_not_persisted(self):
        keys = AppSettings().to_dict()
        self.assertNotIn("dry_run", keys)
        self.assertNotIn("per_physical_sheet", keys)

    def test_the_default_prefix_is_the_saved_one_or_todays_date(self):
        session = Session(AppSettings())
        self.assertEqual(session.default_job_prefix(today="0803"), "0803")
        self.assertRegex(session.default_job_prefix(), r"^\d{4}$")
        session.settings.job_prefix = "7201"
        self.assertEqual(session.default_job_prefix(today="0803"), "7201")

    # -- Milestone 6: the PDF cut-sheet report --------------------------

    def test_the_report_lands_beside_the_anc_files_by_default(self):
        session = session_with_layout()
        with tempfile.TemporaryDirectory() as folder:
            job = session.generate_nc(folder, prefix="7201", created=CREATED)
            self.assertEqual(job.report_path, os.path.join(job.output_dir, "R7201_report.pdf"))
            self.assertIsNone(job.report_problem)
            names = sorted(os.listdir(folder))
            self.assertIn("R7201_report.pdf", names)
            self.assertEqual(
                [name for name in names if name.endswith(".anc")],
                sorted(o.filename for o in job.written),
            )
            with open(job.report_path, "rb") as handle:
                data = handle.read()
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertTrue(data.endswith(b"%%EOF\n"))

    def test_the_report_name_follows_the_job_prefix(self):
        self.assertEqual(Session.report_filename("7201"), "R7201_report.pdf")
        self.assertEqual(Session.report_filename(" 62 "), "R62_report.pdf")

    def test_a_report_failure_never_stops_the_anc_files_going_out(self):
        """Paperwork is paperwork.  A folder with programs and no report is
        a nuisance; a folder with a report and no programs is useless."""
        import faceframe_cnc.report.cutsheet as report_module

        session = session_with_layout()
        original = report_module.write_report

        def explode(*args, **kwargs):
            raise RuntimeError("no disk space for paperwork")

        report_module.write_report = explode
        try:
            with tempfile.TemporaryDirectory() as folder:
                job = session.generate_nc(folder, prefix="7201", created=CREATED)
                self.assertEqual(job.refused, [], job.summary())
                self.assertTrue(job.written, "the programs still go out")
                self.assertIsNone(job.report_path)
                self.assertIn("no disk space for paperwork", job.report_problem)
                self.assertEqual(
                    sorted(os.listdir(folder)),
                    sorted(o.filename for o in job.written),
                )
        finally:
            report_module.write_report = original

    def test_the_report_covers_the_sheets_the_job_wrote(self):
        from tests.test_report import Pdf

        session = session_with_layout()
        with tempfile.TemporaryDirectory() as folder:
            job = session.generate_nc(folder, prefix="7201", created=CREATED)
            with open(job.report_path, "rb") as handle:
                parsed = Pdf(handle.read())
            self.assertEqual(parsed.problems, [])
            self.assertEqual(parsed.page_count, session.unique_sheet_count + 1)
            cover = parsed.text(0)
            for outcome in job.outcomes:
                self.assertIn(outcome.filename, cover)

    def test_a_too_tight_gap_is_reported_not_written(self):
        """The spec's 0.375 gap is 0.05 short of what the perimeter lead-in
        sweeps; the verifier catches it and the file is not written.  The
        layout is installed directly (``Session.optimize`` refuses 0.375
        since the 2026-08-03 floor), using the fixture built to guarantee
        the collision."""
        from tests.test_nc_job import crowded_sheet

        result, _config = crowded_sheet()
        session = Session(AppSettings())
        session.set_result(result)
        with tempfile.TemporaryDirectory() as folder:
            job = session.generate_nc(folder, prefix="7201", created=CREATED)
            self.assertTrue(job.refused, "two parts 0.375 apart must be refused")
            for outcome in job.refused:
                self.assertEqual(outcome.refusal_kind, "verifier")
                self.assertNotIn(outcome.filename, os.listdir(folder))

    # -- the 2026-08-03 stale-settings repro, end to end -----------------

    @unittest.skipUnless(HAVE_PANDAS, "pandas/xlrd are needed to read .xls orders")
    @unittest.skipUnless(HAVE_ORDER, "the sample order spreadsheet is not present")
    def test_a_stale_settings_file_no_longer_produces_refused_sheets(self):
        """The owner's exact repro, replayed through every layer of the fix.

        faceframe_settings.json persisted part_gap 0.375 from before the
        0.455 amendment; loading it, loading the 7-21 order (with NOTHING
        resolved by hand — the WDC row resolves itself now), optimizing and
        generating used to end in 8 of 17 sheets refused with
        "[foreign-cut] ...".  Now the load migrates the gap up to 0.455 with
        a note, and every sheet writes.
        """
        import json

        with tempfile.TemporaryDirectory() as folder:
            settings_path = os.path.join(folder, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump({"part_gap": 0.375, "inside_nesting": True}, handle)

            settings = load_settings(settings_path)
            self.assertEqual(settings.part_gap, 0.455)
            self.assertTrue(settings.migration_notes, "the fix must not be silent")

            session = Session(settings)
            session.load_order(ORDER_XLS)
            self.assertEqual(
                session.needs_attention_rows(),
                [],
                "nothing may need manual resolution on this order any more",
            )
            session.optimize()

            out_dir = os.path.join(folder, "nc")
            job = session.generate_nc(
                out_dir, prefix="7201", pdf_report=False, created=CREATED
            )
            self.assertEqual(job.refused, [], job.summary())
            self.assertEqual(len(job.written), session.unique_sheet_count)
            written = sorted(os.listdir(out_dir))
            self.assertEqual(written, sorted(o.filename for o in job.written))


# --------------------------------------------------------------------------
# Qt
# --------------------------------------------------------------------------

try:
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_QT = False

if HAVE_QT:
    from faceframe_cnc.gui.generate_dialog import GenerateChoices, GenerateDialog
    from faceframe_cnc.gui.main_window import MainWindow

_APP = None


def setUpModule():  # noqa: N802 - unittest naming
    global _APP
    if HAVE_QT:
        _APP = QApplication.instance() or QApplication([])


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class GenerateDialogSmokeTests(unittest.TestCase):
    def setUp(self):
        self.session = session_with_layout()
        self.dialog = GenerateDialog(self.session)
        self.addCleanup(self.dialog.close)

    def test_it_builds_offscreen_with_sensible_defaults(self):
        self.assertRegex(self.dialog.prefix.text(), r"^\d+$")
        self.assertFalse(self.dialog.dry_run.isChecked())
        self.assertFalse(self.dialog.per_physical.isChecked())
        self.assertTrue(
            self.dialog.pdf_report.isChecked(), "the report is on by default"
        )
        self.assertTrue(self.dialog.output_dir.text())

    def test_the_preview_shows_the_first_file_name(self):
        self.dialog.prefix.setText("7201")
        self.assertEqual(self.dialog.preview.text(), "R720101N.anc")

    def test_a_non_numeric_prefix_disables_generate(self):
        from PySide6.QtWidgets import QDialogButtonBox

        ok = self.dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.dialog.prefix.setText("72-01")
        self.assertFalse(ok.isEnabled())
        self.dialog.prefix.setText("7201")
        self.assertTrue(ok.isEnabled())

    def test_choices_reports_what_was_ticked(self):
        self.dialog.prefix.setText("7201")
        self.dialog.dry_run.setChecked(True)
        self.dialog.per_physical.setChecked(True)
        choices = self.dialog.choices()
        self.assertEqual(choices.prefix, "7201")
        self.assertTrue(choices.dry_run)
        self.assertTrue(choices.per_physical_sheet)
        self.assertTrue(choices.pdf_report)
        self.dialog.pdf_report.setChecked(False)
        self.assertFalse(self.dialog.choices().pdf_report)

    def test_the_report_choice_defaults_on_when_it_is_not_stated(self):
        """Callers built before Milestone 6 still get the paperwork."""
        self.assertTrue(GenerateChoices("out", "7201", False, False).pdf_report)


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class MainWindowGenerateTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.session = session_with_layout()
        self.window = MainWindow(
            session=self.session,
            settings_path=os.path.join(self._folder.name, "settings.json"),
        )
        self.addCleanup(self.window.close)
        self.window.refresh()

    def test_the_button_goes_live_once_there_is_a_layout(self):
        self.assertTrue(self.window.generate_button.isEnabled())
        self.session.result = None
        self.window.refresh()
        self.assertFalse(self.window.generate_button.isEnabled())

    def test_clicking_it_runs_the_job_and_reports(self):
        import faceframe_cnc.gui.main_window as module

        target = os.path.join(self._folder.name, "nc")
        seen = {}

        class FakeDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, session, parent=None):
                seen["session"] = session

            def exec(self):
                return int(QDialog.DialogCode.Accepted)

            def choices(self):
                return GenerateChoices(target, "7201", False, False)

        original_dialog = module.GenerateDialog
        original_exec = QMessageBox.exec
        module.GenerateDialog = FakeDialog
        QMessageBox.exec = lambda self: 0
        try:
            self.window.generate_nc()
        finally:
            module.GenerateDialog = original_dialog
            QMessageBox.exec = original_exec

        self.assertIs(seen["session"], self.session)
        written = sorted(os.listdir(target))
        self.assertTrue(written)
        self.assertIn("R7201_report.pdf", written, "the paperwork goes out too")
        for name in written:
            if name.endswith(".pdf"):
                continue
            self.assertRegex(name, r"^R7201\d{2}N\.anc$")
        message = self.window.statusBar().currentMessage()
        self.assertIn("written", message)
        self.assertIn("PDF report", message)

    def test_the_report_can_be_turned_off_from_the_dialog(self):
        import faceframe_cnc.gui.main_window as module

        target = os.path.join(self._folder.name, "nc-no-pdf")

        class FakeDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, session, parent=None):
                pass

            def exec(self):
                return int(QDialog.DialogCode.Accepted)

            def choices(self):
                return GenerateChoices(target, "7201", False, False, False)

        original_dialog = module.GenerateDialog
        original_exec = QMessageBox.exec
        module.GenerateDialog = FakeDialog
        QMessageBox.exec = lambda self: 0
        try:
            self.window.generate_nc()
        finally:
            module.GenerateDialog = original_dialog
            QMessageBox.exec = original_exec

        written = sorted(os.listdir(target))
        self.assertTrue(written)
        self.assertFalse([name for name in written if name.endswith(".pdf")])

    def test_it_refuses_politely_with_no_layout(self):
        import faceframe_cnc.gui.main_window as module

        self.session.result = None
        original_warning = module.QMessageBox.warning
        calls = []
        module.QMessageBox.warning = staticmethod(
            lambda *args, **kwargs: calls.append(args[-1])
        )
        try:
            self.window.generate_nc()
        finally:
            module.QMessageBox.warning = original_warning
        self.assertEqual(calls, ["Optimize a layout first"])


if __name__ == "__main__":
    unittest.main()
