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
from dataclasses import replace

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
from faceframe_cnc.post.from_layout import (
    T11_MAX_BITE,
    bite_ladder,
    plan_sheet,
    post_config_for,
)
from faceframe_cnc.post.generator import fmt, release_path
from faceframe_cnc.post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_RELEASE,
    SECTION_WDC_SLOT,
    PassSpec,
    ToolSpec,
)
from faceframe_cnc.post.reconstruct import ReconstructionError, reconstruct_text
from faceframe_cnc.post.verifier import expected_work

NC_DIR = os.path.join(os.path.dirname(__file__), "..", "reference", "nc_files")

TOL = 1e-3  # the milestone's coordinate tolerance

#: The pre-amendment T13 groove overruns the reference files still contain, by
#: the line each one is reported on (2026-08-05 amendment, Scott, job R0805).
#:
#: Closing the verifier's shallow-cut swept-width waiver refuses the very cut
#: the shop's own CAM made: a 0.6299 panel cutter running the measured 0.375
#: past a part edge sweeps 0.235 into a neighbour 0.455 away, 0.20 deep.  That
#: is the same cut, with the same numbers, that bit two divots out of a WDC
#: frame on job R0805 — see ``tests/test_r0805_regression.py`` and the
#: verifier module docstring.  So R710101N and R730101N are now REFUSED, on
#: purpose, and R720101N is untouched (its parts are nested rather than
#: shoulder to shoulder).  The lines are pinned rather than the count filtered,
#: so any OTHER finding on a reference file still fails.
#:
#: These files are documentation of the machine's dialect and of pre-amendment
#: behaviour, not output: nothing generated is allowed near this list.
LEGACY_GROOVE_FOREIGN_CUTS = {
    "R710101N": (32, 41, 42, 57, 67),
    "R720101N": (),
    "R730101N": (31, 32, 47, 57, 92),
}


def assert_only_legacy_grooves(case, name: str, violations) -> None:
    """``violations`` are exactly this reference file's frozen groove cuts."""
    case.assertEqual(
        [(v.code, v.line) for v in violations],
        [("foreign-cut", line) for line in LEGACY_GROOVE_FOREIGN_CUTS[name]],
        [str(v) for v in violations],
    )
    for violation in violations:
        case.assertIn(
            "up to 0.2 deep",
            violation.message,
            "only the 0.20-deep T13 groove is grandfathered, nothing else",
        )


def read(name: str) -> str:
    with open(os.path.join(NC_DIR, f"{name}.anc"), "r", newline="") as handle:
        return handle.read()


def golden(name: str) -> str:
    """The RE-BLESSED expected bytes for a reference round trip.

    Until 2026-08-05 a round trip had to come back byte-identical to the
    measured original.  The R0805 amendment deliberately moved the T13
    stile-groove endpoints (clamped flush with the part - see
    ``test_r0805_regression.py``), so the expectation moved with it:
    ``reference/goldens/`` holds each reference program regenerated by the
    amended post, 17/17/16 lines away from the originals, every changed line
    a clamped groove endpoint.  The annotated diff is
    ``docs/2026-08-05_golden_reblessing.md``, signed off by Scott on
    2026-08-05 before these tests were re-pointed (spec section 5 - the
    originals in ``reference/nc_files/`` stay untouched as the measured
    source of constants).
    """
    path = os.path.join(NC_DIR, "..", "goldens", f"{name}.anc")
    with open(path, "r", newline="") as handle:
        return handle.read()


def path_of(name: str) -> str:
    return os.path.join(NC_DIR, f"{name}.anc")


class RoundTripTest(unittest.TestCase):
    """Reconstruct a real program, regenerate it, diff it.

    Against the re-blessed goldens since 2026-08-05 (see :func:`golden`)."""

    def assert_round_trip(self, name: str) -> None:
        want = golden(name)
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
        want = golden("R710101N").split("\r\n")
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
                self.assertEqual(generate(program, plan), golden(name))


class VerifierPositiveTest(unittest.TestCase):
    def test_reference_files_pass_apart_from_the_pre_amendment_grooves(self):
        """These three verified with NO findings until 2026-08-05.

        The waiver that made that true is what let job R0805 reach the machine
        (see :data:`LEGACY_GROOVE_FOREIGN_CUTS` and the verifier's module
        docstring).  Two of the three files contain the same overrunning groove
        the amendment refuses, so they are refused too — every finding pinned
        to its line, so nothing else can hide behind this.
        """
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                assert_only_legacy_grooves(self, name, verify_file(path_of(name)))

    def test_generated_files_pass(self):
        """The other half of the statement above, and the amendment's proof:
        run those same three sheets through the post as it stands today and
        the groove no longer leaves the part, so what the shop would cut now
        has no findings at all."""
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
            *cfg.openings_passes,
            cfg.detail_pass,
            *cfg.perimeter_passes,
        )

    def test_the_measured_table_holds_the_feeds_the_files_use(self):
        cfg = default_config()
        self.assertEqual((cfg.panel.entry_feed, cfg.panel.cut_feed), (150.0, 490.0))
        self.assertEqual(
            (cfg.wdc_slot.entry_feed, cfg.wdc_slot.cut_feed), (150.0, 400.0)
        )
        self.assertEqual(len(cfg.openings_passes), 1, "the measured table has one")
        self.assertEqual(
            (
                cfg.openings_passes[-1].entry_feed,
                cfg.openings_passes[-1].cut_feed,
            ),
            (150.0, 545.0),
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
            cfg.openings_passes[-1].cut_feed, cfg.perimeter_passes[0].cut_feed
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
        # Y41.685 is the stile groove's clamped high end on this 27x42 part
        # (42 less the T13 radius) -- it read Y42.375 before the 2026-08-05
        # amendment, when the groove still ran 0.375 past the part.
        text = self.tamper("Y41.685 F490.", "Y41.685 F545.")
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

        ``tabs`` joined the list with the 2026-08-05 amendment: the verifier's
        hold invariant ("no profile is fully separated before the release
        section", milestone 3) has to be able to disagree with where the
        generator PUT the tabs, so it re-derives the placement geometry itself
        rather than importing it.

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
        self.assertNotIn("tabs", joined)



# --------------------------------------------------------------------------
# the hold invariant (2026-08-05 amendment, Scott, job R0805, spec §3d/§6)
# --------------------------------------------------------------------------




def drop_lifts(text: str, z_cut: float) -> str:
    """``text`` with the tab lifts of the pass at ``z_cut`` deleted.

    Four lines per lift — cut on to the foot of the climb, climb to the tab top,
    traverse it, descend back to depth — and every coordinate in this post is
    absolute, so deleting the block leaves a loop that still closes on the same
    rectangle.  It just no longer rises over the tab, which is the mutation spec
    §3b's "it must, or the skin pass destroys the tab" is about.

    The block is found by its CLIMB (the tab top's Z is the only Z0.25 in the
    program) and attributed to a pass by its DESCENT, which restates that pass's
    own depth — so one pass can be flattened while the others keep their lifts.
    """
    top = f" Z{fmt(default_config().tabs.top_z)}"
    want = f"Z{fmt(z_cut)} F"
    lines = text.split("\r\n")
    drop: set[int] = set()
    for index, line in enumerate(lines):
        if line.endswith(top) and want in lines[index + 2]:
            drop.update({index - 1, index, index + 1, index + 2})
    assert drop, f"no tab lift at Z{z_cut} to remove"
    return "\r\n".join(line for i, line in enumerate(lines) if i not in drop)


def drop_blocks_at(text: str, needle: str) -> str:
    """``text`` with every whole feature block whose LEAD-IN states ``needle`` gone.

    The mutation the 2026-08-05 max-bite rule is tested with: delete a rung of a
    depth ladder and leave everything else where it was.  A block runs from its
    preposition to its closing ``G0 Z2.5``, and the walk back stops at the
    previous block's own ``G0 Z2.5`` or at the section's ``Tn`` line, so nothing
    here counts how many lines a preposition takes.
    """
    lines = text.split("\r\n")
    while True:
        anchor = next(
            (
                i
                for i, line in enumerate(lines)
                if line.startswith("G1 ") and needle in line
            ),
            None,
        )
        if anchor is None:
            return "\r\n".join(lines)
        start = anchor
        while start > 0 and lines[start - 1] != "G0 Z2.5" and not re.fullmatch(
            r"T\d+", lines[start - 1]
        ):
            start -= 1
        end = lines.index("G0 Z2.5", anchor)
        del lines[start : end + 1]


class MaxBiteLadderTest(unittest.TestCase):
    """0.4 of material per T11 pass — RATIFIED POLICY, Scott, 2026-08-05.

    In his own words: *"When the 3/8 comp (T11) is being used, only let it take a
    maximum of 0.4 inch of material per pass.  That will help reduce the load on
    it."*  He had just watched a perimeter take the whole 0.756 in one go (the
    onion skin having been dropped earlier the same day) and noted that 0.4
    "basically cuts that in half".

    Like :class:`TabSpec` and :class:`ReleaseSpec` this is a decision and not a
    measurement, so this class states where the number lives, that it is declared
    on the TOOL (the rule is about the bit, so it covers both T11 operations),
    that the ladders built from it invent no offset and no feed, and that the
    MEASURED table declares no limit at all — which is what leaves the reference
    programs read and judged exactly as they were cut.
    """

    @classmethod
    def setUpClass(cls):
        from tests.test_r0805_regression import r0805_layout

        cls.measured = default_config()
        cls.post = post_config_for(None)
        layout, specs, nesting = r0805_layout()
        cls.layout = layout
        cls.sheet_post = post_config_for(nesting)
        cls.program, cls.plan = plan_sheet(
            layout,
            ProgramHeader(name="R080501N", created="05 AUG 26 - 07:30"),
            specs,
            nesting,
            cls.sheet_post,
        )
        cls.text = generate(cls.program, cls.plan, cls.sheet_post)
        cls.lines = cls.text.split("\r\n")

    # -- where the number lives ------------------------------------------

    def test_the_policy_value_is_named_once_and_is_scotts_number(self):
        self.assertEqual(T11_MAX_BITE, 0.4)

    def test_it_is_declared_on_the_t11_tools_of_a_generated_table_only(self):
        self.assertEqual(
            {section: tool.max_bite for section, tool in self.post.tools.items()},
            {
                SECTION_PANEL: None,
                SECTION_WDC_SLOT: None,
                SECTION_OPENINGS: T11_MAX_BITE,
                SECTION_DETAIL: None,
                SECTION_PERIMETER: T11_MAX_BITE,
                SECTION_RELEASE: None,
            },
            "the rule is about the 3/8 comp, so it rides both of its operations",
        )
        self.assertEqual(
            self.post.tool(SECTION_OPENINGS).number,
            self.post.tool(SECTION_PERIMETER).number,
            "and those two operations really are one tool",
        )

    def test_the_measured_table_declares_no_limit_anywhere(self):
        self.assertEqual(
            [tool.max_bite for tool in self.measured.tools.values()],
            [None] * len(self.measured.tools),
        )
        self.assertEqual(ToolSpec("t", "c", "(DIAMETER: 1)", 1.0, 1).max_bite, None)

    def test_a_caller_that_has_already_tuned_the_bit_is_not_overruled(self):
        base = replace(
            self.measured,
            tools={**self.measured.tools, SECTION_PERIMETER: replace(
                self.measured.tool(SECTION_PERIMETER), max_bite=0.25
            )},
        )
        cfg = post_config_for(None, base)
        self.assertEqual(cfg.tool(SECTION_PERIMETER).max_bite, 0.25)
        self.assertEqual(
            [spec.z_cut for spec in cfg.perimeter_passes],
            [0.561, 0.372, 0.183, -0.006],
            "0.756 in four equal 0.189 bites, all inside the tighter limit",
        )
        self.assertEqual(cfg.tool(SECTION_OPENINGS).max_bite, T11_MAX_BITE)

    # -- the arithmetic ---------------------------------------------------

    def test_a_ladder_is_equal_bites_and_ends_on_the_measured_pass(self):
        """Equal bites, not "as much as allowed then the remainder".

        Scott's own words for the 0.756 perimeter were that 0.4 "basically cuts
        that in half" — two passes of 0.378, not 0.4 followed by 0.356.
        """
        through = self.measured.perimeter_passes[-1]
        template = self.measured.perimeter_passes[0]
        rungs = bite_ladder(through, template, 0.4, self.measured.stock_top_z)
        self.assertEqual([spec.z_cut for spec in rungs], [0.372, -0.006])
        self.assertIs(rungs[-1], through, "the last rung IS the measured pass")
        floors = [self.measured.stock_top_z, *[spec.z_cut for spec in rungs[:-1]]]
        bites = [floor - spec.z_cut for floor, spec in zip(floors, rungs)]
        self.assertEqual([round(bite, 9) for bite in bites], [0.378, 0.378])

    def test_the_number_of_bites_is_the_ceiling_of_the_ratio(self):
        top = 0.75
        for limit, want in ((0.4, 2), (0.3, 3), (0.2, 4), (0.15, 6), (0.8, 1)):
            spec = PassSpec(z_cut=top - 0.756, offset=0.0, entry_feed=1.0, cut_feed=2.0)
            rungs = bite_ladder(spec, spec, limit, top)
            with self.subTest(max_bite=limit):
                self.assertEqual(len(rungs), want)
                bites = []
                floor = top
                for rung in rungs:
                    bites.append(round(floor - rung.z_cut, 9))
                    floor = rung.z_cut
                self.assertEqual(len(set(bites)), 1, "equal bites")
                self.assertLessEqual(max(bites), limit + 1e-9)

    def test_no_limit_and_a_cut_inside_the_limit_are_both_one_pass(self):
        spec = PassSpec(z_cut=0.5, offset=0.0, entry_feed=1.0, cut_feed=2.0)
        self.assertEqual(bite_ladder(spec, spec, None, 0.75), (spec,))
        self.assertEqual(bite_ladder(spec, spec, 0.4, 0.75), (spec,), "0.25 is fine")

    # -- the two ladders a generated sheet is cut with --------------------

    def test_the_perimeter_ladder_is_the_two_measured_passes_depths_apart(self):
        rough, through = self.post.perimeter_passes
        skin, measured_through = self.measured.perimeter_passes
        self.assertEqual(through, measured_through, "the last rung is untouched")
        self.assertEqual(
            (rough.offset, rough.entry_feed, rough.cut_feed, rough.lateral_lead),
            (skin.offset, skin.entry_feed, skin.cut_feed, skin.lateral_lead),
            "and the roughing rung is the measured pass 1, at a new depth only",
        )
        self.assertEqual(rough.z_cut, 0.372)
        self.assertAlmostEqual(rough.offset, 0.1895, places=9)

    def test_the_openings_ladder_is_the_measured_pass_at_two_depths(self):
        rough, deep = self.post.openings_passes
        self.assertEqual(deep, self.measured.openings_passes[-1])
        self.assertEqual(replace(rough, z_cut=deep.z_cut), deep, "offset and feeds")
        self.assertEqual([rough.z_cut, deep.z_cut], [0.45, 0.15])

    def test_every_rung_is_inside_the_limit_on_both_ladders(self):
        for section, passes in (
            (SECTION_OPENINGS, self.post.openings_passes),
            (SECTION_PERIMETER, self.post.perimeter_passes),
        ):
            limit = self.post.tool(section).max_bite
            floor = self.post.stock_top_z
            for position, spec in enumerate(passes):
                with self.subTest(section=section, rung=position + 1):
                    self.assertLessEqual(floor - spec.z_cut, limit + 1e-9)
                    self.assertGreater(floor - spec.z_cut, 0.0)
                floor = spec.z_cut

    def test_the_ladders_invent_no_feed_and_no_offset(self):
        """Rule zero: every number in an emitted program is a measured one."""
        measured_offsets = {
            spec.offset
            for spec in (
                *self.measured.openings_passes,
                self.measured.detail_pass,
                *self.measured.perimeter_passes,
            )
        }
        measured_feeds = {
            (spec.entry_feed, spec.cut_feed)
            for spec in (
                *self.measured.openings_passes,
                self.measured.detail_pass,
                *self.measured.perimeter_passes,
            )
        }
        for spec in (*self.post.openings_passes, *self.post.perimeter_passes):
            with self.subTest(z_cut=spec.z_cut):
                self.assertIn(spec.offset, measured_offsets)
                self.assertIn((spec.entry_feed, spec.cut_feed), measured_feeds)

    def test_the_other_tools_passes_are_untouched(self):
        """T13, T17 and T12 are other tools: not this rule's business."""
        self.assertEqual(self.post.panel, self.measured.panel)
        self.assertEqual(self.post.wdc_slot, self.measured.wdc_slot)
        self.assertEqual(self.post.detail_pass, self.measured.detail_pass)

    # -- what the emitted program looks like -----------------------------

    def test_each_profile_is_cut_once_per_rung(self):
        parts = self.program.flat_parts()
        openings = sum(len(part.openings) for part in parts)
        for spec, count in (
            (self.sheet_post.openings_passes[0], openings),
            (self.sheet_post.openings_passes[1], openings),
            (self.sheet_post.perimeter_passes[0], len(parts)),
            (self.sheet_post.perimeter_passes[1], len(parts)),
        ):
            lead_ins = [
                line
                for line in self.lines
                if line.startswith("G1 ") and f"Z{fmt(spec.z_cut)} F150." in line
            ]
            with self.subTest(z_cut=spec.z_cut):
                self.assertEqual(len(lead_ins), count)

    def test_both_bites_of_one_opening_are_emitted_back_to_back(self):
        """Like the two bites of one T17 slot: one rectangle, two depths, so the
        tool does not go away and come back."""
        depths = [
            fmt(self.sheet_post.openings_passes[0].z_cut),
            fmt(self.sheet_post.openings_passes[1].z_cut),
        ]
        order = [
            line.split("Z")[-1].split(" ")[0]
            for line in self.lines
            if line.startswith("G1 ") and any(f"Z{z} F150." in line for z in depths)
        ]
        self.assertEqual(order, depths * (len(order) // 2))
        self.assertTrue(order)

    def test_the_sheet_verifies_clean_against_its_own_manifest(self):
        self.assertEqual(
            [str(v) for v in verify(self.text, self.sheet_post,
                                   expected_work(self.layout, self.sheet_post))],
            [],
        )

    # -- the tabs are unaffected (spec §3b, and item 4 of the amendment) ---

    def test_the_shallow_rungs_are_above_the_tab_top_and_do_not_lift(self):
        """Both ladders' upper rungs cut ABOVE the 0.25 tab top, so they neither
        form a tab nor damage one — and the hold rule that refuses a shallower
        deep pass which fails to lift keys off exactly that comparison."""
        from faceframe_cnc.post import tabs as tabs_module

        top = self.post.tabs.top_z
        for passes in (self.post.openings_passes, self.post.perimeter_passes):
            for spec in passes[:-1]:
                with self.subTest(z_cut=spec.z_cut):
                    self.assertGreater(spec.z_cut, top)
                    self.assertFalse(tabs_module.lifts_over_tabs(spec.z_cut, self.post))
        # ... and the deep rungs still do
        self.assertTrue(
            tabs_module.lifts_over_tabs(self.post.openings_passes[-1].z_cut, self.post)
        )
        self.assertTrue(
            tabs_module.lifts_over_tabs(self.post.perimeter_passes[-1].z_cut, self.post)
        )

    def test_the_number_of_lifts_is_what_it_was_before_the_ladder(self):
        """The ladder adds loops, not lifts: one lift per tab per pass that goes
        below the tab top, which is still T11-deep + T12 on an opening and the
        through pass on a perimeter."""
        from faceframe_cnc.post import tabs as tabs_module

        zones = self.plan.tabs
        opening_lifts = len(
            tabs_module.lifting_cuts(tabs_module.opening_cuts(self.sheet_post), self.sheet_post)
        )
        perimeter_lifts = len(
            tabs_module.lifting_cuts(
                tabs_module.perimeter_cuts(self.sheet_post), self.sheet_post
            )
        )
        self.assertEqual((opening_lifts, perimeter_lifts), (2, 1))
        expected = sum(
            len(z) * (2 if key[1] == "opening" else 1) for key, z in zones.items()
        )
        self.assertEqual(
            self.text.count(f" Z{fmt(self.sheet_post.tabs.top_z)}"), expected
        )

    def test_the_hold_invariant_does_not_ask_a_shallow_rung_to_lift(self):
        """The refusal that WOULD have fired if the rungs were misjudged.

        ``_check_shallow_lifts`` refuses a pass that cuts below the tab top and
        does not rise over a tab.  The ladder's upper rungs cut nowhere near it,
        so the real program is clean — asserted here on the ``hold`` code alone so
        that a future change which drops a rung to 0.2 would be caught by a test
        that says what it is about.
        """
        self.assertEqual(
            [str(v) for v in verify(self.text, self.sheet_post) if v.code == "hold"], []
        )

    # -- the dry-run twin -------------------------------------------------

    def test_the_air_cut_mirrors_every_rung_and_traces_the_same_path(self):
        from faceframe_cnc.post.job import dry_run_config

        air = dry_run_config(self.sheet_post)
        top = self.sheet_post.stock_top_z
        for real, lifted in (
            (self.sheet_post.openings_passes, air.openings_passes),
            (self.sheet_post.perimeter_passes, air.perimeter_passes),
        ):
            self.assertEqual(len(real), len(lifted), "one lifted rung per rung")
            for a, b in zip(real, lifted):
                with self.subTest(z_cut=a.z_cut):
                    self.assertAlmostEqual(b.z_cut, 2 * top - a.z_cut, places=9)
                    self.assertEqual(b.offset, a.offset, "the XY path is the same")
                    self.assertEqual(
                        (b.entry_feed, b.cut_feed), (a.entry_feed, a.cut_feed)
                    )
        self.assertEqual(
            {tool.max_bite for tool in air.tools.values()},
            {None, T11_MAX_BITE},
            "the limit rides the tool, so the air cut still declares it",
        )


class MaxBiteVerifierTest(unittest.TestCase):
    """The independent half: ``max-bite`` refuses what the policy forbids.

    The verifier shares no code with the generator (an AST test forbids the
    import), so it re-derives the ladder from the TEXT: loops are grouped by the
    feature they leave behind, the group's depths are walked down from the stock
    surface, and each step has to be inside the limit the table in hand declares.
    Crafted violations, since a rule that only ever sees good files proves
    nothing.
    """

    @classmethod
    def setUpClass(cls):
        from tests.test_r0805_regression import r0805_layout

        layout, specs, nesting = r0805_layout()
        cls.layout = layout
        cls.specs = specs
        cls.nesting = nesting
        cls.post = post_config_for(nesting)
        cls.header = ProgramHeader(name="R080501N", created="05 AUG 26 - 07:30")

    def emit(self, post):
        program, plan = plan_sheet(
            self.layout, self.header, self.specs, self.nesting, post
        )
        return generate(program, plan, post)

    def bites(self, text, post):
        return [v for v in verify(text, post) if v.code == "max-bite"]

    def test_the_real_program_has_none(self):
        self.assertEqual(self.bites(self.emit(self.post), self.post), [])

    def test_a_table_that_asks_for_too_deep_a_bite_is_refused(self):
        """The configured ladder, judged before any geometry: the measured
        two-pass dialect under the 0.4 limit asks for 0.69 in one bite."""
        measured = default_config()
        bad = replace(
            self.post,
            openings_passes=measured.openings_passes,
            perimeter_passes=measured.perimeter_passes,
        )
        problems = self.bites(self.emit(bad), bad)
        self.assertTrue(problems)
        messages = " | ".join(v.message for v in problems)
        self.assertIn("a bite of 0.69", messages)
        self.assertIn("a bite of 0.6", messages, "the 0.60 opening pass too")
        self.assertIn("T11 is allowed at most 0.4 of material per pass", messages)

    def test_a_program_missing_a_rung_is_refused_by_the_file_rule(self):
        """The mutation the config check cannot see: the table is right and the
        FILE skipped a rung, so the pass that is left took the whole cut."""
        text = self.emit(self.post)
        rough = fmt(self.post.perimeter_passes[0].z_cut)
        without = drop_blocks_at(text, f"Z{rough} F150.")
        self.assertNotEqual(without, text)
        problems = self.bites(without, self.post)
        self.assertTrue(problems, [str(v) for v in verify(without, self.post)])
        self.assertEqual(len(problems), 2, "one per part on the sheet")
        for violation in problems:
            self.assertIn("takes 0.756 of material in one pass", violation.message)
            self.assertIn("owes 2 passes and the program makes fewer", violation.message)
            self.assertGreater(violation.line, 0, "and it points at the loop")

    def test_a_program_missing_an_opening_rung_is_refused_too(self):
        text = self.emit(self.post)
        rough = fmt(self.post.openings_passes[0].z_cut)
        without = drop_blocks_at(text, f"Z{rough} F150.")
        problems = self.bites(without, self.post)
        self.assertTrue(problems)
        for violation in problems:
            self.assertIn("takes 0.6 of material in one pass", violation.message)

    def test_a_ladder_emitted_deep_first_is_refused(self):
        """Order matters as much as count: run the through pass first and the
        roughing rung takes nothing, so the deep one took the lot."""
        program, plan = plan_sheet(
            self.layout, self.header, self.specs, self.nesting, self.post
        )
        swapped_cfg = replace(
            self.post,
            perimeter_passes=(
                self.post.perimeter_passes[1],
                self.post.perimeter_passes[0],
            ),
        )
        swapped = replace(plan, perimeter=[plan.perimeter[1], plan.perimeter[0]])
        text = generate(program, swapped, swapped_cfg)
        problems = self.bites(text, self.post)
        self.assertTrue(problems, [str(v) for v in verify(text, self.post)])
        self.assertIn("0.756 of material in one pass", problems[0].message)

    def test_a_table_with_no_limit_is_not_judged_at_all(self):
        """Which is what leaves the reference programs alone: the rule is a
        decision about a bit, not something measured off those files."""
        measured = default_config()
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                self.assertEqual(
                    [v for v in verify(read(name), measured) if v.code == "max-bite"],
                    [],
                )

    def test_an_air_cut_is_silent_because_it_removes_nothing(self):
        from faceframe_cnc.post.job import dry_run_config

        air = dry_run_config(self.post)
        self.assertEqual(self.bites(self.emit(air), air), [])


class ReleaseSpecTest(unittest.TestCase):
    """The release pass's own numbers, and where each of them comes from.

    Ratified by Scott on 2026-08-05 for job R0805 (spec §3c).  Two of the four
    facts are NOT the spec's own numbers and that is the point of this class: the
    tool and the depth are read from tables that already existed, so they cannot
    drift away from the pass whose kerf the release re-traces.
    """

    def test_the_measured_table_runs_no_release_pass(self):
        """Rule zero: no reference file contains a tab or a release cut."""
        self.assertIsNone(default_config().release)

    def test_a_generated_sheet_does(self):
        from faceframe_cnc.nesting import NestingConfig

        post = post_config_for(NestingConfig())
        self.assertIsNotNone(post.release)
        self.assertEqual(post.release.cut_feed, 150.0)
        self.assertEqual(post.release.entry_feed, 50.0)
        self.assertEqual(post.release.overlap, 0.1)

    def test_the_feeds_are_about_half_the_detail_passs(self):
        """What Scott approved, and what it was proposed against (spec §3c)."""
        from faceframe_cnc.nesting import NestingConfig

        post = post_config_for(NestingConfig())
        self.assertAlmostEqual(
            post.release.cut_feed / post.detail_pass.cut_feed, 0.512, places=3
        )
        self.assertAlmostEqual(
            post.release.entry_feed / post.detail_pass.entry_feed, 0.5, places=9
        )

    def test_the_release_depth_is_the_detail_passs_own(self):
        """One number, not two: the release re-traces the T12 kerf."""
        for post in (default_config(), post_config_for(None)):
            with self.subTest(release=post.release):
                self.assertEqual(post.release_z, post.detail_pass.z_cut)
                self.assertEqual(post.release_z, -0.002)

    def test_the_release_tool_is_the_measured_t12_itself(self):
        post = default_config()
        self.assertIs(post.tool(SECTION_RELEASE), post.tool(SECTION_DETAIL))
        self.assertEqual(post.tool(SECTION_RELEASE).number, 12)

    def test_the_air_cut_lifts_the_release_with_everything_else(self):
        from faceframe_cnc.post.job import dry_run_config

        post = post_config_for(None)
        air = dry_run_config(post)
        self.assertIsNotNone(air.release)
        self.assertEqual(air.release, post.release, "feeds are not depths")
        self.assertEqual(air.release_z, air.detail_pass.z_cut)
        self.assertGreater(air.release_z, air.stock_top_z)


class TabbedReadBackTest(unittest.TestCase):
    """:mod:`~faceframe_cnc.post.reconstruct` reads a tabbed program back.

    Before milestone 3 it refused one outright — "profile loop has 64 moves,
    expected 8" — because a loop that rises over a tab has four extra moves per
    tab.  Three things have to come back now: the sheet (footprints, openings,
    rotation, nesting), the tabs the lifts state, and the release section
    attributed to the profiles it frees.
    """

    @classmethod
    def setUpClass(cls):
        from tests.test_r0805_regression import r0805_layout

        layout, specs, nesting = r0805_layout()
        cls.post = post_config_for(nesting)
        cls.program, cls.plan = plan_sheet(
            layout,
            ProgramHeader(name="R080501N", created="05 AUG 26 - 07:30"),
            specs,
            nesting,
            cls.post,
        )
        cls.text = generate(cls.program, cls.plan, cls.post)
        cls.read_program, cls.read_plan = reconstruct_text(cls.text, cls.post)

    def test_the_sheet_comes_back(self):
        want = [(p.part_number, p.box.rounded(4), p.rotated) for p in self.program.flat_parts()]
        got = [(p.part_number, p.box.rounded(4), p.rotated) for p in self.read_program.flat_parts()]
        self.assertEqual([(b, r) for _, b, r in got], [(b, r) for _, b, r in want])
        self.assertEqual(
            [len(p.openings) for p in self.read_program.flat_parts()],
            [len(p.openings) for p in self.program.flat_parts()],
        )

    def test_the_sections_come_back_including_the_release(self):
        self.assertEqual(
            self.read_plan.sections,
            (
                SECTION_PANEL,
                SECTION_WDC_SLOT,
                SECTION_OPENINGS,
                SECTION_DETAIL,
                SECTION_PERIMETER,
                SECTION_RELEASE,
            ),
        )
        self.assertEqual(
            [ref.profile for ref in self.read_plan.release],
            [ref.profile for ref in self.plan.release],
            "one entry per profile the file releases, in the file's own order",
        )

    def test_the_tabs_come_back_where_the_planner_put_them(self):
        self.assertEqual(set(self.read_plan.tabs), set(self.plan.tabs))
        for key, zones in self.plan.tabs.items():
            got = self.read_plan.tabs[key]
            with self.subTest(profile=key):
                self.assertEqual(len(got), len(zones))
                self.assertEqual(
                    sorted((z.side, round(z.centre, 3), z.length) for z in got),
                    sorted((z.side, round(z.centre, 3), z.length) for z in zones),
                )

    def test_regenerating_it_reproduces_the_program(self):
        """Not required this milestone (spec §5 only re-blesses the reference
        goldens) and true anyway, which is worth pinning while it is."""
        self.assertEqual(generate(self.read_program, self.read_plan, self.post), self.text)

    def test_an_untabbed_reference_still_reads_back_untabbed(self):
        for name in ("R710101N", "R720101N", "R730101N"):
            _program, plan = reconstruct(path_of(name))
            with self.subTest(name=name):
                self.assertIsNone(plan.tabs)
                self.assertEqual(plan.release, [])
                self.assertNotIn(SECTION_RELEASE, plan.sections)

    def test_a_release_cut_over_no_tab_is_refused_rather_than_guessed(self):
        """Reading is not the same act as writing, but it is not credulous."""
        stray = self.text.replace("G1 Z-0.002 F50.", "G1 Z-0.002 F50.", 1)
        head = stray.rindex("(ROUTE TOOL")
        tail = stray[head:]
        # move every release cut on one side a long way off the flush line
        moved = tail.replace("X16.1725 ", "X11.1725 ")
        self.assertNotEqual(moved, tail)
        with self.assertRaises(ReconstructionError) as caught:
            reconstruct_text(stray[:head] + moved, self.post)
        self.assertIn("not flush with any tabbed profile", str(caught.exception))

class HoldInvariantTest(unittest.TestCase):
    """Nothing on a tabbed sheet is separated before the release section.

    The verifier's newest rule, and the one the amendment exists for: two frames
    came off the machine broken because an opening dropout was already loose
    while the perimeter was being cut.  Spec §6 asks for a crafted violation per
    refusal, and that is what this class is — one mutation each, every one of
    them a program the emitter can really produce, and every one refused.

    The rule is INDEPENDENT of both the plan and the manifest.  It re-derives the
    tabs from the commanded motion (which spans of each through profile's
    boundary were never taken below the tab top?) and never asks
    :mod:`~faceframe_cnc.post.tabs` where they were put, which is why an
    ``expected=None`` call is what these tests make.
    """

    @classmethod
    def setUpClass(cls):
        from tests.test_r0805_regression import r0805_layout

        layout, specs, nesting = r0805_layout()
        cls.layout = layout
        cls.post = post_config_for(nesting)
        cls.program, cls.plan = plan_sheet(
            layout,
            ProgramHeader(name="R080501N", created="05 AUG 26 - 07:30"),
            specs,
            nesting,
            cls.post,
        )
        cls.text = generate(cls.program, cls.plan, cls.post)

    def hold(self, text, cfg=None):
        return [v for v in verify(text, cfg or self.post) if v.code == "hold"]

    def order(self, text, cfg=None):
        return [v for v in verify(text, cfg or self.post) if v.code == "cut-order"]

    # -- the sheet as it is really cut ---------------------------------------

    def test_the_real_sheet_passes(self):
        self.assertEqual([str(v) for v in verify(self.text, self.post)], [])

    def test_the_rule_needs_no_manifest(self):
        """It is a property of the FILE, so it holds with expected=None."""
        loose = generate(
            self.program, replace(self.plan, tabs=None, release=[]), self.post
        )
        self.assertTrue(self.hold(loose))

    def test_a_reference_file_has_no_hold_findings(self):
        """The measured table runs no release pass, so the rule is silent.

        R710101N was cut in the two-pass dialect with no tab anywhere in it, and
        judging it by a rule its own CAM never had would refuse three files this
        repo keeps as evidence.  The switch is the post table
        (:attr:`~faceframe_cnc.post.model.PostConfig.release`), which the measured
        one leaves unset.
        """
        self.assertIsNone(default_config().release)
        for name in ("R710101N", "R720101N", "R730101N"):
            with self.subTest(name=name):
                codes = {v.code for v in verify(read(name))}
                self.assertNotIn("hold", codes)

    # -- refusal 1: a profile freed before the release section ---------------

    def test_an_untabbed_program_is_refused_as_freed_early(self):
        loose = generate(
            self.program, replace(self.plan, tabs=None, release=[]), self.post
        )
        problems = self.hold(loose)
        self.assertTrue(problems)
        for problem in problems:
            self.assertIn("no holding tab anywhere on it", problem.message)
            self.assertIn("loose from here on", problem.message)
        # one per through profile: two part footprints and two openings
        self.assertEqual(len(problems), 4)
        self.assertTrue(
            any("part footprint" in p.message for p in problems)
            and any("opening" in p.message for p in problems),
            [p.message for p in problems],
        )

    def test_dropping_the_tabs_from_one_profile_alone_is_refused(self):
        """The narrowest version: three profiles held, one not."""
        key = (1, "perimeter", 0)
        tabs_map = {k: v for k, v in self.plan.tabs.items() if k != key}
        text = generate(
            self.program,
            replace(
                self.plan,
                tabs=tabs_map,
                release=[ref for ref in self.plan.release if ref.profile != key],
            ),
            self.post,
        )
        problems = self.hold(text)
        self.assertEqual(len(problems), 1)
        self.assertIn("no holding tab anywhere on it", problems[0].message)
        self.assertIn("30x33 part footprint", problems[0].message)

    # -- refusal 2: a bridge nothing releases --------------------------------

    def test_tabs_with_no_release_section_are_refused(self):
        text = generate(self.program, replace(self.plan, release=[]), self.post)
        problems = self.hold(text)
        total = sum(len(zones) for zones in self.plan.tabs.values())
        self.assertEqual(len(problems), total, "one per tab left standing")
        self.assertIn("no release cut ever removes", problems[0].message)
        self.assertIn("still attached to the sheet", problems[0].message)
        self.assertIn("no T12 release section at all", problems[0].message)

    def test_releasing_only_the_openings_leaves_the_frames_attached(self):
        text = generate(
            self.program,
            replace(
                self.plan,
                release=[ref for ref in self.plan.release if ref.kind == "opening"],
            ),
            self.post,
        )
        problems = self.hold(text)
        perimeter_tabs = sum(
            len(z) for key, z in self.plan.tabs.items() if key[1] == "perimeter"
        )
        self.assertEqual(len(problems), perimeter_tabs)
        self.assertTrue(all("part footprint" in p.message for p in problems))

    # -- refusal 3: a tab released twice -------------------------------------

    def test_releasing_one_profile_twice_is_refused(self):
        text = generate(
            self.program,
            replace(self.plan, release=self.plan.release + [self.plan.release[-1]]),
            self.post,
        )
        problems = self.hold(text)
        self.assertEqual(len(problems), len(self.plan.tabs[self.plan.release[-1].profile]))
        self.assertIn("is released 2 times", problems[0].message)

    # -- refusal 4: the centreline release spec §8 forbids -------------------

    def test_the_emitter_refuses_a_post_table_whose_release_is_not_flush(self):
        """The first of the two lines of defence, and the cheaper one.

        Spec §8 forbids a centreline release, and the flush path is derived from
        the detail pass's offset and the release tool's radius, so a table where
        those two disagree cannot emit a flush release at all.  The generator says
        so before a byte is written (:func:`~faceframe_cnc.post.generator._check_config`),
        which is why the crafted violation below has to be made by hand.
        """
        wrong = replace(
            self.post,
            tools={
                **self.post.tools,
                SECTION_RELEASE: ToolSpec(
                    number=12,
                    header_comment=self.post.tool(SECTION_RELEASE).header_comment,
                    diameter_comment="(DIAMETER: 0.375)",
                    diameter=0.375,
                    speed=self.post.tool(SECTION_RELEASE).speed,
                ),
            },
        )
        with self.assertRaises(ValueError) as caught:
            generate(self.program, self.plan, wrong)
        message = str(caught.exception)
        self.assertIn("not flush with the finished line", message)
        self.assertIn("leave a rib on the finished edge", message)

    def test_a_release_cut_on_the_t11_centreline_is_refused(self):
        """Spec §8: "do not leave tab ribs on finished edges".

        The mistake that "works": run the release down the middle of the kerf the
        T11 already cut, and it removes the tab — all but the ~0.09 rib of it left
        standing on the FINISHED edge, where it is the operator's problem with a
        chisel.  So the crafted violation moves one part's release cuts from the
        flush line out to the T11 centreline, 0.0875 away, and the verifier has to
        refuse the file even though every cut in it is at a legal depth, a legal
        feed and inside the sheet.

        Made as a text edit because the generator will not emit it (the test
        above), which is the honest way round: the second authority is being asked
        to catch what the first one cannot produce.
        """
        text, moved = self.centreline_release()
        self.assertNotEqual(text, self.text)
        problems = self.hold(text)
        self.assertTrue(problems, [str(v) for v in verify(text, self.post)])
        messages = " | ".join(p.message for p in problems)
        self.assertIn("is not flush with any profile", messages)
        self.assertIn("leaves a rib of tab standing", messages)
        self.assertIn("0.0875 away", messages)
        self.assertEqual(len(problems), 2 * moved, "the cut, and the tab it left")

    def centreline_release(self) -> tuple[str, int]:
        """``(text, tabs moved)``: the release cuts of one side, off the flush
        line and onto the T11 kerf's centreline.

        The side is found from the emitter's own motion stream rather than
        transcribed, so the surgery follows the geometry instead of a coordinate
        that could go stale.
        """
        from faceframe_cnc.post.generator import emit

        radius = self.post.tool(SECTION_RELEASE).radius
        parts = self.program.flat_parts()
        for motion in emit(self.program, self.plan, self.post).motions:
            if motion.section != SECTION_RELEASE or motion.feature.kind != "perimeter":
                continue
            if abs(motion.to_y - motion.from_y) <= abs(motion.to_x - motion.from_x):
                continue  # a cut along X: its constant coordinate is Y
            box = parts[motion.feature.part].box
            flush = release_path(box, "perimeter", radius)
            if abs(motion.from_x - flush.x1) > 1e-9:
                continue  # the low side; the high one keeps the arithmetic plain
            centreline = box.x1 + self.post.perimeter_passes[-1].offset
            head = self.text.rindex("(ROUTE TOOL")
            was = f"X{fmt(flush.x1)} "
            now = f"X{fmt(centreline)} "
            tail = self.text[head:]
            moved = tail.count(was)
            self.assertTrue(moved, "the surgery must actually find its lines")
            return self.text[:head] + tail.replace(was, now), moved
        self.fail("no perimeter release cut runs along Y on this sheet")

    def test_the_real_offsets_are_what_that_test_is_about(self):
        """The two paths and the 0.0875 between them, from the tables."""
        radius = self.post.tool(SECTION_RELEASE).radius
        part = self.program.flat_parts()[0]
        flush = release_path(part.box, "perimeter", radius)
        centreline = part.box.grow(self.post.perimeter_passes[-1].offset)
        self.assertAlmostEqual(flush.x1 - part.box.x1, 0.1, places=9)
        self.assertAlmostEqual(centreline.x1 - flush.x1, 0.0875, places=9)
        opening = part.openings[0]
        self.assertEqual(
            release_path(opening, "opening", radius),
            opening.grow(self.post.detail_pass.offset),
            "an opening's release path IS the T12 detail path (spec §3c)",
        )

    # -- refusal 5: the wrong feeds -----------------------------------------

    def test_release_cuts_at_the_detail_passs_feeds_are_refused(self):
        """"Very slowly" is the whole character of the pass (spec §3c).

        The generic feed rule cannot catch this — T12 legitimately runs the
        detail pass's 293/100 as well, and that rule judges a tool against the
        whole set of feeds its table gives it — so the hold invariant checks the
        release moves against the release feeds by name.
        """
        fast = replace(
            self.post, release=replace(self.post.release, cut_feed=293.0, entry_feed=100.0)
        )
        text = generate(self.program, self.plan, fast)
        self.assertEqual(
            [v.code for v in verify(text, fast) if v.code != "hold"],
            [],
            "the file is legal in every other way",
        )
        problems = self.hold(text, self.post)
        total = sum(len(zones) for zones in self.plan.tabs.values())
        self.assertEqual(len(problems), 2 * total, "the plunge and the cut of each")
        messages = " | ".join(p.message for p in problems)
        self.assertIn("runs at F100. - the release pass runs at F50.", messages)
        self.assertIn("runs at F293. - the release pass runs at F150.", messages)
        self.assertIn("Scott ratified", messages)

    # -- refusal 6: a shallower deep pass that cuts the tab away -------------

    def test_an_opening_pass_that_does_not_lift_is_refused(self):
        """Spec §3b: EVERY pass below the tab top lifts, not just the deepest.

        The T11 opening pass cuts to Z0.15, a tenth below the tab top, so a T11
        that failed to lift would leave 0.15 of tab where 0.25 was ratified — the
        tab is still there, the program still looks right, and the frame is held
        by 60% of what anybody intended.  Crafted by raising the tab top just
        above the T11's depth, so the T12 detail pass still lifts (it is deeper)
        and the T11 no longer does, then judged against the real table.
        """
        text = drop_lifts(self.text, self.post.openings_passes[-1].z_cut)
        self.assertNotEqual(text, self.text)
        problems = self.hold(text)
        self.assertTrue(problems, [str(v) for v in verify(text, self.post)])
        opening_tabs = sum(
            len(z) for key, z in self.plan.tabs.items() if key[1] == "opening"
        )
        shallow = [v for v in problems if "ran straight through" in v.message]
        self.assertEqual(len(shallow), opening_tabs, "one per opening tab")
        self.assertIn("Z0.15", shallow[0].message)
        self.assertIn("has to lift over every tab", shallow[0].message)

    def test_a_two_pass_table_holds_its_skin_pass_to_the_same_rule(self):
        """The case spec §3b spells out: the skin must lift or it destroys the
        tab before the through pass can preserve it.

        The measured PASSES and the measured TOOLS: a 0.06/-0.006 pair under the
        2026-08-05 bite limit is a table ``max-bite`` refuses (0.69 in one bite),
        and this test is about the onion skin's lift, not about that.
        """
        measured = default_config()
        two = replace(
            self.post,
            tools=measured.tools,
            openings_passes=measured.openings_passes,
            perimeter_passes=measured.perimeter_passes,
        )
        program, plan = plan_sheet(
            self.layout,
            ProgramHeader(name="R080501N", created="05 AUG 26 - 07:30"),
            None,
            None,
            two,
        )
        self.assertEqual(len(plan.perimeter), 2)
        self.assertEqual([str(v) for v in verify(generate(program, plan, two), two)], [])
        skin = two.perimeter_passes[0]
        flattened = drop_lifts(generate(program, plan, two), skin.z_cut)
        problems = [
            v
            for v in verify(flattened, two)
            if v.code == "hold" and "ran straight through" in v.message
        ]
        self.assertTrue(problems, [str(v) for v in verify(flattened, two)])
        self.assertIn(f"Z{skin.z_cut:g}", " | ".join(v.message for v in problems))

    # -- the order of the release section (spec §3c) -------------------------

    def test_perimeter_tabs_released_before_opening_tabs_is_refused(self):
        text = generate(
            self.program, replace(self.plan, release=list(reversed(self.plan.release))), self.post
        )
        problems = self.order(text)
        self.assertEqual(len(problems), 1, [str(v) for v in problems])
        self.assertIn("AFTER a part perimeter", problems[0].message)
        self.assertIn("while its frame is still held", problems[0].message)

    def test_a_cut_after_the_release_section_is_refused(self):
        late = replace(
            self.plan,
            sections=(
                SECTION_WDC_SLOT,
                SECTION_OPENINGS,
                SECTION_DETAIL,
                SECTION_PERIMETER,
                SECTION_RELEASE,
                SECTION_PANEL,
            ),
        )
        problems = self.order(generate(self.program, late, self.post))
        self.assertTrue(problems)
        self.assertIn("last machining in the program", problems[0].message)

    def test_a_host_released_before_its_passenger_is_refused(self):
        """Inners before hosts, on a nested sheet (spec §3c)."""
        from tests.test_nc_job import nested_sample

        result, config = nested_sample()
        post = post_config_for(config)
        layout = result.unique_sheets[0][0]
        program, plan = plan_sheet(
            layout,
            ProgramHeader(name="R990101N", created="05 AUG 26 - 07:30"),
            result.demand,
            config,
            post,
        )
        openings = [ref for ref in plan.release if ref.kind == "opening"]
        perimeters = [ref for ref in plan.release if ref.kind == "perimeter"]
        self.assertGreater(len(perimeters), 1)
        text = generate(
            program,
            replace(plan, release=openings + list(reversed(perimeters))),
            post,
        )
        problems = [v for v in verify(text, post) if v.code == "cut-order"]
        self.assertTrue(problems, [str(v) for v in verify(text, post)])
        self.assertIn("is nested in", problems[0].message)
        self.assertIn("already loose", problems[0].message)

    # -- what the manifest adds ---------------------------------------------

    def test_the_manifest_says_a_tabbed_sheet_owes_a_release_section(self):
        manifest = expected_work(self.layout, self.post)
        self.assertTrue(manifest.release)
        self.assertFalse(
            expected_work(self.layout, default_config()).release,
            "the measured table owes none",
        )
        none = generate(self.program, replace(self.plan, tabs=None, release=[]), self.post)
        missing = [
            v
            for v in verify(none, self.post, manifest)
            if v.code == "missing-cut"
        ]
        self.assertEqual(len(missing), 1)
        self.assertIn("no release cut at all", missing[0].message)
        self.assertIn("still attached to the sheet by its tabs", missing[0].message)

    def test_release_cuts_in_a_program_that_owes_none_are_an_extra_cut(self):
        manifest = expected_work(self.layout, self.post)
        extra = [
            v
            for v in verify(self.text, self.post, replace(manifest, release=False))
            if v.code == "extra-cut"
        ]
        self.assertEqual(len(extra), 1)
        self.assertIn("runs no release pass", extra[0].message)

    def test_the_bridges_the_verifier_finds_are_the_tabs_the_plan_placed(self):
        """The two derivations agree — which is the point of having both.

        The verifier counts the spans of each through profile the program never
        took below the tab top; the plan says how many tabs it put there.  Equal
        counts, from two arithmetics that share no code.
        """
        from faceframe_cnc.post import verifier as v

        moves, _ = v._simulate(self.text.split("\r\n"), self.post)
        profiles = v._through_profiles(v._cut_runs(moves), self.post)
        found = sum(len(p.bridges) for p in profiles)
        self.assertEqual(found, sum(len(z) for z in self.plan.tabs.values()))
        for profile in profiles:
            for bridge in profile.bridges:
                with self.subTest(profile=profile.describe(), side=bridge.side):
                    self.assertAlmostEqual(
                        bridge.length, self.post.tabs.length, places=3
                    )

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
        # This class is about the ``rapid`` rule, so that is the precondition
        # it needs.  The file itself is no longer finding-free: since the
        # 2026-08-05 amendment it carries its own pre-amendment groove overruns
        # (:data:`LEGACY_GROOVE_FOREIGN_CUTS`), which have nothing to do with
        # rapids and must not be allowed to mask them either.
        self.assertEqual([v for v in verify(self.text) if v.code == "rapid"], [])
        assert_only_legacy_grooves(self, "R710101N", verify(self.text))

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
        self.assertEqual([v for v in verify(self.text) if v.code == "rapid"], [])
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

    def test_the_references_carry_only_their_grooves_and_a_real_intrusion_still_fails(
        self,
    ):
        """The ramp relaxation is still exactly as wide as it was.

        R710101N's findings are its own pre-amendment groove overruns and
        nothing else (:data:`LEGACY_GROOVE_FOREIGN_CUTS`) — no ramp of it is
        reported — and moving a perimeter loop into its neighbour still is.
        """
        assert_only_legacy_grooves(self, "R710101N", verify_file(path_of("R710101N")))
        text = read("R710101N").replace("X32.055\r\n", "X29.055\r\n", 1)
        moved = [
            v
            for v in verify(text)
            if v.code == "foreign-cut"
            and v.line not in LEGACY_GROOVE_FOREIGN_CUTS["R710101N"]
        ]
        self.assertTrue(moved, [str(v) for v in verify(text)])


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
