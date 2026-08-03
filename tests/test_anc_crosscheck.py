"""Milestone 1c cross-check: geometry engine vs. R730101N.anc T11 cut data.

R730101N.anc is a real production sheet of four drawer frames (spec section
1, ``reference/README.md``): a 3DB24 (24x30, upright), a 3DB30 (30x30,
rotated 90 degrees), a B30 (30x30, rotated 90 degrees), and a B18 (18x30,
upright). Its first "(ROUTE TOOL #11 ...)" section through-cuts all ten
openings before the T12 detail pass and the second T11 (perimeter) pass.

This module:
  (a) decodes the ten opening rectangles from that section
      (faceframe_cnc.anc_reader.extract_rectangles);
  (b) confirms the tool-center-to-opening-size convention against the
      catalog sizes named in docs section 3 / milestone 1;
  (c) groups the ten rectangles into the four physical frames by spatial
      proximity, and checks each frame's opening-size multiset against
      faceframe_cnc.geometry.compute_geometry's predicted openings; and
  (d) checks each frame's *internal* spacing (1.5" member gaps, and the
      derived footprint from opening edges minus 1.5") is self-consistent
      with a rigid WxH footprint -- not against any external/absolute sheet
      position.

Run with: python -m unittest discover tests -v
"""

from __future__ import annotations

import math
import os
import unittest
from collections import Counter
from itertools import combinations

from faceframe_cnc.anc_reader import extract_rectangles
from faceframe_cnc.geometry import compute_geometry

ANC_PATH = os.path.join(
    os.path.dirname(__file__), "..", "reference", "nc_files", "R730101N.anc"
)

# --- T11 through-cut convention -------------------------------------------
#
# T11 is a 0.375"-diameter compression bit (radius 0.1875"). Per
# reference/README.md and docs section 6, the T11 through-cut pass runs the
# tool center INSIDE the finished opening boundary, leaving ~0.01" of stock
# per side uncut; the later 0.200" T12 pass finishes the opening to its true
# size. So the tool's actual cutting contact point on a side is
# (tool_center -/+ radius), and the true (finished) opening edge sits a
# further 0.01" beyond that contact point (the uncut stock). Net effect,
# confirmed below against every span in the file:
#     true_opening_edge = tool_center -/+ (radius + 0.01)
#     true_opening_span = tool_center_span + 2 * (radius + 0.01)
TOOL_RADIUS = 0.1875
STOCK_LEFT_PER_SIDE = 0.01
EDGE_INSET = TOOL_RADIUS + STOCK_LEFT_PER_SIDE  # 0.1975
SPAN_ADJUST = 2 * EDGE_INSET  # 0.395

MEMBER = 1.5  # stile/rail/cross-bar width (docs section 3)
SIZE_TOL = 1e-3  # required precision per the milestone spec
COORD_TOL = 1e-6  # decoded numbers come from exact file literals


def opening_size(rect):
    """Tool-center rect -> (true opening width, true opening height)."""
    return (rect.width + SPAN_ADJUST, rect.height + SPAN_ADJUST)


def actual_extents(rect):
    """Tool-center rect -> true opening edges (min_x, max_x, min_y, max_y)."""
    return (
        rect.min_x - EDGE_INSET,
        rect.max_x + EDGE_INSET,
        rect.min_y - EDGE_INSET,
        rect.max_y + EDGE_INSET,
    )


def _rect_gap(a, b):
    """Euclidean gap between two (min_x, max_x, min_y, max_y) boxes (0 if overlapping/touching)."""
    ax0, ax1, ay0, ay1 = a
    bx0, bx1, by0, by1 = b
    dx = max(ax0 - bx1, bx0 - ax1, 0.0)
    dy = max(ay0 - by1, by0 - ay1, 0.0)
    return math.hypot(dx, dy)


def cluster_into_frames(rects, threshold=2.0):
    """Group rectangle indices into frames by spatial proximity.

    Within one frame, adjacent openings are separated by exactly one 1.5"
    member (measured gap ~1.5"); the closest gap between openings of two
    *different* frames on this sheet is ~3.455" (see the standalone probe
    used to derive this). ``threshold`` sits comfortably between the two,
    so single-linkage clustering on rectangle-to-rectangle gap distance
    recovers the four frames without relying on any hardcoded ordering.
    """
    exts = [actual_extents(r) for r in rects]
    n = len(rects)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    def union(i, j):
        pi, pj = find(i), find(j)
        if pi != pj:
            parent[pi] = pj

    for i, j in combinations(range(n), 2):
        if _rect_gap(exts[i], exts[j]) <= threshold:
            union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


# --- the four frames on this sheet (reference/README.md) ------------------
# (part_number, outside_width, outside_height, rotated_90)
FRAME_SPECS = [
    ("3DB24", 24.0, 30.0, False),
    ("3DB30", 30.0, 30.0, True),
    ("B30", 30.0, 30.0, True),
    ("B18", 18.0, 30.0, False),
]


def engine_size_multiset(part_number, width, height):
    """Engine's predicted openings as a Counter of rounded (min,max) size pairs.

    Sorting each opening's (width, height) into (min, max) makes the
    comparison rotation-invariant by construction: rotating a rectangle 90
    degrees swaps its two dimensions, which is exactly what sorting already
    discards. This is intentional -- "opening size" here means the pair of
    physical dimensions, independent of which one lies along sheet-X vs
    sheet-Y. Section (d) below separately checks the sheet-relative spacing
    and footprint, which IS orientation-sensitive.
    """
    geometry = compute_geometry(part_number, width, height)
    assert not geometry.errors, geometry.errors
    sizes = []
    for opening in geometry.openings:
        lo, hi = sorted((opening.width, opening.height))
        sizes.append((round(lo, 6), round(hi, 6)))
    return Counter(sizes)


def decoded_size_multiset(rects):
    sizes = []
    for r in rects:
        w, h = opening_size(r)
        lo, hi = sorted((w, h))
        sizes.append((round(lo, 6), round(hi, 6)))
    return Counter(sizes)


class ConventionTests(unittest.TestCase):
    """Part (b): confirm the tool-center-to-opening-size convention."""

    def test_known_spans_convert_to_catalog_sizes(self):
        cases = [
            (20.605, 21.0),
            (4.605, 5.0),
            (9.48, 9.875),
            (8.73, 9.125),
            (14.605, 15.0),
            (26.605, 27.0),
            (20.105, 20.5),
        ]
        for span, expected in cases:
            with self.subTest(span=span):
                self.assertAlmostEqual(span + SPAN_ADJUST, expected, delta=1e-9)


class ExtractionTests(unittest.TestCase):
    """Part (a): decode the ten opening rectangles from the first T11 section."""

    @classmethod
    def setUpClass(cls):
        cls.rects = extract_rectangles(ANC_PATH, tool_number=11, section_index=0)

    def test_ten_openings_decoded(self):
        self.assertEqual(len(self.rects), 10)

    def test_all_rectangles_axis_aligned_positive(self):
        for r in self.rects:
            self.assertGreater(r.width, 0)
            self.assertGreater(r.height, 0)

    def test_decoded_sizes_are_exact_catalog_sizes(self):
        # Every decoded opening, converted, must land on a "clean" catalog
        # size (integer or quarter-inch-ish value), not an arbitrary number.
        expected = Counter(
            [
                (5.0, 21.0),
                (9.875, 21.0),
                (9.125, 21.0),
                (5.0, 27.0),
                (20.5, 27.0),
                (5.0, 15.0),
                (15.0, 20.5),
                (9.125, 27.0),
                (9.875, 27.0),
                (5.0, 27.0),
            ]
        )
        self.assertEqual(decoded_size_multiset(self.rects), expected)


class FrameGroupingAndCrossCheckTests(unittest.TestCase):
    """Parts (c) and (d): group into frames, cross-check against the engine."""

    @classmethod
    def setUpClass(cls):
        cls.rects = extract_rectangles(ANC_PATH, tool_number=11, section_index=0)
        cls.groups = cluster_into_frames(cls.rects)

    def test_four_frames_found(self):
        self.assertEqual(len(self.groups), 4)
        sizes = sorted(len(g) for g in self.groups)
        # 3DB24 (3 openings), B30 (2), B18 (2), 3DB30 (3)
        self.assertEqual(sizes, [2, 2, 3, 3])

    def test_each_frame_matches_exactly_one_engine_spec_by_opening_multiset(self):
        """Part (c): decoded opening sizes match the engine's, with multiplicity."""
        engine_multisets = {
            spec[0]: engine_size_multiset(spec[0], spec[1], spec[2]) for spec in FRAME_SPECS
        }
        unmatched_specs = set(engine_multisets)
        matches = {}  # group index -> part_number
        for gi, group in enumerate(self.groups):
            decoded = decoded_size_multiset([self.rects[i] for i in group])
            found = [
                part
                for part in unmatched_specs
                if engine_multisets[part] == decoded
            ]
            self.assertEqual(
                len(found),
                1,
                f"group {group} (decoded {decoded}) should match exactly one "
                f"unclaimed frame spec, matched: {found}",
            )
            matches[gi] = found[0]
            unmatched_specs.remove(found[0])
        self.assertEqual(unmatched_specs, set(), "every frame spec must be used exactly once")
        self.assertEqual(len(matches), 4)

    def test_frame_internal_spacing_and_footprint_self_consistent(self):
        """Part (d): 1.5" member gaps and footprint derived purely from the
        decoded openings, cross-checked against each matched frame's known
        WxH (no external/absolute sheet position is assumed or checked).
        """
        specs_by_part = {s[0]: s for s in FRAME_SPECS}
        engine_multisets = {
            spec[0]: engine_size_multiset(spec[0], spec[1], spec[2]) for spec in FRAME_SPECS
        }

        for group in self.groups:
            exts = [actual_extents(self.rects[i]) for i in group]

            # Identify which sheet axis is the shared ("perpendicular",
            # constant opening-width) axis vs. the "stack" axis (the one
            # the openings + cross members march along) by checking which
            # coordinate range every opening in this frame shares exactly.
            xs0 = {round(e[0], 6) for e in exts}
            xs1 = {round(e[1], 6) for e in exts}
            ys0 = {round(e[2], 6) for e in exts}
            ys1 = {round(e[3], 6) for e in exts}
            shares_x = len(xs0) == 1 and len(xs1) == 1
            shares_y = len(ys0) == 1 and len(ys1) == 1
            self.assertTrue(
                shares_x or shares_y,
                f"frame {group} openings must share either an X or Y range exactly",
            )
            self.assertFalse(
                shares_x and shares_y,
                f"frame {group} openings share both axes -- can't tell stack direction",
            )

            if shares_x:
                perp_span = next(iter(xs1)) - next(iter(xs0))
                stack_axis = 1  # Y
            else:
                perp_span = next(iter(ys1)) - next(iter(ys0))
                stack_axis = 0  # X

            # Sort this frame's openings along the stack axis and check
            # every adjacent gap is exactly one 1.5" cross member.
            def stack_interval(e):
                return (e[0], e[1]) if stack_axis == 0 else (e[2], e[3])

            ordered = sorted(exts, key=lambda e: stack_interval(e)[0])
            for a, b in zip(ordered, ordered[1:]):
                gap = stack_interval(b)[0] - stack_interval(a)[1]
                self.assertAlmostEqual(
                    gap, MEMBER, delta=SIZE_TOL,
                    msg=f"frame {group}: cross-member gap {gap} != {MEMBER}",
                )

            stack_min = min(stack_interval(e)[0] for e in exts)
            stack_max = max(stack_interval(e)[1] for e in exts)
            stack_extent = stack_max - stack_min

            # Footprint derived purely from the decoded openings: each end
            # of the stack has its own 1.5" rail/stile outside the outermost
            # opening edge; the perpendicular axis has a 1.5" stile on each
            # side of the shared opening-width span.
            derived_stack_dim = stack_extent + 2 * MEMBER
            derived_perp_dim = perp_span + 2 * MEMBER

            # Match this frame to its spec via the (already-verified,
            # rotation-invariant) opening-size multiset, independent of
            # test method execution order.
            decoded = decoded_size_multiset([self.rects[i] for i in group])
            found = [p for p, ms in engine_multisets.items() if ms == decoded]
            self.assertEqual(len(found), 1)
            part_number = found[0]
            _, width, height, rotated = specs_by_part[part_number]

            # The stack axis always carries the frame's full outside HEIGHT
            # (the vertical member+opening stack sums to H regardless of
            # on-sheet rotation -- rotation only changes which sheet axis
            # plays this role, per docs section 3 / compute_geometry).
            self.assertAlmostEqual(
                derived_stack_dim, height, delta=SIZE_TOL,
                msg=f"{part_number}: derived stack dimension {derived_stack_dim} != H={height}",
            )
            # The perpendicular axis always carries opening_width + 2*1.5,
            # i.e. the frame's full outside WIDTH.
            self.assertAlmostEqual(
                derived_perp_dim, width, delta=SIZE_TOL,
                msg=f"{part_number}: derived perpendicular dimension {derived_perp_dim} != W={width}",
            )
            # Sanity: opening width itself is W - 3 regardless of rotation.
            self.assertAlmostEqual(perp_span, width - 2 * MEMBER, delta=SIZE_TOL)


if __name__ == "__main__":
    unittest.main()
