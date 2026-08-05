"""The verifier's expected-work manifest: MISSING cuts (2026-08-04 review).

Everything the verifier checked before this could only ever answer "does
this program do something it must not?".  These tests are the other half —
"does it do everything it must?" — and they are mutation tests on purpose:
a check that only ever sees good files proves nothing.  Each one takes a
sheet the post generates and verifies clean, deletes ONE thing a machinist
would notice missing, and requires the verifier to refuse it:

  * the full-depth perimeter pass of one part (the file still recovers every
    part from the onion-skin pass, so every older rule still passes it, and
    the machine hands back a sheet with every frame still attached);
  * one opening's T11 through-cut and T12 finish pass (the verifier used to
    read that area as solid frame and agree with itself);
  * one T17 slot pass on a WDC stile;
  * one T13 panel groove;
  * an onion-skin pass whose Z word has been changed to the through depth,
    which is the same count of loops at the wrong depths.

Plus the two things that must NOT change: a reference file the shop already
cut has no layout behind it, so ``expected=None`` has to behave exactly as
it always did; and ``build_job`` must refuse (``refusal_kind == "verifier"``)
a sheet whose generated text has had a pass dropped, which is checked by
handing it a sabotaged emitter.

The last class extends that gate to the 2026-08-04 feed follow-up: the same
sabotaged emitter, but changing one feed or speed word instead of deleting a
block.  (The F/S grammar itself is asserted in ``tests/test_post.py``.)

Stdlib only.  Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
import re
import unittest

from faceframe_cnc.post import (
    CutPlan,
    FeatureRef,
    JobOptions,
    ProgramHeader,
    build_job,
    default_config,
    dry_run_config,
    generate,
    plan_sheet,
    post_config_for,
    reconstruct,
    verify,
    verify_file,
)
from faceframe_cnc.post import job as job_module
from faceframe_cnc.post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_WDC_SLOT,
)
from faceframe_cnc.post.verifier import ExpectedWork, expected_work
from tests.test_nc_job import CREATED, job_for, nested_sample, wdc_sheet

NC_DIR = os.path.join(os.path.dirname(__file__), "..", "reference", "nc_files")

#: A feature block opens either with the section's first preposition (the one
#: that switches the spindle on) or with a later one, and closes with the
#: retract to the rapid plane.  Both forms restate X and Y absolutely, so
#: removing a whole block leaves the next one landing where it always did.
_FIRST_PREPOSITION = re.compile(r"^G0 G54 G90 X-?[\d.]+ Y-?[\d.]+ M13 S\d+$")
_PREPOSITION = re.compile(r"^X-?[\d.]+ Y-?[\d.]+ Z2\.5$")


def sheet_under_test(result, config):
    """``(layout, text, post config, manifest)`` for one generated sheet."""
    layout = result.unique_sheets[0][0]
    outcome = job_for(result).outcomes[0]
    assert outcome.ok, outcome.describe()
    cfg = post_config_for(config)
    return layout, outcome.text, cfg, expected_work(layout, cfg)


def occurrences(text: str, needle: str) -> list[int]:
    """Line indices (0-based) of every line containing ``needle``."""
    return [i for i, line in enumerate(text.split("\r\n")) if needle in line]


def delete_block(text: str, anchor: int) -> str:
    """Remove the whole feature block that the line ``anchor`` belongs to.

    Deliberately crude, and deliberately NOT a small edit: dropping a cut in
    a way that leaves the file structurally intact is exactly the mistake
    that used to pass, so the mutation has to be the plausible one (a
    feature the emitter never wrote) rather than a mangled line the older
    checks would trip over on their own.
    """
    lines = text.split("\r\n")
    start = anchor
    while start > 0 and not (
        _PREPOSITION.match(lines[start]) or _FIRST_PREPOSITION.match(lines[start])
    ):
        start -= 1
    end = anchor
    while end < len(lines) and lines[end] != "G0 Z2.5":
        end += 1
    assert end < len(lines), "no retract after the anchor line"
    return "\r\n".join(lines[:start] + lines[end + 1 :])


def drop(text: str, needle: str, nth: int = 0) -> str:
    hits = occurrences(text, needle)
    assert len(hits) > nth, f"{needle!r} appears {len(hits)} time(s)"
    return delete_block(text, hits[nth])


class UntouchedSheetsPassTest(unittest.TestCase):
    """The manifest is only worth anything if it agrees with a good file."""

    def test_a_generated_sheet_passes_with_its_manifest(self):
        result, config = nested_sample()
        _layout, text, cfg, expected = sheet_under_test(result, config)
        self.assertEqual([str(v) for v in verify(text, cfg, expected)], [])

    def test_a_wdc_sheet_passes_with_its_manifest(self):
        result, config = wdc_sheet()
        _layout, text, cfg, expected = sheet_under_test(result, config)
        self.assertEqual([str(v) for v in verify(text, cfg, expected)], [])

    def test_a_rotated_wdc_sheet_passes_with_its_manifest(self):
        """The manifest re-derives the rotation transform itself, so a
        rotated part is the case where a second copy could disagree."""
        result, config = wdc_sheet(rotated=True)
        _layout, text, cfg, expected = sheet_under_test(result, config)
        self.assertEqual([str(v) for v in verify(text, cfg, expected)], [])

    def test_the_dry_run_of_the_same_sheet_passes_against_the_lifted_table(self):
        result, config = nested_sample()
        layout = result.unique_sheets[0][0]
        outcome = job_for(result, dry_run=True).outcomes[0]
        self.assertTrue(outcome.ok, outcome.describe())
        air = dry_run_config(post_config_for(config))
        self.assertEqual(
            [str(v) for v in verify(outcome.text, air, expected_work(layout, air))], []
        )

    def test_the_manifest_counts_the_work_the_sheet_owes(self):
        """Three parts, one opening each, no WDC: 12 grooves, 3+3 opening
        passes, 6 perimeter loops."""
        result, config = nested_sample()
        _layout, _text, _cfg, expected = sheet_under_test(result, config)
        self.assertEqual(
            expected.counts(),
            {"groove": 12, "opening": 3, "detail": 3, "perimeter": 6},
        )
        self.assertEqual(len(expected), 24)

    def test_a_wdc_owes_two_grooves_and_four_slot_passes(self):
        result, config = wdc_sheet()
        _layout, _text, _cfg, expected = sheet_under_test(result, config)
        self.assertEqual(
            expected.counts(),
            {"groove": 2, "slot": 4, "opening": 1, "detail": 1, "perimeter": 2},
        )


class MissingCutTest(unittest.TestCase):
    """One deletion per test; every one of them must be refused."""

    @classmethod
    def setUpClass(cls):
        cls.result, cls.config = nested_sample()
        cls.layout, cls.text, cls.cfg, cls.expected = sheet_under_test(
            cls.result, cls.config
        )
        cls.wdc_result, cls.wdc_config = wdc_sheet()
        (
            cls.wdc_layout,
            cls.wdc_text,
            cls.wdc_cfg,
            cls.wdc_expected,
        ) = sheet_under_test(cls.wdc_result, cls.wdc_config)

    def problems(self, text, cfg=None, expected=None):
        return verify(text, cfg or self.cfg, expected or self.expected)

    def test_dropping_the_full_depth_perimeter_pass_is_caught(self):
        """The 0.06 onion skin alone leaves every part on the sheet.

        Nothing else in the verifier sees this: the part is still recovered
        (from the shallow loop), still on the sheet, still cutting nothing it
        should not.
        """
        text = drop(self.text, "Z-0.006 F150.")
        self.assertEqual([str(v) for v in verify(text, self.cfg)], [])  # the old gap
        problems = self.problems(text)
        self.assertEqual([v.code for v in problems], ["missing-cut"])
        message = problems[0].message
        self.assertIn("the full-depth perimeter pass", message)
        self.assertIn("still attached to the", message)

    def test_dropping_the_onion_skin_pass_is_caught_too(self):
        """The SECOND part's skin pass, not the first (2026-08-04 follow-up).

        A section's first feature block is also the one that starts the
        spindle (``M13 S16700``) and applies the tool length comp, so deleting
        it now trips the new ``spindle-speed`` rule as well — correctly, but
        it would make this test about two findings instead of the one it is
        for.  Any part's skin pass proves the same point.
        """
        text = drop(self.text, "Z0.06 F150.", nth=1)
        self.assertEqual([str(v) for v in verify(text, self.cfg)], [])
        problems = self.problems(text)
        self.assertEqual([v.code for v in problems], ["missing-cut"])
        self.assertIn("the onion-skin perimeter pass", problems[0].message)

    def test_an_onion_skin_pass_cut_at_the_through_depth_is_caught(self):
        """Same number of loops, wrong Z semantics: the skin that holds
        every part is gone even though nothing was deleted.

        (This one the older rules DO shout about as well — a through cut on
        the skin pass's wider profile sweeps into its neighbours — but the
        two cuts of the manifest are the ones that name the pass.)
        """
        text = self.text.replace("Z0.06 F150.", "Z-0.006 F150.", 1)
        codes = {v.code for v in self.problems(text)}
        self.assertIn("missing-cut", codes)
        self.assertIn("extra-cut", codes)
        missing = [v for v in self.problems(text) if v.code == "missing-cut"]
        self.assertEqual(len(missing), 1)
        self.assertIn("the onion-skin perimeter pass", missing[0].message)

    def test_dropping_both_of_an_openings_passes_is_caught(self):
        """T11 and T12 gone: the verifier used to read the area as solid."""
        text = drop(self.text, "Z0.15 F150.", nth=2)
        text = drop(text, "Z-0.002 F100.", nth=2)
        self.assertEqual([str(v) for v in verify(text, self.cfg)], [])
        problems = self.problems(text)
        self.assertEqual([v.code for v in problems], ["missing-cut", "missing-cut"])
        messages = " | ".join(v.message for v in problems)
        self.assertIn("T11 through-cut", messages)
        self.assertIn("T12 finish pass", messages)
        self.assertIn("'opening'", messages)

    def test_dropping_only_the_t11_pass_is_caught(self):
        text = drop(self.text, "Z0.15 F150.", nth=2)
        problems = self.problems(text)
        self.assertEqual([v.code for v in problems], ["missing-cut"])
        self.assertIn("T11 through-cut", problems[0].message)

    def test_dropping_only_the_t12_pass_is_caught(self):
        text = drop(self.text, "Z-0.002 F100.", nth=2)
        problems = self.problems(text)
        self.assertEqual([v.code for v in problems], ["missing-cut"])
        self.assertIn("T12 finish pass", problems[0].message)

    def test_dropping_a_t13_groove_is_caught(self):
        text = drop(self.text, "G1 Z0.55 F150.", nth=1)
        self.assertEqual([str(v) for v in verify(text, self.cfg)], [])
        problems = self.problems(text)
        self.assertEqual([v.code for v in problems], ["missing-cut"])
        self.assertIn("T13", problems[0].message)
        self.assertIn("groove", problems[0].message)

    def test_dropping_one_t17_slot_pass_is_caught(self):
        """Both passes are on ONE centreline, so the surviving pass looks
        like a complete slot to everything that only reads coordinates."""
        text = drop(self.wdc_text, "G1 Z0.3125 F150.")
        self.assertEqual([str(v) for v in verify(text, self.wdc_cfg)], [])
        problems = self.problems(text, self.wdc_cfg, self.wdc_expected)
        self.assertEqual([v.code for v in problems], ["missing-cut"])
        message = problems[0].message
        self.assertIn("WDC2436", message)
        self.assertIn("T17 slot", message)
        self.assertIn("pass 2 of 2", message)

    def test_dropping_a_whole_t17_slot_is_caught_as_both_passes(self):
        """The HIGH-side stile's slot, for the reason above: the low-side
        pass 1 is the T17 section's first block and carries its spindle
        start, so deleting it is now two findings, not one."""
        text = drop(self.wdc_text, "G1 Z0.4062 F150.", nth=1)
        text = drop(text, "G1 Z0.3125 F150.", nth=1)
        problems = self.problems(text, self.wdc_cfg, self.wdc_expected)
        self.assertEqual([v.code for v in problems], ["missing-cut", "missing-cut"])
        self.assertTrue(all("high-side stile" in v.message for v in problems), problems)

    def test_every_refusal_names_the_part_the_feature_and_where(self):
        for needle, nth in (
            ("Z-0.006 F150.", 0),
            ("Z0.15 F150.", 2),
            ("G1 Z0.55 F150.", 1),
        ):
            with self.subTest(cut=needle):
                problems = self.problems(drop(self.text, needle, nth))
                self.assertEqual(len(problems), 1)
                message = problems[0].message
                self.assertRegex(message, r"^W\d+ @\(-?\d+\.\d{4},-?\d+\.\d{4}\): ")
                self.assertRegex(message, r"x\[-?\d+\.\d{4}, -?\d+\.\d{4}\]")
                self.assertRegex(message, r"at Z-?[\d.]+\.")

    def test_a_cut_the_layout_never_asked_for_is_caught(self):
        """The manifest is exhaustive, so the check runs both ways."""
        lines = self.text.split("\r\n")
        anchor = occurrences(self.text, "Z-0.006 F150.")[0]
        start = anchor
        while not _PREPOSITION.match(lines[start]):
            start -= 1
        end = anchor
        while lines[end] != "G0 Z2.5":
            end += 1
        block = lines[start : end + 1]
        doubled = "\r\n".join(lines[: end + 1] + block + lines[end + 1 :])
        problems = self.problems(doubled)
        self.assertEqual([v.code for v in problems], ["extra-cut"])
        self.assertIn("not a cut this sheet's layout calls for", problems[0].message)
        self.assertGreater(problems[0].line, 0, "an extra cut has a line to point at")

    def test_the_deletions_really_are_deletions(self):
        """Guard on the surgery itself: a mutation that changed nothing
        would make every test above pass for the wrong reason."""
        for needle, nth in (
            ("Z-0.006 F150.", 0),
            ("Z0.15 F150.", 2),
            ("Z-0.002 F100.", 2),
            ("G1 Z0.55 F150.", 1),
        ):
            with self.subTest(cut=needle):
                text = drop(self.text, needle, nth)
                self.assertLess(len(text), len(self.text))
                self.assertEqual(
                    len(occurrences(text, needle)),
                    len(occurrences(self.text, needle)) - 1,
                )


class ExpectedNoneIsUnchangedTest(unittest.TestCase):
    """No layout exists for a file the shop already cut, so the old
    behaviour has to survive exactly."""

    NAMES = ("R710101N", "R720101N", "R730101N")

    def test_the_reference_files_still_verify_with_no_manifest(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                path = os.path.join(NC_DIR, f"{name}.anc")
                self.assertEqual([str(v) for v in verify_file(path)], [])
                self.assertEqual([str(v) for v in verify_file(path, None, None)], [])

    def test_passing_no_manifest_never_reports_a_missing_or_extra_cut(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                with open(os.path.join(NC_DIR, f"{name}.anc"), "r", newline="") as fh:
                    text = fh.read()
                codes = {v.code for v in verify(text, default_config())}
                self.assertNotIn("missing-cut", codes)
                self.assertNotIn("extra-cut", codes)

    def test_an_empty_manifest_cannot_be_built_by_accident(self):
        """A manifest that quietly came back empty would turn the whole
        check off, so an empty sheet raises instead."""
        with self.assertRaises(ValueError):
            expected_work([])
        self.assertEqual(len(ExpectedWork()), 0)

    def test_a_frame_the_geometry_engine_rejects_has_no_manifest(self):
        result, _config = nested_sample()
        layout = result.unique_sheets[0][0]
        layout.placements[0].width = 2.0  # narrower than two stiles
        with self.assertRaises(ValueError):
            expected_work(layout)


class BuildJobRefusesAMissingCutTest(unittest.TestCase):
    """The gate itself: a sheet whose text lost a pass must not be written.

    ``generate`` is monkeypatched rather than the text tampered with after
    the fact, because what is being tested is that ``build_job`` compares the
    EMITTER's output against the layout — the emitter is the thing that could
    silently stop emitting a section.
    """

    def setUp(self):
        self.result, self.config = nested_sample()
        self.real_generate = job_module.generate

    def tearDown(self):
        job_module.generate = self.real_generate

    def sabotage(self, needle: str, nth: int = 0, only_where_present: bool = False):
        """Make the emitter drop one block from the text it returns.

        ``only_where_present`` is how the dry-run case is aimed: ``_render``
        emits the production program first and the lifted one second, and the
        two state different Z words, so a needle from the lifted table must
        leave the production text alone.
        """
        real = self.real_generate

        def sabotaged(program, plan, config=None):
            text = real(program, plan, config)
            if only_where_present and not occurrences(text, needle):
                return text
            return drop(text, needle, nth)

        job_module.generate = sabotaged

    def options(self, **overrides):
        return JobOptions(
            output_dir="unused", prefix="7201", created=CREATED, **overrides
        )

    def test_a_dropped_through_pass_is_refused_as_a_verifier_failure(self):
        self.sabotage("Z-0.006 F150.")
        job = build_job(self.result, self.options())
        outcome = job.outcomes[0]
        self.assertEqual(outcome.refusal_kind, "verifier")
        self.assertIsNone(outcome.text)
        self.assertTrue(
            any("missing-cut" in problem for problem in outcome.problems),
            outcome.problems,
        )

    def test_a_dropped_groove_is_refused(self):
        self.sabotage("G1 Z0.55 F150.", nth=1)
        job = build_job(self.result, self.options())
        self.assertEqual(job.outcomes[0].refusal_kind, "verifier")
        self.assertTrue(job.refused)

    def test_the_dry_run_rehearsal_is_held_to_the_same_manifest(self):
        """A dry-run file is generated from the LIFTED table, so its Z words
        differ; the cut it is missing is the same one."""
        self.sabotage("Z1.506 F150.", only_where_present=True)  # mirrored through pass
        job = build_job(self.result, self.options(dry_run=True))
        outcome = job.outcomes[0]
        self.assertEqual(outcome.refusal_kind, "verifier")
        self.assertTrue(
            any("missing-cut" in problem for problem in outcome.problems),
            outcome.problems,
        )
        self.assertTrue(
            any("the full-depth perimeter pass" in p for p in outcome.problems),
            outcome.problems,
        )

    def test_the_same_job_without_the_sabotage_is_written(self):
        job = build_job(self.result, self.options())
        self.assertEqual([o.describe() for o in job.refused], [])
        self.assertIsNotNone(job.outcomes[0].text)


class BuildJobRefusesAWrongFeedTest(unittest.TestCase):
    """The same gate, on the feed words (2026-08-04, owner-approved follow-up).

    Every reason the class above monkeypatches ``generate`` rather than
    tampering with the finished text applies unchanged: the thing that could
    silently go wrong is the EMITTER, so the mutation has to happen on the way
    out of it.  What differs is the size of the mutation — one number instead
    of a whole feature block.  A program that cuts every part in exactly the
    right place at the wrong feed used to reach the disk; the feed rule is
    what stops it, and this is the test that the JOB, not just ``verify``,
    stops it.

    The grammar itself is asserted in ``tests/test_post.py`` (section (f) of
    its docstring); this is only the gate.
    """

    def setUp(self):
        self.result, self.config = nested_sample()
        self.real_generate = job_module.generate

    def tearDown(self):
        job_module.generate = self.real_generate

    def retime(self, old: str, new: str):
        """Make the emitter state one wrong feed or speed in what it returns."""
        real = self.real_generate

        def sabotaged(program, plan, config=None):
            text = real(program, plan, config)
            assert old in text, f"{old!r} is not in the emitted text"
            return text.replace(old, new, 1)

        job_module.generate = sabotaged

    def options(self, **overrides):
        return JobOptions(
            output_dir="unused", prefix="7201", created=CREATED, **overrides
        )

    def test_a_perimeter_cut_at_the_wrong_feed_is_refused(self):
        self.retime("F498.2", "F900.")
        job = build_job(self.result, self.options())
        outcome = job.outcomes[0]
        self.assertEqual(outcome.refusal_kind, "verifier")
        self.assertIsNone(outcome.text)
        self.assertTrue(job.refused)
        self.assertTrue(
            any("[feed]" in problem for problem in outcome.problems), outcome.problems
        )
        self.assertTrue(
            any("T11" in p and "F498.2" in p for p in outcome.problems),
            outcome.problems,
        )

    def test_a_plunge_at_the_wrong_feed_is_refused(self):
        self.retime("G1 Z0.55 F150.", "G1 Z0.55 F490.")
        outcome = build_job(self.result, self.options()).outcomes[0]
        self.assertEqual(outcome.refusal_kind, "verifier")
        self.assertTrue(
            any("plunge/ramp into the cut" in p for p in outcome.problems),
            outcome.problems,
        )

    def test_a_wrong_spindle_speed_is_refused(self):
        self.retime("M13 S17000", "M13 S17500")
        outcome = build_job(self.result, self.options()).outcomes[0]
        self.assertEqual(outcome.refusal_kind, "verifier")
        self.assertTrue(
            any("[spindle-speed]" in p for p in outcome.problems), outcome.problems
        )

    def test_the_dry_run_rehearsal_is_held_to_the_same_feeds(self):
        """The lift changes Z words only, so a wrong feed is still wrong."""
        self.retime("F293.", "F900.")
        outcome = build_job(self.result, self.options(dry_run=True)).outcomes[0]
        self.assertEqual(outcome.refusal_kind, "verifier")
        self.assertTrue(
            any("T12" in p and "[feed]" in p for p in outcome.problems),
            outcome.problems,
        )

    def test_the_same_job_without_the_sabotage_is_written(self):
        job = build_job(self.result, self.options())
        self.assertEqual([o.describe() for o in job.refused], [])
        self.assertIsNotNone(job.outcomes[0].text)


# --------------------------------------------------------------------------
# 2026-08-04 review, fix 2: the manifest was an unordered multiset
# --------------------------------------------------------------------------


class CutOrderTest(unittest.TestCase):
    """Every required cut present, in an order that wrecks the sheet.

    ``missing-cut``/``extra-cut`` matched the file against the manifest as a
    multiset, so nothing at all enforced chronology.  Two orders that verified
    clean before this, both catastrophic on the machine:

    * cut every part THROUGH before taking any of them to the onion skin — the
      whole sheet is loose parts while the spindle is still working;
    * free a host before the frame nested in its opening — the inner is then
      sitting in a hole in a slab that is no longer attached to anything.

    And one that had never been stated at all: a part is only held while it is
    still attached, so its through pass has to be the LAST cut that touches it.

    The relations come off the :class:`ExpectedWork` manifest, which is built
    from the layout (an AST test in ``tests/test_post.py`` forbids the verifier
    importing the planner or the emitter), and they are judged on the line each
    matched cut appears on.  All three hold in R710101N, R720101N and R730101N —
    checked in :class:`ReferenceChronologyTest` below, off the files.
    """

    def setUp(self):
        self.result, self.config = nested_sample()
        self.layout = self.result.unique_sheets[0][0]
        self.cfg = post_config_for(self.config)
        self.program, self.plan = plan_sheet(
            self.layout,
            ProgramHeader(name="R990101N", created=CREATED),
            self.result.demand,
            self.config,
            self.cfg,
        )
        self.expected = expected_work(self.layout, self.cfg)

    def check(self, text, config=None):
        config = config or self.cfg
        return verify(text, config, expected_work(self.layout, config))

    def test_the_sheet_as_planned_is_in_order(self):
        text = generate(self.program, self.plan, self.cfg)
        self.assertEqual([str(v) for v in self.check(text)], [])

    def test_cutting_through_before_the_onion_skin_is_refused(self):
        """Both the plan lists AND the depths swap, so every owed cut is
        still there — at its own depth — and only the order is wrong."""
        from dataclasses import replace

        swapped_cfg = replace(
            self.cfg,
            perimeter_passes=(
                self.cfg.perimeter_passes[1],
                self.cfg.perimeter_passes[0],
            ),
        )
        swapped = CutPlan(
            panel=self.plan.panel,
            wdc_slot=self.plan.wdc_slot,
            openings=self.plan.openings,
            perimeter=[self.plan.perimeter[1], self.plan.perimeter[0]],
            detail=self.plan.detail,
            sections=self.plan.sections,
        )
        text = generate(self.program, swapped, swapped_cfg)
        problems = self.check(text)
        self.assertTrue(problems, "the multiset still matches, so only order can fail")
        self.assertEqual({v.code for v in problems}, {"cut-order"})
        self.assertEqual(len(problems), 3, "one per part on the sheet")
        first = problems[0]
        self.assertIn("runs BEFORE the onion-skin pass", first.message)
        self.assertIn("loose under a moving spindle", first.message)
        self.assertRegex(first.message, r"line \d+")

    def test_freeing_a_host_before_its_inner_is_refused(self):
        parts = self.program.flat_parts()
        canonical = [FeatureRef(i, "perimeter") for i in range(len(parts))]
        host_first = CutPlan(
            panel=self.plan.panel,
            wdc_slot=self.plan.wdc_slot,
            openings=self.plan.openings,
            perimeter=[canonical, list(canonical)],
            detail=self.plan.detail,
            sections=self.plan.sections,
        )
        text = generate(self.program, host_first, self.cfg)
        problems = self.check(text)
        self.assertEqual({v.code for v in problems}, {"cut-order"})
        self.assertEqual(len(problems), 1)
        self.assertIn("is nested in", problems[0].message)
        self.assertIn("no longer attached to the sheet", problems[0].message)

    def test_a_cut_after_the_part_is_free_is_refused(self):
        """The T13 grooves moved to the end: same cuts, part already loose."""
        late = CutPlan(
            panel=self.plan.panel,
            wdc_slot=self.plan.wdc_slot,
            openings=self.plan.openings,
            perimeter=self.plan.perimeter,
            detail=self.plan.detail,
            sections=(
                SECTION_OPENINGS,
                SECTION_DETAIL,
                SECTION_PERIMETER,
                SECTION_PANEL,
            ),
        )
        text = generate(self.program, late, self.cfg)
        problems = [v for v in self.check(text) if v.code == "cut-order"]
        self.assertEqual(len(problems), 12, "four grooves on each of three parts")
        self.assertIn("AFTER the full-depth perimeter pass", problems[0].message)
        self.assertIn("nothing holds it in place", problems[0].message)

    def test_the_manifest_carries_the_structure_the_rules_need(self):
        parts = {cut.part for cut in self.expected.cuts}
        self.assertEqual(parts, {0, 1, 2})
        hosts = {cut.part: cut.host for cut in self.expected.cuts}
        self.assertEqual(hosts, {0: None, 1: 0, 2: None}, "part 1 is nested in part 0")
        passes = {
            cut.pass_position for cut in self.expected.cuts if cut.kind == "perimeter"
        }
        self.assertEqual(passes, {0, 1})
        self.assertEqual(
            {cut.pass_position for cut in self.expected.cuts if cut.kind != "perimeter"},
            {None},
        )

    def test_a_missing_cut_is_not_also_reported_as_an_ordering_problem(self):
        """One fault, one finding: the relation whose half is gone is skipped."""
        text = generate(self.program, self.plan, self.cfg)
        dropped = drop(text, "Z-0.006 F150.")
        codes = [v.code for v in self.check(dropped)]
        self.assertIn("missing-cut", codes)
        self.assertNotIn("cut-order", codes)

    def test_the_dry_run_is_held_to_the_same_order(self):
        air = dry_run_config(self.cfg)
        swapped = CutPlan(
            panel=self.plan.panel,
            wdc_slot=self.plan.wdc_slot,
            openings=self.plan.openings,
            perimeter=[self.plan.perimeter[1], self.plan.perimeter[0]],
            detail=self.plan.detail,
            sections=self.plan.sections,
        )
        from dataclasses import replace

        swapped_air = replace(
            air, perimeter_passes=(air.perimeter_passes[1], air.perimeter_passes[0])
        )
        text = generate(self.program, swapped, swapped_air)
        problems = [v for v in self.check(text, air) if v.code == "cut-order"]
        self.assertTrue(problems, [str(v) for v in self.check(text, air)])

    def test_build_job_refuses_a_sheet_whose_passes_are_out_of_order(self):
        """The gate, not just the rule: nothing gets written."""
        real = job_module.plan_sheet

        def swap(*args, **kwargs):
            program, plan = real(*args, **kwargs)
            return program, CutPlan(
                panel=plan.panel,
                wdc_slot=plan.wdc_slot,
                openings=plan.openings,
                perimeter=[plan.perimeter[0], list(reversed(plan.perimeter[1]))],
                detail=plan.detail,
                sections=plan.sections,
            )

        job_module.plan_sheet = swap
        try:
            outcome = build_job(
                self.result,
                JobOptions(output_dir="unused", prefix="7201", created=CREATED),
            ).outcomes[0]
        finally:
            job_module.plan_sheet = real
        self.assertEqual(outcome.refusal_kind, "verifier")
        self.assertIsNone(outcome.text)
        self.assertTrue(
            any("[cut-order]" in p for p in outcome.problems), outcome.problems
        )


class ReferenceChronologyTest(unittest.TestCase):
    """Do the files the shop already cut satisfy the three ordering rules?

    They have no layout behind them, so no manifest — the relations are checked
    straight off each file instead, from what :func:`reconstruct` recovers:
    which part each cut belongs to, which frame is nested in which, and the
    order the sections run in.  This is the "validate first" half of fix 2: a
    rule the references break is a rule that would have to be weakened, and
    none of these is.
    """

    NAMES = ("R710101N", "R720101N", "R730101N")

    def timeline(self, plan):
        """``[(part index, what, perimeter pass or None)]`` in file order."""
        out = []
        for section in plan.sections:
            if section == SECTION_PANEL:
                out.extend((r.part, f"T13 groove {r.index}", None) for r in plan.panel)
            elif section == SECTION_WDC_SLOT:
                out.extend((r.part, f"T17 slot {r.index}", None) for r in plan.wdc_slot)
            elif section == SECTION_OPENINGS:
                out.extend((r.part, f"T11 opening {r.index}", None) for r in plan.openings)
            elif section == SECTION_DETAIL:
                out.extend(
                    (r.part, f"T12 detail {r.index}", None) for r in plan.detail_order()
                )
            elif section == SECTION_PERIMETER:
                for position, refs in enumerate(plan.perimeter):
                    out.extend((r.part, f"perimeter {position}", position) for r in refs)
        return out

    def test_every_reference_satisfies_all_three_rules(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                program, plan = reconstruct(os.path.join(NC_DIR, f"{name}.anc"))
                parts = program.flat_parts()
                last = len(plan.perimeter) - 1
                host_of = {
                    parts.index(child): i
                    for i, part in enumerate(parts)
                    for child in part.children
                }
                onion, through, others = {}, {}, {}
                for order, (part, what, position) in enumerate(self.timeline(plan)):
                    if position == last:
                        through[part] = order
                    elif position == 0:
                        onion[part] = order
                    else:
                        others.setdefault(part, []).append((order, what))

                self.assertEqual(len(through), len(parts), "every part is freed")
                for part, order in onion.items():
                    self.assertLess(order, through[part], f"(a) part {part}")
                for part, order in through.items():
                    if part in host_of:
                        self.assertLess(
                            order, through[host_of[part]], f"(b) inner {part}"
                        )
                for part, order in through.items():
                    for when, what in others.get(part, ()):
                        self.assertLess(when, order, f"(c) part {part}: {what}")

    def test_the_nested_reference_really_exercises_rule_b(self):
        """R720101N is the sheet with frames inside frames, so rule (b) is
        not vacuous above."""
        program, _plan = reconstruct(os.path.join(NC_DIR, "R720101N.anc"))
        self.assertTrue(any(part.children for part in program.parts))


if __name__ == "__main__":
    unittest.main()
