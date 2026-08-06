"""The verifier's expected-work manifest: MISSING cuts (2026-08-04 review).

Everything the verifier checked before this could only ever answer "does
this program do something it must not?".  These tests are the other half —
"does it do everything it must?" — and they are mutation tests on purpose:
a check that only ever sees good files proves nothing.  Each one takes a
sheet the post generates and verifies clean, deletes ONE thing a machinist
would notice missing, and requires the verifier to refuse it:

  * the full-depth perimeter pass of one part (the machine hands back a sheet
    with that frame still attached, and every older rule passes the file);
  * one opening's T11 through-cut and T12 finish pass (the verifier used to
    read that area as solid frame and agree with itself);
  * one T17 slot pass on a WDC stile;
  * one T13 panel groove;
  * an onion-skin pass that is missing, and one whose Z word has been changed
    to the through depth — the same count of loops at the wrong depths.

Which post table each mutation is judged against matters since the 2026-08-05
amendments (Scott, job R0805): a GENERATED sheet has no onion skin and cuts both
T11 operations as max-bite ladders — at most 0.4 of material per pass
(:func:`~faceframe_cnc.post.from_layout.generated_post_passes`,
:func:`~faceframe_cnc.post.from_layout.generated_opening_passes`) — while the
reference programs the shop already ran are in the measured dialect, with one
0.60 opening bite and a 0.06 onion skin, and the verifier must go on judging them
exactly as before.  So the two mutations above that are about the onion skin
build their sheet with the measured table (:func:`two_pass_sheet_under_test`) and
everything else uses the sheet as it is really cut.  The manifest owes what the table in hand
configures, which is asserted directly in
:class:`UntouchedSheetsPassTest`.

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
from dataclasses import replace

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
    SECTION_RELEASE,
    SECTION_WDC_SLOT,
)
from faceframe_cnc.post.verifier import ExpectedWork, expected_work
from tests.test_nc_job import CREATED, job_for, nested_sample, wdc_sheet
from tests.test_post import assert_only_legacy_grooves

NC_DIR = os.path.join(os.path.dirname(__file__), "..", "reference", "nc_files")

#: A feature block opens either with the section's first preposition (the one
#: that switches the spindle on) or with a later one, and closes with the
#: retract to the rapid plane.  Both forms restate X and Y absolutely, so
#: removing a whole block leaves the next one landing where it always did.
_FIRST_PREPOSITION = re.compile(r"^G0 G54 G90 X-?[\d.]+ Y-?[\d.]+ M13 S\d+$")
_PREPOSITION = re.compile(r"^X-?[\d.]+ Y-?[\d.]+ Z2\.5$")


def sheet_under_test(result, config):
    """``(layout, text, post config, manifest)`` for one generated sheet.

    One perimeter pass, because that is what a generated sheet is cut with
    since the 2026-08-05 amendment (Scott, job R0805 —
    :func:`~faceframe_cnc.post.from_layout.generated_post_passes`).
    """
    layout = result.unique_sheets[0][0]
    outcome = job_for(result).outcomes[0]
    assert outcome.ok, outcome.describe()
    cfg = post_config_for(config)
    return layout, outcome.text, cfg, expected_work(layout, cfg)


def two_pass_sheet_under_test(result, config):
    """The same, cut with the MEASURED T11 dialect.

    A generated sheet runs the 2026-08-05 max-bite ladder now, but the verifier
    still has to catch a program that is missing its onion skin — every reference
    file has one, and the rule that judges it must go on being exercised.  So the
    mutation tests that are ABOUT the skin build their sheet with the measured
    T11 dialect (:func:`~faceframe_cnc.post.model.default_config`) instead of the
    generated one: same layout, same planner, same emitter, but the measured
    passes AND the measured tools, because a two-pass 0.06/-0.006 ladder under a
    0.4 bite limit is not a table anything would emit — the limit is what turns
    the perimeter into a 0.372/-0.006 ladder in the first place, and ``max-bite``
    would (rightly) refuse the combination.  The plan follows the table it is
    handed (:func:`~faceframe_cnc.post.from_layout.cut_plan_for`), so two passes
    is what it plans.
    """
    layout = result.unique_sheets[0][0]
    measured = default_config()
    cfg = replace(
        post_config_for(config),
        tools=measured.tools,
        openings_passes=measured.openings_passes,
        perimeter_passes=measured.perimeter_passes,
    )
    program, plan = plan_sheet(
        layout,
        ProgramHeader(name="R990101N", created=CREATED),
        result.demand,
        config,
        cfg,
    )
    assert len(plan.perimeter) == 2, "the measured table plans two passes"
    return layout, generate(program, plan, cfg), cfg, expected_work(layout, cfg)


#: The violation codes that come from the expected-work manifest, as opposed to
#: the rules that judge a file on its own.  See :meth:`MissingCutTest.problems`.
MANIFEST_CODES = ("missing-cut", "extra-cut", "cut-order")


def occurrences(text: str, needle: str) -> list[int]:
    """Line indices (0-based) of every FEATURE-OPENING line containing ``needle``.

    A feature opens with a ``G1`` — a straight plunge (``G1 Z0.55 F150.``) or a
    loop's lead-in ramp (``G1 X15. Z0.15 F150.``) — and this walk is how the
    mutations below find the block they are about to delete, so it must land on
    one of those and nothing else.

    The ``G1`` filter is load bearing since the 2026-08-05 amendment (Scott, job
    R0805): a tab lift's descent back to depth restates the same Z and the same
    entry feed as the lead-in that opened the loop, deliberately (spec §3b — it
    IS a lead-in, into the far side of the tab), so ``"Z-0.002 F100."`` now
    matches once per loop PLUS once per tab on it.  Without the filter the "nth"
    of a needle means whichever tab happens to be third, and the surgery below
    cuts the wrong block.
    """
    return [
        i
        for i, line in enumerate(text.split("\r\n"))
        if needle in line and line.startswith("G1 ")
    ]


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
        """Three parts, one opening each, no WDC: 12 grooves, 3 T12 finish
        passes, and TWO T11 loops per opening and per part footprint — the two
        rungs of the 2026-08-05 max-bite ladder (Scott: at most 0.4 of material
        per T11 pass)."""
        result, config = nested_sample()
        _layout, _text, cfg, expected = sheet_under_test(result, config)
        self.assertEqual(len(cfg.openings_passes), 2, "the T11 ladder has two rungs")
        self.assertEqual(len(cfg.perimeter_passes), 2)
        self.assertEqual(
            expected.counts(),
            {"groove": 12, "opening": 6, "detail": 3, "perimeter": 6},
        )
        self.assertEqual(len(expected), 27)

    def test_the_pass_counts_follow_the_table_they_are_given(self):
        """The manifest owes what the CONFIG configures and nothing else.

        Three tables, three answers, no number written into the verifier: the
        measured two-pass dialect the reference programs are judged against, the
        generated sheet's max-bite ladder, and a table stripped back to a single
        perimeter pass.  Openings the same way — the measured table roughs an
        opening once and the ladder twice.
        """
        result, config = nested_sample()
        _l, _t, generated, laddered = sheet_under_test(result, config)
        _l2, _t2, two_pass, doubled = two_pass_sheet_under_test(result, config)
        single_cfg = replace(
            two_pass, perimeter_passes=two_pass.perimeter_passes[-1:]
        )
        single = expected_work(_l, single_cfg)
        parts = 3
        for cfg, manifest in (
            (generated, laddered),
            (two_pass, doubled),
            (single_cfg, single),
        ):
            with self.subTest(perimeter_passes=len(cfg.perimeter_passes)):
                self.assertEqual(
                    manifest.counts()["perimeter"],
                    parts * len(cfg.perimeter_passes),
                )
                self.assertEqual(
                    manifest.counts()["opening"],
                    parts * len(cfg.openings_passes),
                )
        self.assertEqual(
            [len(generated.perimeter_passes), len(two_pass.perimeter_passes),
             len(single_cfg.perimeter_passes)],
            [2, 2, 1],
        )
        self.assertEqual(
            [len(generated.openings_passes), len(two_pass.openings_passes)],
            [2, 1],
            "the ladder is what makes an opening owe two T11 loops",
        )

    def test_the_two_pass_sheet_also_passes_with_its_own_manifest(self):
        """The fixture the skin mutations below start from has to be clean."""
        result, config = nested_sample()
        _layout, text, cfg, expected = two_pass_sheet_under_test(result, config)
        self.assertEqual([str(v) for v in verify(text, cfg, expected)], [])

    def test_a_wdc_owes_two_grooves_and_four_slot_passes(self):
        result, config = wdc_sheet()
        _layout, _text, _cfg, expected = sheet_under_test(result, config)
        self.assertEqual(
            expected.counts(),
            # two rail grooves (the stiles take T17 instead), both bites of both
            # slots, and two T11 loops each for the opening and the footprint —
            # the rungs of the 2026-08-05 max-bite ladder
            {"groove": 2, "slot": 4, "opening": 2, "detail": 1, "perimeter": 2},
        )


class MissingCutTest(unittest.TestCase):
    """One deletion per test; every one of them must be refused."""

    @classmethod
    def setUpClass(cls):
        cls.result, cls.config = nested_sample()
        cls.layout, cls.text, cls.cfg, cls.expected = sheet_under_test(
            cls.result, cls.config
        )
        # the same sheet in the references' two-pass dialect, for the two
        # mutations that are about the onion skin a generated sheet no longer
        # cuts (see two_pass_sheet_under_test)
        (
            cls.two_pass_layout,
            cls.two_pass_text,
            cls.two_pass_cfg,
            cls.two_pass_expected,
        ) = two_pass_sheet_under_test(cls.result, cls.config)
        cls.wdc_result, cls.wdc_config = wdc_sheet()
        (
            cls.wdc_layout,
            cls.wdc_text,
            cls.wdc_cfg,
            cls.wdc_expected,
        ) = sheet_under_test(cls.wdc_result, cls.wdc_config)

    def problems(self, text, cfg=None, expected=None):
        """The MANIFEST's findings on ``text`` — what this class is about.

        Since the 2026-08-05 amendment a deleted through pass is caught by three
        independent authorities, not one: the manifest says the cut is missing,
        the hold invariant says the release cuts that profile's tabs owed now
        have no tab to remove, and (when a whole opening goes) the foreign-cut
        rule says a through cut is running inside what it now reads as solid
        frame.  That is the design working — three re-derivations of the same
        sheet disagreeing with the file — but these tests are the manifest's, so
        they read the manifest's codes.  :class:`HoldInvariantTest` in
        ``tests/test_post.py`` is where the other voice is asserted.
        """
        return [
            v
            for v in verify(text, cfg or self.cfg, expected or self.expected)
            if v.code in MANIFEST_CODES
        ]

    def test_dropping_the_full_depth_perimeter_pass_is_caught(self):
        """The pass that frees a part, gone: nothing else says so.

        The SECOND part's loop, not the first, for the reason the onion-skin
        test below has always given: the section's first feature block is also
        the one that starts the spindle and applies the tool length comp, so
        deleting that one is a different finding.

        Before the 2026-08-05 amendment a generated sheet's Z0.06 skin loop
        still recovered the part, so every older rule passed the mutated file;
        with one pass configured the part is simply not recovered at all, and
        the older rules still have nothing to say — which is the same gap, and
        the manifest is still what closes it.
        """
        text = drop(self.text, "Z-0.006 F150.", nth=1)
        # 2026-08-05: the manifest is no longer the ONLY thing that notices.  The
        # part's tabs are still standing and its release cuts are still in the
        # program, so the hold invariant refuses the file with no manifest at all
        # — a second, independent statement of the same deletion.
        self.assertTrue([v for v in verify(text, self.cfg) if v.code == "hold"])
        problems = self.problems(text)
        self.assertEqual([v.code for v in problems], ["missing-cut"])
        message = problems[0].message
        self.assertIn("the full-depth perimeter pass", message)
        self.assertIn("still attached to the", message)
        self.assertNotIn(
            "onion skin",
            message,
            "a one-pass program has no skin to send the operator looking for",
        )

    def test_dropping_the_onion_skin_pass_is_caught_too(self):
        """The SECOND part's skin pass, not the first (2026-08-04 follow-up).

        A section's first feature block is also the one that starts the
        spindle (``M13 S16700``) and applies the tool length comp, so deleting
        it now trips the new ``spindle-speed`` rule as well — correctly, but
        it would make this test about two findings instead of the one it is
        for.  Any part's skin pass proves the same point.

        Judged against the measured TWO-pass table: a generated sheet has no
        skin pass to drop since the 2026-08-05 amendment, but the reference
        programs do, and this rule is what would catch one of them losing it.
        """
        text = drop(self.two_pass_text, "Z0.06 F150.", nth=1)
        self.assertEqual([str(v) for v in verify(text, self.two_pass_cfg)], [])
        problems = self.problems(text, self.two_pass_cfg, self.two_pass_expected)
        self.assertEqual([v.code for v in problems], ["missing-cut"])
        self.assertIn("the onion-skin perimeter pass", problems[0].message)

    def test_an_onion_skin_pass_cut_at_the_through_depth_is_caught(self):
        """Same number of loops, wrong Z semantics: the skin that holds
        every part is gone even though nothing was deleted.

        (This one the older rules DO shout about as well — a through cut on
        the skin pass's wider profile sweeps into its neighbours — but the
        two cuts of the manifest are the ones that name the pass.)

        The two-pass table again, for the reason above.
        """
        text = self.two_pass_text.replace("Z0.06 F150.", "Z-0.006 F150.", 1)

        def problems():
            return self.problems(text, self.two_pass_cfg, self.two_pass_expected)

        codes = {v.code for v in problems()}
        self.assertIn("missing-cut", codes)
        self.assertIn("extra-cut", codes)
        missing = [v for v in problems() if v.code == "missing-cut"]
        self.assertEqual(len(missing), 1)
        self.assertIn("the onion-skin perimeter pass", missing[0].message)

    def test_dropping_both_of_an_openings_passes_is_caught(self):
        """T11 and T12 gone: the verifier used to read the area as solid."""
        text = drop(self.text, "Z0.15 F150.", nth=2)
        text = drop(text, "Z-0.002 F100.", nth=2)
        # As above: since the amendment the opening's own release cuts are left
        # milling material nothing opened, so the file is refused on its own too.
        self.assertTrue([v for v in verify(text, self.cfg) if v.code == "hold"])
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
            ("Z-0.006 F150.", 1),
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
        """The manifest is exhaustive, so the check runs both ways.

        The second perimeter loop, not the first: the first is the section's
        spindle-start block, and its preposition is the ``G0 G54 G90 ... M13``
        form rather than the plain ``X.. Y.. Z2.5`` one this surgery scans back
        to, so doubling it would duplicate a chunk of the previous section too.
        """
        lines = self.text.split("\r\n")
        anchor = occurrences(self.text, "Z-0.006 F150.")[1]
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
            ("Z-0.006 F150.", 1),
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

    def test_the_reference_files_verify_the_same_with_no_manifest(self):
        """Whatever the manifest-free verdict IS, it is unchanged by passing
        ``None`` explicitly.

        Since the 2026-08-05 amendment that verdict is no longer "clean" for
        two of the three: they carry their own pre-amendment T13 groove
        overruns, pinned in ``tests/test_post.py``'s
        ``LEGACY_GROOVE_FOREIGN_CUTS``.  What this class is about — the
        no-manifest path behaving exactly as it did — is asserted against that
        list rather than against an empty one.
        """
        for name in self.NAMES:
            with self.subTest(name=name):
                path = os.path.join(NC_DIR, f"{name}.anc")
                assert_only_legacy_grooves(self, name, verify_file(path))
                self.assertEqual(
                    [str(v) for v in verify_file(path, None, None)],
                    [str(v) for v in verify_file(path)],
                )

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

    Rule (a) needs a non-final perimeter pass to be about, and since the
    2026-08-05 max-bite amendment a generated sheet has one again — the ladder's
    roughing rung — so it is tested BOTH on the sheet as it is really cut and on
    the measured two-pass table, whose first pass is the onion skin the reference
    programs contain.  The rule reads the same in both cases and its message
    names whichever pass the table actually configures.  A table with a single
    perimeter pass leaves the rule nothing to relate, and that silent branch is
    tested too.  Rules (b) and (c) are tested on the sheet as it is really cut.
    """

    def setUp(self):
        from dataclasses import replace

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
        # The measured T11 dialect: the measured passes AND the measured tools,
        # for the reason two_pass_sheet_under_test gives — a 0.06/-0.006 pair
        # under a 0.4 bite limit is a table ``max-bite`` refuses.
        measured = default_config()
        self.two_pass_cfg = replace(
            self.cfg,
            tools=measured.tools,
            openings_passes=measured.openings_passes,
            perimeter_passes=measured.perimeter_passes,
        )
        # A genuinely SINGLE-pass table, which no generated sheet is cut with any
        # more: the measured through pass alone, and no bite limit to ladder it.
        self.one_pass_cfg = replace(
            self.cfg,
            tools=measured.tools,
            openings_passes=measured.openings_passes,
            perimeter_passes=measured.perimeter_passes[-1:],
        )
        self.one_pass_plan = plan_sheet(
            self.layout,
            ProgramHeader(name="R990101N", created=CREATED),
            self.result.demand,
            self.config,
            self.one_pass_cfg,
        )[1]
        self.two_pass_plan = plan_sheet(
            self.layout,
            ProgramHeader(name="R990101N", created=CREATED),
            self.result.demand,
            self.config,
            self.two_pass_cfg,
        )[1]

    def check(self, text, config=None):
        config = config or self.cfg
        return verify(text, config, expected_work(self.layout, config))

    def test_the_sheet_as_planned_is_in_order(self):
        text = generate(self.program, self.plan, self.cfg)
        self.assertEqual([str(v) for v in self.check(text)], [])
        self.assertEqual(
            len(self.plan.perimeter), 2, "one list per rung of the max-bite ladder"
        )

    def test_a_single_pass_table_has_no_pass_order_to_get_wrong(self):
        """Rule (a) is silent on a single-pass config, and only rule (a).

        No generated sheet is cut that way any more — the 2026-08-05 max-bite
        ladder gave the perimeter two rungs — but the silent branch still has to
        be right, so it is exercised on a table with the through pass alone.
        There is no pair of passes to relate, so the rule has nothing to demand,
        and nothing is quietly weakened by that: the same sheet with its host
        freed before its inner is still refused (the test below), and so is a cut
        that lands after the pass that frees the part.
        """
        expected = expected_work(self.layout, self.one_pass_cfg)
        positions = {
            cut.pass_position for cut in expected.cuts if cut.kind == "perimeter"
        }
        self.assertEqual(positions, {0}, "the one pass IS the through pass")
        text = generate(self.program, self.one_pass_plan, self.one_pass_cfg)
        self.assertEqual([v.code for v in self.check(text, self.one_pass_cfg)], [])

    def test_cutting_through_before_the_onion_skin_is_refused(self):
        """Both the plan lists AND the depths swap, so every owed cut is
        still there — at its own depth — and only the order is wrong.

        The measured two-pass table, since that is the one with a skin pass to
        run out of order (class docstring).
        """
        cfg = self.two_pass_cfg
        swapped_cfg = replace(
            cfg,
            perimeter_passes=(cfg.perimeter_passes[1], cfg.perimeter_passes[0]),
        )
        plan = self.two_pass_plan
        # ``replace`` rather than a fresh CutPlan so that the holding tabs and the
        # release section ride along untouched (2026-08-05): this test's point is
        # that ONLY the order is wrong, and a sheet that lost its tabs on the way
        # would be refused by the hold invariant for a second, different reason.
        swapped = replace(plan, perimeter=[plan.perimeter[1], plan.perimeter[0]])
        text = generate(self.program, swapped, swapped_cfg)
        problems = self.check(text, cfg)
        self.assertTrue(problems, "the multiset still matches, so only order can fail")
        self.assertEqual({v.code for v in problems}, {"cut-order"})
        self.assertEqual(len(problems), 3, "one per part on the sheet")
        first = problems[0]
        self.assertIn("runs BEFORE the onion-skin perimeter pass", first.message)
        self.assertIn("leaves material holding the part", first.message)
        self.assertRegex(first.message, r"line \d+")

    def test_cutting_through_before_the_roughing_rung_is_refused(self):
        """The same rule on the sheet as it is really cut (2026-08-05).

        The max-bite ladder gave a generated sheet a non-final perimeter pass
        again, so rule (a) has a pair to relate here too — and the message names
        the roughing rung rather than an onion skin the program does not contain.
        """
        cfg = self.cfg
        swapped_cfg = replace(
            cfg,
            perimeter_passes=(cfg.perimeter_passes[1], cfg.perimeter_passes[0]),
        )
        swapped = replace(
            self.plan, perimeter=[self.plan.perimeter[1], self.plan.perimeter[0]]
        )
        text = generate(self.program, swapped, swapped_cfg)
        found = self.check(text, cfg)
        self.assertTrue(found, "the multiset still matches, so only order can fail")
        # TWO independent authorities refuse this file, which is the design
        # working rather than a duplicate: the manifest's chronology rule says the
        # passes are in the wrong order, and ``max-bite`` — which re-derives the
        # ladder from the text and needs no manifest — says the pass that ran
        # first therefore took the whole 0.756.
        self.assertEqual({v.code for v in found}, {"cut-order", "max-bite"})
        self.assertTrue(
            [v for v in verify(text, cfg) if v.code == "max-bite"],
            "and the bite rule says so with no manifest at all",
        )
        problems = [v for v in found if v.code == "cut-order"]
        self.assertEqual(len(problems), 3, "one per part on the sheet")
        message = problems[0].message
        self.assertIn("runs BEFORE perimeter roughing pass 1", message)
        self.assertNotIn(
            "onion skin",
            message,
            "a laddered program has no skin to send the operator looking for",
        )

    def test_freeing_a_host_before_its_inner_is_refused(self):
        parts = self.program.flat_parts()
        # The pinned entry side rides along with each ref (2026-08-05): a
        # perimeter ref built from scratch would drop it, and on a tabbed profile
        # the emitter refuses two passes entering on different edges.
        entry = {ref.part: ref.entry for ref in self.plan.perimeter[-1]}
        canonical = [
            FeatureRef(i, "perimeter", entry=entry.get(i)) for i in range(len(parts))
        ]
        # One list per configured rung (two since the max-bite ladder): the point
        # is the ORDER within the pass that cuts through, so every rung runs in
        # canonical order and the host's through cut lands before its inner's.
        host_first = replace(
            self.plan, perimeter=[list(canonical) for _ in self.cfg.perimeter_passes]
        )
        text = generate(self.program, host_first, self.cfg)
        problems = self.check(text)
        self.assertEqual({v.code for v in problems}, {"cut-order"})
        self.assertEqual(len(problems), 1)
        self.assertIn("is nested in", problems[0].message)
        self.assertIn("no longer attached to the sheet", problems[0].message)

    def test_a_cut_after_the_part_is_free_is_refused(self):
        """The T13 grooves moved to the end: same cuts, part already loose."""
        late = replace(
            self.plan,
            sections=(
                SECTION_OPENINGS,
                SECTION_DETAIL,
                SECTION_PERIMETER,
                # The release section stays where it belongs -- last -- so that
                # the sheet is still tab-held and this test is still about the
                # grooves and nothing else (2026-08-05 amendment).
                SECTION_RELEASE,
                SECTION_PANEL,
            ),
        )
        text = generate(self.program, late, self.cfg)
        problems = [v for v in self.check(text) if v.code == "cut-order"]
        after_free = [
            v for v in problems if "AFTER the full-depth perimeter pass" in v.message
        ]
        self.assertEqual(len(after_free), 12, "four grooves on each of three parts")
        self.assertIn("nothing holds it in place", after_free[0].message)
        # ... and one more, from the rule the 2026-08-05 amendment added: the
        # release section is the last machining in the program, so a T13 section
        # after it is refused on its own account too (spec §3c).
        last = [v for v in problems if "release section" in v.message]
        self.assertEqual(len(last), 1, [str(v) for v in problems])
        self.assertIn("last machining in the program", last[0].message)

    def test_the_manifest_carries_the_structure_the_rules_need(self):
        parts = {cut.part for cut in self.expected.cuts}
        self.assertEqual(parts, {0, 1, 2})
        hosts = {cut.part: cut.host for cut in self.expected.cuts}
        self.assertEqual(hosts, {0: None, 1: 0, 2: None}, "part 1 is nested in part 0")
        passes = {
            cut.pass_position for cut in self.expected.cuts if cut.kind == "perimeter"
        }
        self.assertEqual(passes, {0, 1}, "two rungs of the ladder, two positions")
        self.assertEqual(
            {
                cut.pass_position
                for cut in expected_work(self.layout, self.two_pass_cfg).cuts
                if cut.kind == "perimeter"
            },
            {0, 1},
            "and two positions against the measured two-pass table as well",
        )
        self.assertEqual(
            {
                cut.pass_position
                for cut in expected_work(self.layout, self.one_pass_cfg).cuts
                if cut.kind == "perimeter"
            },
            {0},
            "and one against a table with a single perimeter pass",
        )
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
        """The lift changes Z words only, so the order rules still bite.

        Shown on the two-pass table, where both rule (a) and the lift are in
        play at once; the single-pass dry run is exercised end to end by
        ``tests/test_nc_job.py``'s dry-run acceptance tests.
        """
        air = dry_run_config(self.two_pass_cfg)
        plan = self.two_pass_plan
        swapped = replace(plan, perimeter=[plan.perimeter[1], plan.perimeter[0]])
        swapped_air = replace(
            air, perimeter_passes=(air.perimeter_passes[1], air.perimeter_passes[0])
        )
        text = generate(self.program, swapped, swapped_air)
        problems = [v for v in self.check(text, air) if v.code == "cut-order"]
        self.assertTrue(problems, [str(v) for v in self.check(text, air)])

    def test_build_job_refuses_a_sheet_whose_passes_are_out_of_order(self):
        """The gate, not just the rule: nothing gets written.

        The freeing pass is the LAST one whatever the table configures, so
        reversing it is what puts a host in front of its passenger — which is
        rule (b), and is the mutation that still exists on a one-pass sheet.
        """
        real = job_module.plan_sheet

        def swap(*args, **kwargs):
            program, plan = real(*args, **kwargs)
            return program, replace(
                plan,
                perimeter=list(plan.perimeter[:-1])
                + [list(reversed(plan.perimeter[-1]))],
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
