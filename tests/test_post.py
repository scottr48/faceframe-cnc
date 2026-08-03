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
      everything, pass 2 inners before hosts).

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
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


class GenerationApiTest(unittest.TestCase):
    """The API phase 2 drives: build a program, control the ordering."""

    def sample_program(self) -> SheetProgram:
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
            placements, ProgramHeader(name="R990101N", created="01 JAN 27 - 08:00")
        )

    def full_plan(self, program: SheetProgram, inners_first: bool = False) -> CutPlan:
        parts = program.flat_parts()
        openings = [
            FeatureRef(i, "opening", j)
            for i, part in enumerate(parts)
            for j in range(len(part.openings))
        ]
        panel = [
            FeatureRef(i, "groove", j) for i in range(len(parts)) for j in range(4)
        ]
        order = list(range(len(parts)))
        pass_two = order
        if inners_first:
            hosts = {
                parts.index(child) for part in parts for child in part.children
            }
            pass_two = sorted(order, key=lambda i: (i not in hosts, i))
        return CutPlan(
            panel=panel,
            openings=openings,
            perimeter=[
                [FeatureRef(i, "perimeter") for i in order],
                [FeatureRef(i, "perimeter") for i in pass_two],
            ],
        )

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

    def test_the_verifier_shares_no_emission_code(self):
        """It must be able to disagree with the generator, so it may not
        import it (the templates are a deliberate second copy)."""
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
        self.assertNotIn("generator", " ".join(sorted(imported)))
        self.assertNotIn("reconstruct", " ".join(sorted(imported)))


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
