"""Job R0805, 05 AUG 26 — the sheet that cut a divot, frozen forever.

One 49x97 sheet, one ``WDC2436`` beside one rotated ``W3330``, program
``R080501N.anc``.  It came off the machine wrong: the W3330's T13 stile
grooves ran the measured 0.375 overrun past the part ends, and with the
neighbour only the 0.455 part gap away the 0.6299 panel cutter took two
half-round bites 0.235" into the WDC's right stile, 0.20 deep — the divots in
the shop photos — and ran 0.42 past the 49" sheet edge at the other end.
**The verifier passed the sheet.**  Both facts are fixed by the 2026-08-05
amendment (``CLAUDE_CODE_PROMPT_Tabs_and_Groove_Clamp.md`` §1, §2, §4), and
this module is the permanent regression fixture for the groove half of it.

What is asserted here, in the order the milestone built it
---------------------------------------------------------
1.  :func:`r0805_layout` reproduces the shop sheet.  Not "a sheet like it":
    every coordinate below was recovered from ``R080501N.anc`` by hand (spec
    §1) and is asserted against what the planner computes — the two part
    footprints, the 0.455 gap, and all six groove centrelines including the
    two that misbehaved.  A fixture that does not match the shop file is
    worth nothing.
2.  ``tests/data/r0805_old_emission.anc`` is that sheet as the OLD generator
    wrote it, captured before the clamp landed and committed verbatim.  It is
    the one thing in the repo that still contains the failure, so it is what
    proves the fixed verifier refuses it — forever, whatever the generator
    goes on to do.
3.  The clamped emission: no groove's swept cut leaves its part, nothing is
    cut past the sheet edge, the stile grooves still run the FULL part length
    (flush end to flush end — the groove still breaks out through both rail
    ends, it just stops there), the rail grooves are untouched to the byte,
    and the verifier passes.
4.  Spec §6's sweep across every frame family that HAS stile grooves.

The 2026-08-05 amendments on this sheet: four causes, pinned one at a time
------------------------------------------------------------------------
The frozen old emission predates all of them, so the diff between it and what
the post writes today has four causes and this module pins each one separately
(:class:`ClampedEmissionTest`), section by section:

*   the **groove clamp** — four lines of the T13 section, and nothing else
    before the openings section;
*   the **onion-skin removal** — Scott's second decision the same day (spec
    §3b, decided at the M1 check-in: "don't need it anymore", because the parts
    are tab-held from milestones 2b/3 on), which took the Z0.06 pass away;
*   the **max-bite ladder** — his decision later the same day: at most 0.4 of
    material per T11 pass, "to reduce the load on it".  The perimeter's two
    Z0.06 loops are replaced by two Z0.372 ones (same offset, same feeds, a
    shorter lead-in ramp), and each opening gains a Z0.45 roughing loop
    (:func:`~faceframe_cnc.post.from_layout.generated_post_passes`,
    :func:`~faceframe_cnc.post.from_layout.generated_opening_passes`);
*   the **tabs and the release section**, which the frozen file has nothing at
    all to compare against and which are pinned on their own.

All of them are pinned against the same frozen file, so "nothing else moved" is
still a statement this module makes.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
import re
import unittest
from dataclasses import replace

from faceframe_cnc.geometry import FrameType, infer_frame_type
from faceframe_cnc.nesting import (
    NestingConfig,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    validate_layouts,
)
from faceframe_cnc.post import tabs
from faceframe_cnc.post.from_layout import (
    T11_MAX_BITE,
    panel_groove_indices,
    plan_sheet,
    post_config_for,
)
from faceframe_cnc.post.generator import (
    emit,
    fmt,
    generate,
    groove_segment,
    release_path,
)
from faceframe_cnc.post.motion import MotionKind
from faceframe_cnc.post.model import (
    Box,
    ProgramHeader,
    SECTION_PANEL,
    SECTION_RELEASE,
    default_config,
)
from faceframe_cnc.post.verifier import expected_work, verify

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

#: The frozen pre-amendment emission of this sheet.  Never regenerate it.
OLD_EMISSION = os.path.join(DATA_DIR, "r0805_old_emission.anc")

#: The header the fixture was captured with, so the new emission differs from
#: the frozen one ONLY where the amendment changed the geometry.
CREATED = "05 AUG 26 - 07:30"
PROGRAM_NAME = "R080501N"

#: Field evidence, spec §1: the part edges recovered from the T11 through
#: pass of the real ``R080501N.anc`` (tool radius 0.1875).
WDC_BOX = Box(0.2725, 1.42, 18.2725, 37.42)
W3330_BOX = Box(18.7275, 1.0, 48.7275, 34.0)
#: Edge-to-edge, measured: the 0.455 part gap the packer holds.
FIELD_GAP = 0.455


def r0805_layout():
    """``(layout, specs, nesting config)`` for the R0805 sheet as cut.

    The W3330 is ROTATED — 33 wide by 30 tall as ordered, 30 by 33 as placed
    — which is why its stile grooves run in X and its overrun pointed at the
    WDC instead of at the trim margin.
    """
    layout = SheetLayout(
        placements=[
            Placement(
                "WDC2436",
                WDC_BOX.x0,
                WDC_BOX.y0,
                WDC_BOX.width,
                WDC_BOX.height,
                False,
                [],
            ),
            Placement(
                "W3330",
                W3330_BOX.x0,
                W3330_BOX.y0,
                W3330_BOX.width,
                W3330_BOX.height,
                True,
                [],
            ),
        ]
    )
    specs = [
        PartSpec("WDC2436", 18.0, 36.0, 1),
        PartSpec("W3330", 33.0, 30.0, 1),
    ]
    return layout, specs, NestingConfig()


def r0805_plan():
    """``(program, plan, post config)`` for the R0805 sheet, planned as
    :func:`faceframe_cnc.post.job.build_job` plans it."""
    layout, specs, config = r0805_layout()
    post = post_config_for(config)
    header = ProgramHeader(name=PROGRAM_NAME, o_number=1, created=CREATED)
    program, plan = plan_sheet(layout, header, specs, config, post)
    return program, plan, post


def r0805_text() -> str:
    program, plan, post = r0805_plan()
    return generate(program, plan, post)


def old_emission() -> str:
    with open(OLD_EMISSION, "r", newline="") as handle:
        return handle.read()


def section_of(text, tool_number: int, last: bool = False) -> list[str]:
    """One tool section's lines, head line included, tail line included.

    ``last`` picks the LAST section for that tool number, which is how the T12
    release section is told from the T12 detail pass (they share the tool; the
    release is always the final section, spec §3c).

    ``text`` is the program, either as text or as the list of lines a caller has
    already taken the tab lifts out of.
    """
    lines = text.split("\r\n") if isinstance(text, str) else list(text)
    heads = [i for i, line in enumerate(lines) if line.startswith("(ROUTE TOOL")]
    mine = [
        i
        for i in heads
        if re.match(rf"\(ROUTE TOOL #{tool_number}:", lines[i])
    ]
    assert mine, f"no T{tool_number} section"
    head = mine[-1] if last else mine[0]
    following = [i for i in heads if i > head]
    end = following[0] - 1 if following else len(lines)
    return lines[head:end]



def without_loops_at(lines: list[str], z_word: str) -> list[str]:
    """``lines`` with every whole feature block cut at ``z_word`` removed.

    ``z_word`` is the Z as the file states it plus a trailing space (``"Z0.45 "``),
    so it matches the lead-in that OPENS a loop and nothing else.  A block runs
    from its preposition — whichever of the two forms it uses — to its closing
    ``G0 Z2.5``; the walk back stops at the previous block's own ``G0 Z2.5`` or at
    the section's ``Tn`` line, so no arithmetic about how many lines a
    preposition takes is needed anywhere.
    """
    out = list(lines)
    while True:
        anchor = next(
            (
                i
                for i, line in enumerate(out)
                if line.startswith("G1 ") and z_word in line
            ),
            None,
        )
        if anchor is None:
            return out
        start = anchor
        while start > 0 and out[start - 1] != "G0 Z2.5" and not re.fullmatch(
            r"T\d+", out[start - 1]
        ):
            start -= 1
        end = out.index("G0 Z2.5", anchor)
        del out[start : end + 1]


def section_body(text, tool_number: int, last: bool = False) -> list[str]:
    """One section's lines down to its final retract, template tail dropped.

    ``text`` is the program, either as text or as the list of lines a caller has
    already taken the tab lifts out of.

    A section that is not the program's last closes with the four-line
    ``M59``/``G80``/``G17 G91 G28 Z0 M95``/``M92`` template and the last one runs
    into the program epilogue instead, so two programs whose LAST section differs
    cannot be compared line for line unless the tails come off.  Everything up to
    and including the final ``G0 Z2.5`` is the cutting.
    """
    lines = section_of(text, tool_number, last)
    end = max(i for i, line in enumerate(lines) if line == "G0 Z2.5")
    return lines[: end + 1]

def without_tab_lifts(text: str) -> list[str]:
    """``text``'s lines with every tab lift taken out (2026-08-05 amendment §3b).

    A lift is four moves — cut on to the foot of the climb, climb to the tab top,
    traverse it, descend back to depth — and the CLIMB is the only line in the
    program that commands the tab top's Z, which makes the block findable without
    re-deriving where any tab is.
    """
    top = f" Z{fmt(default_config().tabs.top_z)}"
    lines = text.split("\r\n")
    drop: set[int] = set()
    for index, line in enumerate(lines):
        if line.endswith(top):
            drop.update({index - 1, index, index + 1, index + 2})
    assert drop, "no tab lift found: this helper is measuring the wrong thing"
    return [line for index, line in enumerate(lines) if index not in drop]


def groove_sweeps(program, plan, post, cutting_only=False):
    """``[(part, feature, swept box)]`` for every T13 groove cut emitted.

    Read off the emitter's own motion stream rather than recomputed, so what
    is judged is what the machine is told to do: the tool centre segment of
    every cutting move in the panel section, grown by the T13 radius on all
    four sides.  Growing the segment's bounding box over-states the two
    round ends by a corner each, which is the safe direction.

    A groove is two cutting moves — the plunge, whose sweep is a single
    tool-sized circle at the start point, and the at-depth cut that draws the
    groove.  Both remove material, so both belong in a containment check;
    ``cutting_only`` keeps just the second, which is the one whose LENGTH
    means anything.
    """
    parts = program.flat_parts()
    radius = post.tool(SECTION_PANEL).radius
    out = []
    for motion in emit(program, plan, post).motions:
        if motion.section != SECTION_PANEL or not motion.is_cut:
            continue
        if motion.feature is None or motion.feature.kind != "groove":
            continue
        if cutting_only and motion.kind is not MotionKind.FEED:
            continue
        centre = Box(
            min(motion.from_x, motion.to_x),
            min(motion.from_y, motion.to_y),
            max(motion.from_x, motion.to_x),
            max(motion.from_y, motion.to_y),
        )
        out.append((parts[motion.feature.part], motion.feature, centre.grow(radius)))
    return out


class R0805NestTest(unittest.TestCase):
    """The fixture reproduces the shop sheet, coordinate for coordinate."""

    def setUp(self):
        self.layout, self.specs, self.config = r0805_layout()
        self.program, self.plan, self.post = r0805_plan()
        self.parts = self.program.flat_parts()

    def test_the_optimizer_would_accept_this_layout(self):
        """It was a legal nest — that is the point.  0.455 gap, WDC edge rule
        satisfied, both parts on the sheet."""
        result = NestingResult(
            unique_sheets=[(self.layout, 1)],
            total_sheets=1,
            demand=self.specs,
            config=self.config,
        )
        self.assertEqual(validate_layouts(result, self.config), [])

    def test_the_two_part_footprints_are_the_measured_ones(self):
        self.assertEqual(
            [(p.part_number, p.box.rounded(4), p.rotated) for p in self.parts],
            [
                ("WDC2436", WDC_BOX.rounded(4), False),
                ("W3330", W3330_BOX.rounded(4), True),
            ],
        )

    def test_the_gap_between_the_parts_is_the_measured_0_455(self):
        self.assertAlmostEqual(
            W3330_BOX.x0 - WDC_BOX.x1, FIELD_GAP, places=6
        )

    def test_the_wdc_owes_no_stile_groove(self):
        """Its 2" stiles take the T17 slot instead (2026-08-03 amendment), so
        the grooves that overran belong to the W3330 alone."""
        self.assertEqual(panel_groove_indices("WDC2436"), (1, 3))
        self.assertEqual(
            sorted(
                ref.index for ref in self.plan.panel if ref.part == 0
            ),
            [1, 3],
        )

    def test_the_wdc_rail_grooves_run_the_measured_x_span(self):
        """Spec §1's grooves that BEHAVED: X 0.835 to 17.71, stopping at the
        stile centre lines and never leaving the part."""
        for index in (1, 3):
            start, end = groove_segment(
                self.parts[0], index, self.post.panel, self.post.tool(SECTION_PANEL).radius
            )
            self.assertAlmostEqual(start[0], 0.835, places=4)
            self.assertAlmostEqual(end[0], 17.71, places=4)

    def test_the_w3330_rail_grooves_run_the_measured_y_span(self):
        """X 19.665 and 47.79, spanning Y 1.5625 to 33.4375 — exactly between
        the two stile-groove centre lines."""
        spans = []
        for index in (1, 3):
            start, end = groove_segment(
                self.parts[1], index, self.post.panel, self.post.tool(SECTION_PANEL).radius
            )
            spans.append((round(start[0], 4), round(start[1], 4), round(end[1], 4)))
        self.assertEqual(
            sorted(spans), [(19.665, 1.5625, 33.4375), (47.79, 1.5625, 33.4375)]
        )

    def test_the_w3330_stile_grooves_sit_on_the_measured_centre_lines(self):
        """Y 1.5625 and Y 33.4375 — the 0.5625 stile inset, horizontal because
        the part is rotated.  Where they STOP is the amendment's business."""
        lines = []
        for index in (0, 2):
            start, end = groove_segment(
                self.parts[1], index, self.post.panel, self.post.tool(SECTION_PANEL).radius
            )
            self.assertAlmostEqual(start[1], end[1], places=9)
            lines.append(round(start[1], 4))
        self.assertEqual(sorted(lines), [1.5625, 33.4375])


class FrozenOldEmissionTest(unittest.TestCase):
    """``tests/data/r0805_old_emission.anc``: the failure, kept on purpose.

    Captured from this exact layout with the pre-amendment generator before
    the clamp was written, so it is the only artefact left that still states
    the overrun.  Nothing regenerates it; if the generator ever writes these
    lines again, the verifier assertion below is what says so.
    """

    def setUp(self):
        self.text = old_emission()

    def test_the_frozen_file_is_this_sheet(self):
        self.assertIn(f"({PROGRAM_NAME})", self.text)
        self.assertIn("(ROUTE TOOL #13: T13 - 3/8 PANEL CUTTER)", self.text)
        self.assertIn("(ROUTE TOOL #17: T17 45 VTIP 158-562SC.026-1W-A)", self.text)
        self.assertTrue(self.text.startswith("%\r\n"))
        self.assertTrue(self.text.endswith("M30\r\n%\r\n"))

    def test_the_frozen_file_still_states_the_overrunning_grooves(self):
        """Spec §1: the two stile grooves at Y1.5625 / Y33.4375 running from
        X18.3525 to X49.1025 — 0.375 past each part end at the centreline."""
        self.assertIn("X18.3525 Y1.5625 Z2.5", self.text)
        self.assertIn("X18.3525 Y33.4375 Z2.5", self.text)
        self.assertEqual(self.text.count("X49.1025 F490."), 2)

    def test_the_fixed_verifier_refuses_it_for_cutting_the_wdc(self):
        """The whole reason the sheet reached the machine: this returned []."""
        _, _, post = r0805_plan()
        problems = verify(self.text, post)
        foreign = [v for v in problems if v.code == "foreign-cut"]
        self.assertTrue(foreign, [str(v) for v in problems])
        # the sweep, and the part it entered, both named in the message
        messages = " | ".join(v.message for v in foreign)
        self.assertIn("18.0375", messages, "the swept edge that took the bite")
        self.assertIn(f"x0={WDC_BOX.x0}", messages)
        self.assertIn(f"x1={WDC_BOX.x1}", messages)
        self.assertIn("0.2 deep", messages)

    def test_it_is_refused_with_the_manifest_too(self):
        """Generate hands verify() an expected-work manifest; the refusal must
        not depend on which of the two calls it is."""
        layout, _, config = r0805_layout()
        post = post_config_for(config)
        problems = verify(self.text, post, expected_work(layout, post))
        self.assertTrue([v for v in problems if v.code == "foreign-cut"])


class ClampedEmissionTest(unittest.TestCase):
    """The same sheet, generated by the amended post (2026-08-05, Scott)."""

    def setUp(self):
        self.program, self.plan, self.post = r0805_plan()
        self.parts = self.program.flat_parts()
        self.radius = self.post.tool(SECTION_PANEL).radius
        self.text = generate(self.program, self.plan, self.post)

    # -- the clamp itself --------------------------------------------------

    def test_the_stile_groove_endpoints_are_flush_with_the_part_ends(self):
        """Centreline stops one tool radius in, so the CUT ends at the edge."""
        for index in (0, 2):
            start, end = groove_segment(
                self.parts[1], index, self.post.panel, self.radius
            )
            self.assertAlmostEqual(
                start[0], W3330_BOX.x0 + self.radius, places=9
            )
            self.assertAlmostEqual(end[0], W3330_BOX.x1 - self.radius, places=9)

    def test_the_program_no_longer_states_the_overrun(self):
        self.assertNotIn("X18.3525", self.text)
        self.assertNotIn("X49.1025", self.text)

    def test_the_stile_groove_still_runs_the_full_part_length(self):
        """Flush to flush: the SWEPT groove is exactly as long as the part, so
        it still breaks out through both rail ends the way the shop expects —
        it simply stops there instead of carrying on into the neighbour."""
        cuts = groove_sweeps(self.program, self.plan, self.post, cutting_only=True)
        self.assertEqual(len(cuts), 6, "two WDC rails plus the W3330's four")
        for part, feature, swept in cuts:
            if feature.index not in (0, 2) or part.part_number != "W3330":
                continue
            self.assertAlmostEqual(swept.x0, W3330_BOX.x0, places=9)
            self.assertAlmostEqual(swept.x1, W3330_BOX.x1, places=9)
            self.assertAlmostEqual(swept.width, W3330_BOX.width, places=9)

    # -- nothing is cut where it must not be -------------------------------

    def test_no_groove_sweep_leaves_any_part_box(self):
        for part, feature, swept in groove_sweeps(self.program, self.plan, self.post):
            with self.subTest(part=part.part_number, groove=feature.index):
                self.assertTrue(
                    part.box.contains(swept, 1e-9),
                    f"{feature} sweeps {swept} outside {part.box}",
                )

    def test_no_groove_sweep_reaches_the_other_part(self):
        """The 0.455 gap is empty of T13 now — that is the divot, closed."""
        for part, feature, swept in groove_sweeps(self.program, self.plan, self.post):
            for other in self.parts:
                if other is part:
                    continue
                with self.subTest(part=part.part_number, groove=feature.index):
                    self.assertFalse(other.box.overlaps(swept, 1e-9))

    def test_nothing_is_cut_past_the_sheet_edge(self):
        """The right-hand end used to reach X49.4175 on a 49" sheet."""
        sheet = Box(0.0, 0.0, self.post.sheet_width, self.post.sheet_length)
        for part, feature, swept in groove_sweeps(self.program, self.plan, self.post):
            with self.subTest(part=part.part_number, groove=feature.index):
                self.assertTrue(sheet.contains(swept, 1e-9), f"{swept} off the sheet")

    # -- the verifier's verdict --------------------------------------------

    def test_the_verifier_passes_the_new_emission(self):
        self.assertEqual([str(v) for v in verify(self.text, self.post)], [])

    def test_the_verifier_passes_it_against_the_manifest_too(self):
        layout, _, config = r0805_layout()
        manifest = expected_work(layout, self.post)
        self.assertEqual(
            [str(v) for v in verify(self.text, self.post, manifest)], []
        )

    def test_the_frozen_old_emission_and_the_new_one_disagree(self):
        """Belt and braces: one file is refused and the other passes, and they
        are not the same file."""
        self.assertNotEqual(self.text, old_emission())

    # -- what the amendment must NOT have changed --------------------------

    def test_the_wdc_groove_treatment_is_byte_identical(self):
        """A WDC has no stile groove to clamp (T17 instead, 2026-08-03), so
        both of its rail grooves come out exactly as they did before."""
        old = old_emission()
        for line in (
            "G0 G54 G90 X0.835 Y2.3575 M13 S17500",
            "X0.835 Y36.4825 Z2.5",
        ):
            self.assertIn(line, old)
            self.assertIn(line, self.text)
        self.assertEqual(old.count("X17.71 F490."), 2)
        self.assertEqual(self.text.count("X17.71 F490."), 2)

    def test_the_rail_grooves_are_byte_identical(self):
        """Spec §2: rail grooves already stopped at the stile centre lines and
        are explicitly left alone."""
        old = old_emission()
        for line in ("X19.665 Y1.5625 Z2.5", "X47.79 Y1.5625 Z2.5"):
            self.assertIn(line, old)
            self.assertIn(line, self.text)
        self.assertEqual(old.count("Y33.4375 F490."), 2)
        self.assertEqual(self.text.count("Y33.4375 F490."), 2)

    def test_the_t17_section_is_byte_identical(self):
        """The T17 slot with its 0.875 cone rule is not this amendment's
        business (spec §2, §8), and the section head restates the last T13
        point — a rail groove's end — so not even that moves."""
        self.assertEqual(section_of(self.text, 17), section_of(old_emission(), 17))

    def test_the_t13_diff_is_exactly_the_four_clamped_groove_lines(self):
        """Pin one: the clamp, line by line, in the section it lives in."""
        old_lines = section_of(old_emission(), 13)
        new_lines = section_of(self.text, 13)
        self.assertEqual(len(old_lines), len(new_lines))
        differing = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
        self.assertEqual(
            differing,
            [
                ("X18.3525 Y1.5625 Z2.5", "X19.0424 Y1.5625 Z2.5"),
                ("X49.1025 F490.", "X48.4125 F490."),
                ("X18.3525 Y33.4375 Z2.5", "X19.0424 Y33.4375 Z2.5"),
                ("X49.1025 F490.", "X48.4125 F490."),
            ],
        )

    def test_the_header_and_everything_above_the_openings_is_byte_identical(self):
        old_lines = old_emission().split("\r\n")
        new_lines = self.text.split("\r\n")
        first_t11 = min(
            i for i, line in enumerate(new_lines) if line.startswith("(ROUTE TOOL #11")
        )
        self.assertEqual(
            new_lines[:13], old_lines[:13], "header, banner and prologue"
        )
        self.assertEqual(
            first_t11,
            min(
                i
                for i, line in enumerate(old_lines)
                if line.startswith("(ROUTE TOOL #11")
            ),
        )

    def test_the_flat_twins_perimeter_is_the_frozen_one_with_the_ladder_in_it(self):
        """Pin two: the perimeter passes, against the frozen file, line for line.

        Isolated from the tabs by emitting the SAME plan with the tab top out of
        every pass's reach (:meth:`flat_text`), so the loops come out flat, and
        taken section by section so the openings ladder below cannot shift it.

        The frozen file's perimeter section and today's have the SAME NUMBER OF
        LINES and the same shape — two loops per part, the ``M59`` marker after
        the first — because both amendments happened to leave two passes: the
        2026-08-05 removal took the Z0.06 onion skin away and the max-bite rule
        (Scott, the same day: at most 0.4 of material per T11 pass) put a Z0.372
        roughing rung in its place.  So the whole diff is six lines, three per
        roughing loop, and each of the three is one the pass's own depth decides:

        *   the preposition, whose Y is the start of the lead-in ramp — shorter
            from Z0.372 than from Z0.06, by the post's measured 2:1 ratio;
        *   the lead-in itself, which states the Z;
        *   the ramp-out, the same length at the other end of the loop.

        Everything else — every at-depth move, both offsets, the marker, the
        through loops entire — is the frozen file's own bytes.  Asserted as the
        exact list of differing pairs, the way the T13 clamp is above.
        """
        old_section = section_of(old_emission(), 11, last=True)
        flat_section = section_of(self.flat_text(), 11, last=True)
        self.assertEqual(len(old_section), len(flat_section))
        skin = default_config().perimeter_passes[0]
        rough = self.post.perimeter_passes[0]
        self.assertEqual(
            [(a, b) for a, b in zip(old_section, flat_section) if a != b],
            [
                (
                    "G0 G54 G90 X18.512 Y15.54 M13 S16700",
                    "G0 G54 G90 X18.512 Y16.164 M13 S16700",
                ),
                (
                    f"G1 X18.462 Y19.42 Z{fmt(skin.z_cut)} F150.",
                    f"G1 X18.462 Y19.42 Z{fmt(rough.z_cut)} F150.",
                ),
                ("X18.512 Y23.675 Z2.", "X18.512 Y23.051 Z2."),
                ("X48.967 Y13.62 Z2.5", "X48.967 Y14.244 Z2.5"),
                (
                    f"G1 X48.917 Y17.5 Z{fmt(skin.z_cut)} F150.",
                    f"G1 X48.917 Y17.5 Z{fmt(rough.z_cut)} F150.",
                ),
                ("X48.967 Y21.755 Z2.", "X48.967 Y21.131 Z2."),
            ],
        )
        # and the two ramp endpoints really did move by the difference the two
        # depths imply, rather than by some number of their own
        moved = (rough.z_cut - skin.z_cut) * self.post.ramp_ratio
        self.assertAlmostEqual(16.164 - 15.54, moved, places=9)
        self.assertAlmostEqual(23.675 - 23.051, moved, places=9)
        self.assertEqual(self.text.count("Z0.06"), 0, "no skin pass survives")
        self.assertEqual(old_emission().count("Z0.06"), 2, "the frozen file has two")

    def test_the_flat_twins_openings_are_the_frozen_ones_plus_a_roughing_rung(self):
        """Pin two-b: the openings ladder, against the frozen file.

        The frozen file roughs each opening once, taking the whole 0.60; today's
        program takes it in two 0.3 bites (Scott, 2026-08-05 — FLAGGED for
        ratification, since his instruction named the perimeter and the rule he
        stated is about the tool).  So the section gains one loop per opening and
        nothing else, which is asserted by taking the roughing loops back OUT: what
        is left is the frozen section byte for byte, apart from the decoration the
        post's grammar attaches to whichever loop comes first in a section — the
        spindle-start preposition and the ``G43 H11`` line — which now rides the
        first ROUGHING loop.  Those three lines are lifted from the frozen file
        itself rather than typed, and the two-line "later preposition" form they
        replace is checked to be the same point.
        """
        old_section = section_of(old_emission(), 11)
        flat_section = section_of(self.flat_text(), 11)
        rough, deep = self.post.openings_passes
        self.assertAlmostEqual(
            self.post.stock_top_z - rough.z_cut, 0.3, places=9, msg="equal bites"
        )
        self.assertEqual(deep, default_config().openings_passes[-1], "measured")

        stripped = without_loops_at(flat_section, f"Z{fmt(rough.z_cut)} ")
        self.assertEqual(
            len(stripped),
            len(old_section) - 1,
            "one loop per opening removed, and the decoration one line shorter",
        )
        # the decoration: three frozen lines replacing the two-line later form,
        # and the POINT they preposition to has to be the frozen one
        decoration = old_section[5:8]
        self.assertRegex(decoration[0], r"^G0 G54 G90 X\S+ Y\S+ M13 S16700$")
        coords = " ".join(decoration[0].split()[3:5])
        self.assertEqual(stripped[5:7], [f"{coords} Z2.5", "Z2."])
        rebuilt = stripped[:5] + decoration + stripped[7:]
        self.assertEqual(rebuilt, old_section)

    def test_the_tabs_are_the_only_thing_between_the_flat_twin_and_the_program(self):
        """Pin three: what the tabs add, and that it is only insertions.

        Take the four lines of every tab lift out of the emitted program and what
        is left is the flat twin line for line, with one allowed difference — a
        move may now STATE the cutting feed it used to inherit, because the tab's
        descent put the entry feed in force and F is modal.
        """
        flat = self.flat_text()
        # Section by section, because the two files have a different LAST section
        # (the flat twin's perimeter runs into the epilogue; today's runs into the
        # release section) and only the release section is new.
        for tool in (13, 17, 11, 12):
            with self.subTest(tool=tool):
                self.assert_only_restated_feeds(
                    section_body(flat, tool), section_body(without_tab_lifts(self.text), tool)
                )
        self.assert_only_restated_feeds(
            section_body(flat, 11, last=True),
            section_body(without_tab_lifts(self.text), 11, last=True),
        )

    def assert_only_restated_feeds(self, before: list[str], after: list[str]) -> None:
        """``after`` is ``before``, except that a move may state a modal feed.

        A tab's descent puts the entry feed in force and F is modal, so the next
        at-depth move restates the cutting feed it used to inherit.  That is the
        one difference the tabs are allowed to leave behind once their own four
        lines are taken out.
        """
        self.assertEqual(len(after), len(before))
        self.assertTrue(before, "the sections must not be empty")
        feeds = {
            spec.cut_feed
            for spec in (
                *self.post.openings_passes,
                self.post.detail_pass,
                *self.post.perimeter_passes,
            )
        }
        for plain, tabbed in zip(before, after):
            if plain == tabbed:
                continue
            with self.subTest(before=plain, after=tabbed):
                match = re.fullmatch(rf"{re.escape(plain)} F(\d+(?:\.\d*)?)", tabbed)
                self.assertIsNotNone(match, "only a restated cut feed may differ")
                self.assertIn(float(match.group(1)), feeds)

    def test_every_deep_pass_lifts_over_every_tab_zone(self):
        """Spec §3b: one lift per zone per pass that cuts below the tab top.

        Counted two ways — off the plan's zones and off the emitted text — so a
        pass that quietly stopped lifting would show as a number, not as a
        judgement.
        """
        zones = self.plan.tabs
        self.assertEqual(
            set(zones),
            {(i, "perimeter", 0) for i in range(len(self.parts))}
            | {(i, "opening", 0) for i in range(len(self.parts))},
            "both parts' opening AND both parts' perimeter are held",
        )
        openings = sum(len(z) for key, z in zones.items() if key[1] == "opening")
        perimeters = sum(len(z) for key, z in zones.items() if key[1] == "perimeter")
        self.assertTrue(openings and perimeters)
        deep_openings = len(tabs.lifting_cuts(tabs.opening_cuts(self.post), self.post))
        deep_perimeters = len(
            tabs.lifting_cuts(tabs.perimeter_cuts(self.post), self.post)
        )
        self.assertEqual((deep_openings, deep_perimeters), (2, 1), "T11+T12, then T11")
        self.assertEqual(
            self.text.count(f" Z{fmt(self.post.tabs.top_z)}"),
            openings * deep_openings + perimeters * deep_perimeters,
        )

    def test_the_tab_sizes_and_spacing_are_the_ratified_ones(self):
        """Scott's numbers, on this sheet: 0.25 high, 0.75 long, ≥2 in from a
        corner, and a gap no longer than 10 between footprints."""
        self.assertEqual((self.post.tabs.top_z, self.post.tabs.length), (0.25, 0.75))
        for key, zones in self.plan.tabs.items():
            part = self.parts[key[0]]
            box = part.box if key[1] == "perimeter" else part.openings[key[2]]
            cuts = (
                tabs.perimeter_cuts(self.post)
                if key[1] == "perimeter"
                else tabs.opening_cuts(self.post)
            )
            footprint = tabs.worst_footprint(
                tabs.lifting_cuts(cuts, self.post), self.post
            )
            by_side: dict[str, list] = {}
            for zone in zones:
                self.assertEqual(zone.length, 0.75)
                by_side.setdefault(zone.side, []).append(zone)
            for side, mine in by_side.items():
                half = tabs.side_length(box, side) / 2.0
                mine.sort(key=lambda zone: zone.centre)
                for zone in mine:
                    low, high = zone.span(footprint / 2.0 - zone.length / 2.0)
                    with self.subTest(profile=key, side=side, centre=zone.centre):
                        self.assertGreaterEqual(low, -half + 2.0 - 1e-9)
                        self.assertLessEqual(high, half - 2.0 + 1e-9)
                for first, second in zip(mine, mine[1:]):
                    gap = (second.centre - first.centre) - footprint
                    with self.subTest(profile=key, side=side):
                        self.assertLessEqual(gap, self.post.tabs.max_gap + 1e-9)
                self.assertGreaterEqual(len(mine), 2, f"{side} of {key}")

    # -- the release section, spec §3c --------------------------------------

    def test_the_release_section_is_last_and_is_a_second_t12_section(self):
        heads = [
            int(re.match(r"\(ROUTE TOOL #(\d+)", line).group(1))
            for line in self.text.split("\r\n")
            if line.startswith("(ROUTE TOOL")
        ]
        self.assertEqual(heads, [13, 17, 11, 12, 11, 12])
        body = section_of(self.text, 12, last=True)
        self.assertEqual(body[0], "(ROUTE TOOL #12: T12  0.200 DOWNSHEAR)")
        self.assertEqual(body[4], "T12")
        self.assertNotIn("(ROUTE TOOL", "\r\n".join(body[1:]))

    def test_the_release_cuts_are_one_per_tab_at_the_ratified_feeds(self):
        body = "\r\n".join(section_of(self.text, 12, last=True))
        total = sum(len(z) for z in self.plan.tabs.values())
        self.assertEqual(body.count(f"G1 Z{fmt(self.post.release_z)} F50."), total)
        self.assertEqual(body.count("F150."), total)
        self.assertEqual(self.post.release.entry_feed, 50.0)
        self.assertEqual(self.post.release.cut_feed, 150.0)
        self.assertEqual(self.post.release_z, self.post.detail_pass.z_cut)

    def test_the_release_frees_both_openings_before_either_perimeter(self):
        """Spec §3c's order, read off the emitted motion stream."""
        stream = emit(self.program, self.plan, self.post)
        kinds = [
            motion.feature.kind
            for motion in stream.motions
            if motion.section == SECTION_RELEASE and motion.kind is MotionKind.FEED
        ]
        self.assertEqual(
            kinds,
            ["opening"] * sum(
                len(z) for key, z in self.plan.tabs.items() if key[1] == "opening"
            )
            + ["perimeter"] * sum(
                len(z) for key, z in self.plan.tabs.items() if key[1] == "perimeter"
            ),
        )
        self.assertEqual(
            [ref.kind for ref in self.plan.release],
            ["opening", "opening", "perimeter", "perimeter"],
        )

    def test_every_release_cut_is_flush_with_a_finished_profile(self):
        """Spec §3c/§8: never the T11 centreline.

        Checked numerically off the motion stream: the cut's own swept edge has
        to land ON the finished line, which means its centre stands one T12
        radius off it — inside an opening, outside a footprint.
        """
        radius = self.post.tool(SECTION_RELEASE).radius
        stream = emit(self.program, self.plan, self.post)
        seen = 0
        for motion in stream.motions:
            if motion.section != SECTION_RELEASE or motion.kind is not MotionKind.FEED:
                continue
            seen += 1
            ref = motion.feature
            part = self.parts[ref.part]
            finished = part.box if ref.kind == "perimeter" else part.openings[ref.index]
            path = release_path(finished, ref.kind, radius)
            across = (
                motion.from_y
                if abs(motion.to_x - motion.from_x) > abs(motion.to_y - motion.from_y)
                else motion.from_x
            )
            lines = (
                (path.y0, path.y1)
                if abs(motion.to_x - motion.from_x) > abs(motion.to_y - motion.from_y)
                else (path.x0, path.x1)
            )
            with self.subTest(ref=ref, across=across):
                self.assertTrue(
                    any(abs(across - line) < 1e-9 for line in lines),
                    f"{across} is not a flush line of {path}",
                )
                self.assertAlmostEqual(motion.to_z, self.post.release_z, places=9)
        self.assertEqual(seen, sum(len(z) for z in self.plan.tabs.values()))

    def test_the_generated_post_table_is_why_the_passes_are_what_they_are(self):
        """Both removals and the ladder are post-table decisions, not emitter
        special cases: the skin's Z0.06 is gone, and what a generated sheet runs
        instead is the max-bite ladder built from the tool's own declared limit
        (Scott, 2026-08-05: 0.4 per T11 pass).
        """
        self.assertEqual(
            [spec.z_cut for spec in self.post.perimeter_passes], [0.372, -0.006]
        )
        self.assertEqual(
            [spec.z_cut for spec in self.post.openings_passes], [0.45, 0.15]
        )
        self.assertEqual(self.post.tool("perimeter").max_bite, T11_MAX_BITE)
        self.assertEqual(self.post.tool("openings").max_bite, T11_MAX_BITE)
        self.assertEqual(
            [spec.z_cut for spec in default_config().perimeter_passes],
            [0.06, -0.006],
            "the measured table still describes the references' two-pass dialect",
        )
        self.assertEqual(
            [spec.z_cut for spec in default_config().openings_passes],
            [0.15],
            "and its one 0.60 opening bite",
        )
        self.assertEqual(
            [tool.max_bite for tool in default_config().tools.values()],
            [None] * len(default_config().tools),
            "with no bite limit anywhere, so no reference file is judged by one",
        )
        self.assertIsNone(
            default_config().release,
            "and it runs no release pass, so the reference files owe none",
        )
        self.assertEqual(len(self.plan.perimeter), 2)
        for refs in self.plan.perimeter:
            self.assertEqual(len(refs), len(self.parts), "one loop per part per rung")

    # -- helpers -----------------------------------------------------------

    def flat_text(self) -> str:
        """This sheet, emitted from the same plan with the holding taken out.

        The device that keeps the pins above independent of each other: same
        layout, same planner, same emitter, same clamped grooves and the same
        max-bite ladders — no tabs and no release section.  So what it differs
        from the frozen pre-amendment file by is exactly the pass ladders, and
        what it differs from today's program by is exactly the holding.
        """
        return generate(
            self.program, replace(self.plan, tabs=None, release=[]), self.post
        )

    @staticmethod
    def perimeter_head_index(lines: list[str]) -> int:
        """Index of the LAST ``(ROUTE TOOL #11`` line: the perimeter section."""
        return max(
            i for i, line in enumerate(lines) if line.startswith("(ROUTE TOOL #11")
        )

    @staticmethod
    def perimeter_only(lines: list[str]) -> list[str]:
        """``lines`` up to and including the perimeter section's own tail.

        The frozen file's perimeter section runs into the program epilogue; the
        amended one runs into the release section instead, so the comparison
        stops at the section tail the two still share.
        """
        end = lines.index("M92") + 1 if "M92" in lines else len(lines)
        return lines[:end]


class SweptWidthBoundariesTest(unittest.TestCase):
    """Where the swept-width rule bites on the PART GAP — and how far it moved and back.

    Closing the shallow-cut waiver widened the rule beyond the T13 groove, and
    then the two 2026-08-05 amendments moved the part-gap boundary twice in one
    day, in opposite directions:

    1.  the onion skin went, which took with it the only pass whose at-depth loop
        reached 0.377 past a part edge.  For that half-day a generated sheet ran
        the through pass alone, whose loop reaches exactly 0.375 — tangent, and
        tangent is not an overlap — so a 0.375 gap away from the lead-in edge
        verified clean;
    2.  the max-bite ladder arrived (Scott: at most 0.4 of material per T11
        pass), and its roughing rung runs at the measured pass-1 offset 0.1895,
        the one that carries 0.002 of spring stock.  So the 0.377 reach is back,
        and a 0.375 gap is refused again from any direction.

    A generated sheet's perimeter therefore reaches past a part edge by three
    amounts, and a neighbour meets whichever one points at it:

    *   the roughing rung's at-depth loop: ``0.1895 + radius`` = **0.377**, so a
        neighbour 0.375 away loses 0.002 of material and is refused;
    *   the through pass's at-depth loop: ``0.1875 + radius`` = **exactly
        0.375**, tangent and clean on its own;
    *   either pass's lead-in / lead-out ramp on the entry edge: the ramp stands
        ``lateral_lead`` further out at the ramp plane and closes that gap as it
        descends, so where the through pass's ramp breaks the surface it reaches
        **0.3938** — which is what refuses a neighbour beside the entry edge at
        0.375 whether or not the ladder's roughing rung exists.

    The verifier is a backstop here and not the rule.  The floor lives in
    :data:`faceframe_cnc.nesting.MIN_PART_GAP` (0.455) and is proven where it is
    enforced: ``tests/test_nesting.MinPartGapCrossCheckTests`` pins the constant
    against the post's own measured sweep, and
    ``tests/test_gui_generate.SessionGenerateTests`` proves ``Session.optimize``
    and the settings load refuse anything below it.  The last assertion here is
    the one-line cross-check that the floor really is above every reach — which
    is what keeps it true across a change to the pass ladder like this one.
    """

    def sheet(self, gap: float, side_by_side: bool = False):
        """Two frames ``gap`` apart, stacked in Y or side by side in X.

        Which axis matters: the perimeter lead-in enters on the RIGHT edge by
        default (:func:`~faceframe_cnc.post.generator.default_entry_side`), so
        only a neighbour in +X meets the ramp.
        """
        if side_by_side:
            placements = [
                Placement("W2036", 1.0, 1.0, 20.0, 36.0, False, []),
                Placement("W2436", 21.0 + gap, 1.0, 24.0, 36.0, False, []),
            ]
            specs = [
                PartSpec("W2036", 20.0, 36.0, 1),
                PartSpec("W2436", 24.0, 36.0, 1),
            ]
        else:
            placements = [
                Placement("W3030", 0.5, 1.0, 30.0, 30.0, False, []),
                Placement("W3030", 0.5, 31.0 + gap, 30.0, 30.0, False, []),
            ]
            specs = [PartSpec("W3030", 30.0, 30.0, 2)]
        config = NestingConfig(part_gap=gap)
        post = post_config_for(config)
        program, plan = plan_sheet(
            SheetLayout(placements=placements),
            ProgramHeader(name="R990101N", created=CREATED),
            specs,
            config,
            post,
        )
        return generate(program, plan, post), post

    def test_the_production_part_gap_is_clean(self):
        """0.455 clears both reaches, in either direction."""
        for side_by_side in (False, True):
            with self.subTest(side_by_side=side_by_side):
                text, post = self.sheet(0.455, side_by_side)
                self.assertEqual([str(v) for v in verify(text, post)], [])

    def test_a_0_375_stacked_neighbour_is_refused_by_the_roughing_rung(self):
        """The backstop the max-bite ladder gives back, and which pass gives it.

        Before 2026-08-05 this sheet was refused by the onion-skin pass, which
        reached 0.377 and took 0.002 out of the neighbour.  Dropping the skin gave
        that up for half a day; the ladder's roughing rung runs at the same
        measured 0.1895 offset (0.002 of spring stock for the through pass to
        shave), so the 0.377 reach — and the refusal — is back.  Asserted on the
        pass the finding names, not just on the fact that something complained.
        """
        text, post = self.sheet(0.375)
        problems = [v for v in verify(text, post) if v.code == "foreign-cut"]
        self.assertTrue(problems, [str(v) for v in verify(text, post)])

        rough, through = post.perimeter_passes
        radius = post.tool("perimeter").radius
        self.assertAlmostEqual(rough.offset + radius, 0.377, places=9)
        self.assertAlmostEqual(through.offset + radius, 0.375, places=9)
        self.assertEqual(
            rough.offset,
            default_config().perimeter_passes[0].offset,
            "the roughing rung reuses the MEASURED first pass's offset",
        )
        # It is the roughing rung's own depth that is cited: 0.378 of cut, not
        # the skin's 0.69 and not the through pass's 0.756.
        self.assertTrue(
            any(f"{post.stock_top_z - rough.z_cut:g} deep" in v.message for v in problems),
            [str(v) for v in problems],
        )
        self.assertNotIn("Z0.06", text, "and the onion skin is still gone")

    def test_a_0_375_neighbour_on_the_entry_side_is_still_refused(self):
        """The reach that survives: the ramp where it breaks the surface.

        The lead-in descends from the ramp plane to the cut depth at the post's
        measured 2:1 ratio while closing the 0.05 lateral lead, so at the top
        of the stock it still stands ``lateral_lead * (1 - f)`` outside the
        profile, where ``f`` is how far down the ramp the stock top is.  That
        is 0.0188, and the swept cut therefore reaches 0.3938 — into a
        neighbour 0.375 away, 0.756 deep.
        """
        text, post = self.sheet(0.375, side_by_side=True)
        problems = [v for v in verify(text, post) if v.code == "foreign-cut"]
        self.assertTrue(problems, [str(v) for v in verify(text, post)])

        through = post.perimeter_passes[-1]
        radius = post.tool("perimeter").radius
        buried = (post.approach_z - post.stock_top_z) / (
            post.approach_z - through.z_cut
        )
        reach = through.offset + through.lateral_lead * (1.0 - buried) + radius
        self.assertAlmostEqual(reach, 0.3938, places=4)
        self.assertLess(0.375, reach, "which is why 0.375 is refused here")

        text, post = self.sheet(0.394, side_by_side=True)
        self.assertEqual(
            [str(v) for v in verify(text, post)],
            [],
            "and a gap just past that reach is not",
        )

    def test_the_part_gap_floor_clears_both_reaches(self):
        """The floor is where the part gap is enforced now (class docstring)."""
        from faceframe_cnc.nesting import MIN_PART_GAP

        self.assertEqual(MIN_PART_GAP, 0.455)
        self.assertEqual(NestingConfig().part_gap, MIN_PART_GAP)
        post = post_config_for(NestingConfig())
        radius = post.tool("perimeter").radius
        # EVERY rung of the ladder, not just the through pass: the 2026-08-05
        # max-bite amendment added one at a wider offset, and the floor has to
        # clear the widest reach any configured pass makes.
        self.assertGreater(len(post.perimeter_passes), 1, "there is a ladder to walk")
        for position, spec in enumerate(post.perimeter_passes):
            with self.subTest(perimeter_pass=position + 1):
                self.assertLess(
                    spec.offset + spec.lateral_lead + radius, MIN_PART_GAP
                )

    def test_a_swept_edge_that_exactly_touches_is_not_a_false_refusal(self):
        """Where the exact-touch case comes from, and that it still passes.

        The through pass's sweep reaching exactly 0.375 past the part edge is
        also exactly :attr:`NestingConfig.inner_clearance`, so EVERY nested
        frame the optimizer places has a swept edge landing precisely on its
        host's opening line.  If exact contact counted as an overlap, every
        nested sheet would become unbuildable — so ``R720101N``, the nested
        reference sheet, is regenerated and verified here as the end-to-end
        statement of it.
        """
        from faceframe_cnc.post.reconstruct import reconstruct

        post = default_config()
        radius = post.tool("perimeter").radius
        self.assertAlmostEqual(
            post.perimeter_passes[-1].offset + radius,
            NestingConfig().inner_clearance,
            places=9,
        )
        path = os.path.join(
            os.path.dirname(__file__), "..", "reference", "nc_files", "R720101N.anc"
        )
        program, plan = reconstruct(path)
        self.assertTrue(any(host.children for host in program.parts))
        self.assertEqual([str(v) for v in verify(generate(program, plan))], [])


class EveryStileGrooveStaysInsideItsPartTest(unittest.TestCase):
    """Spec §6: for every frame family that HAS stile grooves.

    A WDC has none (T17 instead), so the families here are the three the
    geometry engine lays out: wall, base and three-drawer, upright and
    rotated, at sizes the shop actually orders.
    """

    SIZES = ((18.0, 30.0), (24.0, 36.0), (30.0, 33.0), (33.0, 30.0), (48.0, 30.0))
    PARTS = ("W3030", "B30", "3DB30", "BBC2436")

    def test_the_families_under_test_really_do_have_stile_grooves(self):
        for part_number in self.PARTS:
            with self.subTest(part=part_number):
                self.assertIsNot(infer_frame_type(part_number), FrameType.WDC)
                self.assertEqual(panel_groove_indices(part_number), (0, 2, 1, 3))

    def test_no_groove_sweep_leaves_its_part(self):
        config = default_config()
        radius = config.tool(SECTION_PANEL).radius
        for part_number in self.PARTS:
            for width, height in self.SIZES:
                for rotated in (False, True):
                    box = Box.from_size(
                        1.0,
                        1.0,
                        height if rotated else width,
                        width if rotated else height,
                    )
                    part = _bare_part(part_number, box, rotated)
                    for index in range(4):
                        start, end = groove_segment(part, index, config.panel, radius)
                        swept = Box(
                            min(start[0], end[0]) - radius,
                            min(start[1], end[1]) - radius,
                            max(start[0], end[0]) + radius,
                            max(start[1], end[1]) + radius,
                        )
                        with self.subTest(
                            part=part_number,
                            size=(width, height),
                            rotated=rotated,
                            groove=index,
                        ):
                            self.assertTrue(
                                box.contains(swept, 1e-9),
                                f"groove {index} sweeps {swept} outside {box}",
                            )

    def test_a_stile_groove_sweep_is_exactly_as_long_as_its_part(self):
        """Clamped flush, not inset: the groove still runs out through both
        rail ends (that is what the shop's joint needs), it just stops at the
        part edge instead of 0.69 past it."""
        config = default_config()
        radius = config.tool(SECTION_PANEL).radius
        for rotated in (False, True):
            box = Box.from_size(2.0, 3.0, 30.0 if not rotated else 33.0,
                                33.0 if not rotated else 30.0)
            part = _bare_part("W3033", box, rotated)
            for index in (0, 2):
                start, end = groove_segment(part, index, config.panel, radius)
                with self.subTest(rotated=rotated, groove=index):
                    if rotated:
                        self.assertAlmostEqual(start[0] - radius, box.x0, places=9)
                        self.assertAlmostEqual(end[0] + radius, box.x1, places=9)
                    else:
                        self.assertAlmostEqual(start[1] - radius, box.y0, places=9)
                        self.assertAlmostEqual(end[1] + radius, box.y1, places=9)


def _bare_part(part_number: str, box: Box, rotated: bool):
    from faceframe_cnc.post.model import PartProgram

    return PartProgram(part_number=part_number, box=box, rotated=rotated)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
