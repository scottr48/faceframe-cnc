"""Milestone 5 phase 2: NC generation for OPTIMIZED sheets.

Phase 1 proved the post by round-tripping files the machine had already
run.  Nothing here round-trips: these sheets have never existed, which is
the whole point, so what is checked instead is

  (a) the real 7-21-26 order, nested at the default 0.455 gap, generates a
      verified program for EVERY unique sheet -- zero refusals -- and the
      0.375 gap the spec asks for still cannot (the finding is pinned, not
      hidden);
  (b) WDC sheets carry their T17 stile slot, in the right section, on the
      right centreline, with the right per-pass depth and overrun; a WDC
      whose slot would reach a neighbour or the sheet edge is refused;
  (c) the 2026-08-03 onion-skin order is really in the emitted text: pass 1
      on every part before any pass-2 cut, and every nested inner cut free
      before its host;
  (d) openings (including nested inners' own openings) are all cut before
      any perimeter;
  (e) naming, O-numbers and the banner;
  (f) dry-run files air-cut, verify under the dry-run table and are
      rejected by the production one;
  (g) the same input produces byte-identical files apart from the date;
  (h) nothing is ever written for a sheet the verifier rejects;
  (i) the milestone's acceptance run: the whole order, at the shipping
      defaults, written to disk with zero refusals, in production and in
      dry-run form;
  (j) 2026-08-04 review -- HOW the files reach the disk: a program is
      published atomically (a failed write cannot truncate the file that is
      already there), and a folder regenerated for the same prefix ends up
      holding this job and nothing else, with every stale file quarantined
      rather than deleted and every move reported;
  (k) 2026-08-04 review -- that every sheet contains every cut its layout
      calls for, production and rehearsal, checked against a manifest built
      from the layout rather than from the plan the emitter used.  The
      mutation tests behind that check (delete a pass, delete an opening,
      delete a slot) live in ``tests/test_verifier_manifest.py``.

Stdlib only.  Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from faceframe_cnc.nesting import (
    NestingConfig,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    nest,
    validate_layouts,
)
from faceframe_cnc.post import (
    JobError,
    JobOptions,
    ProgramHeader,
    build_job,
    default_config,
    dry_run_config,
    generate,
    plan_sheet,
    post_config_for,
    sheet_filename,
    verify,
    write_job,
)
from faceframe_cnc.post.job import (
    PARTIAL_SUFFIX,
    SUPERSEDED_DIR_NAME,
    job_file_pattern,
    now_stamp,
)
from faceframe_cnc.post.from_layout import (
    WDC_SLOT_END_REACH,
    WdcNotSupportedError,
    cut_plan_for,
    part_depths,
    panel_groove_indices,
    sheet_program_from_layout,
    wdc_slot_lines,
)
from faceframe_cnc.post.reconstruct import reconstruct, reconstruct_text
from faceframe_cnc.post.verifier import expected_work
from tests.test_nesting import ORDER_7_21_26

CREATED = "01 JAN 27 - 08:00"
NC_DIR = os.path.join(os.path.dirname(__file__), "..", "reference", "nc_files")


def nested_order(part_gap: float = 0.455) -> tuple[NestingResult, NestingConfig]:
    config = NestingConfig(inside_nesting=True, part_gap=part_gap)
    return nest(ORDER_7_21_26, config), config


def has_wdc(contents: dict) -> bool:
    return any(name.upper().startswith("WDC") for name in contents)


def job_for(result, **overrides):
    options = JobOptions(
        output_dir=overrides.pop("output_dir", "unused"),
        prefix=overrides.pop("prefix", "7201"),
        created=overrides.pop("created", CREATED),
        **overrides,
    )
    return build_job(result, options)


# --------------------------------------------------------------------------
# (a) + (b) the real order
# --------------------------------------------------------------------------


class RealOrderTest(unittest.TestCase):
    """The 7-21-26 acceptance fixture, nested, at both candidate gaps."""

    @classmethod
    def setUpClass(cls):
        cls.jobs = {}
        for gap in (0.375, 0.455):
            result, config = nested_order(gap)
            cls.jobs[gap] = (result, config, job_for(result))

    def test_the_layouts_themselves_are_valid(self):
        for gap, (result, config, _job) in self.jobs.items():
            with self.subTest(gap=gap):
                self.assertEqual(validate_layouts(result, config), [])

    def test_every_sheet_generates_and_verifies_at_the_production_gap(self):
        """The Milestone 5 acceptance test: no sheet of the real order is
        refused, WDC frames included."""
        result, _config, job = self.jobs[0.455]
        self.assertEqual(len(job.outcomes), result.unique_sheet_count)
        for outcome in job.outcomes:
            with self.subTest(sheet=outcome.filename):
                self.assertEqual(outcome.problems, [], outcome.describe())
                self.assertIsNotNone(outcome.text)
        self.assertEqual([o.filename for o in job.outcomes if not o.ok], [])

    def test_the_wdc_sheets_are_the_ones_carrying_a_t17_section(self):
        _result, _config, job = self.jobs[0.455]
        wdc = [o for o in job.outcomes if has_wdc(o.contents)]
        plain = [o for o in job.outcomes if not has_wdc(o.contents)]
        self.assertTrue(wdc, "the order contains WDC2436")
        self.assertTrue(plain, "the order also has plain sheets")
        for outcome in wdc:
            with self.subTest(sheet=outcome.filename):
                self.assertIn("(ROUTE TOOL #17:", outcome.text)
        for outcome in plain:
            with self.subTest(sheet=outcome.filename):
                self.assertNotIn("T17", outcome.text)

    def test_the_only_non_wdc_refusal_is_the_gap_being_too_narrow(self):
        """The 0.375 finding, pinned rather than hidden.

        A perimeter lead-in ramp stands 0.05 off the profile, the profile is
        0.1875 outside the part edge and the tool radius is 0.1875, so the
        swept tool reaches 0.425 past the part edge.  Two parts 0.375 apart
        are 0.05 short of that, and the verifier says so.  Nothing is
        written for those sheets.  This is why 0.455 is the default.
        """
        _result, _config, job = self.jobs[0.375]
        refused = [o for o in job.outcomes if not o.ok]
        self.assertTrue(refused, "0.375 cannot be cut; that is the whole finding")
        for outcome in refused:
            with self.subTest(sheet=outcome.filename):
                self.assertEqual(outcome.refusal_kind, "verifier")
                self.assertTrue(
                    all("foreign-cut" in p for p in outcome.problems),
                    outcome.problems,
                )

    def test_the_reference_files_space_parts_at_the_gap_the_post_needs(self):
        """R710101N's own spacing is 0.455, not the spec's 0.375."""
        program, _plan = reconstruct(os.path.join(NC_DIR, "R710101N.anc"))
        boxes = [p.box for p in program.flat_parts()]
        gaps = set()
        for i, a in enumerate(boxes):
            for b in boxes[i + 1 :]:
                dx = max(a.x0, b.x0) - min(a.x1, b.x1)
                dy = max(a.y0, b.y0) - min(a.y1, b.y1)
                for value in (dx, dy):
                    if value > 1e-6:
                        gaps.add(round(value, 4))
        self.assertIn(0.455, gaps, sorted(gaps))

        cfg = default_config()
        pass_two = cfg.perimeter_passes[-1]
        reach = pass_two.offset + pass_two.lateral_lead + cfg.tools["perimeter"].radius
        self.assertAlmostEqual(reach, 0.425)
        self.assertGreater(0.455, reach)
        self.assertLess(0.375, reach)

    def test_the_job_covers_every_unique_sheet_exactly_once(self):
        for gap, (result, _config, job) in self.jobs.items():
            with self.subTest(gap=gap):
                self.assertEqual(len(job.outcomes), result.unique_sheet_count)
                self.assertEqual(
                    sum(o.run_quantity for o in job.outcomes), result.total_sheets
                )


# --------------------------------------------------------------------------
# (b) the T17 WDC stile slot, in detail
# --------------------------------------------------------------------------


def wdc_sheet(x: float = 4.0, y: float = 4.0, rotated: bool = False):
    """One WDC2436 alone on a sheet, clear of everything."""
    w, h = (36.0, 18.0) if rotated else (18.0, 36.0)
    layout = SheetLayout([Placement("WDC2436", x, y, w, h, rotated=rotated)])
    demand = [PartSpec("WDC2436", 18.0, 36.0, 1)]
    config = NestingConfig()
    return (
        NestingResult(
            unique_sheets=[(layout, 1)], total_sheets=1, demand=demand, config=config
        ),
        config,
    )


class WdcSlotSectionTest(unittest.TestCase):
    """What a WDC sheet actually emits, read back out of the text."""

    @classmethod
    def setUpClass(cls):
        cls.result, cls.config = wdc_sheet()
        cls.outcome = job_for(cls.result).outcomes[0]
        assert cls.outcome.ok, cls.outcome.describe()
        cls.lines = cls.outcome.text.split("\r\n")

    def section(self, number: int) -> list[str]:
        """The body of the ``(ROUTE TOOL #number ...)`` section."""
        heads = [
            i for i, line in enumerate(self.lines) if line.startswith("(ROUTE TOOL")
        ]
        for position, head in enumerate(heads):
            if self.lines[head].startswith(f"(ROUTE TOOL #{number}:"):
                end = heads[position + 1] if position + 1 < len(heads) else len(self.lines)
                return self.lines[head:end]
        self.fail(f"no T{number} section")

    def test_the_section_sits_after_t13_and_before_the_first_t11(self):
        heads = [line for line in self.lines if line.startswith("(ROUTE TOOL")]
        numbers = [int(re.match(r"\(ROUTE TOOL #(\d+)", h).group(1)) for h in heads]
        self.assertEqual(numbers, [13, 17, 11, 12, 11])

    def test_the_tool_block_is_verbatim_rfk0101n(self):
        body = self.section(17)
        self.assertEqual(body[0], "(ROUTE TOOL #17: T17 45 VTIP 158-562SC.026-1W-A)")
        self.assertEqual(body[1], "(DIAMETER: 0.96)")
        self.assertEqual(body[2], "M59")
        self.assertTrue(body[3].startswith("G0 G54 G90 X"))
        self.assertEqual(body[4], "T17")
        self.assertTrue(body[5].endswith("M13 S16000"))
        self.assertEqual(body[6], "G43 H17 Z2.5")
        self.assertEqual(body[7], "G0 Z2.")
        # ... down to the section tail (the last line is the blank that
        # separates this section from the next).
        self.assertEqual(body[-5:-1], ["M59", "G80", "G17 G91 G28 Z0 M95", "M92"])

        # Straight plunge, one cut move, retract -- no lateral ramp.
        self.assertEqual(body.count("G1 Z0.4062 F150."), 2)
        self.assertEqual(body.count("G1 Z0.3125 F150."), 2)
        self.assertEqual(len([b for b in body if b.endswith("F400.")]), 4)
        self.assertNotIn("F150.", body[5])

    def test_two_stiles_two_passes_each_on_the_measured_centrelines(self):
        cuts = self.slot_cuts()
        self.assertEqual(len(cuts), 4, "two stiles, two depth passes each")
        # 2" stile, centreline 34 mm from the INSIDE edge => 0.6614 from the
        # outside one; the part sits at x=4 and is 18 wide.
        inset = 2.0 - 1.3386
        self.assertEqual(
            [c["x"] for c in cuts],
            [4.0 + inset, 4.0 + inset, 22.0 - inset, 22.0 - inset],
            "low-side stile first, both its passes, then the high side",
        )
        self.assertEqual([c["z"] for c in cuts], [0.4062, 0.3125, 0.4062, 0.3125])

    def test_the_overrun_is_the_bit_radius_at_that_pass_s_depth(self):
        for cut in self.slot_cuts():
            with self.subTest(z=cut["z"]):
                # 45 degrees per side: the effective radius IS the depth of
                # cut, and 0.75 is the top of the stock.
                overrun = round(0.75 - cut["z"], 4)
                self.assertAlmostEqual(cut["y0"], 4.0 - overrun, places=4)
                self.assertAlmostEqual(cut["y1"], 40.0 + overrun, places=4)
                self.assertLess(overrun, 0.48, "past the T17 shoulder radius")

    def test_a_rotated_wdc_puts_its_slots_on_the_other_axis(self):
        result, _config = wdc_sheet(rotated=True)
        outcome = job_for(result).outcomes[0]
        self.assertTrue(outcome.ok, outcome.describe())
        cuts = self.slot_cuts(outcome.text.split("\r\n"), along="x")
        inset = 2.0 - 1.3386
        self.assertEqual(
            [c["y"] for c in cuts],
            [4.0 + inset, 4.0 + inset, 22.0 - inset, 22.0 - inset],
        )
        for cut in cuts:
            overrun = round(0.75 - cut["z"], 4)
            self.assertAlmostEqual(cut["x0"], 4.0 - overrun, places=4)
            self.assertAlmostEqual(cut["x1"], 40.0 + overrun, places=4)

    def slot_cuts(self, lines=None, along: str = "y"):
        """``[{across, along-start, along-end, z}]`` read out of the T17 block."""
        lines = lines if lines is not None else self.lines
        heads = [i for i, line in enumerate(lines) if line.startswith("(ROUTE TOOL")]
        head = next(i for i in heads if lines[i].startswith("(ROUTE TOOL #17:"))
        end = next((i for i in heads if i > head), len(lines))
        across = "x" if along == "y" else "y"

        cuts = []
        position = {"x": 0.0, "y": 0.0}
        pending = None
        for raw in lines[head:end]:
            for letter, value in re.findall(r"([XYZ])(-?\d*\.?\d+)", raw):
                if letter in ("X", "Y"):
                    position[letter.lower()] = float(value)
            if raw.startswith("G1 Z"):
                pending = {
                    across: position[across],
                    f"{along}0": position[along],
                    "z": float(re.search(r"Z(-?[\d.]+)", raw).group(1)),
                }
            elif pending is not None and raw.endswith("F400."):
                pending[f"{along}1"] = position[along]
                cuts.append(pending)
                pending = None
        return cuts

    def test_the_generated_wdc_sheet_verifies(self):
        self.assertEqual(
            [str(v) for v in verify(self.outcome.text, post_config_for(self.config))],
            [],
        )

    def test_a_wdc_frame_keeps_its_rail_grooves_only(self):
        self.assertEqual(panel_groove_indices("W2436"), (0, 2, 1, 3))
        self.assertEqual(panel_groove_indices("WDC2436"), (1, 3))
        # ... and that is what the T13 section emits: two grooves, not four.
        self.assertEqual(
            len([line for line in self.section(13) if line.startswith("G1 Z0.55")]), 2
        )

    def test_the_tool_table_matches_the_reference_file_it_came_from(self):
        path = os.path.join(NC_DIR, "RFK0101N.anc")
        with open(path, "r", newline="") as handle:
            reference = handle.read()
        tool = default_config().tools["wdc_slot"]
        self.assertIn(tool.header_comment, reference)
        self.assertIn(tool.diameter_comment, reference)
        self.assertIn(f"S{tool.speed}", reference)
        self.assertIn("G43 H17 Z2.5", reference)
        self.assertEqual(tool.diameter, 0.96)

    def test_the_slot_geometry_helper_agrees_with_the_emitted_code(self):
        from faceframe_cnc.post import from_layout

        program = sheet_program_from_layout(
            SheetLayout([Placement("W2436", 0.0, 0.0, 24.0, 36.0)]),
            ProgramHeader(name="R1", created=CREATED),
        )
        part = program.flat_parts()[0]
        (a0, a1), (b0, b1) = wdc_slot_lines(part, overrun=0.0)
        inset = 2.0 - 1.3386
        self.assertAlmostEqual(a0[0], inset)
        self.assertAlmostEqual(b0[0], 24.0 - inset)
        self.assertEqual((a0[1], a1[1]), (0.0, 36.0))
        self.assertEqual((b0[1], b1[1]), (0.0, 36.0))
        self.assertAlmostEqual(from_layout.wdc_slot_z(), 0.3125)
        self.assertEqual(from_layout.WDC_SLOT_PASS_DEPTHS, (0.4062, 0.3125))

    def test_a_wdc_sheet_reads_back_and_regenerates_byte_for_byte(self):
        """The round-trip proof, applied to the section this milestone added.

        Reconstruction recovers the part's rotation from the T17 slots
        themselves — a WDC has no T13 stile groove to vote with — and the
        slot's two passes collapse back into the one plan entry that emitted
        them.
        """
        for rotated in (False, True):
            with self.subTest(rotated=rotated):
                width, height = (36.0, 18.0) if rotated else (18.0, 36.0)
                layout = SheetLayout(
                    [
                        Placement("WDC2436", 4.0, 4.0, width, height, rotated=rotated),
                        Placement("W2436", 4.0, 44.0, 24.0, 36.0),
                    ]
                )
                demand = [
                    PartSpec("WDC2436", 18.0, 36.0, 1),
                    PartSpec("W2436", 24.0, 36.0, 1),
                ]
                program, plan = plan_sheet(
                    layout,
                    ProgramHeader(name="R990101N", created=CREATED),
                    demand,
                    NestingConfig(),
                )
                text = generate(program, plan)
                again, replan = reconstruct_text(text)
                self.assertEqual(
                    [(r.part, r.index) for r in replan.wdc_slot],
                    [(r.part, r.index) for r in plan.wdc_slot],
                )
                self.assertEqual(
                    again.flat_parts()[0].rotated,
                    rotated,
                    "rotation must come back out of the slots",
                )
                self.assertEqual(generate(again, replan), text)

    def test_a_t17_section_this_post_did_not_write_is_refused(self):
        from faceframe_cnc.post.reconstruct import ReconstructionError

        text = self.outcome.text
        # move one pass off its centreline: no part owns that slot any more
        broken = text.replace("X4.6614 Y3.5625 Z2.5", "X5.1 Y3.5625 Z2.5", 1)
        self.assertNotEqual(broken, text)
        with self.assertRaises(ReconstructionError):
            reconstruct_text(broken)

    def test_there_is_no_flag_that_skips_the_slot(self):
        import inspect

        from faceframe_cnc.post import from_layout

        source = inspect.getsource(from_layout)
        self.assertNotIn("allow_wdc", source)
        self.assertNotIn("skip_wdc", source)
        self.assertIsNotNone(from_layout.T17, "the WDC slot is cut now")


class WdcClearanceRefusalTest(unittest.TestCase):
    """A slot that would reach something is refused, three times over."""

    def crowded(self, offset: float):
        """A WDC with a neighbour ``offset`` beyond its top stile ends."""
        layout = SheetLayout(
            [
                Placement("WDC2436", 4.0, 4.0, 18.0, 36.0),
                Placement("W2436", 4.0, 40.0 + offset, 24.0, 36.0),
            ]
        )
        demand = [
            PartSpec("WDC2436", 18.0, 36.0, 1),
            PartSpec("W2436", 24.0, 36.0, 1),
        ]
        config = NestingConfig()
        return (
            NestingResult(
                unique_sheets=[(layout, 1)],
                total_sheets=1,
                demand=demand,
                config=config,
            ),
            config,
        )

    def test_the_reach_is_twice_the_slot_depth(self):
        self.assertAlmostEqual(WDC_SLOT_END_REACH, 0.875)

    def test_a_neighbour_half_an_inch_past_the_stile_end_is_refused(self):
        """0.5 clears the 0.455 part gap and is still inside the cone."""
        result, config = self.crowded(0.5)
        with self.assertRaises(WdcNotSupportedError) as caught:
            plan_sheet(
                result.unique_sheets[0][0],
                ProgramHeader(name="R990101N", created=CREATED),
                result.demand,
                config,
            )
        message = str(caught.exception)
        for fragment in ("WDC2436", "W2436", "T17", "0.875"):
            self.assertIn(fragment, message)

    def test_the_optimizer_never_produces_such_a_layout(self):
        result, config = self.crowded(0.5)
        problems = validate_layouts(result, config)
        self.assertTrue(problems, "validate_layouts must flag the near neighbour")
        self.assertTrue(any("T17" in p or "45-degree" in p for p in problems), problems)

    def test_the_verifier_rejects_the_file_if_one_is_force_generated(self):
        """Bypass the planner's check and let the independent verifier see it.

        The plan is built against a post table whose slot is a scratch 0.05
        deep — which clears everything — and then EMITTED with the real
        table.  The plan carries no depths (that is the point of a plan), so
        the file that comes out is the real 7/16 slot beside a part 0.5
        away, and nothing but the verifier's own geometry stands in the way.
        """
        from dataclasses import replace

        result, config = self.crowded(0.5)
        production = post_config_for(config)
        shallow = replace(
            production,
            wdc_slot=replace(production.wdc_slot, z_cuts=(0.7,)),
        )
        program, plan = plan_sheet(
            result.unique_sheets[0][0],
            ProgramHeader(name="R990101N", created=CREATED),
            result.demand,
            config,
            shallow,
        )
        self.assertEqual(len(plan.wdc_slot), 2)

        text = generate(program, plan, production)
        violations = verify(text, production)
        codes = {v.code for v in violations}
        self.assertIn("v-slot", codes, [str(v) for v in violations])

    def test_a_wdc_too_close_to_the_sheet_edge_is_refused(self):
        layout = SheetLayout([Placement("WDC2436", 4.0, 0.5, 18.0, 36.0)])
        demand = [PartSpec("WDC2436", 18.0, 36.0, 1)]
        config = NestingConfig()
        with self.assertRaises(WdcNotSupportedError) as caught:
            plan_sheet(
                layout,
                ProgramHeader(name="R990101N", created=CREATED),
                demand,
                config,
            )
        self.assertIn("sheet", str(caught.exception))

        result = NestingResult(
            unique_sheets=[(layout, 1)],
            total_sheets=1,
            demand=demand,
            config=config,
        )
        self.assertTrue(validate_layouts(result, config))

    def test_a_comfortable_neighbour_is_fine(self):
        result, config = self.crowded(WDC_SLOT_END_REACH)
        self.assertEqual(validate_layouts(result, config), [])
        outcome = job_for(result).outcomes[0]
        self.assertTrue(outcome.ok, outcome.describe())

    def test_the_verifier_catches_a_slot_swept_off_the_sheet(self):
        """Hand-shift a clean file's slot to the sheet edge.  The centreline
        still sits inside the 0.375 overhang; the CONE does not."""
        result, config = wdc_sheet()
        production = post_config_for(config)
        text = job_for(result).outcomes[0].text
        self.assertEqual([str(v) for v in verify(text, production)], [])

        # The part sits at y=4, so Y3.5625 is where the deep pass starts,
        # 0.4375 below it.  Drop that start to the sheet edge: the commanded
        # point is legal (Y0 is on the sheet) but the cone reaches Y-0.4375,
        # past the 0.375 the trim margin allows.
        moved = text.replace("Y3.5625", "Y0.", 1)
        self.assertNotEqual(moved, text)
        violations = verify(moved, production)
        self.assertIn("v-slot", {v.code for v in violations})
        self.assertNotIn(
            "bounds",
            {v.code for v in violations},
            "the CENTRELINE is inside the overhang; only the cone is not",
        )

    def test_the_verifier_catches_a_slot_cut_to_an_unplanned_depth(self):
        result, config = wdc_sheet()
        production = post_config_for(config)
        text = job_for(result).outcomes[0].text
        deeper = text.replace("G1 Z0.3125 F150.", "G1 Z0.05 F150.", 1)
        codes = {v.code for v in verify(deeper, production)}
        self.assertIn("geometry", codes)


# --------------------------------------------------------------------------
# (c) + (d) the cutting order, read back out of the emitted text
# --------------------------------------------------------------------------


def nested_sample() -> tuple[NestingResult, NestingConfig]:
    """A two-host sheet with a frame nested in each, plus a loose frame."""
    config = NestingConfig(inside_nesting=True, part_gap=0.455)
    layout = SheetLayout(
        [
            Placement(
                "W2742",
                0.5,
                1.0,
                27.0,
                42.0,
                children=[Placement("W3012", 8.0, 7.0, 12.0, 30.0, rotated=True)],
            ),
            Placement(
                "W2436",
                28.5,
                1.0,
                24.0 - 4.0,
                36.0,
                children=[],
            ),
        ]
    )
    # the second placement above is a plain 20x36 frame, kept simple so the
    # sheet stays inside 49" with the 0.455 gap
    layout.placements[1] = Placement("W2036", 28.5, 1.0, 20.0, 36.0)
    demand = [
        PartSpec("W2742", 27.0, 42.0, 1),
        PartSpec("W3012", 30.0, 12.0, 1),
        PartSpec("W2036", 20.0, 36.0, 1),
    ]
    result = NestingResult(
        unique_sheets=[(layout, 1)], total_sheets=1, demand=demand, config=config
    )
    return result, config


class CuttingOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result, cls.config = nested_sample()
        cls.job = job_for(cls.result)
        cls.outcome = cls.job.outcomes[0]
        assert cls.outcome.ok, cls.outcome.describe()
        cls.text = cls.outcome.text
        cls.lines = cls.text.split("\r\n")

    def test_the_layout_used_here_is_itself_legal(self):
        self.assertEqual(validate_layouts(self.result, self.config), [])

    def test_section_order_is_panel_openings_detail_perimeter(self):
        heads = [line for line in self.lines if line.startswith("(ROUTE TOOL")]
        numbers = [int(re.match(r"\(ROUTE TOOL #(\d+)", h).group(1)) for h in heads]
        self.assertEqual(numbers, [13, 11, 12, 11])

    def test_every_opening_is_cut_before_any_perimeter(self):
        """Sections do it structurally; this checks the emitted Z words."""
        cfg = default_config()
        opening_z = f"Z{_fmt(cfg.openings_pass.z_cut)} "
        detail_z = f"Z{_fmt(cfg.detail_pass.z_cut)} "
        skin_z = f"Z{_fmt(cfg.perimeter_passes[0].z_cut)} "
        last_opening = max(
            i for i, line in enumerate(self.lines) if opening_z in line or detail_z in line
        )
        first_perimeter = min(i for i, line in enumerate(self.lines) if skin_z in line)
        self.assertLess(last_opening, first_perimeter)

    def test_the_first_t11_section_cuts_the_nested_inners_own_openings(self):
        program, plan = reconstruct_text(self.text)
        parts = program.flat_parts()
        depths = part_depths(program)
        inners = {i for i, d in enumerate(depths) if d > 0}
        self.assertTrue(inners, "the sample sheet must have a nested frame")
        cut = {ref.part for ref in plan.openings}
        self.assertTrue(
            inners <= cut,
            "an inner frame's own opening must be routed while its slab is "
            "still host waste",
        )
        # ... and every other part's openings are in there too
        self.assertEqual(
            sorted(cut),
            sorted(i for i, part in enumerate(parts) if part.openings),
        )

    def test_openings_run_deepest_nesting_first(self):
        program, plan = reconstruct_text(self.text)
        depths = part_depths(program)
        order = [ref.part for ref in plan.openings]
        seen_shallow = False
        for index in order:
            if depths[index] == 0:
                seen_shallow = True
            elif seen_shallow:
                self.fail("a nested inner's opening was cut after a host's")

    def test_onion_skin_pass_one_finishes_before_pass_two_starts(self):
        cfg = default_config()
        skin = f"Z{_fmt(cfg.perimeter_passes[0].z_cut)} "
        through = f"Z{_fmt(cfg.perimeter_passes[1].z_cut)} "
        skin_lines = [i for i, line in enumerate(self.lines) if skin in line]
        through_lines = [i for i, line in enumerate(self.lines) if through in line]
        self.assertTrue(skin_lines and through_lines)
        self.assertLess(
            max(skin_lines),
            min(through_lines),
            "every part must be taken to the onion skin before any is cut free",
        )

    def test_pass_two_frees_every_inner_before_any_host(self):
        program, plan = reconstruct_text(self.text)
        depths = part_depths(program)
        self.assertEqual(len(plan.perimeter), 2)

        first = [ref.part for ref in plan.perimeter[0]]
        second = [ref.part for ref in plan.perimeter[1]]
        self.assertEqual(sorted(first), list(range(len(program.flat_parts()))))
        self.assertEqual(sorted(second), sorted(first))

        inner_positions = [i for i, part in enumerate(second) if depths[part] > 0]
        host_positions = [i for i, part in enumerate(second) if depths[part] == 0]
        self.assertTrue(inner_positions and host_positions)
        self.assertLess(
            max(inner_positions),
            min(host_positions),
            "pass 2 must free every nested frame before any outer part",
        )

    def test_pass_one_uses_the_plain_canonical_order(self):
        program, plan = reconstruct_text(self.text)
        self.assertEqual(
            [ref.part for ref in plan.perimeter[0]],
            list(range(len(program.flat_parts()))),
        )

    def test_grooves_are_stiles_then_rails_per_part_in_canonical_order(self):
        program, plan = reconstruct_text(self.text)
        parts = program.flat_parts()
        got = [(ref.part, ref.index) for ref in plan.panel]
        want = [
            (index, groove)
            for index in range(len(parts))
            for groove in panel_groove_indices(parts[index].part_number)
        ]
        self.assertEqual(got, want)
        self.assertFalse(any(ref.reverse for ref in plan.panel))

    def test_no_lead_in_override_leaks_out_of_the_reference_files(self):
        _program, plan = reconstruct_text(self.text)
        overrides = [ref for ref in plan.openings if ref.entry is not None]
        overrides += [
            ref for refs in plan.perimeter for ref in refs if ref.entry is not None
        ]
        self.assertEqual(overrides, [], "generated sheets use the default entry rule")

    def test_the_generated_sheet_verifies(self):
        self.assertEqual([str(v) for v in verify(self.text, post_config_for(self.config))], [])


def _fmt(value: float) -> str:
    from faceframe_cnc.post.generator import fmt

    return fmt(value)


# --------------------------------------------------------------------------
# (e) naming, O-numbers, banner
# --------------------------------------------------------------------------


class NamingAndBannerTest(unittest.TestCase):
    def setUp(self):
        self.result, self.config = nested_order(0.455)

    def test_file_names_and_o_numbers_run_in_step(self):
        job = job_for(self.result, prefix="7201")
        for position, outcome in enumerate(job.outcomes):
            self.assertEqual(outcome.filename, f"R7201{position + 1:02d}N.anc")
            self.assertEqual(outcome.o_number, position + 1)
        self.assertEqual(sheet_filename("62", 2), "R6202N.anc")
        self.assertEqual(sheet_filename("7301", 12), "R730112N.anc")

    def test_a_refused_sheet_keeps_its_index_rather_than_renumbering_the_rest(self):
        """The sheet index is on the operator's paperwork, so a refusal
        leaves a gap in the FILES rather than shifting every later sheet.

        The default gap generates everything, so this needs the 0.375 job,
        which the verifier refuses sheet by sheet.
        """
        tight, _config = nested_order(0.375)
        job = job_for(tight)
        refused = [o.sheet_index for o in job.outcomes if not o.ok]
        self.assertTrue(refused, "0.375 must still be refused")
        indices = [o.sheet_index for o in job.outcomes]
        self.assertEqual(indices, list(range(1, len(indices) + 1)))
        self.assertTrue(any(o.ok for o in job.outcomes))

    def test_the_o_number_line_matches_the_file_name(self):
        job = job_for(self.result)
        outcome = next(o for o in job.outcomes if o.ok)
        lines = outcome.text.split("\r\n")
        self.assertEqual(
            lines[1], f"O{outcome.o_number:04d} ({outcome.filename[:-4]})"
        )

    def test_the_banner_sits_where_the_verifier_accepts_it(self):
        job = job_for(self.result)
        outcome = next(o for o in job.outcomes if o.ok)
        lines = outcome.text.split("\r\n")
        self.assertEqual(lines[3], "(MATERIAL: MDF 3/4 )")
        self.assertEqual(lines[4], "(LOAD: Material face DOWN)")
        banner = []
        index = 5
        while lines[index].startswith("("):
            banner.append(lines[index])
            index += 1
        self.assertEqual(lines[index], "G0 G20 G91 G28 Z0 M15")
        self.assertTrue(banner)
        self.assertIn("FACEFRAME NESTING OPTIMIZER", banner[0])
        self.assertTrue(any("SHEET" in line and "RUN QTY" in line for line in banner))
        self.assertTrue(any(line.startswith("(CONTENTS:") for line in banner))
        for name, count in outcome.contents.items():
            self.assertIn(f"{count}x{name}", " ".join(banner))
        self.assertEqual([str(v) for v in verify(outcome.text)], [])

    def test_the_banner_reports_nested_frames(self):
        job = job_for(self.result)
        outcome = next(o for o in job.outcomes if o.ok and o.nested)
        self.assertIn(f"(NESTED: {outcome.nested} FRAME", outcome.text)

    def test_no_banner_line_carries_a_stray_parenthesis(self):
        job = job_for(self.result, prefix="1")
        outcome = next(o for o in job.outcomes if o.ok)
        for line in outcome.text.split("\r\n"):
            if line.startswith("(") and not line.startswith("(ROUTE"):
                self.assertEqual(line.count("("), 1, line)
                self.assertEqual(line.count(")"), 1, line)

    def test_a_bad_prefix_is_refused_before_anything_is_planned(self):
        for bad in ("", "72-01", "abcd", "123456789"):
            with self.subTest(prefix=bad):
                with self.assertRaises(JobError):
                    job_for(self.result, prefix=bad)

    def test_a_job_that_would_need_a_three_digit_index_is_refused(self):
        with self.assertRaises(JobError) as caught:
            job_for(self.result, first_sheet_index=90)
        self.assertIn("two digits", str(caught.exception))

    def test_one_file_per_physical_sheet_is_available(self):
        unique = job_for(self.result)
        physical = job_for(self.result, per_physical_sheet=True)
        self.assertEqual(len(unique.outcomes), self.result.unique_sheet_count)
        self.assertEqual(len(physical.outcomes), self.result.total_sheets)
        self.assertTrue(all(o.run_quantity == 1 for o in physical.outcomes))
        self.assertEqual(
            len({o.filename for o in physical.outcomes}), self.result.total_sheets
        )


# --------------------------------------------------------------------------
# (f) dry run
# --------------------------------------------------------------------------


class DryRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result, cls.config = nested_sample()
        cls.production = job_for(cls.result).outcomes[0]
        cls.air = job_for(cls.result, dry_run=True).outcomes[0]

    def test_the_air_cut_generates_and_is_marked(self):
        self.assertTrue(self.air.ok, self.air.describe())
        self.assertIn("DRY RUN", self.air.text)
        self.assertIn("NOT A PRODUCTION PROGRAM", self.air.text)
        self.assertNotIn("DRY RUN", self.production.text)

    def test_no_cutting_move_reaches_the_stock(self):
        top = default_config().stock_top_z
        feeding = False
        for line in self.air.text.split("\r\n"):
            codes = {int(g) for g in re.findall(r"G(\d+)", line)}
            if 0 in codes:
                feeding = False
            elif 1 in codes:
                feeding = True
            match = re.search(r"Z(-?\d*\.?\d+)", line)
            if feeding and match:
                self.assertGreater(
                    float(match.group(1)),
                    top - 1e-9,
                    f"a feed move dips into the stock: {line!r}",
                )

    def test_it_verifies_under_the_dry_run_table_and_fails_the_production_one(self):
        production = post_config_for(self.config)
        air = dry_run_config(production)
        self.assertTrue(air.dry_run)
        self.assertEqual([str(v) for v in verify(self.air.text, air)], [])
        self.assertTrue(
            verify(self.air.text, production),
            "a lifted file must not pass as a production program",
        )

    def test_the_dry_run_check_catches_a_cut_that_is_not_lifted(self):
        air = dry_run_config(post_config_for(self.config))
        sabotaged = self.air.text.replace("Z1.506 F150.", "Z-0.006 F150.", 1)
        self.assertNotEqual(sabotaged, self.air.text)
        codes = {v.code for v in verify(sabotaged, air)}
        self.assertIn("dry-run", codes)

    def test_only_the_z_words_and_ramp_lengths_move(self):
        """Same tools, feeds, speeds and section structure as production."""
        def skeleton(text):
            return [
                line
                for line in text.split("\r\n")
                if line.startswith("(ROUTE")
                or line.startswith("(DIAMETER")
                or re.match(r"^T\d+$", line)
                or line in ("M59", "G80", "G17 G91 G28 Z0 M95", "M92")
            ]

        self.assertEqual(skeleton(self.air.text), skeleton(self.production.text))
        for feed in ("F150.", "F545.", "F490.", "F498.2", "F293.", "F100."):
            self.assertEqual(
                self.air.text.count(feed),
                self.production.text.count(feed),
                feed,
            )

    def test_the_depths_are_derived_from_the_measured_table(self):
        cfg = default_config()
        air = dry_run_config(cfg)
        top = cfg.stock_top_z
        for real, lifted in (
            (cfg.panel.z_cut, air.panel.z_cut),
            (cfg.openings_pass.z_cut, air.openings_pass.z_cut),
            (cfg.detail_pass.z_cut, air.detail_pass.z_cut),
        ):
            self.assertAlmostEqual(lifted, 2 * top - real)
            self.assertGreater(lifted, top)
            self.assertLess(lifted, air.approach_z)
        self.assertEqual(air.z_min, cfg.z_min)  # G28 Z0 must still be legal
        self.assertEqual(air.rapid_z, cfg.rapid_z)
        self.assertEqual(air.approach_z, cfg.approach_z)

    def test_a_dry_run_is_refused_when_the_production_program_would_be(self):
        """The air cut is a rehearsal; there is nothing to rehearse if the
        real program is unsafe."""
        result, _config = crowded_sheet()
        real = job_for(result).outcomes[0]
        air = job_for(result, dry_run=True).outcomes[0]
        self.assertFalse(real.ok)
        self.assertFalse(air.ok)
        self.assertEqual(air.refusal_kind, "verifier")


# --------------------------------------------------------------------------
# (g) determinism, (h) the write gate
# --------------------------------------------------------------------------


def crowded_sheet() -> tuple[NestingResult, NestingConfig]:
    """Two parts exactly 0.375 apart: legal to the packer, too tight for the
    perimeter lead-in's swept tool (0.425)."""
    config = NestingConfig(part_gap=0.375)
    layout = SheetLayout(
        [
            Placement("W2036", 1.0, 1.0, 20.0, 36.0),
            Placement("W2436", 21.375, 1.0, 24.0, 36.0),
        ]
    )
    demand = [PartSpec("W2436", 24.0, 36.0, 1), PartSpec("W2036", 20.0, 36.0, 1)]
    return (
        NestingResult(
            unique_sheets=[(layout, 1)], total_sheets=1, demand=demand, config=config
        ),
        config,
    )


class WriteGateTest(unittest.TestCase):
    def test_files_land_on_disk_with_crlf_and_the_right_names(self):
        result, _config = nested_sample()
        with tempfile.TemporaryDirectory() as folder:
            job = write_job(
                result,
                JobOptions(output_dir=folder, prefix="7201", created=CREATED),
            )
            self.assertEqual(sorted(os.listdir(folder)), ["R720101N.anc"])
            with open(job.files[0], "r", newline="") as handle:
                text = handle.read()
            self.assertTrue(text.startswith("%\r\n"))
            self.assertTrue(text.endswith("M30\r\n%\r\n"))
            self.assertEqual(text, job.outcomes[0].text)

    def test_a_sheet_the_verifier_rejects_leaves_no_file_behind(self):
        result, _config = crowded_sheet()
        with tempfile.TemporaryDirectory() as folder:
            job = write_job(
                result, JobOptions(output_dir=folder, prefix="99", created=CREATED)
            )
            self.assertEqual(os.listdir(folder), [])
            self.assertEqual(len(job.refused), 1)
            self.assertEqual(job.refused[0].refusal_kind, "verifier")
            self.assertTrue(any("foreign-cut" in p for p in job.refused[0].problems))

    def test_a_wdc_sheet_with_room_for_its_slot_is_written(self):
        config = NestingConfig()
        result = NestingResult(
            unique_sheets=[(SheetLayout([Placement("WDC2436", 1.0, 1.0, 18.0, 36.0)]), 1)],
            total_sheets=1,
            demand=[PartSpec("WDC2436", 18.0, 36.0, 1)],
            config=config,
        )
        with tempfile.TemporaryDirectory() as folder:
            job = write_job(
                result, JobOptions(output_dir=folder, prefix="99", created=CREATED)
            )
            self.assertEqual(job.refused, [], job.summary())
            self.assertEqual(os.listdir(folder), ["R9901N.anc"])
            with open(job.files[0], "r", newline="") as handle:
                self.assertIn("T17", handle.read())

    def test_a_wdc_sheet_whose_slot_runs_off_the_sheet_leaves_no_file_behind(self):
        """0.1 from the front edge: legal to look at, and the slot would cut
        0.775 into the machine's spoilboard fence."""
        config = NestingConfig()
        result = NestingResult(
            unique_sheets=[(SheetLayout([Placement("WDC2436", 1.0, 0.1, 18.0, 36.0)]), 1)],
            total_sheets=1,
            demand=[PartSpec("WDC2436", 18.0, 36.0, 1)],
            config=config,
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(JobError):
                # the layout validator catches it before any sheet is planned
                write_job(
                    result, JobOptions(output_dir=folder, prefix="99", created=CREATED)
                )
            self.assertEqual(os.listdir(folder), [])

        # ... and if it somehow got past the validator, plan_sheet refuses.
        with self.assertRaises(WdcNotSupportedError):
            plan_sheet(
                result.unique_sheets[0][0],
                ProgramHeader(name="R990101N", created=CREATED),
                result.demand,
                config,
            )

    def test_an_invalid_layout_stops_the_whole_job(self):
        config = NestingConfig(inside_nesting=True)
        # a child that does not fit inside its host's opening
        layout = SheetLayout(
            [
                Placement(
                    "W2436",
                    1.0,
                    1.0,
                    24.0,
                    36.0,
                    children=[Placement("B30", 2.0, 2.0, 30.0, 30.0)],
                )
            ]
        )
        result = NestingResult(
            unique_sheets=[(layout, 1)],
            total_sheets=1,
            demand=[PartSpec("W2436", 24.0, 36.0, 1), PartSpec("B30", 30.0, 30.0, 1)],
            config=config,
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(JobError) as caught:
                write_job(
                    result, JobOptions(output_dir=folder, prefix="99", created=CREATED)
                )
            self.assertIn("validator", str(caught.exception))
            self.assertEqual(os.listdir(folder), [])

    def test_an_empty_result_is_refused(self):
        config = NestingConfig()
        empty = NestingResult(unique_sheets=[], total_sheets=0, demand=[], config=config)
        with self.assertRaises(JobError):
            build_job(empty, JobOptions(output_dir="x", prefix="1"))

    def test_a_part_the_order_does_not_list_is_refused(self):
        config = NestingConfig()
        result = NestingResult(
            unique_sheets=[(SheetLayout([Placement("W2436", 1.0, 1.0, 24.0, 36.0)]), 1)],
            total_sheets=1,
            demand=[PartSpec("W2436", 24.0, 36.0, 1)],
            config=config,
        )
        # sneak an unordered part past the packer's own validator
        result.unique_sheets[0][0].placements.append(
            Placement("W2436", 26.0, 1.0, 24.0, 36.0)
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(JobError):
                write_job(
                    result, JobOptions(output_dir=folder, prefix="1", created=CREATED)
                )


# --------------------------------------------------------------------------
# (j) 2026-08-04 review: how the files reach the disk
# --------------------------------------------------------------------------


#: Frame sizes for the throwaway one-frame-per-sheet fixtures below.  Real
#: enough for the geometry engine and the verifier; small enough that a
#: quarantine test is not a nesting test.
_FIXTURE_SIZES = {
    "W2036": (20.0, 36.0),
    "W2436": (24.0, 36.0),
    "W3036": (30.0, 36.0),
}


def one_frame_per_sheet(*part_numbers: str) -> tuple[NestingResult, NestingConfig]:
    """A job of ``len(part_numbers)`` unique sheets, one frame on each.

    The point is the FILE COUNT: these tests are about what is in the output
    folder after a job that is longer or shorter than the one before it, so
    the sheets themselves are deliberately trivial and all generate clean.
    """
    config = NestingConfig(part_gap=0.455)
    sheets = []
    demand = []
    for name in part_numbers:
        width, height = _FIXTURE_SIZES[name]
        sheets.append((SheetLayout([Placement(name, 1.0, 1.0, width, height)]), 1))
        demand.append(PartSpec(name, width, height, 1))
    result = NestingResult(
        unique_sheets=sheets,
        total_sheets=len(sheets),
        demand=demand,
        config=config,
    )
    return result, config


class _DiesHalfWay:
    """A file handle that writes half the program and then reports a full disk.

    Stands in for the shop PC losing the disk (or the power) part way through
    a write — the failure the old ``open(path, "w")`` turned into a truncated
    production file.
    """

    def __init__(self, handle):
        self._handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._handle.close()
        return False

    def write(self, text):
        self._handle.write(text[: len(text) // 2])
        raise OSError(28, "no space left on device")

    def flush(self):
        self._handle.flush()

    def fileno(self):
        return self._handle.fileno()


class StaleFileQuarantineTest(unittest.TestCase):
    """The output folder must hold THIS job and nothing that looks like it.

    Three shop-floor stories, all of them ending with the operator cutting a
    program nobody generated for this order:

    (a) the order shrinks and yesterday's higher-numbered files stay put;
    (b) a sheet is refused this run and yesterday's file of that name stays,
        so the ONE sheet the post refused to stand behind is the one still
        cuttable;
    (c) a write dies half way (see :class:`AtomicWriteTest`).

    The rule is one line — a file matching this job's names that this run did
    not write is not part of this job — and the answer is quarantine, never
    delete: it is somebody's previous job and may be its only copy.
    """

    STAMP = "20260804-151204"

    def write(self, result, folder, **overrides):
        options = JobOptions(
            output_dir=folder,
            prefix=overrides.pop("prefix", "7201"),
            created=CREATED,
            quarantine_stamp=overrides.pop("quarantine_stamp", self.STAMP),
            **overrides,
        )
        return write_job(result, options)

    def read(self, *parts) -> str:
        with open(os.path.join(*parts), "r", newline="") as handle:
            return handle.read()

    def test_a_first_run_into_a_clean_folder_quarantines_nothing(self):
        result, _config = one_frame_per_sheet("W2036", "W2436")
        with tempfile.TemporaryDirectory() as folder:
            job = self.write(result, folder)
            self.assertEqual(job.superseded, [])
            self.assertEqual(job.quarantine_problems, [])
            self.assertTrue(job.quarantine_ok)
            self.assertIsNone(job.quarantine_dir, "no empty folder is left behind")
            self.assertEqual(
                sorted(os.listdir(folder)), ["R720101N.anc", "R720102N.anc"]
            )

    def test_regenerating_a_shorter_job_quarantines_the_leftover_sheets(self):
        """Failure mode (a): three sheets yesterday, one today."""
        long_run, _config = one_frame_per_sheet("W2036", "W2436", "W3036")
        short_run, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            first = self.write(long_run, folder)
            self.assertEqual(len(first.written), 3)
            stale_text = {
                name: self.read(folder, name)
                for name in ("R720102N.anc", "R720103N.anc")
            }

            second = self.write(short_run, folder, quarantine_stamp="20260804-160000")
            self.assertEqual([o.filename for o in second.written], ["R720101N.anc"])
            self.assertEqual(sorted(os.listdir(folder)), ["R720101N.anc", "superseded"])
            self.assertEqual(second.quarantine_problems, [])
            self.assertEqual(
                second.quarantine_dir,
                os.path.join(second.output_dir, SUPERSEDED_DIR_NAME, "20260804-160000"),
            )
            self.assertEqual(
                [item.filename for item in second.superseded],
                ["R720102N.anc", "R720103N.anc"],
            )
            for item in second.superseded:
                with self.subTest(file=item.filename):
                    self.assertIn("no sheet", item.reason)
                    self.assertFalse(os.path.exists(item.old_path), "it moved")
                    self.assertEqual(
                        self.read(item.new_path),
                        stale_text[item.filename],
                        "quarantine copies the bytes, it does not rewrite them",
                    )
            # ... and the job says so, in words the UI can show verbatim.
            self.assertEqual(len(second.superseded_lines()), 2)
            self.assertIn("R720103N.anc", second.summary())
            self.assertIn("nothing deleted", second.summary())

    def test_a_refused_sheets_stale_file_is_quarantined_not_left_looking_current(self):
        """Failure mode (b), and the one that matters most.

        Yesterday's R9901N.anc was fine; today's sheet 1 puts two parts 0.375
        apart and the verifier refuses it.  ``written`` stays False and the
        production name must end up EMPTY — a refusal that leaves a cuttable
        file of that name is worse than no refusal at all.
        """
        good, _config = one_frame_per_sheet("W2036")
        crowded, _config = crowded_sheet()
        with tempfile.TemporaryDirectory() as folder:
            self.write(good, folder, prefix="99")
            before = self.read(folder, "R9901N.anc")

            second = self.write(
                crowded, folder, prefix="99", quarantine_stamp="20260804-170000"
            )
            outcome = second.outcomes[0]
            self.assertEqual(outcome.filename, "R9901N.anc")
            self.assertEqual(outcome.refusal_kind, "verifier")
            self.assertFalse(outcome.written)
            self.assertIsNone(outcome.path)
            self.assertFalse(os.path.exists(os.path.join(folder, "R9901N.anc")))
            self.assertEqual(sorted(os.listdir(folder)), [SUPERSEDED_DIR_NAME])

            self.assertEqual([i.filename for i in second.superseded], ["R9901N.anc"])
            self.assertIn("refused", second.superseded[0].reason)
            self.assertEqual(outcome.superseded_path, second.superseded[0].new_path)
            self.assertEqual(self.read(outcome.superseded_path), before)
            self.assertIn(outcome.superseded_path, outcome.describe())

    def test_files_that_are_not_this_jobs_are_never_touched(self):
        """The sweep matches the job's exact naming pattern, not ``R*.anc``.

        Everything below is somebody else's business: another prefix's
        programs, prefixes whose digit count differs by one either way, this
        job's own PDF paperwork, a hand backup, a shop note, and index 00,
        which no job can write because the first sheet index is 1.
        """
        result, _config = one_frame_per_sheet("W2036")
        others = {
            "R720201N.anc": "another prefix's job",
            "R72010N.anc": "prefix 720 sheet 10 - one digit short",
            "R7201001N.anc": "prefix 72010 sheet 01 - one digit long",
            "R7201_report.pdf": "this job's own paperwork",
            "R720102N.bak": "somebody's hand backup",
            "R720100N.anc": "index 00, which no job can have written",
            "notes.txt": "the shop's own note",
        }
        with tempfile.TemporaryDirectory() as folder:
            for name, text in others.items():
                with open(os.path.join(folder, name), "w", newline="") as handle:
                    handle.write(text)

            job = self.write(result, folder)
            self.assertEqual(job.superseded, [])
            self.assertEqual(job.quarantine_problems, [])
            self.assertIsNone(job.quarantine_dir)
            self.assertNotIn(SUPERSEDED_DIR_NAME, os.listdir(folder))
            for name, text in others.items():
                with self.subTest(file=name):
                    self.assertEqual(self.read(folder, name), text)

    def test_a_partial_left_by_an_interrupted_run_is_quarantined_too(self):
        """Half a program is not a program, whatever it is called.

        Nothing a completed write leaves behind is called ``.partial``, so one
        in the folder is an earlier run that died.  Sheet 01's is simply
        reused — this run writes through that exact name and publishes it —
        but sheet 05's belongs to no sheet of this job and stays half a
        program for ever, so out it goes.
        """
        result, _config = one_frame_per_sheet("W2036")
        alive = "R720101N.anc" + PARTIAL_SUFFIX
        orphan = "R720105N.anc" + PARTIAL_SUFFIX
        with tempfile.TemporaryDirectory() as folder:
            for name in (alive, orphan):
                with open(os.path.join(folder, name), "w", newline="") as handle:
                    handle.write("%\r\nO0001 (HALF A PROGRAM")

            job = self.write(result, folder)
            self.assertEqual([i.filename for i in job.superseded], [orphan])
            self.assertIn("interrupted", job.superseded[0].reason)
            self.assertTrue(os.path.exists(job.superseded[0].new_path))
            self.assertEqual(job.quarantine_problems, [])
            self.assertEqual(
                sorted(os.listdir(folder)), ["R720101N.anc", SUPERSEDED_DIR_NAME]
            )
            self.assertEqual(self.read(folder, "R720101N.anc"), job.outcomes[0].text)

    def test_two_runs_in_the_same_second_get_their_own_folders(self):
        """The Generate button is right there; two runs can share a second."""
        long_run, _config = one_frame_per_sheet("W2036", "W2436")
        short_run, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            self.write(long_run, folder)
            second = self.write(short_run, folder)
            self.write(long_run, folder)
            fourth = self.write(short_run, folder)
            self.assertEqual(os.path.basename(second.quarantine_dir), self.STAMP)
            self.assertEqual(
                os.path.basename(fourth.quarantine_dir), f"{self.STAMP}-2"
            )
            self.assertEqual(len(second.superseded), 1)
            self.assertEqual(len(fourth.superseded), 1)
            self.assertEqual(
                sorted(os.listdir(os.path.join(folder, SUPERSEDED_DIR_NAME))),
                [self.STAMP, f"{self.STAMP}-2"],
            )

    def test_a_stale_file_that_cannot_be_moved_is_a_loud_problem(self):
        """Locked by the machine's file browser, read-only, no permission.

        The folder is then NOT safe to hand over, so the failure is reported
        on the job by name — never swallowed, and never turned into a delete.
        """
        from faceframe_cnc.post import job as job_module

        long_run, _config = one_frame_per_sheet("W2036", "W2436")
        short_run, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            self.write(long_run, folder)
            real_replace = job_module.os.replace

            def refuse_the_move(src, dst):
                if SUPERSEDED_DIR_NAME in str(dst):
                    raise OSError(13, "permission denied")
                return real_replace(src, dst)

            job_module.os.replace = refuse_the_move
            try:
                job = self.write(short_run, folder)
            finally:
                job_module.os.replace = real_replace

            self.assertEqual(job.superseded, [])
            self.assertFalse(job.quarantine_ok)
            self.assertTrue(
                any("R720102N.anc" in p for p in job.quarantine_problems),
                job.quarantine_problems,
            )
            self.assertTrue(
                any("permission denied" in p for p in job.quarantine_problems),
                job.quarantine_problems,
            )
            self.assertIn("STALE FILE STILL", job.summary())
            self.assertTrue(
                os.path.exists(os.path.join(folder, "R720102N.anc")),
                "the report has to be honest: the file really is still there",
            )
            self.assertEqual([o.filename for o in job.written], ["R720101N.anc"])

    def test_the_pattern_is_the_exact_inverse_of_sheet_filename(self):
        pattern = job_file_pattern("7201")
        for index in (1, 9, 10, 99):
            with self.subTest(index=index):
                self.assertTrue(pattern.match(sheet_filename("7201", index)))
        for other in (
            "R720201N.anc",
            "R72010N.anc",
            "R7201001N.anc",
            "R7201_report.pdf",
            "R720101N.anc" + PARTIAL_SUFFIX,
            "R720101N.bak",
        ):
            with self.subTest(other=other):
                self.assertIsNone(pattern.match(other))
        self.assertTrue(
            pattern.match("r720101n.anc"),
            "the shop PC's filesystem ignores case, so the sweep must too",
        )
        self.assertIsNone(
            job_file_pattern("720").match("R720101N.anc"),
            "len(prefix) + 2 digits is what keeps prefixes apart",
        )

    def test_the_quarantine_stamp_sorts_and_carries_no_path_characters(self):
        import time

        stamp = now_stamp(time.struct_time((2026, 8, 4, 15, 12, 4, 1, 216, -1)))
        self.assertEqual(stamp, "20260804-151204")
        self.assertRegex(now_stamp(), r"^\d{8}-\d{6}$")
        self.assertLess("20260804-151204", "20260804-151205")

    def test_a_stamp_that_would_escape_the_output_folder_is_refused(self):
        result, _config = one_frame_per_sheet("W2036")
        for bad in ("..", "../..", "a/b", "a\\b", "c:x", ""):
            with self.subTest(stamp=bad):
                with self.assertRaises(JobError):
                    job_for(result, quarantine_stamp=bad)


class AtomicWriteTest(unittest.TestCase):
    """Failure mode (c): a production file name holding half a program.

    ``open(path, "w")`` truncates before the first byte goes in, so a full
    disk, a crash or the shop PC being switched off used to leave a file that
    starts with a valid header and simply stops.  Every gate in this package
    is upstream of the write, so nothing would ever catch it.

    Now the text goes to ``<name>.partial`` and is renamed on afterwards, so
    the final name only ever holds the previous program or the whole new one.
    A failure means the sheet is reported (``refusal_kind == "write"``) and
    what was there before is intact — and then quarantined, because a file
    this run did not write must not sit in the folder looking current.
    """

    def options(self, folder, **overrides):
        return JobOptions(
            output_dir=folder,
            prefix="7201",
            created=CREATED,
            quarantine_stamp=overrides.pop("quarantine_stamp", "20260804-151204"),
            **overrides,
        )

    def read(self, *parts) -> str:
        with open(os.path.join(*parts), "r", newline="") as handle:
            return handle.read()

    def test_the_bytes_on_disk_are_exactly_what_the_verifier_saw(self):
        """The publish-through-a-rename may not change CONTENT at all: the
        whole post is proved byte for byte."""
        result, _config = one_frame_per_sheet("W2036", "W2436")
        with tempfile.TemporaryDirectory() as folder:
            job = write_job(result, self.options(folder))
            for outcome in job.outcomes:
                with self.subTest(sheet=outcome.filename):
                    with open(outcome.path, "rb") as handle:
                        raw = handle.read()
                    self.assertEqual(raw, outcome.text.encode("ascii"))
                    self.assertEqual(
                        raw.count(b"\n"), raw.count(b"\r\n"), "no bare LF may creep in"
                    )
                    self.assertTrue(raw.startswith(b"%\r\n"))
                    self.assertTrue(raw.endswith(b"M30\r\n%\r\n"))

            # Rerunning the same job replaces its own files and finds nothing
            # stale: same names, same bytes, no quarantine folder.
            again = write_job(result, self.options(folder))
            self.assertEqual(again.superseded, [])
            self.assertEqual(again.quarantine_problems, [])
            self.assertEqual(
                sorted(os.listdir(folder)), ["R720101N.anc", "R720102N.anc"]
            )
            for outcome in again.outcomes:
                self.assertEqual(self.read(outcome.path), outcome.text)

    def test_no_partial_file_survives_a_successful_job(self):
        result, _config = one_frame_per_sheet("W2036", "W2436")
        with tempfile.TemporaryDirectory() as folder:
            write_job(result, self.options(folder))
            self.assertEqual(
                [n for n in os.listdir(folder) if n.endswith(PARTIAL_SUFFIX)], []
            )

    def test_a_failed_publish_leaves_the_earlier_file_whole_and_reports_it(self):
        """``os.replace`` itself fails: nothing ever reaches the final name."""
        from faceframe_cnc.post import job as job_module

        result, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            write_job(result, self.options(folder))
            before = self.read(folder, "R720101N.anc")
            real_replace = job_module.os.replace

            def die_on_publish(src, dst):
                if str(src).endswith(PARTIAL_SUFFIX):
                    raise OSError(28, "no space left on device")
                return real_replace(src, dst)

            job_module.os.replace = die_on_publish
            try:
                job = write_job(
                    result, self.options(folder, quarantine_stamp="20260804-160000")
                )
            finally:
                job_module.os.replace = real_replace

            outcome = job.outcomes[0]
            self.assertFalse(outcome.written)
            self.assertEqual(outcome.refusal_kind, "write")
            self.assertTrue(
                any("could not write" in p for p in outcome.problems), outcome.problems
            )
            # The pre-existing program was never opened, let alone truncated:
            # its bytes come back whole out of the quarantine folder, and the
            # production name is empty rather than stale.
            self.assertEqual([i.filename for i in job.superseded], ["R720101N.anc"])
            self.assertEqual(self.read(job.superseded[0].new_path), before)
            self.assertEqual(sorted(os.listdir(folder)), [SUPERSEDED_DIR_NAME])

    def test_a_write_that_dies_mid_program_never_touches_the_final_name(self):
        """The disk fills up half way through the text."""
        from faceframe_cnc.post import job as job_module

        result, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            write_job(result, self.options(folder))
            before = self.read(folder, "R720101N.anc")
            real_open = open

            def half_write(path, *args, **kwargs):
                handle = real_open(path, *args, **kwargs)
                if str(path).endswith(PARTIAL_SUFFIX):
                    return _DiesHalfWay(handle)
                return handle

            job_module.open = half_write
            try:
                job = write_job(
                    result, self.options(folder, quarantine_stamp="20260804-160000")
                )
            finally:
                del job_module.open

            outcome = job.outcomes[0]
            self.assertEqual(outcome.refusal_kind, "write")
            self.assertFalse(outcome.written)
            self.assertEqual(
                [n for n in os.listdir(folder) if n.endswith(PARTIAL_SUFFIX)],
                [],
                "the half-written file is cleaned up, not published",
            )
            self.assertEqual([i.filename for i in job.superseded], ["R720101N.anc"])
            self.assertEqual(
                self.read(job.superseded[0].new_path),
                before,
                "the whole earlier program, not the half this run managed",
            )
            self.assertEqual(sorted(os.listdir(folder)), [SUPERSEDED_DIR_NAME])

    def test_a_failed_write_into_a_clean_folder_leaves_no_debris(self):
        from faceframe_cnc.post import job as job_module

        result, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            real_open = open

            def half_write(path, *args, **kwargs):
                handle = real_open(path, *args, **kwargs)
                if str(path).endswith(PARTIAL_SUFFIX):
                    return _DiesHalfWay(handle)
                return handle

            job_module.open = half_write
            try:
                job = write_job(result, self.options(folder))
            finally:
                del job_module.open

            self.assertEqual(os.listdir(folder), [])
            self.assertEqual(job.superseded, [])
            self.assertEqual(job.quarantine_problems, [])
            self.assertEqual(job.written, [])
            self.assertEqual(job.refused[0].refusal_kind, "write")


class DeterminismTest(unittest.TestCase):
    def test_the_same_input_gives_byte_identical_files(self):
        result, _config = nested_order(0.455)
        first = job_for(result)
        second = job_for(result)
        self.assertEqual(
            [(o.filename, o.text) for o in first.outcomes],
            [(o.filename, o.text) for o in second.outcomes],
        )

    def test_only_the_date_line_moves_when_the_clock_does(self):
        result, _config = nested_sample()
        morning = job_for(result, created="01 JAN 27 - 08:00").outcomes[0].text
        evening = job_for(result, created="02 FEB 28 - 19:45").outcomes[0].text
        a = morning.split("\r\n")
        b = evening.split("\r\n")
        self.assertEqual(len(a), len(b))
        differing = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        self.assertEqual(differing, [2])
        self.assertEqual(b[2], "(CREATED ON 02 FEB 28 - 19:45)")

    def test_a_generated_date_looks_like_the_reference_files(self):
        import time

        from faceframe_cnc.post.job import now_created

        self.assertRegex(now_created(), r"^\d{2} [A-Z]{3} \d{2} - \d{2}:\d{2}$")
        # 31 JUL 26 - 15:20 is R720101N's own header line.
        stamp = time.struct_time((2026, 7, 31, 15, 20, 0, 4, 212, -1))
        self.assertEqual(now_created(stamp), "31 JUL 26 - 15:20")

    def test_the_plan_does_not_depend_on_dictionary_or_list_churn(self):
        result, config = nested_sample()
        header = ProgramHeader(name="R990101N", created=CREATED)
        program_a, plan_a = plan_sheet(
            result.unique_sheets[0][0], header, result.demand, config
        )
        program_b, plan_b = plan_sheet(
            result.unique_sheets[0][0], header, list(reversed(result.demand)), config
        )
        self.assertEqual(plan_a, plan_b)
        self.assertEqual(
            generate(program_a, plan_a), generate(program_b, plan_b)
        )


# --------------------------------------------------------------------------
# plan-level unit checks
# --------------------------------------------------------------------------


class PlanShapeTest(unittest.TestCase):
    def program(self):
        result, config = nested_sample()
        return sheet_program_from_layout(
            result.unique_sheets[0][0],
            ProgramHeader(name="R990101N", created=CREATED),
            result.demand,
            config,
        )

    def test_grooves_use_the_measured_insets(self):
        from faceframe_cnc.post.generator import groove_segment

        program = self.program()
        part = program.flat_parts()[0]
        panel = default_config().panel
        stile_low, _ = groove_segment(part, 0, panel)
        rail_low, rail_high = groove_segment(part, 1, panel)
        self.assertAlmostEqual(stile_low[0], part.box.x0 + 0.5625)
        self.assertAlmostEqual(rail_low[1], part.box.y0 + 0.9375)
        self.assertAlmostEqual(rail_low[0], part.box.x0 + 0.5625)
        self.assertAlmostEqual(rail_high[0], part.box.x1 - 0.5625)
        _, stile_high_end = groove_segment(part, 0, panel)
        self.assertAlmostEqual(stile_high_end[1], part.box.y1 + 0.375)

    def test_every_part_gets_four_grooves_and_every_opening_is_planned(self):
        program = self.program()
        plan = cut_plan_for(program)
        parts = program.flat_parts()
        self.assertEqual(len(plan.panel), 4 * len(parts))
        self.assertEqual(
            len(plan.openings), sum(len(part.openings) for part in parts)
        )
        self.assertIsNone(plan.detail, "T12 repeats the T11 opening order")
        self.assertEqual(plan.detail_order(), plan.openings)

    def test_a_part_too_small_for_the_groove_pattern_is_refused(self):
        from faceframe_cnc.post import Box, PartProgram, SheetPlanError, SheetProgram

        program = SheetProgram(
            header=ProgramHeader(name="R990101N", created=CREATED),
            parts=[
                PartProgram(
                    "TINY",
                    Box(0.0, 0.0, 1.0, 4.0),
                    openings=[Box(0.2, 0.2, 0.8, 3.8)],
                )
            ],
        )
        with self.assertRaises(SheetPlanError) as caught:
            cut_plan_for(program)
        self.assertIn("panel-groove pattern", str(caught.exception))

    def test_a_frame_the_geometry_engine_rejects_is_refused(self):
        from faceframe_cnc.post import SheetPlanError

        layout = SheetLayout([Placement("W0202", 1.0, 1.0, 2.0, 2.0)])
        with self.assertRaises(SheetPlanError):
            plan_sheet(layout, ProgramHeader(name="R1", created=CREATED))

    def test_part_depths_counts_nesting(self):
        program = self.program()
        self.assertEqual(part_depths(program), [0, 1, 0])


# --------------------------------------------------------------------------
# (i) Milestone 5 acceptance: the whole order, out to disk, nothing refused
# --------------------------------------------------------------------------


class MilestoneAcceptanceTest(unittest.TestCase):
    """The 7-21-26 order at the shipping defaults, end to end.

    Nothing here is a unit: it is the question the owner will ask on the
    shop floor — "can I press Generate on this order and take the folder to
    the machine?"  The answer has to be every sheet, no exceptions, WDC
    frames included, in production and in rehearsal.
    """

    @classmethod
    def setUpClass(cls):
        cls.config = NestingConfig(inside_nesting=True)
        cls.result = nest(ORDER_7_21_26, cls.config)

    def test_the_defaults_are_the_owner_approved_ones(self):
        self.assertEqual(self.config.part_gap, 0.455)
        self.assertEqual(self.config.inner_clearance, 0.375)

    def test_every_unique_sheet_is_written_and_none_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            job = write_job(
                self.result,
                JobOptions(output_dir=folder, prefix="7201", created=CREATED),
            )
            self.assertEqual(
                [o.describe() for o in job.refused], [], "zero refusals is the bar"
            )
            self.assertEqual(len(job.written), self.result.unique_sheet_count)
            self.assertEqual(
                sorted(os.listdir(folder)),
                sorted(o.filename for o in job.outcomes),
            )
            self.assertEqual(
                sum(o.run_quantity for o in job.outcomes), self.result.total_sheets
            )
            for path in job.files:
                with self.subTest(path=os.path.basename(path)):
                    with open(path, "r", newline="") as handle:
                        text = handle.read()
                    self.assertTrue(text.startswith("%\r\n"))
                    self.assertTrue(text.endswith("M30\r\n%\r\n"))
                    self.assertEqual([str(v) for v in verify(text, self.post())], [])

    def test_the_wdc_sheets_and_only_those_carry_a_t17_section(self):
        job = job_for(self.result)
        wdc = [o for o in job.outcomes if has_wdc(o.contents)]
        self.assertTrue(wdc, "the order has WDC2436 on it")
        for outcome in job.outcomes:
            with self.subTest(sheet=outcome.filename):
                heads = [
                    int(m)
                    for m in re.findall(r"\(ROUTE TOOL #(\d+):", outcome.text)
                ]
                if has_wdc(outcome.contents):
                    self.assertEqual(heads, [13, 17, 11, 12, 11])
                else:
                    self.assertEqual(heads, [13, 11, 12, 11])

    def test_every_wdc_frame_on_every_sheet_gets_two_slots_of_two_passes(self):
        job = job_for(self.result)
        for outcome in job.outcomes:
            if not has_wdc(outcome.contents):
                continue
            frames = sum(
                count for name, count in outcome.contents.items()
                if name.upper().startswith("WDC")
            )
            with self.subTest(sheet=outcome.filename):
                deep = outcome.text.count("G1 Z0.4062 F150.")
                full = outcome.text.count("G1 Z0.3125 F150.")
                self.assertEqual(deep, 2 * frames, "one first pass per stile")
                self.assertEqual(full, 2 * frames, "one finish pass per stile")

    def test_the_dry_run_of_the_same_order_air_cuts_and_verifies(self):
        job = job_for(self.result, dry_run=True)
        self.assertEqual([o.describe() for o in job.refused], [])
        air = dry_run_config(self.post())
        top = self.post().stock_top_z
        for outcome in job.outcomes:
            with self.subTest(sheet=outcome.filename):
                self.assertEqual([str(v) for v in verify(outcome.text, air)], [])
                if has_wdc(outcome.contents):
                    # the slot's Z mirrors, its XY path does not
                    self.assertIn("G1 Z1.0938 F150.", outcome.text)
                    self.assertIn("G1 Z1.1875 F150.", outcome.text)
                    self.assertNotIn("G1 Z0.4062", outcome.text)
        self.assertAlmostEqual(air.wdc_slot.z_cuts[0], 2 * top - 0.4062)
        self.assertAlmostEqual(air.wdc_slot.z_cuts[1], 2 * top - 0.3125)

    def test_the_air_cut_traces_the_production_slot_path_exactly(self):
        """Only Z words move.  A dry run whose slot is SHORTER than the real
        one would rehearse a cut nobody is going to make."""
        production = job_for(self.result)
        air = job_for(self.result, dry_run=True)
        for real, lifted in zip(production.outcomes, air.outcomes):
            if not has_wdc(real.contents):
                continue
            with self.subTest(sheet=real.filename):
                self.assertEqual(
                    re.findall(r"^[XY][\d.]+ F400\.$", real.text, re.M),
                    re.findall(r"^[XY][\d.]+ F400\.$", lifted.text, re.M),
                )

    def test_the_optimizer_leaves_the_room_the_slot_needs(self):
        """Independently of the post: every WDC on every sheet has the
        slot's full reach beyond both stile ends."""
        self.assertEqual(validate_layouts(self.result, self.config), [])
        seen = 0
        for layout, _run in self.result.unique_sheets:
            for placement in _walk_placements(layout.placements):
                if not placement.part_number.upper().startswith("WDC"):
                    continue
                seen += 1
                low, high, limit = (
                    (placement.x, placement.x + placement.width, self.config.sheet_width)
                    if placement.rotated
                    else (
                        placement.y,
                        placement.y + placement.height,
                        self.config.sheet_height,
                    )
                )
                if _host_of(layout.placements, placement) is None:
                    self.assertGreaterEqual(low, WDC_SLOT_END_REACH - 1e-9)
                    self.assertLessEqual(high, limit - WDC_SLOT_END_REACH + 1e-9)
        self.assertTrue(seen, "the order has WDC frames to check")

    def test_every_sheet_contains_every_cut_its_layout_calls_for(self):
        """The other half of "can I take this folder to the machine?"
        (2026-08-04 review).

        ``build_job`` now hands the verifier an expected-work manifest, so
        the run above already refuses a sheet with a cut missing.  This test
        states the same thing from outside the job writer — manifest built
        straight off the layout, verifier fed both — so that the acceptance
        run cannot pass by the manifest quietly coming back empty.
        """
        job = job_for(self.result)
        post = self.post()
        self.assertEqual(len(job.outcomes), len(self.result.unique_sheets))
        owed = 0
        for outcome, (layout, _run) in zip(job.outcomes, self.result.unique_sheets):
            expected = expected_work(layout, post)
            owed += len(expected)
            with self.subTest(sheet=outcome.filename):
                self.assertTrue(len(expected) > 0)
                self.assertEqual(
                    [str(v) for v in verify(outcome.text, post, expected)], []
                )
        self.assertGreater(owed, 0, "the manifests must actually describe work")

    def test_the_dry_run_of_the_whole_order_owes_the_same_work_lifted(self):
        job = job_for(self.result, dry_run=True)
        air = dry_run_config(self.post())
        for outcome, (layout, _run) in zip(job.outcomes, self.result.unique_sheets):
            with self.subTest(sheet=outcome.filename):
                self.assertEqual(
                    [str(v) for v in verify(outcome.text, air, expected_work(layout, air))],
                    [],
                )
                self.assertEqual(
                    expected_work(layout, air).counts(),
                    expected_work(layout, self.post()).counts(),
                    "a rehearsal owes exactly the cuts the real program does",
                )

    def test_the_frame_geometry_and_the_post_table_agree_on_the_reach(self):
        """``geometry`` tells the optimizer how much room to leave and
        ``post.model`` tells the machine how deep to cut.  They are separate
        modules on purpose; they must not drift."""
        from faceframe_cnc import geometry

        cfg = default_config()
        deepest = min(cfg.wdc_slot.z_cuts)
        reach = cfg.wdc_slot_reach(cfg.wdc_slot.z_cuts.index(deepest))
        self.assertAlmostEqual(2.0 * reach, WDC_SLOT_END_REACH)
        self.assertAlmostEqual(geometry.WDC_SLOT_END_REACH, WDC_SLOT_END_REACH)
        self.assertAlmostEqual(
            cfg.stock_top_z - deepest, geometry.WDC_SLOT_DEPTH
        )
        self.assertAlmostEqual(
            cfg.wdc_slot.inset_from_inside_edge,
            geometry.WDC_SLOT_INSET_FROM_INSIDE_EDGE,
        )
        self.assertAlmostEqual(cfg.wdc_slot.stile_width, geometry.WDC_STILE_INSET)

    def post(self):
        return post_config_for(self.config)


def _walk_placements(placements):
    for placement in placements:
        yield placement
        yield from _walk_placements(placement.children)


def _host_of(placements, target):
    for placement in placements:
        for child in _walk_placements(placement.children):
            if child is target:
                return placement
    return None


if __name__ == "__main__":
    unittest.main()
