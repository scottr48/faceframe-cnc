"""Milestone 2b: the holding tabs — where they go, and how a pass forms one.

The 2026-08-05 amendment (Scott, job R0805,
``CLAUDE_CODE_PROMPT_Tabs_and_Groove_Clamp.md`` §3a/§3b) after two frames broke
during the perimeter cut.  Two halves, tested separately here because they are
two different kinds of claim:

*   **placement** (§3a) — :mod:`faceframe_cnc.post.tabs`, pure geometry.  Counts
    and spacing per side length, the ≥2" corner clearance with the ramps
    counted, tabs kept clear of the lead-in span (with a case built to prove
    the relocation is load-bearing rather than decorative), determinism, and
    the fallback chain for sides too short for Scott's numbers;
*   **formation** (§3b) — the emitter's lift, judged on the MOTION STREAM and
    on the text, because a tab is not a new kind of cut: it is the loop's own
    ramp grammar used twice, and the machine has to read it as the dialect it
    already accepts.

What is deliberately NOT here (milestone 3): wiring tabs into the plans
:mod:`~faceframe_cnc.post.from_layout` builds, the T12 release section, and the
verifier's hold invariant.  Tabs are opt-in data no production caller sets yet,
so this module builds its own plans — and the last class states the other half
of that: a plan with no tabs emits exactly the bytes it emitted before.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import ast
import hashlib
import re
import dataclasses
import unittest
from dataclasses import replace

from faceframe_cnc.geometry import compute_geometry
from faceframe_cnc.post import tabs
from faceframe_cnc.post.from_layout import plan_sheet, post_config_for
from faceframe_cnc.post.generator import (
    emit,
    entry_side_for,
    generate,
    loop_points,
    loop_spans,
)
from faceframe_cnc.post.job import dry_run_config
from faceframe_cnc.post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_WDC_SLOT,
    SIDES,
    Box,
    CutPlan,
    FeatureRef,
    PartProgram,
    ProgramHeader,
    SheetProgram,
    TabSpec,
    default_config,
)
from faceframe_cnc.post.tabs import TabZone
from faceframe_cnc.post.verifier import expected_work, verify
from tests.test_r0805_regression import CREATED, r0805_layout

TOL = 1e-9


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def profile_tabs(program, plan, config):
    """The ``{profile: zones}`` mapping for a sheet — milestone 3's job, here.

    Placement needs the side the passes lead in on, and there is one answer per
    profile, so this insists every pass that will lift agrees on it — exactly
    the discipline milestone 3 has to keep when it puts this in
    :mod:`~faceframe_cnc.post.from_layout` (the emitter refuses the alternative,
    see :class:`RefusalTest`).
    """
    parts = program.flat_parts()
    out = {}
    for ref in plan.openings:
        opening = parts[ref.part].openings[ref.index]
        cuts = tabs.opening_cuts(config)
        out[ref.profile] = tabs.place_tabs(
            opening, agreed_entry(opening, "opening", cuts, config, ref), cuts, config
        )
    for ref in plan.perimeter[-1]:
        box = parts[ref.part].box
        cuts = tabs.perimeter_cuts(config)
        out[ref.profile] = tabs.place_tabs(
            box, agreed_entry(box, "perimeter", cuts, config, ref), cuts, config
        )
    return out


def agreed_entry(box, kind, cuts, config, ref=None):
    """The one entry side every lifting pass over ``box`` chooses."""
    # an air-cut table has no lifting pass at all, and then any side will do:
    # placement returns no zones either way
    pool = tabs.lifting_cuts(cuts, config) or tuple(cuts)
    sides = {
        entry_side_for(
            box.grow(spec.offset),
            kind,
            tool,
            spec,
            config,
            override=None if ref is None else ref.entry,
        )
        for spec, tool in pool
    }
    if len(sides) != 1:
        raise AssertionError(f"the passes over this {kind} disagree: {sides}")
    return sides.pop()


def tabbed_r0805(config=None):
    """``(program, plan, config, text)`` for the R0805 sheet, tab-held.

    The real shop sheet (``tests/test_r0805_regression``) rather than an
    invented one, so the numbers under test are the sizes the machine cuts.
    ``config`` defaults to the generated post table — one perimeter pass — and
    :func:`~faceframe_cnc.post.model.default_config` is passed in where the
    reference two-pass dialect is what is being asserted.
    """
    layout, specs, nesting = r0805_layout()
    post = config or post_config_for(nesting)
    program, plan = plan_sheet(
        layout, ProgramHeader(name="R080501N", created=CREATED), specs, nesting, post
    )
    plan.tabs = profile_tabs(program, plan, post)
    return program, plan, post, generate(program, plan, post)


def by_side(zones):
    return {side: [z for z in zones if z.side == side] for side in SIDES}


def travel_order(zones, entry):
    """``zones`` in the order the tool meets them, leading in on ``entry``.

    Written out here rather than asked of the module under test: the loop leaves
    the middle of its entry side going one way, comes round the other three
    sides counter-clockwise, and arrives back at the entry side's near half.
    """
    groups = by_side(zones)
    first = SIDES.index(entry)
    ordered = [z for z in sorted(groups[entry], key=lambda z: z.centre) if z.centre > 0]
    for step in (1, 2, 3):
        ordered += sorted(groups[SIDES[(first + step) % 4]], key=lambda z: z.centre)
    ordered += [
        z for z in sorted(groups[entry], key=lambda z: z.centre) if z.centre < 0
    ]
    return ordered


def worst_ramp(cuts, config):
    return max(
        tabs.tab_ramp(spec.z_cut, config)
        for spec, _ in tabs.lifting_cuts(cuts, config)
    )


def free_runs(zones, length, ramp, exclusion=None):
    """The unsupported runs of cut along one side, corners included.

    A run that contains the lead-in/lead-out span is dropped: that span is
    ~8.4" of cut on the entry side that no tab may sit in (spec §3a), so the
    gap it creates is not something placement can do anything about, and
    pretending otherwise would be testing the impossible.
    """
    spans = sorted(zone.span(ramp) for zone in zones)
    edges = [(-length / 2.0, -length / 2.0), *spans, (length / 2.0, length / 2.0)]
    runs = []
    for (_, run_start), (run_end, _) in zip(edges, edges[1:]):
        if (
            exclusion is not None
            and run_start < exclusion[1]
            and exclusion[0] < run_end
        ):
            continue
        runs.append(run_end - run_start)
    return runs


def lifts(motions, config):
    """``[(climb, traverse, descent)]`` — every tab lift in a motion stream.

    Found by shape, not by asking the emitter: a move that rises to the tab
    top, a move that stays there, and a move that goes back down.
    """
    top = config.tabs.top_z
    found = []
    for climb, traverse, descent in zip(motions, motions[1:], motions[2:]):
        at_top = (
            climb.to_z is not None
            and abs(climb.to_z - top) < TOL
            and climb.from_z is not None
            and climb.from_z < top - TOL
        )
        stays = (
            traverse.from_z is not None
            and abs(traverse.from_z - top) < TOL
            and abs(traverse.to_z - top) < TOL
        )
        drops = descent.to_z is not None and descent.to_z < top - TOL
        if at_top and stays and drops:
            found.append((climb, traverse, descent))
    return found


def travel(motion, side, box):
    """``(from, to)`` travel offsets of a move along ``side`` of ``box``."""
    return (
        tabs.travel_offset(box, side, (motion.from_x, motion.from_y)),
        tabs.travel_offset(box, side, (motion.to_x, motion.to_y)),
    )


def length_of(motion):
    return abs(motion.to_x - motion.from_x) + abs(motion.to_y - motion.from_y)


def perpendicular(motion, side, box):
    """``(where the side is, where the move is)`` across ``side``."""
    if side == "bottom":
        return (box.y0, motion.to_y)
    if side == "top":
        return (box.y1, motion.to_y)
    if side == "right":
        return (box.x1, motion.to_x)
    return (box.x0, motion.to_x)


def side_of(motion, box):
    """Which side of ``box`` a move runs along — and it must run along one."""
    for side in SIDES:
        constant, value = perpendicular(motion, side, box)
        moves = abs(
            tabs.travel_offset(box, side, (motion.to_x, motion.to_y))
            - tabs.travel_offset(box, side, (motion.from_x, motion.from_y))
        )
        if abs(constant - value) < TOL and moves > TOL:
            return side
    raise AssertionError(f"{motion} runs along no side of {box}")


# --------------------------------------------------------------------------
# the ratified numbers
# --------------------------------------------------------------------------


class RatifiedNumbersTest(unittest.TestCase):
    """Scott's four numbers, and the measured ones they are built on.

    The tab table is the one thing in ``post.model`` that is policy rather than
    measurement (2026-08-05, job R0805), so it is pinned here as policy: the
    values Scott gave, and the fact that everything derived from them comes out
    of the MEASURED ramp ratio rather than a second constant.
    """

    def setUp(self):
        self.config = default_config()

    def test_the_tab_table_is_the_ratified_one(self):
        spec = self.config.tabs
        self.assertEqual(spec, TabSpec())
        self.assertEqual(spec.top_z, 0.25, "0.25 of material left standing")
        self.assertEqual(spec.length, 0.75, "full height along the path")
        self.assertEqual(spec.corner_clearance, 2.0, "spec §3a: >= 2 from corners")
        self.assertTrue(
            8.0 <= spec.max_gap <= 10.0, "spec §3: target 8-10 between tabs"
        )

    def test_the_ramp_is_the_posts_own_measured_ratio(self):
        """No new geometry: 2 of travel per 1 of Z, as every lead-in ramp in
        the reference files does (R710101N 112/167/222)."""
        self.assertEqual(self.config.ramp_ratio, 2.0)
        through = self.config.perimeter_passes[-1]
        self.assertAlmostEqual(tabs.tab_ramp(through.z_cut, self.config), 0.512, 9)
        self.assertAlmostEqual(
            tabs.tab_footprint(through.z_cut, self.config), 1.774, 9
        )
        # spec §3: "≈0.51 per ramp, ≈1.77 total footprint per tab"
        self.assertAlmostEqual(
            tabs.tab_footprint(self.config.openings_passes[-1].z_cut, self.config),
            1.15,
            9,
        )

    def test_only_the_passes_that_cut_below_the_tab_top_lift(self):
        """Spec §3b's table, read off the post table itself."""
        lifting = {
            "panel groove": self.config.panel.z_cut,
            "T17 slot pass 1": self.config.wdc_slot.z_cuts[0],
            "T17 slot pass 2": self.config.wdc_slot.z_cuts[1],
            "openings": self.config.openings_passes[-1].z_cut,
            "detail": self.config.detail_pass.z_cut,
            "perimeter pass 1": self.config.perimeter_passes[0].z_cut,
            "perimeter pass 2": self.config.perimeter_passes[1].z_cut,
        }
        self.assertEqual(
            {
                what: tabs.lifts_over_tabs(z, self.config)
                for what, z in lifting.items()
            },
            {
                "panel groove": False,
                "T17 slot pass 1": False,
                "T17 slot pass 2": False,
                "openings": True,
                "detail": True,
                "perimeter pass 1": True,
                "perimeter pass 2": True,
            },
        )

    def test_a_groove_crossing_a_tab_needs_no_special_handling(self):
        """Spec §3a's last bullet, as arithmetic: both shallow floors are
        ABOVE the tab top, so a groove or a V slot passing over a tab zone
        removes nothing from the tab."""
        self.assertGreater(self.config.panel.z_cut, self.config.tabs.top_z)
        self.assertGreater(min(self.config.wdc_slot.z_cuts), self.config.tabs.top_z)
        self.assertAlmostEqual(
            self.config.panel.z_cut - self.config.tabs.top_z, 0.30, 9
        )

    def test_an_air_cut_places_no_tabs_and_lifts_over_none(self):
        """Every depth in a dry-run table is mirrored above the stock, so no
        pass cuts below the tab top and there is nothing to hold."""
        dry = dry_run_config(self.config)
        self.assertEqual(tabs.lifting_cuts(tabs.perimeter_cuts(dry), dry), ())
        self.assertEqual(
            tabs.place_tabs(
                Box.from_size(1.0, 1.0, 30.0, 33.0),
                "right",
                tabs.perimeter_cuts(dry),
                dry,
            ),
            (),
        )


# --------------------------------------------------------------------------
# placement (spec §3a)
# --------------------------------------------------------------------------


class PlacementTest(unittest.TestCase):
    """Counts, spacing, clearance and symmetry, per side length."""

    def setUp(self):
        self.config = default_config()
        self.perimeter = tabs.perimeter_cuts(self.config)
        self.openings = tabs.opening_cuts(self.config)

    def place(self, width, height, entry="right", cuts=None):
        return tabs.place_tabs(
            Box.from_size(1.0, 1.0, width, height),
            entry,
            cuts or self.perimeter,
            self.config,
        )

    def test_two_tabs_on_a_fourteen_inch_side(self):
        """Spec §6: "2 on 14"".  The WDC frame's opening is 14 x 33, so its two
        short sides are the real case."""
        sides = by_side(self.place(14.0, 33.0, cuts=self.openings))
        self.assertEqual(len(sides["bottom"]), 2)
        self.assertEqual(len(sides["top"]), 2)

    def test_three_or_four_tabs_on_a_thirty_to_thirty_three_inch_side(self):
        """Spec §6: "3-4 on 30-33"" — for the perimeter and the opening tool
        alike, and whether or not the side is the one led in on."""
        for length in (30.0, 33.0):
            for cuts in (self.perimeter, self.openings):
                sides = by_side(self.place(length, length, cuts=cuts))
                for side in SIDES:
                    with self.subTest(length=length, side=side, cuts=len(cuts)):
                        self.assertIn(len(sides[side]), (3, 4))

    def test_scotts_count_guidance_holds_on_every_side_the_shop_cuts(self):
        """"roughly 2 on a short side, 3-4 on a long side" (spec §3), stated as
        the table this placement actually produces."""
        expected = {14.0: 2, 18.0: 3, 27.0: 3, 30.0: 4, 33.0: 4, 36.0: 4}
        for length, count in expected.items():
            with self.subTest(length=length):
                # the side away from the lead-in: one interval, so this is the
                # count the spacing rule asks for on its own
                sides = by_side(self.place(20.0, length, entry="right"))
                self.assertEqual(len(sides["left"]), count)

    def test_no_unsupported_run_is_longer_than_the_target(self):
        """The spacing rule's own promise, measured between tab FOOTPRINTS."""
        ramp = worst_ramp(self.perimeter, self.config)
        exclusion = tabs.entry_exclusion(
            tabs.lifting_cuts(self.perimeter, self.config), self.config
        )
        for length in (8.0, 12.0, 14.0, 18.0, 24.0, 27.0, 30.0, 33.0, 36.0, 48.0):
            zones = by_side(self.place(length, length))
            for side in SIDES:
                runs = free_runs(
                    zones[side],
                    length,
                    ramp,
                    exclusion if side == "right" else None,
                )
                with self.subTest(length=length, side=side):
                    # an 8" entry side has no run to measure: the lead-in span
                    # covers it (:class:`FallbackChainTest`)
                    self.assertLessEqual(
                        max(runs, default=0.0), self.config.tabs.max_gap + TOL, runs
                    )

    def test_every_tab_including_its_ramps_clears_the_corners(self):
        """Spec §3a: >= 2" from the corners, ramps counted — at the deepest
        pass's footprint, which is the longest one.

        The entry side is held at 33" here so that all four sides are placed by
        the normal rule; the sides short enough to spend their clearance are
        :class:`FallbackChainTest`'s subject.
        """
        ramp = worst_ramp(self.perimeter, self.config)
        for length in (8.0, 14.0, 24.0, 33.0, 48.0):
            zones = by_side(self.place(length, 33.0))
            for side in SIDES:
                half = (length if side in ("bottom", "top") else 33.0) / 2.0
                for zone in zones[side]:
                    low, high = zone.span(ramp)
                    with self.subTest(length=length, side=side, centre=zone.centre):
                        self.assertGreaterEqual(
                            low + half, self.config.tabs.corner_clearance - TOL
                        )
                        self.assertGreaterEqual(
                            half - high, self.config.tabs.corner_clearance - TOL
                        )

    def test_a_side_away_from_the_lead_in_is_symmetric_about_its_midpoint(self):
        for length in (14.0, 24.0, 30.0, 33.0, 36.0):
            zones = by_side(self.place(length, length))
            for side in ("bottom", "top", "left"):
                centres = sorted(zone.centre for zone in zones[side])
                with self.subTest(length=length, side=side):
                    self.assertEqual(
                        [round(c, 9) for c in centres],
                        [round(-c, 9) for c in reversed(centres)],
                    )

    def test_no_zone_crosses_a_corner(self):
        ramp = worst_ramp(self.perimeter, self.config)
        for width, height in ((14.0, 33.0), (30.0, 33.0), (8.0, 48.0)):
            zones = tabs.place_tabs(
                Box.from_size(1.0, 1.0, width, height),
                "right",
                self.perimeter,
                self.config,
            )
            for zone in zones:
                low, high = zone.span(ramp)
                half = tabs.side_length(
                    Box.from_size(1.0, 1.0, width, height), zone.side
                ) / 2.0
                with self.subTest(size=(width, height), zone=zone):
                    self.assertGreater(low, -half)
                    self.assertLess(high, half)

    def test_placement_is_deterministic(self):
        """Project ethos, and the reason there is no wall clock or randomness
        anywhere in the module: the same profile always gets the same tabs."""
        first = self.place(30.0, 33.0)
        second = self.place(30.0, 33.0)
        self.assertEqual(first, second)
        digests = {
            hashlib.sha256(repr(self.place(30.0, 33.0)).encode()).hexdigest()
            for _ in range(3)
        }
        self.assertEqual(len(digests), 1)

    def test_the_zones_come_back_in_a_fixed_order(self):
        zones = self.place(30.0, 33.0)
        self.assertEqual(
            [zone.side for zone in zones],
            [side for side in SIDES for _ in by_side(zones)[side]],
        )
        for side, group in by_side(zones).items():
            with self.subTest(side=side):
                self.assertEqual(
                    [zone.centre for zone in group],
                    sorted(zone.centre for zone in group),
                )

    def test_a_zone_carries_no_coordinate(self):
        """A tab is a position on a profile, not a place on the sheet: the same
        frame nested anywhere gets the same zones."""
        here = tabs.place_tabs(
            Box.from_size(1.0, 1.0, 30.0, 33.0), "right", self.perimeter, self.config
        )
        there = tabs.place_tabs(
            Box.from_size(17.5, 60.25, 30.0, 33.0),
            "right",
            self.perimeter,
            self.config,
        )
        self.assertEqual(here, there)


class LeadInTest(unittest.TestCase):
    """Tabs stay clear of the lead-in / lead-out span (spec §3a).

    The span is derived, never written down: spec §3a says "perimeter lead-in
    ramps are ~4" long" and that ~4 is
    ``(approach_z - z_cut) * ramp_ratio`` out of the post's own measured table,
    which is what :func:`~faceframe_cnc.post.tabs.entry_exclusion` computes.
    """

    def setUp(self):
        self.config = default_config()
        self.perimeter = tabs.lifting_cuts(
            tabs.perimeter_cuts(self.config), self.config
        )
        self.exclusion = tabs.entry_exclusion(self.perimeter, self.config)

    def test_the_exclusion_is_the_loops_own_geometry(self):
        through = self.config.perimeter_passes[-1]
        ramp = (self.config.approach_z - through.z_cut) * self.config.ramp_ratio
        self.assertAlmostEqual(ramp, 4.012, 9, "spec §3a's ~4 inch lead-in")
        self.assertAlmostEqual(self.exclusion[0], -ramp, 9)
        self.assertAlmostEqual(
            self.exclusion[1],
            self.config.tool(SECTION_PERIMETER).diameter + ramp,
            9,
            "the overshoot is one tool diameter, then the lead-out ramp",
        )

    def test_it_matches_the_motion_the_emitter_really_writes(self):
        """Belt and braces: the same span measured off :func:`loop_points`."""
        box = Box.from_size(1.0, 1.0, 30.0, 33.0)
        through = self.config.perimeter_passes[-1]
        points = loop_points(
            box, "right", self.config.tool(SECTION_PERIMETER), through, self.config
        )
        offsets = [
            tabs.travel_offset(box, "right", point)
            for point in (points[0], points[7], points[8])
        ]
        self.assertAlmostEqual(min(offsets), self.exclusion[0], 9)
        self.assertAlmostEqual(max(offsets), self.exclusion[1], 9)

    def naive(self, length):
        """Where an even spread over the whole side WOULD put the tabs.

        The module's own spacing rule with the lead-in exclusion left out — the
        placement this amendment had to fix.
        """
        footprint = tabs.worst_footprint(self.perimeter, self.config)
        reach = footprint / 2.0
        limit = length / 2.0 - self.config.tabs.corner_clearance - reach
        return tabs._spread(-limit, limit, footprint, self.config.tabs.max_gap)

    def test_a_naive_symmetric_group_would_straddle_the_lead_in(self):
        """The case has to be real for the relocation to mean anything.

        A 24" entry side takes three tabs by the spacing rule, and the middle
        one of a symmetric three sits exactly on the side's midpoint — which is
        the lead-in point itself.
        """
        centres = self.naive(24.0)
        self.assertEqual(len(centres), 3)
        self.assertAlmostEqual(centres[1], 0.0, 9)
        ramp = worst_ramp(self.perimeter, self.config)
        straddling = TabZone("right", centres[1], self.config.tabs.length)
        low, high = straddling.span(ramp)
        self.assertLess(low, self.exclusion[1])
        self.assertGreater(high, self.exclusion[0])

    def test_the_tab_is_relocated_and_never_shrunk(self):
        zones = by_side(
            tabs.place_tabs(
                Box.from_size(1.0, 1.0, 10.0, 24.0),
                "right",
                tabs.perimeter_cuts(self.config),
                self.config,
            )
        )["right"]
        ramp = worst_ramp(self.perimeter, self.config)
        self.assertGreaterEqual(len(zones), 2, "spec §3: minimum 2 per side")
        for zone in zones:
            with self.subTest(zone=zone):
                self.assertEqual(
                    zone.length, self.config.tabs.length, "never shrunk"
                )
                low, high = zone.span(ramp)
                # touching the end of the span is clear of it -- placement puts
                # the relocated tab's ramp foot exactly there, and the lead-in
                # ramp does not even break the surface until 1.512 later
                self.assertFalse(
                    low < self.exclusion[1] - TOL and self.exclusion[0] < high - TOL,
                    f"{zone} overlaps the lead-in span {self.exclusion}",
                )

    def test_no_zone_fouls_any_lifting_passs_lead_in(self):
        """Placement clears the WORST pass; every shallower one is then clear
        too, which is what :func:`~faceframe_cnc.post.tabs.entry_conflict`
        checks per pass at emission time."""
        for cuts in (tabs.perimeter_cuts(self.config), tabs.opening_cuts(self.config)):
            for length in (14.0, 24.0, 30.0, 33.0, 36.0):
                box = Box.from_size(1.0, 1.0, 20.0, length)
                zones = tabs.place_tabs(box, "right", cuts, self.config)
                for spec, tool in tabs.lifting_cuts(cuts, self.config):
                    with self.subTest(length=length, z=spec.z_cut):
                        self.assertIsNone(
                            tabs.entry_conflict(
                                zones,
                                "right",
                                tabs.entry_exclusion(((spec, tool),), self.config),
                                tabs.tab_ramp(spec.z_cut, self.config),
                            )
                        )

    def test_only_the_entry_side_loses_ground_to_it(self):
        """The other three sides are placed as if there were no lead-in, which
        is why they stay symmetric."""
        box = Box.from_size(1.0, 1.0, 24.0, 24.0)
        cuts = tabs.perimeter_cuts(self.config)
        for entry in SIDES:
            zones = by_side(tabs.place_tabs(box, entry, cuts, self.config))
            with self.subTest(entry=entry):
                self.assertNotIn(
                    0.0, [round(z.centre, 9) for z in zones[entry]]
                )
                for side in SIDES:
                    if side != entry:
                        self.assertIn(
                            0.0, [round(z.centre, 9) for z in zones[side]]
                        )


class FallbackChainTest(unittest.TestCase):
    """Sides too short for Scott's numbers.

    Steps 3 and 4 of the chain in the module docstring (reduce the corner
    clearance for one centred tab; then give the side up entirely) are this
    milestone's PROPOSAL, not a ratified decision — flagged for review with the
    milestone.  Steps 1 and 2 (fewer tabs, then one) follow from Scott's own
    "a side too short to fit two gets one, centered".
    """

    def setUp(self):
        self.config = default_config()
        self.cuts = tabs.perimeter_cuts(self.config)
        self.footprint = tabs.worst_footprint(
            tabs.lifting_cuts(self.cuts, self.config), self.config
        )
        self.ramp = worst_ramp(self.cuts, self.config)

    def side(self, length):
        return by_side(
            tabs.place_tabs(
                Box.from_size(1.0, 1.0, 20.0, length), "bottom", self.cuts, self.config
            )
        )["left"]

    def test_the_boundaries_of_the_chain_are_the_footprint_and_the_clearance(self):
        clearance = self.config.tabs.corner_clearance
        self.assertAlmostEqual(self.footprint, 1.774, 9)
        self.assertAlmostEqual(2 * clearance + self.footprint, 5.774, 9)
        self.assertAlmostEqual(2 * clearance + 2 * self.footprint, 7.548, 9)

    def test_a_side_too_short_for_two_gets_one_centred(self):
        """Scott's own fallback: 6" leaves 0.24" of play once both clearances
        and one footprint are taken out — nowhere near a second tab."""
        zones = self.side(6.0)
        self.assertEqual(len(zones), 1)
        self.assertAlmostEqual(zones[0].centre, 0.0, 9)
        low, high = zones[0].span(self.ramp)
        self.assertGreaterEqual(
            3.0 + low, self.config.tabs.corner_clearance - TOL, "clearance kept"
        )
        self.assertGreaterEqual(3.0 - high, self.config.tabs.corner_clearance - TOL)

    def test_two_still_fit_just_above_that(self):
        self.assertEqual(len(self.side(7.6)), 2)

    def test_the_clearance_gives_way_before_the_tab_does(self):
        """Step 3: a 4" side cannot hold a tab 2" from both corners, so it holds
        one centred with the clearance reduced symmetrically — the tab is the
        thing that holds the part, the clearance is a preference."""
        zones = self.side(4.0)
        self.assertEqual(len(zones), 1)
        self.assertAlmostEqual(zones[0].centre, 0.0, 9)
        low, high = zones[0].span(self.ramp)
        self.assertAlmostEqual(2.0 + low, (4.0 - self.footprint) / 2.0, 9)
        self.assertLess(2.0 + low, self.config.tabs.corner_clearance)
        self.assertGreater(2.0 + low, 0.0, "the footprint still fits on the side")

    def test_a_side_shorter_than_one_tab_gets_none(self):
        """Step 4: nothing can be held there, and the neighbours do it."""
        self.assertEqual(self.side(1.5), [])
        self.assertEqual(
            self.side(self.footprint), [], "a tab that exactly fills the side is not one"
        )
        every = tabs.place_tabs(
            Box.from_size(1.0, 1.0, 20.0, 1.5), "bottom", self.cuts, self.config
        )
        self.assertEqual([z.side for z in every].count("left"), 0)
        self.assertTrue(
            [z for z in every if z.side == "bottom"], "the long sides still hold it"
        )

    def test_an_openings_zone_fits_the_shorter_t11_path_too(self):
        """A tab is placed on the finished profile but cut on each pass's own
        path, and an opening's T11 path is 0.395 shorter per side.  On a normal
        side the 2" clearance swallows that; on a tiny one it decides between a
        tab and none, and a zone that ran off the end of a pass's side would be
        a refusal at emission time (:class:`RefusalTest`) rather than a tab.
        """
        cuts = tabs.opening_cuts(self.config)
        inward = min(spec.offset for spec, _ in tabs.lifting_cuts(cuts, self.config))
        self.assertAlmostEqual(inward, -0.1975, 9)
        footprint = tabs.worst_footprint(
            tabs.lifting_cuts(cuts, self.config), self.config
        )
        for length, expected in ((footprint + 0.3, 0), (footprint + 0.6, 1)):
            zones = by_side(
                tabs.place_tabs(
                    Box.from_size(1.0, 1.0, 20.0, length), "bottom", cuts, self.config
                )
            )["left"]
            with self.subTest(length=round(length, 4)):
                self.assertEqual(len(zones), expected)
                for zone in zones:
                    low, high = zone.span(
                        max(
                            tabs.tab_ramp(spec.z_cut, self.config)
                            for spec, _ in tabs.lifting_cuts(cuts, self.config)
                        )
                    )
                    self.assertGreater(low, -(length + 2 * inward) / 2.0)
                    self.assertLess(high, (length + 2 * inward) / 2.0)

    def test_an_entry_side_the_lead_in_swallows_gets_none(self):
        """Also step 4: on a 4" side the lead-in and lead-out ramps cover the
        whole side, so the one tab it could hold has nowhere to stand."""
        zones = by_side(
            tabs.place_tabs(
                Box.from_size(1.0, 1.0, 4.0, 27.0), "bottom", self.cuts, self.config
            )
        )
        self.assertEqual(zones["bottom"], [])
        self.assertEqual(len(zones["top"]), 1, "the same side, not led in on")
        self.assertGreaterEqual(
            len(zones["left"]) + len(zones["right"]), 2, "held by its long sides"
        )


class RealGeometryTest(unittest.TestCase):
    """Placement on the profiles the geometry engine really produces."""

    def setUp(self):
        self.config = default_config()

    def zones_for(self, part_number, width, height, kind="opening", index=0):
        geom = compute_geometry(part_number, width, height)
        self.assertEqual(geom.errors, [])
        opening = geom.openings[index]
        box = Box.from_size(1.0, 1.0, opening.width, opening.height)
        cuts = tabs.opening_cuts(self.config)
        return box, by_side(
            tabs.place_tabs(
                box, agreed_entry(box, kind, cuts, self.config), cuts, self.config
            )
        )

    def test_the_wdc_opening(self):
        """14 x 33 (2026-08-03 amendment): 2 tabs on each 14" side, 4 on each
        33" one — Scott's counts exactly."""
        _, zones = self.zones_for("WDC2436", 18.0, 36.0)
        self.assertEqual(
            {side: len(group) for side, group in zones.items()},
            {"bottom": 2, "top": 2, "right": 4, "left": 4},
        )

    def test_a_small_drawer_opening(self):
        """A 3DB30's top drawer opening is 27 x 5, and 5" is inside the
        fallback chain: one centred tab per short side, with the corner
        clearance reduced (:class:`FallbackChainTest`), 3 per long side."""
        box, zones = self.zones_for("3DB30", 30.0, 30.0, index=0)
        self.assertAlmostEqual(box.width, 27.0, 9)
        self.assertAlmostEqual(box.height, 5.0, 9)
        self.assertEqual(len(zones["right"]), 1)
        self.assertEqual(len(zones["left"]), 1)
        self.assertEqual(len(zones["top"]), 3)
        self.assertGreaterEqual(len(zones["bottom"]), 2)

    def test_the_r0805_sheet(self):
        """Both parts of the job that broke, openings and perimeters."""
        program, plan, post, _ = tabbed_r0805()
        parts = program.flat_parts()
        counts = {}
        for key, zones in plan.tabs.items():
            part, kind, index = key
            counts[(parts[part].part_number, kind)] = len(zones)
            self.assertTrue(zones, key)
            for side in SIDES:
                self.assertTrue(
                    [z for z in zones if z.side == side],
                    f"{key} has no tab on its {side} side",
                )
        self.assertEqual(
            counts,
            {
                ("WDC2436", "opening"): 12,
                ("WDC2436", "perimeter"): 14,
                ("W3330", "opening"): 14,
                ("W3330", "perimeter"): 16,
            },
        )

    def test_every_opening_the_shop_orders_gets_at_least_two_tabs_a_side(self):
        """Nothing the engine produces falls through the chain to zero on more
        than a genuinely tiny side."""
        for part_number, width, height in (
            ("W3330", 33.0, 30.0),
            ("W3012", 30.0, 12.0),
            ("B30", 30.0, 30.0),
            ("3DB24", 24.0, 30.0),
            ("WDC2436", 18.0, 36.0),
        ):
            geom = compute_geometry(part_number, width, height)
            for index in range(len(geom.openings)):
                box, zones = self.zones_for(part_number, width, height, index=index)
                for side in SIDES:
                    with self.subTest(part=part_number, opening=index, side=side):
                        short = tabs.side_length(box, side) < 5.8
                        self.assertGreaterEqual(
                            len(zones[side]), 1 if short else 2, zones
                        )


# --------------------------------------------------------------------------
# formation (spec §3b)
# --------------------------------------------------------------------------


class FormationTest(unittest.TestCase):
    """Every pass below Z0.25 rises over every tab, and how.

    Judged on the motion stream and the rendered text, which is where the
    milestone stops: a tabbed program's cut ORDER and hold invariant are
    milestone 3's business, and the verifier is not weakened here to suit it.
    """

    def setUp(self):
        # the reference two-pass dialect, so the onion-skin pass is under test
        # too (spec §3b: it must lift, or it destroys the tab before pass 2)
        self.config = default_config()
        self.program, self.plan, self.post, self.text = tabbed_r0805(self.config)
        self.stream = emit(self.program, self.plan, self.post)
        self.parts = self.program.flat_parts()
        self.lines = self.text.split("\r\n")

    def pass_box(self, ref, spec):
        part = self.parts[ref.part]
        base = part.box if ref.kind == "perimeter" else part.openings[ref.index]
        return base.grow(spec.offset)

    def deep_passes(self):
        """``(section, pass_index, spec)`` for every pass that must lift.

        Only the passes that reach BELOW the tab top, because those are the only
        ones spec §3b is about.  Since the 2026-08-05 max-bite amendment both T11
        operations can be a LADDER, and a ladder's upper rungs are above the tabs
        — Z0.45 on an opening, Z0.372 on a perimeter — so they are excluded here
        by the post's own :func:`~faceframe_cnc.post.tabs.lifts_over_tabs` rather
        than by a list written out.  The pass index matches what the emitter tags
        its motions with: ``None`` for a single-rung ladder, the rung's number
        otherwise.
        """
        numbered = len(self.post.openings_passes) > 1
        for index, spec in enumerate(self.post.openings_passes):
            if tabs.lifts_over_tabs(spec.z_cut, self.post):
                yield SECTION_OPENINGS, (index if numbered else None), spec
        yield SECTION_DETAIL, None, self.post.detail_pass
        for index, spec in enumerate(self.post.perimeter_passes):
            if tabs.lifts_over_tabs(spec.z_cut, self.post):
                yield SECTION_PERIMETER, index, spec

    def deep_opening_pass(self):
        """``(pass index, spec)`` of the T11 opening rung that lifts."""
        for section, pass_index, spec in self.deep_passes():
            if section == SECTION_OPENINGS:
                return pass_index, spec
        raise AssertionError("no T11 opening pass reaches below the tab top")

    def lifts_of(self, section, pass_index, ref):
        motions = [
            m
            for m in self.stream.motions
            if m.section == section
            and m.pass_index == pass_index
            and m.feature == ref
        ]
        return lifts(motions, self.post)

    def test_every_deep_pass_lifts_over_every_zone(self):
        seen = 0
        for section, pass_index, spec in self.deep_passes():
            refs = (
                self.plan.openings
                if section == SECTION_OPENINGS
                else self.plan.detail_order()
                if section == SECTION_DETAIL
                else self.plan.perimeter[pass_index]
            )
            for ref in refs:
                found = self.lifts_of(section, pass_index, ref)
                with self.subTest(section=section, pass_index=pass_index, ref=ref):
                    self.assertEqual(len(found), len(self.plan.zones_for(ref)))
                seen += len(found)
        # Every opening's tabs are lifted over once per opening pass that reaches
        # below the tab top (the deep T11 rung, then T12) and every perimeter's
        # once per perimeter pass that does — which the post table answers, and
        # which is 2 and 1 for both dialects: a max-bite ladder's extra rung sits
        # above the tabs and so is not one of them.
        opening_lifts = len(
            tabs.lifting_cuts(tabs.opening_cuts(self.post), self.post)
        )
        perimeter_lifts = len(
            tabs.lifting_cuts(tabs.perimeter_cuts(self.post), self.post)
        )
        expected = sum(
            len(self.plan.zones_for(ref)) for ref in self.plan.openings
        ) * opening_lifts + sum(
            len(self.plan.zones_for(ref)) for ref in self.plan.perimeter[-1]
        ) * perimeter_lifts
        self.assertEqual(seen, expected)
        self.assertGreater(seen, 0)

    def test_the_ramp_geometry_is_the_posts_own_slope(self):
        for section, pass_index, spec in self.deep_passes():
            ramp = tabs.tab_ramp(spec.z_cut, self.post)
            for climb, traverse, descent in lifts(
                [
                    m
                    for m in self.stream.motions
                    if m.section == section and m.pass_index == pass_index
                ],
                self.post,
            ):
                with self.subTest(section=section, pass_index=pass_index):
                    self.assertAlmostEqual(climb.to_z, self.post.tabs.top_z, 9)
                    self.assertAlmostEqual(length_of(climb), ramp, 9)
                    self.assertAlmostEqual(
                        length_of(climb) / (climb.to_z - climb.from_z),
                        self.post.ramp_ratio,
                        9,
                    )
                    self.assertAlmostEqual(
                        length_of(traverse), self.post.tabs.length, 9
                    )
                    self.assertAlmostEqual(descent.to_z, spec.z_cut, 9)
                    self.assertAlmostEqual(length_of(descent), ramp, 9)
                    self.assertAlmostEqual(
                        length_of(descent) / (descent.from_z - descent.to_z),
                        self.post.ramp_ratio,
                        9,
                    )

    def test_each_lift_lands_on_its_zone_on_that_passs_own_path(self):
        """The mapping from a finished-profile zone to an offset path — and the
        order, which is the order the tool MEETS the tabs.

        The loop is counter-clockwise from the middle of its entry side, so the
        travel order is: the entry side's far half, the next three sides round,
        then the entry side's near half.  Getting that wrong on one side out of
        four is exactly the bug this asserts against.
        """
        for section, pass_index, spec in self.deep_passes():
            refs = (
                self.plan.openings
                if section == SECTION_OPENINGS
                else self.plan.detail_order()
                if section == SECTION_DETAIL
                else self.plan.perimeter[pass_index]
            )
            for ref in refs:
                box = self.pass_box(ref, spec)
                entry = agreed_entry(
                    self.parts[ref.part].box
                    if ref.kind == "perimeter"
                    else self.parts[ref.part].openings[ref.index],
                    ref.kind,
                    tabs.perimeter_cuts(self.post)
                    if ref.kind == "perimeter"
                    else tabs.opening_cuts(self.post),
                    self.post,
                    ref,
                )
                zones = travel_order(self.plan.zones_for(ref), entry)
                found = self.lifts_of(section, pass_index, ref)
                mapped = []
                for climb, traverse, descent in found:
                    side = side_of(traverse, box)
                    start, end = travel(traverse, side, box)
                    mapped.append((side, (start + end) / 2.0))
                    with self.subTest(section=section, ref=ref, side=side):
                        # the run at tab height IS the zone's full-height span
                        self.assertAlmostEqual(
                            end - start, self.post.tabs.length, 9
                        )
                        # and it sits on this pass's rectangle, not the profile
                        self.assertAlmostEqual(
                            *perpendicular(traverse, side, box), places=9
                        )
                self.assertEqual(
                    [(side, round(centre, 9)) for side, centre in mapped],
                    [(z.side, round(z.centre, 9)) for z in zones],
                )

    def test_the_t11_and_t12_kerfs_lift_at_the_same_profile_positions(self):
        """Spec §3b: "the same angular positions on the profile, so one tab
        block spans both kerfs" — the two passes run on rectangles 0.0975
        apart, so this is only true because a zone is profile-relative."""
        deep_index, deep_spec = self.deep_opening_pass()
        for ref in self.plan.openings:
            positions = []
            for section, pass_index, spec in (
                (SECTION_OPENINGS, deep_index, deep_spec),
                (SECTION_DETAIL, None, self.post.detail_pass),
            ):
                box = self.pass_box(ref, spec)
                here = []
                for _, traverse, _ in self.lifts_of(section, pass_index, ref):
                    side = side_of(traverse, box)
                    start, end = travel(traverse, side, box)
                    here.append((side, round((start + end) / 2.0, 9)))
                positions.append(sorted(here))
            with self.subTest(ref=ref):
                self.assertEqual(positions[0], positions[1])
                self.assertTrue(positions[0])
                self.assertNotAlmostEqual(
                    self.pass_box(ref, deep_spec).x0,
                    self.pass_box(ref, self.post.detail_pass).x0,
                    msg="the two kerfs really are different rectangles",
                )

    def test_the_shallow_sections_are_byte_identical(self):
        """T13 and T17 cut above the tab top, so a tabbed program's groove and
        slot sections are the untabbed program's, to the byte — including where
        a groove crosses a tab zone (spec §3a's last bullet)."""
        plain = generate(self.program, replace_tabs(self.plan, None), self.post)
        for marker in (SECTION_PANEL, SECTION_WDC_SLOT):
            head = self.post.tool(marker).header_comment
            with self.subTest(section=marker):
                self.assertEqual(
                    section_lines(plain, head), section_lines(self.text, head)
                )
        self.assertEqual(
            [], lifts([m for m in self.stream.motions
                       if m.section in (SECTION_PANEL, SECTION_WDC_SLOT)], self.post)
        )

    def test_a_groove_really_does_cross_a_tabbed_profile(self):
        """The claim in the bullet above is not vacuous: the W3330's stile
        grooves run the full length of the part, so they cross the perimeter
        profile's two ends 0.5625 in from each corner, 0.30 above the tab top.
        """
        part = self.parts[1]
        self.assertEqual(part.part_number, "W3330")
        self.assertTrue(part.rotated)
        line = part.box.y0 + self.post.panel.stile_inset
        self.assertLess(part.box.y0, line)
        self.assertLess(line, part.box.y1)
        self.assertFalse(tabs.lifts_over_tabs(self.post.panel.z_cut, self.post))

    def test_a_pass_at_or_above_the_tab_top_never_lifts(self):
        """Not just the shallow sections: a perimeter pass moved above 0.25
        stops lifting, which is the rule rather than a section list."""
        shallow = replace(
            self.post,
            perimeter_passes=(
                replace(self.post.perimeter_passes[-1], z_cut=0.3),
            ),
        )
        program, plan = plan_sheet(
            *self.r0805_args(), post_config=shallow
        )
        plan.tabs = {
            key: value
            for key, value in self.plan.tabs.items()
            if key[1] == "perimeter"
        }
        stream = emit(program, plan, shallow)
        self.assertEqual(
            lifts([m for m in stream.motions if m.section == SECTION_PERIMETER],
                  shallow),
            [],
        )
        self.assertNotIn("Z0.25", stream.text)

    def r0805_args(self):
        layout, specs, nesting = r0805_layout()
        return layout, ProgramHeader(name="R080501N", created=CREATED), specs, nesting

    def test_an_air_cut_of_a_tabbed_plan_lifts_over_nothing(self):
        dry = dry_run_config(self.post)
        program, plan = plan_sheet(*self.r0805_args(), post_config=dry)
        plan.tabs = profile_tabs(program, plan, dry)
        self.assertEqual(plan.tabs, {key: () for key in plan.tabs})
        self.assertNotIn("Z0.25", generate(program, plan, dry))

    # -- the text the machine reads ----------------------------------------

    def test_the_lift_lines_are_the_dialect_the_machine_already_accepts(self):
        """Four line forms, each one a form the reference files contain: an
        axis-only cut move, the lead-out ramp's ``X.. Z..``, and the lead-in
        ramp's ``X.. Z.. F..``."""
        axis = r"[XY]-?\d+(\.\d*)?"
        forms = (
            re.compile(rf"^{axis}( {axis})?$"),
            re.compile(rf"^{axis}( {axis})? Z-?\d+(\.\d*)?$"),
            re.compile(rf"^{axis}( {axis})? Z-?\d+(\.\d*)? F\d+(\.\d*)?$"),
            re.compile(rf"^{axis}( {axis})? F\d+(\.\d*)?$"),
        )
        for _, traverse, descent in lifts(self.stream.motions, self.post):
            for motion in (traverse, descent):
                line = self.lines[motion.line_index]
                with self.subTest(line=line):
                    self.assertTrue(
                        any(form.match(line) for form in forms), line
                    )

    def test_the_climb_keeps_the_modal_cut_feed_and_the_descent_states_entry(self):
        """The convention found in the loop this lift lives in: the lead-OUT
        ramp climbs with no F word at the modal cutting feed (R710101N 119) and
        the lead-IN ramp states the entry feed on the way down (line 112).  So
        no F value appears that the section did not already use."""
        for section, pass_index, spec in self.deep_passes():
            for climb, traverse, descent in lifts(
                [
                    m
                    for m in self.stream.motions
                    if m.section == section and m.pass_index == pass_index
                ],
                self.post,
            ):
                with self.subTest(section=section, pass_index=pass_index):
                    self.assertNotIn("F", self.lines[climb.line_index])
                    self.assertEqual(climb.feed, spec.cut_feed)
                    self.assertNotIn("F", self.lines[traverse.line_index])
                    self.assertEqual(traverse.feed, spec.cut_feed)
                    self.assertIn(f"F{fmt_feed(spec.entry_feed)}",
                                  self.lines[descent.line_index])
                    self.assertEqual(descent.feed, spec.entry_feed)
                    # and the cut feed is restated on the next at-depth move,
                    # because F is modal
                    after = self.lines[descent.line_index + 1]
                    self.assertIn(f"F{fmt_feed(spec.cut_feed)}", after)

    def test_no_feed_value_appears_that_an_untabbed_program_did_not_use(self):
        plain = generate(self.program, replace_tabs(self.plan, None), self.post)
        self.assertEqual(feed_words(self.text), feed_words(plain))

    def test_the_awkward_sizes_emit_and_verify_too(self):
        """The sheet the fallback chain is actually for: a 3DB30's 27x5 drawer
        opening (one relaxed tab on each 5" side) beside a W3012 whose 12" right
        edge is both its entry side and too short for the lead-in plus two
        clearances (one relaxed tab).  Both come out as legal, verified code.
        """
        from faceframe_cnc.nesting import NestingConfig, PartSpec, Placement, SheetLayout

        layout = SheetLayout(
            placements=[
                Placement("3DB30", 1.0, 1.0, 30.0, 30.0, False, []),
                Placement("W3012", 1.0, 32.0, 30.0, 12.0, False, []),
            ]
        )
        specs = [PartSpec("3DB30", 30.0, 30.0, 1), PartSpec("W3012", 30.0, 12.0, 1)]
        nesting = NestingConfig()
        post = post_config_for(nesting)
        program, plan = plan_sheet(
            layout, ProgramHeader(name="R990101N", created=CREATED), specs, nesting, post
        )
        plan.tabs = profile_tabs(program, plan, post)
        parts = program.flat_parts()
        drawer = plan.tabs[(0, "opening", 0)]
        self.assertEqual(len(by_side(drawer)["right"]), 1, "the 5\" side")
        short_entry = by_side(plan.tabs[(1, "perimeter", 0)])["right"]
        self.assertEqual(len(short_entry), 1, "the 12\" entry side")
        self.assertEqual(parts[1].part_number, "W3012")
        text = generate(program, plan, post)
        stream = emit(program, plan, post)
        self.assertEqual(
            len(lifts(stream.motions, post)),
            sum(len(z) for z in plan.tabs.values())
            + sum(len(plan.zones_for(ref)) for ref in plan.openings),
            "openings lift twice, perimeters once on a generated sheet",
        )
        self.assertEqual(
            [str(v) for v in verify(text, post, expected_work(layout, post))], []
        )

    def test_the_verifier_still_passes_a_tabbed_program(self):
        """Not a weakened verifier — an unweakened one, which has no rule the
        lift breaks: the lift stays inside the profile, above the pass depth,
        at feeds the tool already runs.  (The rules that will judge a tabbed
        program's HOLDING are milestone 3's.)"""
        layout, _, nesting = r0805_layout()
        post = post_config_for(nesting)
        program, plan = plan_sheet(
            layout, ProgramHeader(name="R080501N", created=CREATED), None, nesting, post
        )
        plan.tabs = profile_tabs(program, plan, post)
        text = generate(program, plan, post)
        self.assertEqual([str(v) for v in verify(text, post)], [])
        self.assertEqual(
            [str(v) for v in verify(text, post, expected_work(layout, post))], []
        )


class EveryEntrySideTest(unittest.TestCase):
    """The lift on all four entry sides, and on a rotated part.

    The R0805 sheet only ever leads in on the right edge, and a per-side bug in
    the travel bookkeeping would hide behind that: three of the six cut moves of
    a loop belong to sides the entry side decides, and two of them are the entry
    side split in half around the lead-in point.  So this drives the loop from
    each of the four edges in turn, with tabs on every side.
    """

    def setUp(self):
        self.post = post_config_for(None)
        self.box = Box.from_size(3.0, 4.0, 30.0, 33.0)

    def test_loop_spans_walk_the_whole_rectangle_once(self):
        """Every side, once, in counter-clockwise order from the entry side."""
        tool = self.post.tool(SECTION_PERIMETER)
        spec = self.post.perimeter_passes[-1]
        for entry in SIDES:
            points = loop_points(self.box, entry, tool, spec, self.post)
            spans = loop_spans(self.box, entry, points)
            first = SIDES.index(entry)
            with self.subTest(entry=entry):
                self.assertEqual(
                    [side for side, _, _ in spans],
                    [SIDES[(first + step) % 4] for step in (0, 1, 2, 3)]
                    + [entry, entry],
                )
                for side, start, end in spans:
                    half = tabs.side_length(self.box, side) / 2.0
                    self.assertLess(start, end, "the tool travels one way")
                    self.assertGreaterEqual(start, -half - TOL)
                self.assertAlmostEqual(spans[0][1], 0.0, 9)
                self.assertAlmostEqual(
                    spans[0][2], tabs.side_length(self.box, entry) / 2.0, 9
                )
                self.assertAlmostEqual(spans[4][2], 0.0, 9)
                self.assertAlmostEqual(spans[5][1], 0.0, 9)
                self.assertAlmostEqual(spans[5][2], tool.diameter, 9)
                for side, start, end in spans[1:4]:
                    half = tabs.side_length(self.box, side) / 2.0
                    self.assertAlmostEqual(start, -half, 9)
                    self.assertAlmostEqual(end, half, 9)

    def emit_from(self, entry, rotated=False):
        box = (
            Box.from_size(3.0, 4.0, self.box.height, self.box.width)
            if rotated
            else self.box
        )
        part = PartProgram(
            part_number="W3033",
            box=box,
            rotated=rotated,
            openings=[box.grow(-3.0)],
        )
        program = SheetProgram(
            header=ProgramHeader(name="R990101N", created=CREATED), parts=[part]
        )
        ref = FeatureRef(0, "perimeter", entry=entry)
        zones = tabs.place_tabs(box, entry, tabs.perimeter_cuts(self.post), self.post)
        plan = CutPlan(
            openings=[FeatureRef(0, "opening", 0, entry=entry)],
            # One ordered list per configured depth pass — two since the
            # 2026-08-05 max-bite ladder, and only the through rung lifts.
            perimeter=[[ref] for _ in self.post.perimeter_passes],
            sections=(SECTION_OPENINGS, SECTION_PERIMETER),
            tabs={ref.profile: zones},
        )
        return zones, emit(program, plan, self.post), box

    def test_every_zone_is_lifted_over_from_any_entry_side(self):
        for entry in SIDES:
            zones, stream, box = self.emit_from(entry)
            found = lifts(
                [m for m in stream.motions if m.section == SECTION_PERIMETER], self.post
            )
            spec = self.post.perimeter_passes[-1]
            path = box.grow(spec.offset)
            with self.subTest(entry=entry):
                self.assertEqual(len(found), len(zones))
                mapped = []
                for _, traverse, _ in found:
                    side = side_of(traverse, path)
                    start, end = travel(traverse, side, path)
                    mapped.append((side, round((start + end) / 2.0, 9)))
                self.assertEqual(
                    mapped,
                    [
                        (zone.side, round(zone.centre, 9))
                        for zone in travel_order(zones, entry)
                    ],
                )

    def test_a_rotated_part_needs_no_special_case(self):
        """Rotation is already baked into the box, so it falls out: a 33x30
        part's tabs are a 30x33 part's with the sides swapped."""
        upright = by_side(
            tabs.place_tabs(
                self.box, "right", tabs.perimeter_cuts(self.post), self.post
            )
        )
        turned = by_side(
            tabs.place_tabs(
                Box.from_size(3.0, 4.0, self.box.height, self.box.width),
                "bottom",
                tabs.perimeter_cuts(self.post),
                self.post,
            )
        )
        self.assertEqual(
            [len(upright[side]) for side in ("bottom", "right", "top", "left")],
            [len(turned[side]) for side in ("right", "bottom", "left", "top")],
        )
        for rotated in (False, True):
            zones, stream, box = self.emit_from("bottom", rotated=rotated)
            with self.subTest(rotated=rotated):
                self.assertEqual(
                    len(
                        lifts(
                            [
                                m
                                for m in stream.motions
                                if m.section == SECTION_PERIMETER
                            ],
                            self.post,
                        )
                    ),
                    len(zones),
                )


class RefusalTest(unittest.TestCase):
    """A plan whose tabs and geometry contradict each other is refused loudly.

    Silence is the failure mode that matters here: a plan that believes a part
    is tab-held while the emitter quietly cuts the tab away, or never emits it,
    would put a loose part on the machine with nothing to say about it.
    """

    def setUp(self):
        self.config = default_config()
        self.program, self.plan, self.post, _ = tabbed_r0805(self.config)

    def emit_with(self, tabs_map):
        return generate(self.program, replace_tabs(self.plan, tabs_map), self.post)

    def test_a_zone_that_crosses_a_corner(self):
        ref = self.plan.perimeter[-1][0]
        box = self.program.flat_parts()[ref.part].box
        with self.assertRaises(ValueError) as caught:
            self.emit_with(
                {ref.profile: (TabZone("bottom", box.width / 2.0 - 0.2, 0.75),)}
            )
        self.assertIn("may not cross a corner", str(caught.exception))

    def test_a_zone_sitting_on_the_lead_in(self):
        ref = self.plan.perimeter[-1][0]
        with self.assertRaises(ValueError) as caught:
            self.emit_with({ref.profile: (TabZone("right", 0.0, 0.75),)})
        self.assertIn("lead-in", str(caught.exception))

    def test_two_zones_that_overlap_once_their_ramps_are_counted(self):
        ref = self.plan.perimeter[-1][0]
        with self.assertRaises(ValueError) as caught:
            self.emit_with(
                {
                    ref.profile: (
                        TabZone("bottom", 0.0, 0.75),
                        TabZone("bottom", 1.0, 0.75),
                    )
                }
            )
        self.assertIn("overlap", str(caught.exception))

    def test_tabs_on_a_profile_the_program_never_cuts(self):
        with self.assertRaises(ValueError) as caught:
            self.emit_with({(99, "perimeter", 0): (TabZone("bottom", 0.0, 0.75),)})
        self.assertIn("never cuts a loop for", str(caught.exception))

    def test_a_pass_that_leads_in_on_another_side(self):
        """The one thing placement cannot know by itself: it is told which side
        the passes enter on, and a pass that enters elsewhere would ramp
        through a tab.  :func:`~faceframe_cnc.post.tabs.entry_conflict` catches
        it with the zone named."""
        ref = self.plan.perimeter[-1][0]
        box = self.program.flat_parts()[ref.part].box
        zones = tabs.place_tabs(
            box, "bottom", tabs.perimeter_cuts(self.post), self.post
        )
        with self.assertRaises(ValueError) as caught:
            self.emit_with({ref.profile: zones})
        message = str(caught.exception)
        self.assertIn("leads in on its right edge", message)
        self.assertIn("cut the tab away", message)



class ReleaseGeometryTest(unittest.TestCase):
    """:func:`~faceframe_cnc.post.tabs.release_span` and the travel order.

    Still pure geometry: offsets along a profile, no coordinates, no G-code.  The
    emitted result is pinned in ``tests/test_r0805_regression.py``; what is here
    is the arithmetic those coordinates come out of.
    """

    def setUp(self):
        _layout, _specs, nesting = r0805_layout()
        self.post = post_config_for(nesting)

    def test_the_release_reserves_the_worst_ramp_the_z_floor_admits(self):
        """Table-independent on purpose, so an air cut traces the same path."""
        ramp = tabs.release_ramp(self.post)
        self.assertAlmostEqual(
            ramp,
            (self.post.tabs.top_z - self.post.z_min) * self.post.ramp_ratio,
            places=9,
        )
        self.assertAlmostEqual(ramp, 0.512, places=9)
        air = dry_run_config(self.post)
        self.assertEqual(tabs.release_ramp(air), ramp)
        self.assertEqual(
            tabs.lifting_cuts(tabs.perimeter_cuts(air), air),
            (),
            "and the air table lifts over nothing at all",
        )

    def test_the_span_is_the_tab_plus_both_ramps_plus_the_overlap(self):
        zone = tabs.TabZone(side="bottom", centre=3.0, length=0.75)
        low, high = tabs.release_span(zone, self.post)
        reach = 0.75 / 2.0 + 0.512 + self.post.release.overlap
        self.assertAlmostEqual(low, 3.0 - reach, places=9)
        self.assertAlmostEqual(high, 3.0 + reach, places=9)
        self.assertAlmostEqual(high - low, 1.974, places=9)

    def test_the_span_starts_beyond_the_deepest_pass_s_own_ramp(self):
        """Which is what makes the plunge a plunge into already-open kerf."""
        zone = tabs.TabZone(side="bottom", centre=0.0, length=0.75)
        low, _high = tabs.release_span(zone, self.post)
        for cuts in (tabs.opening_cuts(self.post), tabs.perimeter_cuts(self.post)):
            footprint = tabs.worst_footprint(
                tabs.lifting_cuts(cuts, self.post), self.post
            )
            with self.subTest(footprint=footprint):
                self.assertLess(low, -footprint / 2.0, "past the foot of the climb")

    def test_a_table_with_no_release_pass_refuses_to_guess(self):
        with self.assertRaises(ValueError) as caught:
            tabs.release_span(
                tabs.TabZone("bottom", 0.0, 0.75), default_config()
            )
        self.assertIn("configures no release pass", str(caught.exception))

    def test_travel_order_starts_at_the_entry_point_and_goes_round(self):
        zones = [
            tabs.TabZone("left", 1.0, 0.75),
            tabs.TabZone("right", -2.0, 0.75),
            tabs.TabZone("right", 3.0, 0.75),
            tabs.TabZone("top", 0.0, 0.75),
            tabs.TabZone("bottom", 0.0, 0.75),
        ]
        order = tabs.travel_sequence(zones, "right")
        self.assertEqual(
            [(z.side, z.centre) for z in order],
            [
                ("right", 3.0),   # ahead of the lead-in point
                ("top", 0.0),     # then round counter-clockwise
                ("left", 1.0),
                ("bottom", 0.0),
                ("right", -2.0),  # and back onto the entry side, closing
            ],
        )

    def test_travel_order_is_deterministic_and_total(self):
        zones = tuple(
            tabs.TabZone(side, centre, 0.75)
            for side in SIDES
            for centre in (-4.0, 0.0, 4.0)
        )
        for entry in SIDES:
            with self.subTest(entry=entry):
                once = tabs.travel_sequence(zones, entry)
                self.assertEqual(once, tabs.travel_sequence(list(reversed(zones)), entry))
                self.assertEqual(sorted(once, key=repr), sorted(zones, key=repr))

    def test_an_unknown_entry_side_is_refused(self):
        with self.assertRaises(ValueError):
            tabs.travel_sequence((), "diagonal")

class ByteExactnessTest(unittest.TestCase):
    """What the tab field costs a program that has none, and what it adds.

    The suite's own byte guards do most of this — the three reference
    round-trips in ``tests/test_post`` and ``tests/test_motion``, and the
    section-by-section diff of the R0805 sheet against its frozen pre-amendment
    emission in ``tests/test_r0805_regression``.  What is added here is the part
    those cannot say: that an EMPTY tab mapping is byte-identical to no mapping
    at all, and that filling one in only ever INSERTS lines.

    Milestone 3 wired the tabs into the planner, so ``self.plan`` is now a
    TABBED plan and the untabbed baseline is built from it by taking the tabs
    and the release section back out (:meth:`untabbed`).  That is the honest
    comparison either way round: same layout, same planner, same emitter, one
    field different.
    """

    def setUp(self):
        layout, specs, nesting = r0805_layout()
        self.post = post_config_for(nesting)
        self.program, self.plan = plan_sheet(
            layout,
            ProgramHeader(name="R080501N", created=CREATED),
            specs,
            nesting,
            self.post,
        )

    def untabbed(self, plan=None):
        """``plan`` with no holding tabs and no release section."""
        return dataclasses.replace(plan or self.plan, tabs=None, release=[])

    def test_the_planner_now_builds_a_tabbed_plan(self):
        """Milestone 3: every through profile on a generated sheet is held."""
        self.assertIsNotNone(self.plan.tabs)
        parts = self.program.flat_parts()
        want = {(i, "perimeter", 0) for i in range(len(parts))} | {
            (i, "opening", j)
            for i, part in enumerate(parts)
            for j in range(len(part.openings))
        }
        self.assertEqual(set(self.plan.tabs), want)
        for zones in self.plan.tabs.values():
            self.assertTrue(zones)
        self.assertTrue(self.plan.zones_for(self.plan.openings[0]))
        self.assertEqual(
            [ref.profile for ref in self.plan.release],
            [ref.profile for ref in self.plan.openings]
            + [ref.profile for ref in self.plan.perimeter[-1]],
            "openings first, then perimeters, each in the freeing pass's order",
        )

    def test_a_reconstructed_reference_plan_carries_no_tabs(self):
        import os

        from faceframe_cnc.post.reconstruct import reconstruct

        for name in ("R710101N", "R720101N", "R730101N"):
            path = os.path.join(
                os.path.dirname(__file__), "..", "reference", "nc_files", f"{name}.anc"
            )
            _, plan = reconstruct(path)
            with self.subTest(name=name):
                self.assertIsNone(plan.tabs)

    def test_none_and_empty_are_the_same_program(self):
        plain = generate(self.program, self.untabbed(), self.post)
        for empty in ({}, {key: () for key in self.plan.tabs}):
            with self.subTest(empty=empty):
                self.assertEqual(
                    generate(
                        self.program,
                        replace_tabs(self.untabbed(), empty),
                        self.post,
                    ),
                    plain,
                )

    def test_tabbing_a_plan_adds_the_lift_and_nothing_else(self):
        """The exact shape of the diff, so nothing can hide in it.

        Take the tab lifts back out of a tabbed program — the four lines each
        one contributes — and what is left is the untabbed program line for
        line, in order, with one allowed difference: a move may now STATE the
        cutting feed it used to inherit, because the tab's descent put the entry
        feed in force and F is modal.  Every other byte, in every section, is
        untouched.
        """
        # [:-1] drops the empty string after the file's final CRLF
        plain = generate(self.program, self.untabbed(), self.post).split("\r\n")[:-1]
        # Tabs, but no release section: the release is a whole extra section and
        # is pinned on its own in ``tests/test_r0805_regression``.  What is
        # isolated here is the LIFT, which is the only thing tabs change about
        # the sections that were there before.
        stream = emit(
            self.program,
            dataclasses.replace(self.plan, release=[]),
            self.post,
        )
        lift_lines = set()
        found = lifts(stream.motions, self.post)
        for climb, traverse, descent in found:
            # the split move that ends at the foot of the climb, then the three
            # moves of the lift itself
            lift_lines.update(
                {
                    climb.line_index - 1,
                    climb.line_index,
                    traverse.line_index,
                    descent.line_index,
                }
            )
        self.assertEqual(len(lift_lines), 4 * len(found))
        kept = [e.text for e in stream.events if e.line_index not in lift_lines]
        self.assertEqual(len(kept), len(plain))
        self.assertGreater(len(stream.events), len(plain), "lines really were added")
        feeds = {
            spec.cut_feed
            for spec in (
                *self.post.openings_passes,
                self.post.detail_pass,
                *self.post.perimeter_passes,
            )
        }
        for before, after in zip(plain, kept):
            if before == after:
                continue
            with self.subTest(before=before, after=after):
                match = re.fullmatch(
                    rf"{re.escape(before)} F(\d+(?:\.\d*)?)", after
                )
                self.assertIsNotNone(match, "only a restated cut feed may differ")
                self.assertIn(float(match.group(1)), feeds)

    def test_the_tab_module_is_free_of_the_verifier(self):
        """The verifier re-derives the hold invariant independently in
        milestone 3; neither module may lean on the other (project ethos, and
        the mirror of ``tests/test_post`` 's own import test)."""
        with open(tabs.__file__, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        joined = " ".join(sorted(imported))
        for forbidden in ("verifier", "generator", "from_layout", "job"):
            self.assertNotIn(forbidden, joined)


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------


def replace_tabs(plan, tabs_map):
    """A copy of ``plan`` with a different tab mapping."""
    return CutPlan(
        panel=plan.panel,
        wdc_slot=plan.wdc_slot,
        openings=plan.openings,
        perimeter=plan.perimeter,
        detail=plan.detail,
        sections=plan.sections,
        tabs=tabs_map,
    )


def section_lines(text, header_comment):
    """The lines of one tool section of a program."""
    lines = text.split("\r\n")
    start = lines.index(header_comment)
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("(ROUTE TOOL #")
        ),
        len(lines),
    )
    return lines[start:end]


def feed_words(text):
    return set(re.findall(r"F(\d+(?:\.\d*)?)", text))


def fmt_feed(value):
    from faceframe_cnc.post.generator import fmt

    return fmt(value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
