"""Milestone 5 phase 2: NC generation for OPTIMIZED sheets.

Phase 1 proved the post by round-tripping files the machine had already
run.  Nothing here round-trips: these sheets have never existed, which is
the whole point, so what is checked instead is

  (a) the real 7-21-26 order, nested at the default 0.455 gap, generates a
      verified program for EVERY unique sheet -- zero refusals -- and the
      0.375 gap the spec asks for still cannot be cut (the finding is pinned,
      not hidden: since the 2026-08-05 amendment it is 9 of the 17 sheets
      rather than all of them, and WHY is pinned too);
  (b) WDC sheets carry their T17 stile slot, in the right section, on the
      right centreline, with the right per-pass depth and overrun; a WDC
      whose slot would reach a neighbour or the sheet edge is refused;
  (c) the perimeter order is really in the emitted text: ONE pass per part
      since the 2026-08-05 amendment (no 0.06 skin lap anywhere), and every
      nested inner cut free before its host;
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


#: A part gap no perimeter pass can honour, whichever way the neighbour lies.
#: The through pass's at-depth loop sweeps exactly 0.375 past a part edge, so
#: 0.375 is tangent to a neighbour and (since the 2026-08-05 amendment removed
#: the 0.377 onion-skin pass) only refused where the lead-in ramp points at it.
#: 0.25 is 0.125 INSIDE the neighbour on every side — an unambiguously
#: uncuttable sheet, which is what a fixture about refusals needs.
UNCUTTABLE_GAP = 0.25


def mixed_order() -> "NestingResult":
    """An order that produces one sheet the verifier passes and one it refuses.

    A test about what happens to the REST of a job when one sheet is refused
    needs a job with a rest: a 48x90 frame gets a sheet to itself and has no
    neighbour to cut into, while the pair of 30x30s share one at
    :data:`UNCUTTABLE_GAP` and cannot be cut.
    """
    demand = [PartSpec("W4890", 48.0, 90.0, 1), PartSpec("W3030", 30.0, 30.0, 2)]
    return nest(demand, NestingConfig(part_gap=UNCUTTABLE_GAP))


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

        A perimeter lead-in ramp stands 0.05 off the profile and closes that
        gap as it descends, so where it breaks the surface the swept tool still
        reaches 0.3938 past the part edge (0.425 at the ramp plane, where it is
        cutting nothing).  Two parts 0.375 apart with the ramp pointing at the
        gap are 0.019 short of that, and the verifier says so.  Nothing is
        written for those sheets.  This is why 0.455 is the default.

        It is EVERY sheet of this order, and it is worth saying which pass makes
        that true.  For half of 2026-08-05 it was a majority only: dropping the
        onion skin left the through pass's at-depth loop reaching exactly 0.375 —
        tangent, taking nothing out of a neighbour — so a sheet whose parts
        neighbour each other away from the lead-in edge verified clean.  The
        max-bite amendment later that day (Scott: at most 0.4 of material per T11
        pass) put a roughing rung back at the measured 0.1895 offset, whose loop
        sweeps 0.377, so the backstop is back and the whole order is refused
        again at 0.375 (``tests/test_r0805_regression.SweptWidthBoundariesTest``
        pins all three reaches).  The part gap itself is still enforced by
        :data:`faceframe_cnc.nesting.MIN_PART_GAP` at optimize time, because a
        pass ladder is a machining decision and could move again.
        """
        _result, _config, job = self.jobs[0.375]
        refused = [o for o in job.outcomes if not o.ok]
        self.assertTrue(refused, "0.375 cannot be cut; that is the whole finding")
        self.assertEqual(
            [o.filename for o in job.outcomes if o.ok],
            [],
            "every sheet of this order is refused at 0.375",
        )
        for outcome in refused:
            with self.subTest(sheet=outcome.filename):
                self.assertEqual(outcome.refusal_kind, "verifier")
                self.assertTrue(
                    all("foreign-cut" in p for p in outcome.problems),
                    outcome.problems,
                )

    def test_a_gap_tighter_than_the_through_pass_refuses_every_sheet(self):
        """Below 0.375 the loop itself is inside the neighbour, not tangent.

        The backstop that is left, stated on the fixture that needs no
        assumptions about which way a neighbour lies (:data:`UNCUTTABLE_GAP`).
        """
        job = job_for(mixed_order())
        refused = [o for o in job.outcomes if not o.ok]
        self.assertTrue(refused)
        for outcome in refused:
            with self.subTest(sheet=outcome.filename):
                self.assertTrue(
                    any("foreign-cut" in p for p in outcome.problems),
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
        self.assertEqual(numbers, [13, 17, 11, 12, 11, 12])

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
    """The order in the emitted text, read back out of it.

    Everything here is read against ``post_config_for(config)`` — the post
    table the job was actually generated with, one perimeter pass since the
    2026-08-05 amendment — and not against the measured table, which still
    describes the references' two-pass dialect.  ``reconstruct_text`` needs
    the same table for the same reason: a file's perimeter loops are matched
    to the passes the table configures, and it refuses to guess.
    """

    @classmethod
    def setUpClass(cls):
        cls.result, cls.config = nested_sample()
        cls.job = job_for(cls.result)
        cls.outcome = cls.job.outcomes[0]
        assert cls.outcome.ok, cls.outcome.describe()
        cls.text = cls.outcome.text
        cls.lines = cls.text.split("\r\n")
        cls.post = post_config_for(cls.config)

    def replan(self):
        """``(program, plan)`` read back out of the emitted text."""
        return reconstruct_text(self.text, self.post)

    def test_the_layout_used_here_is_itself_legal(self):
        self.assertEqual(validate_layouts(self.result, self.config), [])

    def test_section_order_is_panel_openings_detail_perimeter_release(self):
        """T13 -> T11 openings -> T12 detail -> T11 perimeter -> T12 RELEASE.

        The last section is the 2026-08-05 amendment's tab release (spec §3c):
        the same T12 as the detail pass, in the spindle a second time, and always
        last because everything on the sheet is held until it has run.
        """
        heads = [line for line in self.lines if line.startswith("(ROUTE TOOL")]
        numbers = [int(re.match(r"\(ROUTE TOOL #(\d+)", h).group(1)) for h in heads]
        self.assertEqual(numbers, [13, 11, 12, 11, 12])

    def test_every_opening_is_cut_before_any_perimeter(self):
        """Sections do it structurally; this checks the emitted Z words."""
        cfg = self.post
        opening_z = f"Z{_fmt(cfg.openings_passes[-1].z_cut)} "
        detail_z = f"Z{_fmt(cfg.detail_pass.z_cut)} "
        perimeter_z = f"Z{_fmt(cfg.perimeter_passes[0].z_cut)} "
        # Lead-in lines only (they start with G1): since the 2026-08-05
        # amendment a tab lift's descent restates the same Z and feed as the
        # lead-in, and the T12 RELEASE section plunges to the detail depth after
        # the perimeter on purpose (spec §3c), so "the last line mentioning the
        # detail Z" is no longer the last opening cut.
        release_head = max(
            i for i, line in enumerate(self.lines) if line.startswith("(ROUTE TOOL")
        )
        last_opening = max(
            i
            for i, line in enumerate(self.lines[:release_head])
            if line.startswith("G1 ") and (opening_z in line or detail_z in line)
        )
        first_perimeter = min(
            i
            for i, line in enumerate(self.lines)
            if line.startswith("G1 ") and perimeter_z in line
        )
        self.assertLess(last_opening, first_perimeter)

    def test_the_first_t11_section_cuts_the_nested_inners_own_openings(self):
        program, plan = self.replan()
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
        program, plan = self.replan()
        depths = part_depths(program)
        order = [ref.part for ref in plan.openings]
        seen_shallow = False
        for index in order:
            if depths[index] == 0:
                seen_shallow = True
            elif seen_shallow:
                self.fail("a nested inner's opening was cut after a host's")

    def test_the_perimeter_runs_the_max_bite_ladder_and_no_onion_skin(self):
        """Both 2026-08-05 amendments, read off the emitted text.

        Scott's decision at the milestone-1 check-in (spec §3b): the 0.06 skin
        pass held every part while the rest of the sheet was cut, the parts are
        tab-held from milestone 2b on, so the skin has no job left.  His decision
        later the same day: the 3/8 comp may take at most 0.4 of material per
        pass, "to reduce the load on it", so the 0.756 the through pass was left
        with becomes two equal 0.378 bites — Z0.372 then Z-0.006.

        So a generated sheet cuts each perimeter TWICE, but at neither of the
        measured two-pass dialect's depths, and the skin's Z0.06 appears nowhere.
        The measured table still carries both of its passes for the reference
        programs, which is why every number below is read off the table the job
        used.
        """
        cfg = self.post
        self.assertEqual([spec.z_cut for spec in cfg.perimeter_passes], [0.372, -0.006])
        depth = cfg.stock_top_z - cfg.perimeter_passes[-1].z_cut
        limit = cfg.tool("perimeter").max_bite
        self.assertEqual(limit, 0.4)
        for position, spec in enumerate(cfg.perimeter_passes):
            floor = cfg.stock_top_z if position == 0 else cfg.perimeter_passes[
                position - 1
            ].z_cut
            with self.subTest(perimeter_pass=position + 1):
                self.assertAlmostEqual(floor - spec.z_cut, depth / 2.0, 9)
                self.assertLessEqual(floor - spec.z_cut, limit)
        skin = default_config().perimeter_passes[0]
        self.assertNotIn(
            f"Z{_fmt(skin.z_cut)} ",
            self.text,
            "no cutting move may be made at the old skin depth",
        )

        program, plan = self.replan()
        self.assertEqual(len(plan.perimeter), 2)
        for refs in plan.perimeter:
            self.assertEqual(len(refs), len(program.flat_parts()))
        # Lead-ins only: a tab lift descends back to the same depth at the same
        # entry feed (spec §3b), so the bare string appears once per tab too.
        for spec in cfg.perimeter_passes:
            needle = f"Z{_fmt(spec.z_cut)} F150."
            lead_ins = [
                line
                for line in self.lines
                if line.startswith("G1 ") and needle in line
            ]
            with self.subTest(z_cut=spec.z_cut):
                self.assertEqual(
                    len(lead_ins),
                    len(program.flat_parts()),
                    "one lead-in per part per rung, and only one",
                )

    def test_the_through_pass_frees_every_inner_before_any_host(self):
        """The onion-skin order's pass-2 rule, now the ladder's last rung.

        Whatever the ladder is, the LAST pass is the one that cuts an outline
        right through, so it is the one that has to take every nested inner
        before any host; the rungs above it only score, and run canonically.
        """
        program, plan = self.replan()
        depths = part_depths(program)
        self.assertEqual(len(plan.perimeter), 2)
        self.assertEqual(
            [ref.part for ref in plan.perimeter[0]],
            list(range(len(program.flat_parts()))),
            "the roughing rung runs in canonical order",
        )

        order = [ref.part for ref in plan.perimeter[-1]]
        self.assertEqual(sorted(order), list(range(len(program.flat_parts()))))

        inner_positions = [i for i, part in enumerate(order) if depths[part] > 0]
        host_positions = [i for i, part in enumerate(order) if depths[part] == 0]
        self.assertTrue(inner_positions and host_positions)
        self.assertLess(
            max(inner_positions),
            min(host_positions),
            "the freeing pass must free every nested frame before any outer part",
        )

    def test_grooves_are_stiles_then_rails_per_part_in_canonical_order(self):
        program, plan = self.replan()
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
        _program, plan = self.replan()
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

        The default gap generates everything, so this needs a job with both
        kinds of sheet in it (:func:`mixed_order`).
        """
        job = job_for(mixed_order())
        refused = [o.sheet_index for o in job.outcomes if not o.ok]
        self.assertTrue(refused, "the too-tight pair must still be refused")
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
        # Against the table the sheet was GENERATED with: a generated sheet runs
        # one perimeter pass and a T12 release section since the 2026-08-05
        # amendment, and the verifier judges depths and feeds against the table
        # in its hand (post_config_for), not against the measured reference one.
        self.assertEqual(
            [str(v) for v in verify(outcome.text, post_config_for(self.config))], []
        )

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

    def test_a_job_whose_o_numbers_would_run_past_9999_is_refused_up_front(self):
        """2026-08-04 review, fix 8.

        ``O0001`` has four digits and ``_O_RE`` in the verifier says so, so a
        job starting near the top used to generate happily until the sheet whose
        number crossed 10000, and then refuse THAT sheet with a ``header``
        finding about its O line — which tells the operator nothing about the
        number he typed into the options.  Now the range is judged before any
        sheet is planned, and the message says where to start instead.
        """
        result, _config = nested_order(0.455)
        count = len(result.unique_sheets)
        self.assertGreater(count, 1)
        with self.assertRaises(JobError) as caught:
            job_for(result, first_o_number=10000 - count + 1)
        message = str(caught.exception)
        self.assertIn(f"needs {count} programs", message)
        self.assertIn("O10000", message)
        self.assertIn(f"O{10000 - count + 1:04d}", message)
        self.assertIn(str(9999 - count + 1), message, "and what to use instead")

        # The last value that fits still works, and numbers right up to 9999.
        job = job_for(result, first_o_number=9999 - count + 1)
        self.assertEqual(job.outcomes[-1].o_number, 9999)
        self.assertEqual([o.describe() for o in job.refused], [])
        self.assertIn("O9999 (", job.outcomes[-1].text)

    def test_the_o_number_range_check_needs_the_sheet_count(self):
        """The options alone cannot know it, so ``validate`` takes it."""
        options = JobOptions(output_dir="x", prefix="7201", first_o_number=9999)
        self.assertEqual(options.validate(), [])
        self.assertEqual(options.validate(1), [])
        self.assertTrue(options.validate(2))
        self.assertTrue(JobOptions(output_dir="x", first_o_number=10000).validate())

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



def feed_word(line: str) -> float | None:
    """The F value on ``line``, or ``None`` when it states no feed."""
    match = re.search(r"\bF(\d+(?:\.\d*)?)", line)
    return None if match is None else float(match.group(1))


def without_tab_lifts(text: str) -> list[str]:
    """``text``'s lines with every tab lift taken out (2026-08-05 amendment §3b).

    A lift is four moves — cut on to the foot of the climb, climb to the tab top,
    traverse it, descend back to depth — and the CLIMB is the only line in the
    program that commands the tab top's Z, which makes the block findable without
    re-deriving where any tab is.  Used to compare an air cut against its
    production twin: an air cut lifts over nothing, because every one of its
    depths is above the stock and there is no standing material up there.
    """
    top = f" Z{_fmt(default_config().tabs.top_z)}"
    lines = text.split("\r\n")
    drop: set[int] = set()
    for index, line in enumerate(lines):
        if line.endswith(top):
            drop.update({index - 1, index, index + 1, index + 2})
    assert drop, "no tab lift found: this helper is measuring the wrong thing"
    return [line for index, line in enumerate(lines) if index not in drop]

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

    def test_only_the_z_words_ramp_lengths_and_the_tab_lifts_move(self):
        """Same tools, feeds, speeds and section structure as production.

        Two things the lift changes, and no others.  The Z words (and the ramp
        lengths those imply) are the point of an air cut.  The TAB LIFTS are the
        second, and they are gone rather than lifted: a tab is 0.25" of standing
        material and every cut in this program is a foot and a half above the
        stock, so there is nothing to rise over and rising would mean DESCENDING
        (:func:`~faceframe_cnc.post.tabs.lifts_over_tabs`).  The release section
        itself is lifted like every other one and traces exactly the production
        XY path, which the slot-path test below states for the whole order.

        So the feed counts are compared against the production program re-emitted
        from the SAME plan with the tabs taken out — an exact statement, rather
        than a looser assertion that would stop noticing a feed that moved.
        """
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

        # The tab lifts, stated as the difference they are: the tab top's Z is
        # the only Z0.25 in the program, so its presence in one text and absence
        # from the other IS the second difference, and there is no third.
        top = f" Z{_fmt(default_config().tabs.top_z)}"
        self.assertIn(top, self.production.text, "production stands its tabs up")
        self.assertNotIn(
            top,
            self.air.text,
            "an air cut lifts over nothing: every depth is above the stock, so "
            "rising to the tab top would be a DESCENT",
        )

        # Line for line against the production program with its lift blocks taken
        # out.  The air cut carries one extra line, its own banner; every other
        # line pairs up, and the only feed difference allowed is a cutting feed a
        # tab's descent forced the production file to restate.
        flat = without_tab_lifts(self.production.text)
        air = [line for line in self.air.text.split("\r\n") if "DRY RUN" not in line]
        self.assertEqual(len(flat), len(air), "line for line, the lifts aside")
        feeds = {
            spec.cut_feed
            for spec in (
                *default_config().openings_passes,
                default_config().detail_pass,
                *post_config_for(self.config).perimeter_passes,
            )
        }
        restated = 0
        for lifted, lifted_air in zip(flat, air):
            here, there = feed_word(lifted), feed_word(lifted_air)
            with self.subTest(production=lifted, air=lifted_air):
                if there is not None:
                    self.assertEqual(here, there, "the air cut invents no feed")
                elif here is not None:
                    self.assertIn(here, feeds, "and adds only a restated cut feed")
                    restated += 1
        self.assertTrue(restated, "the restatements are what the lifts leave behind")

    def test_the_release_section_traces_the_production_xy_exactly(self):
        """Spec §3c/§3d: the lift moves the release section's Z and nothing else.

        The release span is derived from the tab top and the post's Z FLOOR
        (:func:`~faceframe_cnc.post.tabs.release_ramp`) rather than from the
        depths of the passes that lifted, precisely so that this holds: an air
        table has no lifting pass at all, and a release cut sized from one would
        come out shorter than the production cut it is rehearsing.
        """
        from faceframe_cnc.post.generator import emit
        from faceframe_cnc.post.model import SECTION_RELEASE

        post = post_config_for(self.config)
        air = dry_run_config(post)
        layout = self.result.unique_sheets[0][0]
        program, plan = plan_sheet(
            layout,
            ProgramHeader(name="R990101N", created=CREATED),
            self.result.demand,
            self.config,
            post,
        )
        self.assertTrue(plan.release, "the sheet has a release section to compare")

        def cuts(cfg):
            return [
                (round(m.to_x, 4), round(m.to_y, 4), m.feed)
                for m in emit(program, plan, cfg).motions
                if m.section == SECTION_RELEASE
            ]

        self.assertEqual(cuts(post), cuts(air))
        depths = {
            m.to_z
            for m in emit(program, plan, air).motions
            if m.section == SECTION_RELEASE and m.feed == air.release.cut_feed
        }
        self.assertEqual(depths, {air.release_z})
        self.assertGreater(air.release_z, air.stock_top_z, "and it is an air cut")

    def test_the_depths_are_derived_from_the_measured_table(self):
        cfg = default_config()
        air = dry_run_config(cfg)
        top = cfg.stock_top_z
        for real, lifted in (
            (cfg.panel.z_cut, air.panel.z_cut),
            (cfg.openings_passes[-1].z_cut, air.openings_passes[-1].z_cut),
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
    """Two parts exactly 0.375 apart, side by side in X: legal to the packer,
    too tight for the perimeter lead-in.

    The order of the two placements matters and is not incidental: the lead-in
    enters on the RIGHT edge by default, so the left part's ramp is what
    reaches into the gap (0.3938 where it breaks the surface, vs the 0.375 the
    at-depth loop sweeps).  Since the 2026-08-05 amendment removed the
    onion-skin pass, that ramp is the only thing that refuses this sheet —
    stacking the same two parts in Y would verify clean at 0.375
    (``tests/test_r0805_regression.SweptWidthBoundariesTest``).
    """
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

            stale_text["R720101N.anc"] = self.read(folder, "R720101N.anc")

            second = self.write(short_run, folder, quarantine_stamp="20260804-160000")
            self.assertEqual([o.filename for o in second.written], ["R720101N.anc"])
            self.assertEqual(sorted(os.listdir(folder)), ["R720101N.anc", "superseded"])
            self.assertEqual(second.quarantine_problems, [])
            self.assertEqual(
                second.quarantine_dir,
                os.path.join(second.output_dir, SUPERSEDED_DIR_NAME, "20260804-160000"),
            )
            # Sheet 01 is REPLACED, 02 and 03 are left over; since the
            # 2026-08-04 review's fix 7d all three of the previous run's
            # programs survive, in the one folder, rather than the replaced one
            # being quietly destroyed.
            self.assertEqual(
                [item.filename for item in second.superseded],
                ["R720101N.anc", "R720102N.anc", "R720103N.anc"],
            )
            self.assertIn("replaced by this run", second.superseded[0].reason)
            for item in second.superseded[1:]:
                with self.subTest(file=item.filename):
                    self.assertIn("no sheet", item.reason)
            for item in second.superseded:
                with self.subTest(file=item.filename):
                    self.assertEqual(
                        self.read(item.new_path),
                        stale_text[item.filename],
                        "quarantine copies the bytes, it does not rewrite them",
                    )
            self.assertFalse(
                os.path.exists(second.superseded[1].old_path), "it moved"
            )
            # ... and the job says so, in words the UI can show verbatim.
            self.assertEqual(len(second.superseded_lines()), 3)
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

        Nothing a completed write leaves behind is a temp file, so one in the
        folder is an earlier run that died.  Since the 2026-08-04 review's fix
        7f the temp name carries the writing run's pid and clock, so THIS run
        shares a temp name with nobody: both leftovers — the one at a name this
        job writes and the one at a name it does not — are somebody else's half
        program, and both are quarantined rather than one of them being
        overwritten in place.
        """
        result, _config = one_frame_per_sheet("W2036")
        alive = "R720101N.anc" + PARTIAL_SUFFIX
        orphan = "R720105N.anc" + PARTIAL_SUFFIX + "-999-abc"
        with tempfile.TemporaryDirectory() as folder:
            for name in (alive, orphan):
                with open(os.path.join(folder, name), "w", newline="") as handle:
                    handle.write("%\r\nO0001 (HALF A PROGRAM")

            job = self.write(result, folder)
            self.assertEqual(
                sorted(i.filename for i in job.superseded), sorted([alive, orphan])
            )
            for item in job.superseded:
                with self.subTest(file=item.filename):
                    self.assertIn("interrupted", item.reason)
                    self.assertTrue(os.path.exists(item.new_path))
                    self.assertEqual(
                        self.read(item.new_path), "%\r\nO0001 (HALF A PROGRAM"
                    )
            self.assertEqual(job.quarantine_problems, [])
            self.assertEqual(
                sorted(os.listdir(folder)), ["R720101N.anc", SUPERSEDED_DIR_NAME]
            )
            self.assertEqual(self.read(folder, "R720101N.anc"), job.outcomes[0].text)

    def test_the_temp_name_is_this_run_s_alone(self):
        """Fix 7f: two runs may not take turns inside one temp file."""
        from faceframe_cnc.post.job import partial_suffix

        first, second = partial_suffix(), partial_suffix()
        self.assertNotEqual(first, second)
        for suffix in (first, second):
            with self.subTest(suffix=suffix):
                self.assertTrue(suffix.startswith(PARTIAL_SUFFIX))
                self.assertRegex(suffix, r"^\.partial-\d+-[0-9a-f]+$")
                self.assertNotIn(".anc", suffix)
                # The sweep still has to recognise it as a temp file.
                self.assertIsNone(
                    job_file_pattern("7201").match("R720101N.anc" + suffix)
                )

    def test_two_runs_in_the_same_second_get_their_own_folders(self):
        """The Generate button is right there; two runs can share a second."""
        long_run, _config = one_frame_per_sheet("W2036", "W2436")
        short_run, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            first = self.write(long_run, folder)
            second = self.write(short_run, folder)
            third = self.write(long_run, folder)
            fourth = self.write(short_run, folder)
            self.assertIsNone(first.quarantine_dir, "a clean folder needs none")
            self.assertEqual(os.path.basename(second.quarantine_dir), self.STAMP)
            self.assertEqual(
                os.path.basename(third.quarantine_dir), f"{self.STAMP}-2"
            )
            self.assertEqual(
                os.path.basename(fourth.quarantine_dir), f"{self.STAMP}-3"
            )
            # run 2: sheet 01 replaced + sheet 02 left over; run 3: sheet 01
            # replaced; run 4: sheet 01 replaced + sheet 02 left over.
            self.assertEqual(len(second.superseded), 2)
            self.assertEqual(len(third.superseded), 1)
            self.assertEqual(len(fourth.superseded), 2)
            self.assertEqual(
                sorted(os.listdir(os.path.join(folder, SUPERSEDED_DIR_NAME))),
                [self.STAMP, f"{self.STAMP}-2", f"{self.STAMP}-3"],
            )

    def test_a_stale_file_that_cannot_be_moved_is_a_loud_problem(self):
        """Locked by the machine's file browser, read-only, no permission.

        The folder is then NOT safe to hand over, so the failure is reported
        on the job by name — never swallowed, and never turned into a delete.

        And since fix 7d the same applies to a file this run wants to REPLACE:
        if the previous version cannot be moved into the quarantine, the sheet
        is not published either, because overwriting it would destroy the only
        copy.  Every quarantine move in this test fails, so nothing is written
        at all and both files are reported by name.
        """
        from faceframe_cnc.post import job as job_module

        long_run, _config = one_frame_per_sheet("W2036", "W2436")
        short_run, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            self.write(long_run, folder)
            before = {
                name: self.read(folder, name)
                for name in ("R720101N.anc", "R720102N.anc")
            }
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
            for name in ("R720101N.anc", "R720102N.anc"):
                with self.subTest(file=name):
                    self.assertTrue(
                        any(name in p for p in job.quarantine_problems),
                        job.quarantine_problems,
                    )
            self.assertTrue(
                any("permission denied" in p for p in job.quarantine_problems),
                job.quarantine_problems,
            )
            self.assertIn("STALE FILE STILL", job.summary())
            # Nothing was published and nothing was destroyed: both of the
            # earlier run's programs are still there, byte for byte.
            self.assertEqual([o.filename for o in job.written], [])
            self.assertEqual(job.refused[0].refusal_kind, "write")
            for name, text in before.items():
                with self.subTest(file=name):
                    self.assertEqual(self.read(folder, name), text)
            self.assertEqual(
                [n for n in os.listdir(folder) if PARTIAL_SUFFIX in n],
                [],
                "the unpublished partial is cleaned up, not left in the folder",
            )

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

            # Rerunning the same job republishes both names with the same
            # bytes -- and, since the 2026-08-04 review's fix 7d, keeps the
            # version it replaced: the previous run's programs are still
            # readable out of superseded/, which is what makes generating a
            # DIFFERENT order into this folder and prefix survivable.
            again = write_job(result, self.options(folder, quarantine_stamp="a2"))
            self.assertEqual(again.quarantine_problems, [])
            self.assertEqual(
                sorted(os.listdir(folder)),
                ["R720101N.anc", "R720102N.anc", SUPERSEDED_DIR_NAME],
            )
            self.assertEqual(
                [i.filename for i in again.superseded],
                ["R720101N.anc", "R720102N.anc"],
            )
            for outcome, item in zip(again.outcomes, again.superseded):
                self.assertEqual(self.read(outcome.path), outcome.text)
                self.assertIn("replaced by this run", item.reason)
                self.assertEqual(self.read(item.new_path), outcome.text)
                self.assertEqual(outcome.superseded_path, item.new_path)

    def test_no_partial_file_survives_a_successful_job(self):
        result, _config = one_frame_per_sheet("W2036", "W2436")
        with tempfile.TemporaryDirectory() as folder:
            write_job(result, self.options(folder))
            self.assertEqual(
                [n for n in os.listdir(folder) if PARTIAL_SUFFIX in n], []
            )

    def test_nothing_is_published_until_every_program_is_on_the_disk(self):
        """Fix 7e: publication is one tight loop after ALL the writes.

        The disk fills up on the LAST sheet of a three-sheet job.  Under the
        old write-then-rename-per-sheet loop the folder ended up holding this
        run's sheets 1 and 2 beside yesterday's sheet 3 — three plausible
        programs, one of them from another job.  Now the failure is found
        before anything is renamed, so what is on the disk when it happens is
        still exactly the previous job.
        """
        from faceframe_cnc.post import job as job_module

        result, _config = one_frame_per_sheet("W2036", "W2436", "W3036")
        with tempfile.TemporaryDirectory() as folder:
            write_job(result, self.options(folder))
            before = {
                name: self.read(folder, name) for name in sorted(os.listdir(folder))
            }
            real_open = open
            published: list[str] = []
            real_replace = job_module.os.replace

            def watch(src, dst):
                if PARTIAL_SUFFIX in str(src):
                    published.append(os.path.basename(str(dst)))
                return real_replace(src, dst)

            def die_on_the_last(path, *args, **kwargs):
                handle = real_open(path, *args, **kwargs)
                if "R720103N" in str(path) and PARTIAL_SUFFIX in str(path):
                    return _DiesHalfWay(handle)
                return handle

            job_module.open = die_on_the_last
            job_module.os.replace = watch
            try:
                job = write_job(
                    result, self.options(folder, quarantine_stamp="20260804-170000")
                )
            finally:
                del job_module.open
                job_module.os.replace = real_replace

            # Sheet 3 is refused; 1 and 2 are published, and every rename
            # happened after the failure was already known.
            self.assertEqual([o.filename for o in job.refused], ["R720103N.anc"])
            self.assertEqual(job.refused[0].refusal_kind, "write")
            self.assertEqual(published, ["R720101N.anc", "R720102N.anc"])
            # Sheet 3's earlier program is quarantined rather than left in the
            # folder looking as current as the two just written.
            self.assertEqual(
                sorted(os.listdir(folder)),
                ["R720101N.anc", "R720102N.anc", SUPERSEDED_DIR_NAME],
            )
            self.assertEqual(
                self.read(job.refused[0].superseded_path), before["R720103N.anc"]
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
                if PARTIAL_SUFFIX in str(src):
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
                if PARTIAL_SUFFIX in str(path):
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
                [n for n in os.listdir(folder) if PARTIAL_SUFFIX in n],
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

    def test_the_partial_is_read_back_before_anything_is_published(self):
        """A write that "succeeded" and a file that holds the program are two
        different claims (fix 7e)."""
        from faceframe_cnc.post import job as job_module

        result, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            real_open = open

            class _Lies:
                """Reports success and puts nothing like the program on disk."""

                def __init__(self, handle):
                    self._handle = handle

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    self._handle.close()
                    return False

                def write(self, text):
                    self._handle.write(text[:-20])

                def flush(self):
                    self._handle.flush()

                def fileno(self):
                    return self._handle.fileno()

            def lie(path, mode="r", *args, **kwargs):
                handle = real_open(path, mode, *args, **kwargs)
                if PARTIAL_SUFFIX in str(path) and "w" in mode:
                    return _Lies(handle)
                return handle

            job_module.open = lie
            try:
                job = write_job(result, self.options(folder))
            finally:
                del job_module.open

            outcome = job.outcomes[0]
            self.assertEqual(outcome.refusal_kind, "write")
            self.assertIn("not the program the verifier passed", outcome.problems[0])
            self.assertEqual(os.listdir(folder), [], "and nothing is left behind")

    def test_a_failed_write_into_a_clean_folder_leaves_no_debris(self):
        from faceframe_cnc.post import job as job_module

        result, _config = one_frame_per_sheet("W2036")
        with tempfile.TemporaryDirectory() as folder:
            real_open = open

            def half_write(path, *args, **kwargs):
                handle = real_open(path, *args, **kwargs)
                if PARTIAL_SUFFIX in str(path):
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
        """The insets are measured and unchanged; the stile groove's ENDS are
        clamped inside the part (2026-08-05 amendment, job R0805) so the swept
        cut finishes flush with the part edge instead of 0.69 past it."""
        from faceframe_cnc.post.generator import groove_segment
        from faceframe_cnc.post.model import SECTION_PANEL

        program = self.program()
        part = program.flat_parts()[0]
        config = default_config()
        panel = config.panel
        radius = config.tool(SECTION_PANEL).radius
        stile_low, stile_low_end = groove_segment(part, 0, panel, radius)
        rail_low, rail_high = groove_segment(part, 1, panel, radius)
        self.assertAlmostEqual(stile_low[0], part.box.x0 + 0.5625)
        self.assertAlmostEqual(rail_low[1], part.box.y0 + 0.9375)
        self.assertAlmostEqual(rail_low[0], part.box.x0 + 0.5625)
        self.assertAlmostEqual(rail_high[0], part.box.x1 - 0.5625)
        self.assertAlmostEqual(stile_low[1], part.box.y0 + radius)
        self.assertAlmostEqual(stile_low_end[1], part.box.y1 - radius)

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
                    self.assertEqual(heads, [13, 17, 11, 12, 11, 12])
                else:
                    self.assertEqual(heads, [13, 11, 12, 11, 12])

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
