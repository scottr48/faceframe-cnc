"""Milestone 5 phase 1: the .anc post, proved by round-tripping real files.

The acceptance test of this phase is the round trip: read a production NC
file back into a layout (part footprints, rotations, openings, which frame
is nested in which) plus a sequencing plan, regenerate the program from
that model, and diff against the original.  Everything numeric in the
regenerated file -- every coordinate, feed, speed, Z level, G/M word,
CRLF and the ``%`` wrappers -- is recomputed by the post from its measured
tables; a plan carries nothing but integers and enums (order, which
opening, which edge the tool led in on).

Covered here:
  (a) byte-identical round trip of R710101N (four plain frames),
      R720101N (THE nested-frames sheet) and R730101N (drawer frames);
  (b) the diff still holds when the date comment and O-number -- the two
      lines a round trip is allowed to normalise -- are changed, and
      nothing else moves with them;
  (c) what the reconstruction recovered (sizes, rotations, nesting) is
      what the shop says is on those sheets;
  (d) the independent verifier passes all three real files and catches
      hand-corrupted variants: too deep, off the sheet, cutting through a
      neighbouring part, and a broken footer;
  (e) the generation API phase 2 needs: build a program from packer
      placements, and reorder the perimeter passes (onion skin: pass 1
      everything, pass 2 inners before hosts);
  (f) the F/S grammar the reference files actually use, and the verifier's
      feed and spindle-speed rules against it (2026-08-04, owner-approved
      follow-up): the grammar is asserted off the files first, then a
      generated sheet is tampered with one number at a time -- a wrong
      cutting feed for each tool class, a wrong plunge feed, a wrong spindle
      speed, a spindle started with no speed at all, and the modal case
      where a cutting move silently inherits the plunge feed.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
import re
import unittest

from faceframe_cnc.nesting import Placement
from faceframe_cnc.post import (
    Box,
    CutPlan,
    FeatureRef,
    PartProgram,
    ProgramHeader,
    SheetProgram,
    default_config,
    generate,
    program_from_placements,
    reconstruct,
    verify,
    verify_file,
)
from faceframe_cnc.post.generator import fmt
from faceframe_cnc.post.reconstruct import ReconstructionError, reconstruct_text

NC_DIR = os.path.join(os.path.dirname(__file__), "..", "reference", "nc_files")

TOL = 1e-3  # the milestone's coordinate tolerance


def read(name: str) -> str:
    with open(os.path.join(NC_DIR, f"{name}.anc"), "r", newline="") as handle:
        return handle.read()


def path_of(name: str) -> str:
    return os.path.join(NC_DIR, f"{name}.anc")


class RoundTripTest(unittest.TestCase):
    """Reconstruct a real program, regenerate it, diff it."""

    def assert_round_trip(self, name: str) -> None:
        want = read(name)
        program, plan = reconstruct(path_of(name))
        got = generate(program, plan)

        want_lines = want.split("\r\n")
        got_lines = got.split("\r\n")
        self.assertEqual(
            len(got_lines),
            len(want_lines),
            f"{name}: line count {len(got_lines)} != {len(want_lines)}",
        )
        for number, (a, b) in enumerate(zip(want_lines, got_lines), start=1):
            self.assertEqual(b, a, f"{name} line {number}")
        self.assertEqual(got, want, f"{name}: byte diff outside the line list")

    def test_r710101n_round_trips_byte_for_byte(self):
        self.assert_round_trip("R710101N")

    def test_r720101n_nested_frames_round_trips_byte_for_byte(self):
        self.assert_round_trip("R720101N")

    def test_r730101n_drawer_frames_round_trips_byte_for_byte(self):
        self.assert_round_trip("R730101N")

    def test_only_the_date_and_o_number_may_differ(self):
        """The two lines a round trip is allowed to normalise, and no others."""
        want = read("R710101N").split("\r\n")
        program, plan = reconstruct(path_of("R710101N"))
        program.header = ProgramHeader(
            name=program.header.name,
            o_number=17,
            created="01 JAN 27 - 08:00",
            material_comment=program.header.material_comment,
            load_comment=program.header.load_comment,
        )
        got = generate(program, plan).split("\r\n")

        self.assertEqual(len(got), len(want))
        differing = [i for i, (a, b) in enumerate(zip(want, got)) if a != b]
        self.assertEqual(differing, [1, 2], f"unexpected differences: {differing}")
        self.assertEqual(got[1], "O0017 (R710101N)")
        self.assertEqual(got[2], "(CREATED ON 01 JAN 27 - 08:00)")

    def test_generated_text_keeps_crlf_and_percent_wrappers(self):
        program, plan = reconstruct(path_of("R710101N"))
        text = generate(program, plan)
        self.assertTrue(text.startswith("%\r\n"))
        self.assertTrue(text.endswith("M30\r\n%\r\n"))
        self.assertNotIn("\n", text.replace("\r\n", ""))


class ReconstructionTest(unittest.TestCase):
    """What the reconstruction says is on the sheet must be what is on it."""

    def sizes(self, program):
        return [
            (
                round(p.box.width, 4),
                round(p.box.height, 4),
                round(p.box.x0, 4),
                round(p.box.y0, 4),
                p.rotated,
            )
            for p in program.flat_parts()
        ]

    def test_r710101n_layout(self):
        program, _ = reconstruct(path_of("R710101N"))
        self.assertEqual(
            self.sizes(program),
            [
                (18.0, 30.0, 30.455, 0.0, False),
                (30.0, 30.0, 0.0, 0.0, True),
                (30.0, 30.0, 0.0, 30.455, True),
                (30.0, 12.0, 0.0, 60.91, False),
            ],
        )
        # every part is a single-opening wall frame, inset 1.5 all round
        for part in program.flat_parts():
            self.assertEqual(len(part.openings), 1)
            opening = part.openings[0]
            self.assertAlmostEqual(opening.width, part.box.width - 3.0, delta=TOL)
            self.assertAlmostEqual(opening.height, part.box.height - 3.0, delta=TOL)
        self.assertEqual([len(p.children) for p in program.parts], [0, 0, 0, 0])

    def test_r720101n_recovers_both_nested_frames(self):
        """18x30 inside a 27x42; 30x12 rotated to 12x30 inside a 24x42."""
        program, _ = reconstruct(path_of("R720101N"))
        hosts = program.parts
        self.assertEqual(len(hosts), 2, "both hosts should be top level")

        pairs = []
        for host in hosts:
            self.assertEqual(len(host.children), 1)
            inner = host.children[0]
            pairs.append(
                (
                    (round(host.box.width, 4), round(host.box.height, 4)),
                    (round(inner.box.width, 4), round(inner.box.height, 4)),
                    inner.rotated,
                )
            )
        self.assertIn(((27.0, 42.0), (18.0, 30.0), False), pairs)
        self.assertIn(((24.0, 42.0), (12.0, 30.0), True), pairs)

        # each inner sits inside its host's routed opening with clearance
        for host in hosts:
            inner = host.children[0]
            opening = host.openings[0]
            self.assertTrue(opening.contains(inner.box))
            for gap in (
                inner.box.x0 - opening.x0,
                opening.x1 - inner.box.x1,
                inner.box.y0 - opening.y0,
                opening.y1 - inner.box.y1,
            ):
                self.assertGreaterEqual(gap, 0.375 - TOL)

    def test_r730101n_rotations_match_the_shop_description(self):
        """3DB30 and B30 are the rotated pair; their T13 grooves say so."""
        program, _ = reconstruct(path_of("R730101N"))
        by_size = {
            (round(p.box.width, 4), round(p.box.height, 4), round(p.box.y0, 4)): p
            for p in program.flat_parts()
        }
        self.assertFalse(by_size[(18.0, 30.0, 0.0)].rotated)  # B18 upright
        self.assertTrue(by_size[(30.0, 30.0, 0.0)].rotated)  # 3DB30 rotated
        self.assertTrue(by_size[(30.0, 30.0, 30.455)].rotated)  # B30 rotated
        self.assertFalse(by_size[(24.0, 30.0, 60.91)].rotated)  # 3DB24 upright

        # the rotated B30's openings are the base-frame pattern, turned 90
        b30 = by_size[(30.0, 30.0, 30.455)]
        self.assertEqual(
            sorted((round(o.width, 4), round(o.height, 4)) for o in b30.openings),
            [(5.0, 27.0), (20.5, 27.0)],
        )

    def test_perimeter_runs_all_of_pass_one_before_pass_two(self):
        """The onion skin already exists in the references: every part is
        taken to Z0.06 before any part is cut through at Z-0.006."""
        config = default_config()
        self.assertEqual([p.z_cut for p in config.perimeter_passes], [0.06, -0.006])
        for name in ("R710101N", "R720101N", "R730101N"):
            program, plan = reconstruct(path_of(name))
            self.assertEqual(len(plan.perimeter), 2, name)
            first = [ref.part for ref in plan.perimeter[0]]
            second = [ref.part for ref in plan.perimeter[1]]
            self.assertEqual(len(first), len(program.flat_parts()), name)
            self.assertEqual(first, second, f"{name}: the two passes share an order")

    def test_r720101n_cuts_each_inner_before_its_host(self):
        program, plan = reconstruct(path_of("R720101N"))
        parts = program.flat_parts()
        hosts = {
            parts.index(child): parts.index(part)
            for part in parts
            for child in part.children
        }
        self.assertTrue(hosts)
        for pass_refs in plan.perimeter:
            order = [ref.part for ref in pass_refs]
            for inner, host in hosts.items():
                self.assertLess(
                    order.index(inner),
                    order.index(host),
                    "an inner must come free before its host",
                )

    def test_reconstruction_refuses_an_unknown_cut_depth(self):
        text = read("R710101N").replace("Z0.06 F150.", "Z0.09 F150.", 1)
        with self.assertRaises(ReconstructionError):
            reconstruct_text(text)

    def test_reconstruction_refuses_the_t2_roughing_style(self):
        """R620101N's older T2 section is deliberately not replicated."""
        with self.assertRaises(ReconstructionError):
            reconstruct(path_of("R620101N"))


class OddSixteenthRoundTripTest(unittest.TestCase):
    """2026-08-04 review, fix 9: an exact midpoint against a printed one.

    ``reconstruct`` compared everything at 1e-6 while the post can only PRINT
    four decimals.  Nothing halves a coordinate except an edge midpoint, which
    is where a lead-in lands — so a frame whose size is an odd number of
    sixteenths (a real thing: 30 1/16") put its opening's mid-x at 16.03125, the
    post printed ``X16.0312``, and reading the file back refused it as "not the
    midpoint of any edge" of a program it had just written itself.
    """

    def sheet(self, width, height):
        program = program_from_placements(
            [Placement("W3030", 0.0, 0.0, width, height, False, [])],
            ProgramHeader(name="R990113N", created=CREATED),
        )
        plan = CutPlan(
            panel=[FeatureRef(0, "groove", j) for j in range(4)],
            openings=[FeatureRef(0, "opening", 0)],
            perimeter=[[FeatureRef(0, "perimeter")], [FeatureRef(0, "perimeter")]],
        )
        return generate(program, plan)

    def test_an_odd_sixteenth_frame_round_trips_byte_for_byte(self):
        text = self.sheet(30.0625, 30.0625)
        # the lead-in lands on the midpoint of the right edge: 15.03125 exactly,
        # which the post can only print as four decimals.  That
        # half-ten-thousandth is the whole bug.
        self.assertIn("Y15.0312", text)
        self.assertEqual([str(v) for v in verify(text)], [])
        program, plan = reconstruct_text(text)
        self.assertEqual(generate(program, plan), text)

    def test_a_spread_of_odd_sixteenths_all_round_trip(self):
        """Sixteenths only, on purpose.

        A sixteenth is 0.0625 — four decimals, so it survives the post's own
        printing exactly, and only the MIDPOINTS of such a part land on the
        thirty-second that used to break the read-back.  A part measured in
        thirty-seconds is a different and much older limitation (its own
        dimensions cannot be printed at four decimals at all) and is not what
        this fix is about.
        """
        for width, height in (
            (30.0625, 30.0625),
            (24.1875, 36.3125),
            (18.5625, 30.0),
            (20.0625, 42.1875),
        ):
            with self.subTest(size=(width, height)):
                text = self.sheet(width, height)
                self.assertEqual([str(v) for v in verify(text)], [])
                program, plan = reconstruct_text(text)
                self.assertEqual(generate(program, plan), text)

    def test_the_tolerance_is_wide_enough_for_a_printed_coordinate(self):
        from faceframe_cnc.post.reconstruct import PLACES, PRINTED_TOL, TOL

        self.assertEqual(PLACES, 4)
        self.assertGreater(PRINTED_TOL, 0.5 * 10 ** -PLACES)
        self.assertLess(PRINTED_TOL, 0.001, "still far tighter than any real feature")
        self.assertLess(TOL, PRINTED_TOL, "printed-against-printed stays exact")

    def test_the_whole_numbers_the_references_use_are_unaffected(self):
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                program, plan = reconstruct(path_of(name))
                self.assertEqual(generate(program, plan), read(name))


class VerifierPositiveTest(unittest.TestCase):
    def test_reference_files_pass(self):
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                self.assertEqual([str(v) for v in verify_file(path_of(name))], [])

    def test_generated_files_pass(self):
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                program, plan = reconstruct(path_of(name))
                self.assertEqual([str(v) for v in verify(generate(program, plan))], [])

    def test_reference_files_stay_inside_the_measured_overhang(self):
        """0.375 past a part edge is the largest excursion in the files."""
        config = default_config()
        self.assertAlmostEqual(config.overhang, 0.375)
        for name in ("R710101N", "R720101N", "R730101N"):
            text = read(name)
            tightened = verify(text, config=_with(config, overhang=0.2))
            self.assertTrue(
                any(v.code == "bounds" for v in tightened),
                f"{name}: a 0.2 overhang should be too tight",
            )


def _with(config, **overrides):
    from dataclasses import replace

    return replace(config, **overrides)


class VerifierNegativeTest(unittest.TestCase):
    """Hand-corrupted variants of R710101N; each must be caught."""

    def codes(self, text) -> set:
        return {v.code for v in verify(text)}

    def test_cut_too_deep_is_caught(self):
        text = read("R710101N").replace("Z-0.006 F150.", "Z-0.2 F150.", 1)
        self.assertIn("z-limit", self.codes(text))

    def test_travel_too_high_is_caught(self):
        text = read("R710101N").replace("G43 H13 Z2.5", "G43 H13 Z4.5", 1)
        self.assertIn("z-limit", self.codes(text))

    def test_move_off_the_sheet_is_caught(self):
        text = read("R710101N").replace("X28.3025 F545.", "X58.3025 F545.", 1)
        self.assertIn("bounds", self.codes(text))

    def test_part_hanging_off_the_sheet_is_caught(self):
        """Shift one part's perimeter loops off the far edge of the sheet."""
        text = read("R710101N").replace("X48.6445", "X50.6445").replace(
            "X48.6425", "X50.6425"
        )
        codes = self.codes(text)
        self.assertTrue({"bounds", "part-bounds"} & codes, codes)

    def test_cut_through_a_neighbouring_part_is_caught(self):
        """Stretch one T12 opening loop 3 inches into the next part."""
        text = read("R710101N").replace("X32.055\r\n", "X29.055\r\n", 1)
        self.assertIn("foreign-cut", self.codes(text))

    def test_broken_footer_is_caught(self):
        text = read("R710101N").replace("G90 X24. Y96.", "G90 X24. Y95.", 1)
        self.assertIn("footer", self.codes(text))

    def test_broken_header_is_caught(self):
        text = read("R710101N").replace("(MATERIAL: MDF 3/4 )", "(MATERIAL: MDF 1/2 )", 1)
        self.assertIn("header", self.codes(text))

    def test_truncated_program_is_caught(self):
        text = read("R710101N").replace("M30\r\n%\r\n", "M30\r\n")
        self.assertTrue({"wrapper", "footer"} & self.codes(text))

    def test_lf_line_endings_are_caught(self):
        text = read("R710101N").replace("\r\n", "\n")
        self.assertIn("line-endings", self.codes(text))

    def test_an_invented_g_code_is_caught(self):
        text = read("R710101N").replace("G1 Z0.55 F150.", "G2 Z0.55 F150.", 1)
        self.assertIn("code", self.codes(text))

    def test_a_missing_section_close_is_caught(self):
        text = read("R710101N").replace("G17 G91 G28 Z0 M95\r\n", "", 1)
        self.assertIn("section", self.codes(text))


# --------------------------------------------------------------------------
# (f) feeds and spindle speeds (2026-08-04, owner-approved follow-up)
# --------------------------------------------------------------------------


class FeedAndSpeedGrammarTest(unittest.TestCase):
    """What the reference programs' F/S grammar IS.

    The verifier's feed rule was read off these three files, so the first
    thing worth asserting is that it describes them: every F word in them is
    one of the measured table's feeds, every S word one of its speeds, and
    each tool section states its speed exactly once.  If a future table entry
    is mistyped, this is the test that says the rule and the files have come
    apart — before the mutation tests below start passing for the wrong
    reason.
    """

    NAMES = ("R710101N", "R720101N", "R730101N")

    def specs(self):
        cfg = default_config()
        return (
            cfg.panel,
            cfg.wdc_slot,
            cfg.openings_pass,
            cfg.detail_pass,
            *cfg.perimeter_passes,
        )

    def test_the_measured_table_holds_the_feeds_the_files_use(self):
        cfg = default_config()
        self.assertEqual((cfg.panel.entry_feed, cfg.panel.cut_feed), (150.0, 490.0))
        self.assertEqual(
            (cfg.wdc_slot.entry_feed, cfg.wdc_slot.cut_feed), (150.0, 400.0)
        )
        self.assertEqual(
            (cfg.openings_pass.entry_feed, cfg.openings_pass.cut_feed), (150.0, 545.0)
        )
        self.assertEqual(
            (cfg.detail_pass.entry_feed, cfg.detail_pass.cut_feed), (100.0, 293.0)
        )
        for position, spec in enumerate(cfg.perimeter_passes):
            with self.subTest(pass_number=position + 1):
                self.assertEqual((spec.entry_feed, spec.cut_feed), (150.0, 498.2))

    def test_t11_really_does_use_two_cutting_feeds_in_one_program(self):
        """Why the rule is a SET of feeds per tool and not one number."""
        cfg = default_config()
        self.assertEqual(
            cfg.tools["openings"].number, cfg.tools["perimeter"].number, "both are T11"
        )
        self.assertNotEqual(
            cfg.openings_pass.cut_feed, cfg.perimeter_passes[0].cut_feed
        )
        for name in self.NAMES:
            with self.subTest(name=name):
                text = read(name)
                self.assertIn("F545.", text)
                self.assertIn("F498.2", text)

    def test_every_f_word_in_a_reference_file_is_a_table_feed(self):
        allowed = set()
        for spec in self.specs():
            allowed.add(round(spec.entry_feed, 4))
            allowed.add(round(spec.cut_feed, 4))
        for name in self.NAMES:
            with self.subTest(name=name):
                found = {
                    round(float(value), 4)
                    for value in re.findall(r"F(\d*\.?\d+)", read(name))
                }
                self.assertTrue(found, "the file states no feed at all")
                self.assertEqual(found - allowed, set())

    def test_every_s_word_in_a_reference_file_is_a_table_speed(self):
        speeds = {tool.speed for tool in default_config().tools.values()}
        for name in self.NAMES:
            with self.subTest(name=name):
                found = {int(value) for value in re.findall(r"\bS(\d+)\b", read(name))}
                self.assertTrue(found)
                self.assertEqual(found - speeds, set())

    def test_each_tool_section_states_its_speed_exactly_once_on_the_m13(self):
        for name in self.NAMES:
            lines = read(name).split("\r\n")
            heads = [i for i, line in enumerate(lines) if line.startswith("(ROUTE TOOL")]
            self.assertEqual(len(heads), 4, name)
            for position, head in enumerate(heads):
                end = heads[position + 1] if position + 1 < len(heads) else len(lines)
                body = lines[head:end]
                with_s = [line for line in body if re.search(r"\bS\d+\b", line)]
                with self.subTest(name=name, section=body[0]):
                    self.assertEqual(len(with_s), 1)
                    self.assertIn("M13", with_s[0], "the S rides on the spindle start")


class VerifierFeedTest(unittest.TestCase):
    """Mutation tests for the feed and spindle-speed rules.

    Every one of these files verified clean before one number in it was
    changed, and every changed number is one a real post table gives some
    other tool or some other kind of move — the mistake a hand edit or a
    mistyped table makes, not a mangled line the older rules would trip over
    on their own.  A refusal has to name the tool and say what the table says
    it must be, because the operator reading it has neither file open.
    """

    @classmethod
    def setUpClass(cls):
        program = sample_program()
        cls.text = generate(program, full_plan(program))
        wdc_program, wdc_plan = wdc_program_and_plan()
        cls.wdc_text = generate(wdc_program, wdc_plan)

    def test_the_sheets_under_test_verify_clean_to_begin_with(self):
        self.assertEqual([str(v) for v in verify(self.text)], [])
        self.assertEqual([str(v) for v in verify(self.wdc_text)], [])

    def refusals(self, text: str, code: str, source: str | None = None):
        original = self.text if source is None else source
        self.assertNotEqual(text, original, "the mutation changed nothing")
        problems = [v for v in verify(text) if v.code == code]
        self.assertTrue(problems, [str(v) for v in verify(text)])
        return problems

    def tamper(self, old: str, new: str, source: str | None = None) -> str:
        text = self.text if source is None else source
        self.assertIn(old, text)
        return text.replace(old, new, 1)

    # -- one wrong cutting feed per tool class ------------------------------

    def test_a_wrong_t11_perimeter_cutting_feed_is_caught(self):
        text = self.tamper("Y42.1895 F498.2", "Y42.1895 F900.")
        problems = self.refusals(text, "feed")
        message = problems[0].message
        self.assertIn("a cutting move", message)
        self.assertIn("T11", message)
        self.assertIn("F900.", message)
        self.assertIn("F498.2", message)
        self.assertIn("perimeter pass 1 of 2", message)

    def test_a_wrong_t11_opening_cutting_feed_is_caught(self):
        text = self.tamper("Y40.3025 F545.", "Y40.3025 F600.")
        message = self.refusals(text, "feed")[0].message
        self.assertIn("T11", message)
        self.assertIn("F545. (T11 opening through-cut)", message)

    def test_a_wrong_t12_cutting_feed_is_caught(self):
        text = self.tamper("Y40.4 F293.", "Y40.4 F490.")
        message = self.refusals(text, "feed")[0].message
        self.assertIn("T12", message)
        self.assertIn("F490.", message)
        self.assertIn("F293. (T12 opening finish pass)", message)

    def test_a_wrong_t13_cutting_feed_is_caught(self):
        text = self.tamper("Y42.375 F490.", "Y42.375 F545.")
        message = self.refusals(text, "feed")[0].message
        self.assertIn("T13", message)
        self.assertIn("F490. (T13 panel groove)", message)

    def test_a_wrong_t17_cutting_feed_is_caught(self):
        text = self.tamper("Y40.3438 F400.", "Y40.3438 F490.", self.wdc_text)
        problems = self.refusals(text, "feed", self.wdc_text)
        message = problems[0].message
        self.assertIn("T17", message)
        self.assertIn("F400. (T17 45-degree stile slot)", message)

    # -- entry feeds, speeds, and the modal case ---------------------------

    def test_a_wrong_plunge_feed_is_caught(self):
        """The T13 groove's plunge: a straight descent, so an ENTRY move."""
        text = self.tamper("G1 Z0.55 F150.", "G1 Z0.55 F490.")
        message = self.refusals(text, "feed")[0].message
        self.assertIn("a plunge/ramp into the cut", message)
        self.assertIn("entry feed for T13 is F150.", message)

    def test_a_wrong_ramp_in_feed_is_caught(self):
        """A profile loop's lead-in ramp descends too, so the same rule."""
        text = self.tamper("Y21. Z-0.002 F100.", "Y21. Z-0.002 F150.")
        message = self.refusals(text, "feed")[0].message
        self.assertIn("a plunge/ramp into the cut", message)
        self.assertIn("entry feed for T12 is F100.", message)

    def test_a_wrong_spindle_speed_is_caught(self):
        text = self.tamper("M13 S17000", "M13 S17500")
        message = self.refusals(text, "spindle-speed")[0].message
        self.assertIn("S17500 with T12 in the spindle", message)
        self.assertIn("runs T12 at S17000", message)

    def test_starting_the_spindle_without_a_speed_is_caught(self):
        text = self.tamper("M13 S16700", "M13")
        codes = {v.code for v in verify(text)}
        self.assertEqual(codes, {"spindle-speed"})
        messages = " | ".join(
            v.message for v in verify(text) if v.code == "spindle-speed"
        )
        self.assertIn("the spindle is started (M13) with no S word", messages)
        self.assertIn("T11", messages)

    def test_a_cutting_move_that_inherits_the_plunge_feed_is_caught(self):
        """The bug the modal rule exists for (2026-08-04 follow-up).

        Nothing is added and nothing looks wrong: the first cutting move of a
        loop simply loses its F word, so it — and the three corners, the
        return, the overshoot and the lead-out that follow it — run at the
        F150 the ramp left behind.  A 545-ipm cut done at 150 is a burnt bit,
        and the only way to see it in the text is to track modality.
        """
        text = self.tamper("Y40.3025 F545.", "Y40.3025")
        problems = self.refusals(text, "feed")
        first = problems[0]
        self.assertEqual(
            [v.code for v in verify(text)], ["feed"] * len(problems), "one rule, many moves"
        )
        self.assertIn("inherited from the F word on line", first.message)
        self.assertIn("F150.", first.message)
        self.assertIn("cutting feed for T11", first.message)
        # attributed to the move that RUNS at the wrong feed, not to the
        # line that stated it
        stated_on = int(
            re.search(r"inherited from the F word on line (\d+)", first.message).group(1)
        )
        self.assertGreater(first.line, stated_on)
        lines = text.split("\r\n")
        self.assertEqual(lines[stated_on - 1].split()[-1], "F150.")
        self.assertEqual(lines[first.line - 1], "Y40.3025")

    def test_an_f_word_on_a_rapid_is_only_judged_where_it_feeds(self):
        """Rapids carry no feed check; what they leave behind is checked.

        The F on the ``G0`` is not a fault in itself (the control ignores it
        for that move) and the loop that follows restates its own feeds, so
        this file is clean — but the same wrong number left where a cutting
        move will inherit it is not.
        """
        harmless = self.tamper("G0 Z2.5\r\n", "G0 Z2.5 F900.\r\n")
        self.assertEqual([str(v) for v in verify(harmless)], [])
        inherited = self.tamper("Y40.4 F293.", "Y40.4")
        self.assertTrue([v for v in verify(inherited) if v.code == "feed"])

    def test_the_check_reads_the_config_it_is_given_not_a_constant(self):
        """A shop that re-times a tool must be judged by ITS table.

        The same text is clean under the table it was emitted with and
        refused under one whose T13 cut feed has been changed — which is the
        whole reason the rule is derived from :class:`PostConfig` rather than
        written down twice.
        """
        from dataclasses import replace

        config = default_config()
        self.assertEqual([str(v) for v in verify(self.text, config)], [])
        retimed = _with(config, panel=replace(config.panel, cut_feed=520.0))
        problems = [v for v in verify(self.text, retimed) if v.code == "feed"]
        self.assertTrue(problems)
        self.assertIn("F520.", problems[0].message)

    def test_a_dry_run_file_is_held_to_the_same_feeds(self):
        """The lift moves Z words only, so the feed rule must still bite.

        This is why the rule does not exempt moves above the stock: in an air
        cut EVERY cutting move is above it.
        """
        from faceframe_cnc.post import dry_run_config

        program = sample_program()
        air = dry_run_config(default_config())
        text = generate(program, full_plan(program), air)
        self.assertEqual([str(v) for v in verify(text, air)], [])
        tampered = text.replace("F498.2", "F900.", 1)
        self.assertNotEqual(tampered, text)
        problems = [v for v in verify(tampered, air) if v.code == "feed"]
        self.assertTrue(problems, [str(v) for v in verify(tampered, air)])
        self.assertIn("F498.2", problems[0].message)


CREATED = "01 JAN 27 - 08:00"


def sample_program() -> SheetProgram:
    """A sheet the OPTIMIZER could have invented, not one reconstructed.

    Module level since the 2026-08-04 feed follow-up, which needed a
    generated sheet of its own to tamper with; :class:`GenerationApiTest`
    still reaches it through the methods it always used.
    """
    placements = [
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
    return program_from_placements(
        placements, ProgramHeader(name="R990101N", created=CREATED)
    )


def full_plan(program: SheetProgram, inners_first: bool = False) -> CutPlan:
    """Every groove, every opening and both perimeter passes of ``program``."""
    parts = program.flat_parts()
    openings = [
        FeatureRef(i, "opening", j)
        for i, part in enumerate(parts)
        for j in range(len(part.openings))
    ]
    panel = [FeatureRef(i, "groove", j) for i in range(len(parts)) for j in range(4)]
    order = list(range(len(parts)))
    pass_two = order
    if inners_first:
        hosts = {parts.index(child) for part in parts for child in part.children}
        pass_two = sorted(order, key=lambda i: (i not in hosts, i))
    return CutPlan(
        panel=panel,
        openings=openings,
        perimeter=[
            [FeatureRef(i, "perimeter") for i in order],
            [FeatureRef(i, "perimeter") for i in pass_two],
        ],
    )


def wdc_program_and_plan() -> tuple[SheetProgram, CutPlan]:
    """One WDC2436 alone and clear of everything, so its T17 slot is legal.

    A WDC frame is the only way to get a T17 section, and T17 is the only
    tool whose feeds no other test reaches: its rail-pair-only groove list
    (2026-08-03 amendment) and its two slot passes are emitted by hand here
    rather than through the planner, which this module deliberately does not
    import.
    """
    program = program_from_placements(
        [Placement("WDC2436", 4.0, 4.0, 18.0, 36.0, False, [])],
        ProgramHeader(name="R990102N", created=CREATED),
    )
    plan = CutPlan(
        panel=[FeatureRef(0, "groove", 1), FeatureRef(0, "groove", 3)],
        wdc_slot=[FeatureRef(0, "wdc_slot", 0), FeatureRef(0, "wdc_slot", 1)],
        openings=[FeatureRef(0, "opening", 0)],
        perimeter=[[FeatureRef(0, "perimeter")], [FeatureRef(0, "perimeter")]],
    )
    return program, plan


class GenerationApiTest(unittest.TestCase):
    """The API phase 2 drives: build a program, control the ordering."""

    def sample_program(self) -> SheetProgram:
        return sample_program()

    def full_plan(self, program: SheetProgram, inners_first: bool = False) -> CutPlan:
        return full_plan(program, inners_first)

    def test_program_from_placements_rotates_openings_with_the_part(self):
        program = self.sample_program()
        host = program.parts[0]
        self.assertEqual(len(host.children), 1)
        inner = host.children[0]
        self.assertEqual(
            (round(inner.openings[0].width, 4), round(inner.openings[0].height, 4)),
            (9.0, 27.0),
            "a 30x12 frame turned 90 degrees has a 9x27 opening",
        )
        self.assertTrue(host.openings[0].contains(inner.box))

    def test_a_generated_sheet_verifies(self):
        program = self.sample_program()
        text = generate(program, self.full_plan(program))
        self.assertEqual([str(v) for v in verify(text)], [])

    def test_pass_two_ordering_is_the_plan_s_to_choose(self):
        """The onion-skin amendment needs pass 2 to run inners first."""
        program = self.sample_program()
        plan = self.full_plan(program, inners_first=True)
        self.assertEqual([r.part for r in plan.perimeter[0]], [0, 1, 2])
        self.assertEqual([r.part for r in plan.perimeter[1]], [1, 0, 2])
        text = generate(program, plan)
        self.assertEqual([str(v) for v in verify(text)], [])
        # regenerating from the emitted file recovers the same pass 2 order
        _, replan = reconstruct_text(text)
        self.assertEqual([r.part for r in replan.perimeter[1]], [1, 0, 2])

    def test_section_order_is_configurable(self):
        program = self.sample_program()
        plan = self.full_plan(program)
        plan.sections = ("openings", "detail", "perimeter")
        text = generate(program, plan)
        self.assertNotIn("PANEL CUTTER", text)
        self.assertEqual([str(v) for v in verify(text)], [])

    def test_a_plan_may_not_reference_a_missing_feature(self):
        program = self.sample_program()
        plan = self.full_plan(program)
        plan.openings.append(FeatureRef(0, "opening", 9))
        with self.assertRaises(ValueError):
            generate(program, plan)
        plan.openings.pop()
        plan.perimeter[0].append(FeatureRef(99, "perimeter"))
        with self.assertRaises(ValueError):
            generate(program, plan)

    def test_a_banner_does_not_break_verification(self):
        program = self.sample_program()
        config = _with(
            default_config(),
            banner_lines=("(GENERATED BY FACEFRAME OPTIMIZER)", "(SHEET: W2742 + W3012)"),
        )
        text = generate(program, self.full_plan(program), config)
        self.assertIn("(GENERATED BY FACEFRAME OPTIMIZER)", text)
        self.assertEqual([str(v) for v in verify(text, config)], [])


class SafetyGateTest(unittest.TestCase):
    """The post refuses dangerous work before it writes a line."""

    def program(self):
        return SheetProgram(
            header=ProgramHeader(name="R990101N", created="01 JAN 27 - 08:00"),
            parts=[
                PartProgram(
                    "W3030",
                    Box(0.0, 0.0, 30.0, 30.0),
                    openings=[Box(1.5, 1.5, 28.5, 28.5)],
                )
            ],
        )

    def plan(self):
        return CutPlan(
            panel=[FeatureRef(0, "groove", i) for i in range(4)],
            openings=[FeatureRef(0, "opening", 0)],
            perimeter=[[FeatureRef(0, "perimeter")], [FeatureRef(0, "perimeter")]],
        )

    def test_a_cut_below_the_z_floor_is_refused(self):
        from dataclasses import replace

        config = default_config()
        deeper = replace(
            config,
            perimeter_passes=(
                config.perimeter_passes[0],
                replace(config.perimeter_passes[1], z_cut=-0.25),
            ),
        )
        with self.assertRaises(ValueError) as caught:
            generate(self.program(), self.plan(), deeper)
        self.assertIn("spoilboard", str(caught.exception))

    def test_a_travel_above_the_z_ceiling_is_refused(self):
        config = _with(default_config(), rapid_z=3.5)
        with self.assertRaises(ValueError):
            generate(self.program(), self.plan(), config)

    def test_a_sheet_size_mismatch_is_refused(self):
        program = self.program()
        program.sheet_width = 61.0
        with self.assertRaises(ValueError):
            generate(program, self.plan())

    def test_a_feature_too_small_for_the_tool_is_refused(self):
        program = self.program()
        program.parts[0].openings = [Box(1.5, 1.5, 1.7, 1.7)]
        with self.assertRaises(ValueError):
            generate(program, self.plan())

    def test_a_diameter_comment_that_has_drifted_is_refused(self):
        """2026-08-04 review, fix 10.

        The verifier arms its 45-degree cone rule by matching the diameter the
        PROGRAM declares against the float in the table, so a comment that no
        longer says the same number as its own ``diameter`` silently swaps the
        v-slot check for a stream of misleading foreign-cut refusals — and the
        comment is also what the operator at the machine reads.  One
        measurement, two places, cross-checked where the table is in hand.
        """
        from dataclasses import replace

        from faceframe_cnc.post.model import T11

        config = default_config()
        drifted = _with(
            config,
            tools={
                **config.tools,
                "perimeter": replace(T11, diameter_comment="(DIAMETER: 0.5)"),
            },
        )
        with self.assertRaises(ValueError) as caught:
            generate(self.program(), self.plan(), drifted)
        message = str(caught.exception)
        self.assertIn("(DIAMETER: 0.5)", message)
        self.assertIn("0.375", message)
        self.assertIn("may not disagree", message)

    def test_a_diameter_comment_that_is_not_a_diameter_comment_is_refused(self):
        from dataclasses import replace

        from faceframe_cnc.post.model import T13

        config = default_config()
        broken = _with(
            config,
            tools={**config.tools, "panel": replace(T13, diameter_comment="(16 MM)")},
        )
        with self.assertRaises(ValueError) as caught:
            generate(self.program(), self.plan(), broken)
        self.assertIn("not a (DIAMETER: n) comment", str(caught.exception))

    def test_the_measured_table_agrees_with_itself(self):
        config = default_config()
        for section, tool in config.tools.items():
            with self.subTest(section=section):
                stated = re.fullmatch(
                    r"\(DIAMETER: (\d*\.?\d+)\)", tool.diameter_comment
                )
                self.assertIsNotNone(stated, tool.diameter_comment)
                self.assertAlmostEqual(float(stated.group(1)), tool.diameter)

    def test_the_verifier_shares_no_emission_code(self):
        """It must be able to disagree with the generator, so it may not
        import it (the templates are a deliberate second copy).

        ``from_layout`` is on the same list since the 2026-08-04 review: the
        expected-work manifest is built from the LAYOUT and the measured
        tables, never from the planner that told the emitter what to cut, or
        it could not catch the emitter leaving a cut out.  (Importing
        ``from_layout`` would pull in ``generator`` anyway, which is the
        other half of the reason.)
        """
        import ast

        import faceframe_cnc.post.verifier as verifier

        with open(verifier.__file__, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        joined = " ".join(sorted(imported))
        self.assertNotIn("generator", joined)
        self.assertNotIn("reconstruct", joined)
        self.assertNotIn("from_layout", joined)
        self.assertNotIn("job", joined)


class RapidSafetyTest(unittest.TestCase):
    """2026-08-04 review, fix 1: rapids were exempt from every material rule.

    Every check in the verifier opened with ``if move.rapid: continue`` — a
    ``G0`` cuts nothing on purpose — so nothing at all said where a rapid may
    go, and the worst single edit a person can make to one of these files
    verified clean: turn a ``G0 Z2.5`` retract into ``G0 Z0.`` and the next
    reposition drags a spinning bit across the whole sheet at spoilboard level.
    """

    def setUp(self):
        self.text = read("R710101N")
        self.assertEqual([str(v) for v in verify(self.text)], [])

    def codes(self, text):
        return [v for v in verify(text)]

    def test_a_retract_edited_down_to_the_spoilboard_is_caught(self):
        head = self.text.index("T13")
        at = self.text.index("G0 Z2.5", head)
        text = self.text[:at] + "G0 Z0." + self.text[at + len("G0 Z2.5") :]
        problems = [v for v in self.codes(text) if v.code == "rapid"]
        self.assertEqual(len(problems), 2, [str(v) for v in problems])
        plunge, traverse = problems
        self.assertIn("plunges from Z0.55 to Z0.0", plunge.message)
        self.assertIn("entry feed", plunge.message)
        self.assertIn("traverses", traverse.message)
        self.assertIn("dragged", traverse.message)
        # the traverse is reported on the line that makes it, not the edit
        self.assertGreater(traverse.line, plunge.line)

    def test_a_rapid_that_only_climbs_out_of_the_cut_is_fine(self):
        """The ``G0 Z2.5`` at the end of every feature starts at cut depth.

        A rule phrased as "no rapid may touch a Z below the stock top" would
        refuse every reference file, which is why the rule is about travelling
        and descending rather than about the lowest Z on the line.
        """
        self.assertEqual([str(v) for v in verify(self.text)], [])
        self.assertIn("G1 Z0.55 F150.\r\nY31.0175 F490.\r\nG0 Z2.5", self.text)

    def test_the_homing_lines_are_exempt_because_z_is_unknown_there(self):
        """After ``G28`` the spindle is at machine home, not at Z0.

        The fixed header, section tails and footer all home Z and then move in
        XY, and the footer parks at X24 Y96 while the program's absolute Z is
        anybody's guess.  Every one of those is a rapid, and none of them is a
        finding — which is what makes the rule usable at all.
        """
        for literal in (
            "G0 G20 G91 G28 Z0 M15",
            "G17 G91 G28 Z0 M95",
            "G91 G28 Z0 M15",
            "G90 X24. Y96.",
        ):
            with self.subTest(line=literal):
                self.assertIn(literal, self.text)
        self.assertEqual([v for v in verify(self.text) if v.code == "rapid"], [])

    def test_a_dry_run_files_rapids_stay_high(self):
        from faceframe_cnc.post import dry_run_config

        program = sample_program()
        air = dry_run_config(default_config())
        text = generate(program, full_plan(program), air)
        self.assertEqual([str(v) for v in verify(text, air)], [])

    def test_a_ramp_plane_below_the_stock_is_refused_before_a_line_is_written(self):
        """The generator side of the same hole.

        ``approach_z`` is reached by a ``G0``, so a table with it BELOW the
        stock top emits a rapid plunge into the part at every single feature.
        The old ``_check_config`` only compared it against the Z ceiling, so
        such a table generated happily and — before fix 1 — verified clean too.
        """
        program, plan = sample_program(), None
        plan = full_plan(program)
        low = _with(default_config(), approach_z=0.6)
        with self.assertRaises(ValueError) as caught:
            generate(program, plan, low)
        message = str(caught.exception)
        self.assertIn("ramp plane", message)
        self.assertIn("0.75 top of the stock", message)
        self.assertIn("rapid-plunge 0.15", message)

    def test_both_rapid_planes_are_held_to_the_stock_top(self):
        """Whichever of the two is reported first, a table that rapids into the
        material does not generate."""
        with self.assertRaises(ValueError) as caught:
            generate(sample_program(), full_plan(sample_program()),
                     _with(default_config(), rapid_z=0.5, approach_z=0.4))
        self.assertIn("top of the stock", str(caught.exception))

    def test_a_retract_plane_below_the_ramp_plane_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            generate(sample_program(), full_plan(sample_program()),
                     _with(default_config(), rapid_z=1.5, approach_z=2.0))
        self.assertIn("would descend", str(caught.exception))


class ToolLengthCompTest(unittest.TestCase):
    """2026-08-04 review, fix 3: the ``H`` word was read by nothing.

    ``G43 H13 Z2.5`` selects tool 13's LENGTH offset.  Point it at another
    tool's row and every Z in the section is out by the difference between the
    two tools — a spoilboard strike or a sheet still joined by onion skin,
    depending which way it is wrong — and the text still looks perfectly
    ordinary.  The verifier's word loop handled G/M/T/F/X/Y/Z and dropped H
    silently, so this verified clean.
    """

    def test_the_references_pair_every_g43_with_its_own_tool(self):
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                text = read(name)
                pairs = re.findall(r"(?m)^G43 H(\d+) Z", text)
                tools = [
                    line[1:]
                    for line in text.split("\r\n")
                    if re.fullmatch(r"T\d+", line)
                ]
                self.assertEqual(pairs, tools)
                self.assertEqual(
                    re.findall(r"H(\d+)", text), pairs + ["0"], "and H0 in the footer"
                )

    def test_the_wrong_tool_s_length_offset_is_caught(self):
        text = read("R710101N").replace("G43 H11 Z2.5", "G43 H12 Z2.5", 1)
        problems = [v for v in verify(text) if v.code == "tool-comp"]
        self.assertEqual(len(problems), 1, [str(v) for v in verify(text)])
        self.assertIn("G43 H12 with T11 in the spindle", problems[0].message)
        self.assertIn("H11", problems[0].message)

    def test_a_g43_with_no_h_word_at_all_is_caught(self):
        text = read("R710101N").replace("G43 H13 Z2.5", "G43 Z2.5", 1)
        problems = [v for v in verify(text) if v.code == "tool-comp"]
        self.assertTrue(problems)
        self.assertIn("without an H word", problems[0].message)

    def test_an_h_word_anywhere_else_is_refused(self):
        text = read("R710101N").replace("Y31.0175 F490.", "Y31.0175 H11 F490.", 1)
        problems = [v for v in verify(text) if v.code == "tool-comp"]
        self.assertTrue(problems)
        self.assertIn("this post states an H only on a G43 line", problems[0].message)

    def test_the_footers_h0_is_exempt_by_position_not_by_value(self):
        """``G90 H0 M25`` cancels the offset on the way out and is fine.

        An ``H0`` planted somewhere else is not, which is why the exemption is
        the line's POSITION in the fixed footer rather than the number 0.
        """
        self.assertEqual([v for v in verify(read("R710101N")) if v.code == "tool-comp"], [])
        text = read("R710101N").replace("M89 B0\r\nG08 P1", "M89 B0\r\nH0\r\nG08 P1", 1)
        self.assertTrue([v for v in verify(text) if v.code == "tool-comp"])


class SpindleStartTest(unittest.TestCase):
    """2026-08-04 review, fix 4: an S word is not a running spindle.

    ``_check_speeds`` caught a wrong ``S``, an ``M13`` with no ``S`` and a
    section that states no speed at all.  It could not catch the plainest
    version: delete the ``M13`` and keep the ``S``.  The register is loaded, the
    spindle is stopped, and the next line feeds a stationary bit into 3/4 MDF.
    """

    def setUp(self):
        program = sample_program()
        self.text = generate(program, full_plan(program))
        self.assertEqual([str(v) for v in verify(self.text)], [])

    def test_deleting_m13_but_keeping_the_speed_is_caught(self):
        text = self.text.replace("M13 S17500", "S17500", 1)
        self.assertNotEqual(text, self.text)
        problems = [v for v in verify(text) if v.code == "spindle-start"]
        self.assertEqual(len(problems), 1, [str(v) for v in verify(text)])
        self.assertIn("T13 section feeds into the material", problems[0].message)
        self.assertIn("stationary bit", problems[0].message)
        self.assertEqual(
            {v.code for v in verify(text)},
            {"spindle-start"},
            "one hole, one finding - the speed rule has nothing to add here",
        )

    def test_it_is_reported_once_per_section_not_once_per_move(self):
        text = self.text.replace("M13 S16700", "S16700")
        problems = [v for v in verify(text) if v.code == "spindle-start"]
        self.assertEqual(len(problems), 2, "the two T11 sections, once each")

    def test_every_reference_section_starts_its_spindle(self):
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                self.assertEqual(
                    [v for v in verify_file(path_of(name)) if v.code == "spindle-start"],
                    [],
                )


class ModalGCodeTest(unittest.TestCase):
    """2026-08-04 review, fix 5: ``G91`` accepted, coordinates read absolute.

    ``G90`` and ``G91`` are both in the reference files, so ``code`` passed
    either one anywhere.  Meanwhile every X/Y/Z in this module is read as an
    absolute coordinate, because that is all the post writes — so a ``G91``
    slipped in before a loop left the verifier checking a program the control
    would not run, while the control ran an incremental runaway.  The post never
    writes ``G91`` outside its fixed lines, so refusing the mode change is the
    honest answer rather than implementing incremental interpretation.
    """

    def setUp(self):
        program = sample_program()
        self.text = generate(program, full_plan(program))
        self.assertEqual([str(v) for v in verify(self.text)], [])

    def test_g91_before_a_body_loop_is_refused(self):
        lines = self.text.split("\r\n")
        at = next(i for i, line in enumerate(lines) if line.startswith("G1 X"))
        lines.insert(at, "G91")
        text = "\r\n".join(lines)
        problems = [v for v in verify(text) if v.code == "g-mode"]
        self.assertEqual(len(problems), 1, [str(v) for v in verify(text)])
        self.assertIn("G91", problems[0].message)
        self.assertIn("read as absolute", problems[0].message)
        self.assertEqual(problems[0].line, at + 1)

    def test_g90_on_a_body_line_is_refused_as_well(self):
        text = self.text.replace("G1 Z0.55 F150.", "G90 G1 Z0.55 F150.", 1)
        self.assertTrue([v for v in verify(text) if v.code == "g-mode"])

    def test_g28_may_not_be_manufactured_mid_body(self):
        """It is what makes the absolute Z unknown, so the rapid rule needs it
        pinned to the fixed lines as much as ``G91`` does."""
        text = self.text.replace("G0 Z2.5\r\n", "G28 Z0\r\nG0 Z2.5\r\n", 1)
        self.assertTrue([v for v in verify(text) if v.code == "g-mode"])

    def test_the_fixed_lines_and_the_section_prepositions_are_fine(self):
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                self.assertEqual(
                    [str(v) for v in verify_file(path_of(name)) if v.code == "g-mode"],
                    [],
                )
        # both shapes of the pattern-pinned preposition, which carry a G90
        self.assertIn("G0 G54 G90 X0. Y0.", self.text)
        self.assertRegex(self.text, r"G0 G54 G90 X[\d.]+ Y[\d.]+ M13 S\d+")

    def test_a_mangled_prologue_line_gets_its_header_finding_not_an_exemption(self):
        text = self.text.replace("G90 G40 M22", "G90 G41 M22", 1)
        codes = {v.code for v in verify(text)}
        self.assertIn("header", codes)
        self.assertIn("g-mode", codes, "a line that is not the template is not fixed")


class RampProfileTest(unittest.TestCase):
    """2026-08-04 review, fix 6a: a ramp is not at its deepest Z all the way.

    ``foreign-cut`` judged a whole move at ``min(z0, z1)``.  A perimeter lead-in
    ramp descends 1 in Z per 2 of travel, so it is about four inches long and
    spends most of that an inch or two ABOVE the sheet — and a legal 24x6
    valance sitting above a 30" frame was refused for "cutting into" the
    neighbour with a segment physically above it.
    """

    def sheet(self, placements):
        program = program_from_placements(
            placements, ProgramHeader(name="R990107N", created=CREATED)
        )
        parts = program.flat_parts()
        plan = CutPlan(
            panel=[
                FeatureRef(i, "groove", j) for i in range(len(parts)) for j in range(4)
            ],
            openings=[
                FeatureRef(i, "opening", j)
                for i, part in enumerate(parts)
                for j in range(len(part.openings))
            ],
            perimeter=[
                [FeatureRef(i, "perimeter") for i in range(len(parts))],
                [FeatureRef(i, "perimeter") for i in range(len(parts))],
            ],
        )
        return program, plan

    def test_a_short_part_above_a_neighbour_is_no_longer_refused(self):
        program, plan = self.sheet(
            [
                Placement("W3030", 0.0, 0.0, 30.0, 30.0, False, []),
                Placement("W2406", 0.0, 30.455, 24.0, 6.0, False, []),
            ]
        )
        text = generate(program, plan)
        self.assertEqual([str(v) for v in verify(text)], [])
        # ...and the ramp really does start inside the neighbour's footprint,
        # 1.7 inches up in the air, which is the whole point.
        self.assertIn("X24.2395 Y29.575", text)

    def test_a_ramp_that_reaches_a_neighbour_AT_DEPTH_is_still_refused(self):
        """The relaxation is about height, not about neighbours.

        Same sheet, with the ramp's descent flattened so the segment crossing
        the neighbour is at the cutting Z instead of above it.
        """
        program, plan = self.sheet(
            [
                Placement("W3030", 0.0, 0.0, 30.0, 30.0, False, []),
                Placement("W2406", 0.0, 30.455, 24.0, 6.0, False, []),
            ]
        )
        text = generate(program, plan)
        self.assertIn("G1 X24.1875 Y33.455 Z-0.006 F150.", text)
        flat = text.replace(
            "X24.2375 Y29.443 Z2.5\r\nZ2.\r\nG1 X24.1875 Y33.455 Z-0.006 F150.",
            "X24.2375 Y29.443 Z2.5\r\nZ2.\r\nG1 Z-0.006 F150.\r\n"
            "X24.1875 Y33.455 F498.2",
            1,
        )
        self.assertNotEqual(flat, text)
        problems = [v for v in verify(flat) if v.code == "foreign-cut"]
        self.assertTrue(problems, [str(v) for v in verify(flat)])
        self.assertIn("0.756 deep", problems[0].message)

    def test_the_references_still_pass_and_a_real_intrusion_still_fails(self):
        self.assertEqual([str(v) for v in verify_file(path_of("R710101N"))], [])
        text = read("R710101N").replace("X32.055\r\n", "X29.055\r\n", 1)
        self.assertTrue([v for v in verify(text) if v.code == "foreign-cut"])


class EntrySideFallbackTest(unittest.TestCase):
    """2026-08-04 review, fix 6b: the measured default does not always fit.

    ``default_entry_side`` is "the right edge" for every perimeter, measured off
    the references.  The lead-in ramp is about four inches long and runs along
    the entry edge, so on a 48x5 frame near the front of the sheet the right
    edge puts it at Y-1.012 — an inch outside the sheet plus its trim overhang,
    which the verifier refuses and is right to.  The refusal was the post's
    fault, not the layout's.
    """

    def frame(self, x, y, width, height):
        program = program_from_placements(
            [Placement("W4805", x, y, width, height, False, [])],
            ProgramHeader(name="R990108N", created=CREATED),
        )
        plan = CutPlan(
            panel=[FeatureRef(0, "groove", j) for j in range(4)],
            openings=[FeatureRef(0, "opening", 0)],
            perimeter=[[FeatureRef(0, "perimeter")], [FeatureRef(0, "perimeter")]],
        )
        return program, plan

    def test_a_long_shallow_frame_at_the_front_edge_now_generates(self):
        program, plan = self.frame(0.5, 0.5, 48.0, 5.0)
        text = generate(program, plan)
        self.assertEqual([str(v) for v in verify(text)], [])
        self.assertNotIn("Y-0.88", text, "the right-edge ramp would have gone there")
        self.assertNotIn("Y-1.012", text)

    def test_it_falls_back_to_an_edge_that_fits_and_says_which(self):
        from faceframe_cnc.post.generator import default_entry_side, entry_side_for
        from faceframe_cnc.post.model import T11

        config = default_config()
        spec = config.perimeter_passes[1]
        tight = Box(0.3105, 0.3105, 48.6895, 5.6895)
        self.assertEqual(default_entry_side(tight, "perimeter"), "right")
        self.assertEqual(entry_side_for(tight, "perimeter", T11, spec, config), "bottom")

    def test_the_default_is_kept_whenever_it_fits(self):
        """Which is what keeps every reference and every 7-21 sheet identical."""
        from faceframe_cnc.post.generator import default_entry_side, entry_side_for
        from faceframe_cnc.post.model import T11

        config = default_config()
        for spec in config.perimeter_passes:
            for box in (
                Box(10.0, 10.0, 40.0, 40.0),
                Box(1.0, 20.0, 31.0, 32.0),
                Box(0.5, 30.0, 48.5, 60.0),
            ):
                with self.subTest(box=box, z=spec.z_cut):
                    self.assertEqual(
                        entry_side_for(box, "perimeter", T11, spec, config),
                        default_entry_side(box, "perimeter"),
                    )

    def test_an_override_is_obeyed_as_given(self):
        from faceframe_cnc.post.generator import entry_side_for
        from faceframe_cnc.post.model import T11

        config = default_config()
        self.assertEqual(
            entry_side_for(
                Box(0.3105, 0.3105, 48.6895, 5.6895),
                "perimeter",
                T11,
                config.perimeter_passes[1],
                config,
                override="left",
            ),
            "left",
        )

    def test_a_part_no_edge_fits_is_refused_with_the_reason(self):
        from faceframe_cnc.post.generator import entry_side_for
        from faceframe_cnc.post.model import T11

        config = _with(default_config(), sheet_width=6.0, sheet_length=6.0)
        with self.assertRaises(ValueError) as caught:
            entry_side_for(
                Box(1.0, 1.0, 5.0, 5.0),
                "perimeter",
                T11,
                config.perimeter_passes[1],
                config,
            )
        message = str(caught.exception)
        self.assertIn("no lead-in edge fits", message)
        for side in ("bottom", "right", "top", "left"):
            self.assertIn(side, message)
        self.assertIn("Re-nest", message)

    def test_the_planner_refusal_names_the_part(self):
        """No edge fits at all: an offcut too small for a four-inch lead-in.

        Built by hand rather than through the packer, because the packer cannot
        produce it -- which is the point: the refusal exists for a hand-placed
        or hand-edited layout, and it has to say which part and why.
        """
        program = SheetProgram(
            header=ProgramHeader(name="R990109N", created=CREATED),
            parts=[
                PartProgram(
                    "W0606",
                    Box(1.0, 1.0, 7.0, 7.0),
                    openings=[Box(2.5, 2.5, 5.5, 5.5)],
                )
            ],
            sheet_width=8.0,
            sheet_length=8.0,
        )
        plan = CutPlan(
            panel=[FeatureRef(0, "groove", j) for j in range(4)],
            openings=[FeatureRef(0, "opening", 0)],
            perimeter=[[FeatureRef(0, "perimeter")], [FeatureRef(0, "perimeter")]],
        )
        tiny = _with(default_config(), sheet_width=8.0, sheet_length=8.0)
        with self.assertRaises(ValueError) as caught:
            generate(program, plan, tiny)
        message = str(caught.exception)
        self.assertIn("W0606", message)
        self.assertIn("cannot be cut on this sheet", message)
        self.assertIn("no lead-in edge fits", message)


class VSlotOverhangTest(unittest.TestCase):
    """2026-08-04 review, fix 11: the verifier was softer than the planner.

    ``from_layout`` refuses a T17 sweep the sheet does not CONTAIN; the verifier
    allowed the sheet plus the ordinary 0.375 trim overhang.  A cone running off
    the edge is a cut into the fence, so the looser rule was simply wrong.
    """

    def wdc_at(self, x, y):
        program = program_from_placements(
            [Placement("WDC2436", x, y, 18.0, 36.0, False, [])],
            ProgramHeader(name="R990110N", created=CREATED),
        )
        plan = CutPlan(
            panel=[FeatureRef(0, "groove", 1), FeatureRef(0, "groove", 3)],
            wdc_slot=[FeatureRef(0, "wdc_slot", 0), FeatureRef(0, "wdc_slot", 1)],
            openings=[FeatureRef(0, "opening", 0)],
            perimeter=[[FeatureRef(0, "perimeter")], [FeatureRef(0, "perimeter")]],
        )
        return generate(program, plan)

    def test_a_cone_inside_the_old_overhang_but_off_the_sheet_is_refused(self):
        """0.5 from the front edge: the deep pass reaches Y-0.375 exactly, so
        the old rule let it through and the planner would not have."""
        problems = [v for v in verify(self.wdc_at(4.0, 0.5)) if v.code == "v-slot"]
        self.assertTrue(problems)
        self.assertIn("off the 49.0x97.0 sheet", problems[0].message)
        self.assertIn("fence", problems[0].message)

    def test_a_wdc_with_the_full_reach_clear_of_the_edge_is_fine(self):
        self.assertEqual([str(v) for v in verify(self.wdc_at(4.0, 4.0))], [])

    def test_the_planner_and_the_verifier_now_draw_the_same_line(self):
        from faceframe_cnc.nesting import NestingConfig, SheetLayout
        from faceframe_cnc.post.from_layout import WdcNotSupportedError, plan_sheet

        layout = SheetLayout(
            placements=[Placement("WDC2436", 4.0, 0.5, 18.0, 36.0, False, [])]
        )
        with self.assertRaises(WdcNotSupportedError):
            plan_sheet(layout, ProgramHeader(name="R990111N", created=CREATED))
        self.assertTrue([v for v in verify(self.wdc_at(4.0, 0.5)) if v.code == "v-slot"])

    def test_rfk0101n_does_not_need_the_overhang(self):
        """The file the T17 grammar was measured from, checked before tightening.

        RFK0101N does not verify clean under this post's table for reasons that
        predate all of this (it loads face UP, cuts at depths this post does not
        use and runs a T16 the table has no row for), so what matters here is
        narrower and is exactly what fix 11 could have broken: not one of its
        cone sweeps is outside the sheet, so the tightened rule adds nothing.
        """
        text = read("RFK0101N")
        off_sheet = [
            v
            for v in verify(text)
            if v.code == "v-slot" and "off the" in v.message
        ]
        self.assertEqual([str(v) for v in off_sheet], [])


class CutOwnershipTest(unittest.TestCase):
    """2026-08-04 recorded follow-up (fix 12): ``_owner_of`` could self-skip.

    ``foreign-cut`` skips the part a move is judged to belong to — the smallest
    recovered footprint whose 0.375-grown box contains the whole move — because
    a T13 groove legitimately cuts its own part.  When a frame is nested inside
    another's opening, a cut belonging to the INNER that reaches beyond the
    inner is contained by the HOST instead, so the host is the part skipped, and
    an inner cut running straight through the host's stile was reported by
    nothing at all.

    The tamper below moves the inner's overshoot — a move at full depth, after
    the loop has closed, so no other rule notices it — 9 inches sideways into
    the host's right stile.  It stays inside the host's grown box, which is what
    used to buy it the exemption.
    """

    def program(self):
        return program_from_placements(
            [
                Placement(
                    "W2742",
                    1.0,
                    1.0,
                    27.0,
                    42.0,
                    False,
                    [Placement("W3012", 5.0, 7.0, 12.0, 30.0, True, [])],
                )
            ],
            ProgramHeader(name="R990112N", created=CREATED),
        )

    def text(self):
        program = self.program()
        parts = program.flat_parts()
        plan = CutPlan(
            panel=[
                FeatureRef(i, "groove", j) for i in range(len(parts)) for j in range(4)
            ],
            openings=[
                FeatureRef(i, "opening", j)
                for i, part in enumerate(parts)
                for j in range(len(part.openings))
            ],
            perimeter=[
                [FeatureRef(i, "perimeter") for i in range(len(parts))],
                [FeatureRef(1, "perimeter"), FeatureRef(0, "perimeter")],
            ],
        )
        return generate(program, plan)

    def test_a_through_cut_into_the_containing_host_is_caught(self):
        text = self.text()
        self.assertEqual([str(v) for v in verify(text)], [])
        lines = text.split("\r\n")
        # the inner's through-pass overshoot: entry Y22., overshoot Y22.375
        at = next(
            i
            for i in range(len(lines))
            if lines[i] == "Y22.375"
            and lines[i - 1] == "Y22."
            and "Z-0.006" in lines[i - 6]
        )
        lines[at] = "X26.4 Y22.375"
        tampered = "\r\n".join(lines)

        problems = verify(tampered)
        self.assertTrue(problems, "the intrusion has to be reported by something")
        foreign = [v for v in problems if v.code == "foreign-cut"]
        self.assertTrue(foreign, [str(v) for v in problems])
        self.assertIn("attributed to", foreign[0].message)
        self.assertIn("never through a frame member", foreign[0].message)
        self.assertEqual(foreign[0].line, at + 1)
        # nothing ELSE changed: the footprints still come back the same, so the
        # finding is about the cut and not about a mangled loop
        self.assertEqual({v.code for v in problems}, {"foreign-cut"})

    def test_the_move_really_is_attributed_to_the_host(self):
        """The mechanism, stated directly, so the test cannot pass by accident."""
        import faceframe_cnc.post.verifier as verifier

        text = self.text()
        lines = text.split("\r\n")
        at = next(
            i
            for i in range(len(lines))
            if lines[i] == "Y22.375"
            and lines[i - 1] == "Y22."
            and "Z-0.006" in lines[i - 6]
        )
        lines[at] = "X26.4 Y22.375"
        config = default_config()
        moves, _ = verifier._simulate(lines, config)
        parts, _found, _problems = verifier._recover_parts(moves, config)
        move = next(m for m in moves if m.line == at + 1)
        owner = verifier._owner_of(move, parts, config)
        self.assertEqual((owner.box.x0, owner.box.x1), (1.0, 28.0), "the HOST")
        self.assertEqual(
            sorted((p.box.x0, p.box.x1) for p in parts), [(1.0, 28.0), (5.0, 17.0)]
        )


class FormattingTest(unittest.TestCase):
    def test_numbers_print_the_way_the_reference_post_prints_them(self):
        self.assertEqual(fmt(2.0), "2.")
        self.assertEqual(fmt(0.0), "0.")
        self.assertEqual(fmt(-0.0), "0.")
        self.assertEqual(fmt(0.55), "0.55")
        self.assertEqual(fmt(-0.006), "-0.006")
        self.assertEqual(fmt(498.2), "498.2")
        self.assertEqual(fmt(490.0), "490.")
        self.assertEqual(fmt(30.1895), "30.1895")


class SolidRegionTest(unittest.TestCase):
    def test_a_part_s_solid_excludes_its_openings(self):
        part = PartProgram(
            "W3030",
            Box(0.0, 0.0, 30.0, 30.0),
            openings=[Box(1.5, 1.5, 28.5, 28.5)],
        )
        solids = part.solid_boxes()
        self.assertEqual(len(solids), 4)
        self.assertAlmostEqual(sum(b.width * b.height for b in solids), 900 - 729)
        for band in solids:
            self.assertFalse(band.overlaps(Box(1.5, 1.5, 28.5, 28.5)))


if __name__ == "__main__":
    unittest.main()
