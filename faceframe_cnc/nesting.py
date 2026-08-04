"""Sheet-nesting optimizer — footprint packing and frame-inside-frame nesting.

Covers spec sections 4a (footprint packing), 4b (frame-inside-frame) and 4c
(sheet uniqueness): pack whole faceframe footprints (outside W x H) onto
49 x 97 sheets, honouring the edge-to-edge part gap (0.455" by default —
:attr:`NestingConfig.part_gap` says why it is not the spec's 0.375"),
allowing 90 degree rotation of every part, and grouping identical sheet
pictures into runs.

Frame-inside-frame (spec 4b, the capability the shop's CAM lacks) is opt-in
via ``NestingConfig.inside_nesting``; with it off, this module behaves
exactly as it did in Milestone 2.  With it on, a pairing phase
(:mod:`faceframe_cnc.inside`) runs FIRST and decides which small frames sit
inside which large frames' routed openings; the packer then packs the hosts
— whose footprints are unchanged by their passengers — plus everything that
was not nested.  Each nested frame is one whole footprint the sheet no
longer has to find room for.

Hosts reach the packer under a synthetic part number (``HOST\\x1fINNER``) so
that "a W3036 carrying a W3024" and "a bare W3036" are distinct sheet
contents for run grouping, which is what spec 4c means by a unique sheet
picture.  The synthetic names never escape :func:`nest`.

Stdlib only.  Fully deterministic: same input always yields a byte-identical
result (every collection is sorted before it is iterated; no randomness).

Coordinate system: sheet origin at the lower-left corner, x across the 49"
width, y along the 97" length, matching the NC post's G54 origin.

Algorithm
---------
Pattern-based nesting, which is how the shop actually runs these jobs:

1.  Build candidate sheet layouts from the remaining demand mix with a
    shelf (level) packer.  Each shelf is filled by an exact bounded
    knapsack across the sheet width, so a shelf is the best row that the
    remaining demand can supply at that shelf height.
2.  Keep the best candidate and stamp it out as many times as the remaining
    demand allows (``run_qty``), decrement demand, repeat.

Step 2 is what maximises identical sheet pictures (objective 4c) — a run of
N identical sheets is one NC program and one PDF page.

Within a shelf of height ``h`` each part type has exactly one sensible
orientation: the one whose placed height is the largest value <= ``h``.
That orientation is also the narrowest, so it dominates the alternative on
both height fill and width consumption; this reduces the knapsack to one
item per part type and keeps it fast and exact.

The one thing greedy packing gets badly wrong on a real order is spending
the scarce narrow parts (B18, WDC2436, W3012) on whichever wide part asks
first, which strands 30x30 frames three-to-a-sheet at 57% fill.  Rows are
therefore ranked not only by density but by the sheet length they SAVE — a
part's "solo cost" is the sheet length it eats when nested only with its
own clones, so a row's ``sum(solo_cost) - row_height`` is what that row is
worth.  Finally, since no single greedy rule wins on every order and a run
costs tens of milliseconds, :func:`nest` runs a small portfolio of
deterministic strategies and keeps the best result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import (
    WDC_SLOT_END_REACH,
    FrameType,
    compute_geometry,
    infer_frame_type,
    wdc_slot_axis_is_height,
)
from .inside import assign_inners, best_fit, end_clearance_for

__all__ = [
    "EPS",
    "MIN_PART_GAP",
    "NestingError",
    "NestingConfig",
    "PartSpec",
    "Placement",
    "SheetLayout",
    "NestingResult",
    "nest",
    "place_inner",
    "slot_end_clearance",
    "clearance_needs",
    "validate_layouts",
]

#: Tolerance for all geometric comparisons.  Touching at exactly ``part_gap``
#: is legal; anything closer by more than EPS is a violation.
EPS = 1e-9

#: Area is carried through the knapsack as an integer (hundredths of a
#: square inch, see ``_area_units``) so the DP is exact and reproducible.
_AREA_SCALE = 100

#: The knapsack's objective is lexicographic, packed into one integer so the
#: DP stays a single pass:
#:
#: 1. maximise placed area (density is what drives the sheet count);
#: 2. among equal-area rows, maximise the parts' SOLO COST — the sheet
#:    length each part would eat if it had to be nested with nothing but
#:    copies of itself (see ``_solo_cost``).  A row's solo-cost total minus
#:    its own height is the sheet length that row SAVES, so this term hands
#:    the scarce narrow parts (B18, WDC2436, W3012) to the wide parts that
#:    waste the most without a partner instead of to whoever asks first;
#: 3. finally, remaining quantity, so two families with the SAME footprint
#:    (B30 and 3DB30 are both 30x30) take turns rather than letting the
#:    alphabetically-first one hog every partner and strand the other.
_AREA_WEIGHT = 1_000_000_000_000
_SOLO_WEIGHT = 1_000
_SOLO_SCALE = 1000
_ABUNDANCE_CAP = 999


@dataclass(frozen=True, order=True)
class _Strategy:
    """One deterministic way to drive the greedy packer.

    No single greedy rule wins on every order, and each run costs only tens
    of milliseconds, so :func:`nest` runs the whole portfolio and keeps the
    best result (see ``_STRATEGIES``).  That is cheaper and far more
    predictable than tuning one rule against one order.
    """

    #: How many part families to build candidate sheets around each round,
    #: taken in "wastes the most sheet on its own" order.
    seed_breadth: int
    #: Rank equally dense shelves by the sheet length they save.
    shelf_saving: bool
    #: Rank candidate sheets by retired sheet length ("saving") or by raw
    #: placed area ("area").
    sheet_key: str


#: Cheapest (narrowest seed breadth) first, so that when the work budget
#: below cuts the portfolio short the strategies that ran are the ones with
#: the best quality-per-cost.
_STRATEGIES: tuple[_Strategy, ...] = tuple(
    _Strategy(breadth, shelf_saving, sheet_key)
    for breadth in (1, 3, 5)
    for shelf_saving, sheet_key in (
        (True, "saving"),
        (False, "area"),
        (True, "area"),
        (False, "saving"),
    )
)

#: Deterministic work budget, counted in knapsack DP element-operations
#: (NOT wall-clock, which would make results machine-dependent).  Once a
#: strategy pushes the total past this, the portfolio stops early; the first
#: strategy always runs.  Sized so a full 13-family, 245-part order explores
#: everything in well under a second while a pathological input still
#: returns promptly.
_OPS_BUDGET = 40_000_000

#: Grid resolutions tried when quantising widths for the knapsack.  Frame
#: dimensions in inches and half inches are exact at 8; the larger values
#: cover odd user-entered sizes.  Anything not representable falls back to
#: the largest scale with conservative rounding (item widths up, capacity
#: down), which can only ever under-fill a shelf, never overfill it — at
#: worst a row is judged 1/16" wider than it is.  Capping the grid at 16
#: keeps the DP small for orders with awkward fractional dimensions.
#:
#: The PART GAP is deliberately not one of the values the grid has to
#: represent (see :attr:`_Context.gap_units`): the production gap 0.455 is
#: not a dyadic fraction, and asking the grid to carry it both drove every
#: order to the coarsest scale and cost a whole unit of capacity per row —
#: enough that a part exactly as wide as the sheet no longer fit on it.
_SCALE_CANDIDATES = (1, 2, 4, 8, 16)


class NestingError(ValueError):
    """A nesting request that cannot be satisfied (bad input or a part that
    does not fit on a sheet in either orientation)."""


#: The least edge-to-edge part gap the NC post can actually cut (inches).
#:
#: This is a MACHINE fact, not a packing preference.  The post's T11
#: perimeter lead-in ramp stands off the profile line, which itself sits a
#: tool radius outside the part edge, so the 0.375-diameter tool sweeps
#: 0.425 past the edge of the part it is cutting out (measured from the
#: post table; ``tests/test_nesting.py`` recomputes the reach from
#: ``faceframe_cnc.post.model`` and pins it below this constant).  Any
#: neighbouring part closer than that sweep gets cut into, and the NC
#: verifier refuses the sheet.  0.455 is the gap measured off the shop's
#: own machine files (R710101N.anc) and owner-approved on 2026-08-03.
#:
#: This module deliberately does NOT enforce the floor —
#: ``NestingConfig.part_gap`` stays a free setting so replication and
#: what-if runs are possible — but every path that feeds USER settings into
#: the optimizer (the GUI session) must clamp or refuse below it, because a
#: tighter gap silently packs sheets the post must then refuse at Generate
#: time.
MIN_PART_GAP = 0.455


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class NestingConfig:
    """Sheet and spacing settings.  All values are inches and configurable."""

    sheet_width: float = 49.0
    sheet_height: float = 97.0
    #: Minimum edge-to-edge distance between any two parts on a sheet.
    #:
    #: Defaults to :data:`MIN_PART_GAP` (0.455), not the spec's 0.375
    #: (owner decision 2026-08-03).  Two independent measurements say so:
    #: ``R710101N.anc`` — a program the shop has run — spaces its own parts
    #: exactly 0.455 apart, and the post needs it, because a perimeter
    #: lead-in stands 0.05 off a profile that is already 0.1875 outside the
    #: part edge, so the 0.1875-radius tool sweeps 0.425 past the edge.  At
    #: 0.375 the neighbouring part is inside that sweep and the NC verifier
    #: refuses the sheet.  On the 7-21-26 order the wider gap costs nothing:
    #: 47 sheets footprint-only and 40 with inside nesting, the same as at
    #: 0.375.
    part_gap: float = MIN_PART_GAP
    #: Minimum clearance per side between a frame nested inside another
    #: frame's opening and that opening (spec 4b).
    #:
    #: INDEPENDENT of :attr:`part_gap` since 2026-08-03.  It used to alias it,
    #: on the reasoning that both are "how close may material get"; they are
    #: not the same question.  ``part_gap`` is set by what the perimeter tool
    #: sweeps outside a part (above), while 0.375 here is the clearance
    #: ``R720101N.anc`` actually holds around both of its nested frames — the
    #: file this post reproduces byte for byte — so raising the sheet gap must
    #: not tighten or loosen the nest.
    inner_clearance: float = 0.375
    #: SOFT preference: keep parts this far from the sheet edges when the
    #: packing allows it.  Parts may go all the way to the edge when they
    #: must; edge contact is scored as a last resort, never as an error.
    edge_cushion: float = 0.5
    #: SOFT preference (2026-08-03 amendment): when a sheet's shelf stack
    #: leaves vertical slack, start the stack this far off the front edge
    #: (Y=0, the sheet origin) — i.e. parts sit exactly this far from the
    #: front edge when there is at least this much slack, all the available
    #: slack when there is less, and flush (0) when there is none.  Whatever
    #: slack is left over goes to the back edge instead.  This refines, not
    #: replaces, ``edge_cushion``: it is the vertical front-edge target
    #: specifically, applied from leftover slack AFTER shelf selection, so
    #: it never changes which parts fit on a sheet or the sheet count.
    front_margin: float = 1.0
    #: Spec 4b: place small frames inside larger frames' routed openings.
    #: Default OFF so plain footprint packing (Milestone 2) is unchanged.
    inside_nesting: bool = False
    #: Spec 4b: allow an inner frame to host a frame of its own (depth 3+).
    #: Default OFF.  The optimizer never builds depth-2 nests on its own —
    #: this flag only decides whether :func:`validate_layouts` accepts one,
    #: so a hand-built GUI layout can go deeper when the user asks for it.
    inside_recursion: bool = False
    #: When inside nesting is on, also run a plain footprint pass so the
    #: summary can quote "sheets saved vs no-inside baseline" (spec 5).
    #: Costs one extra pack; turn it off if the delta is not wanted.
    inside_baseline: bool = True

    @property
    def sheet_area(self) -> float:
        return self.sheet_width * self.sheet_height


@dataclass(frozen=True)
class PartSpec:
    """One optimizer input line.

    ``width`` / ``height`` are the frame's outside footprint exactly as
    ordered.  The optimizer never alters them — rotation swaps which one
    lands on the sheet's x axis, nothing more.
    """

    part_number: str
    width: float
    height: float
    qty: int

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass
class Placement:
    """One part placed on a sheet.

    ``x`` / ``y`` are the lower-left corner on the sheet.  ``width`` /
    ``height`` are the dimensions AS PLACED (already swapped when
    ``rotated`` is true), so ``x + width`` and ``y + height`` are the
    part's real extents without any further reasoning.

    ``children`` holds frame-inside-frame passengers (spec 4b): whole
    frames placed inside this part's routed openings.  A child's ``x`` /
    ``y`` are SHEET coordinates like everyone else's — not relative to the
    host — so no caller has to compose transforms, and ``rotated`` is the
    child's ABSOLUTE orientation on the sheet (a child turned 90° inside a
    host that is itself turned 90° comes out upright).  Empty unless
    ``NestingConfig.inside_nesting`` is on.
    """

    part_number: str
    x: float
    y: float
    width: float
    height: float
    rotated: bool = False
    children: list = field(default_factory=list)

    @property
    def area(self) -> float:
        return self.width * self.height


# --------------------------------------------------------------------------
# Directional clearance: the WDC 45-degree stile slot (2026-08-03)
# --------------------------------------------------------------------------
#
# Every other rule in this module is isotropic — ``part_gap`` in all four
# directions — because every other cut this shop makes stays inside the trim
# margin.  The T17 slot does not: it is a 45-degree V groove down each 2"
# stile, and a 45-degree bit cutting 0.4375 deep removes material 0.4375
# either side of its path.  The pass runs its centre 0.4375 past the end of
# the stile and the cone reaches 0.4375 further, so a WDC frame CUTS the
# 0.875 beyond each of its two stile ends.
#
# That direction is the frame's HEIGHT axis (stiles are the vertical
# members), which the packer's 90-degree rotation maps onto the sheet's X
# axis.  A neighbour or a sheet edge inside that reach gets carved up to
# 0.42 deep; the owner has not approved marking a finished frame, so the
# room is reserved rather than the damage accepted.


def slot_end_clearance(part_number: str, config: "NestingConfig") -> float:
    """Clearance a part needs beyond each END of its height axis.

    ``part_gap`` for everything the shop cuts except a WDC frame, which
    needs the full reach of its T17 slot.  Accepts the packer's synthetic
    ``HOST\\x1fINNER`` names: it is the HOST whose stiles are on the sheet.
    """
    host = part_number.split(_SYNTHETIC_SEP)[0]
    if infer_frame_type(host) is FrameType.WDC:
        return max(config.part_gap, WDC_SLOT_END_REACH)
    return config.part_gap


def clearance_needs(
    placement: "Placement", config: "NestingConfig"
) -> tuple[float, float]:
    """``(x, y)`` clearance this PLACED part demands from other material.

    Both are ``part_gap`` unless the part is a WDC, in which case the axis
    its stiles run along carries the slot's reach instead.
    """
    ends = slot_end_clearance(placement.part_number, config)
    if ends <= config.part_gap:
        return config.part_gap, config.part_gap
    if wdc_slot_axis_is_height(placement.rotated):
        return config.part_gap, ends
    return ends, config.part_gap


def _pack_dims(spec: "PartSpec", config: "NestingConfig") -> tuple[float, float]:
    """The rectangle the PACKER reserves for a part, as ordered.

    For everything but a WDC this is the footprint itself.  A WDC's is its
    footprint grown at both ends of its height axis by the part of the
    slot's reach that the ordinary ``part_gap`` does NOT already cover —
    ``slot_end_clearance - part_gap``, 0.420 at the shipping numbers.  The
    real part is then centred in that rectangle (``_build_sheet``).

    Reserving only the top-up is deliberate and is what keeps two adjacent
    WDCs from wasting material: the packer already leaves ``part_gap``
    between two reserved rectangles, so 0.420 + 0.455 is exactly the 0.875
    the stile end needs.  It also means this rectangle alone says NOTHING
    about the sheet edge, where there is no neighbour and so no ``part_gap``
    to top it up — that is :func:`_edge_inset`'s job, and getting it wrong
    was the bug the 2026-08-04 review found: the packer would seat a WDC
    reserved rectangle against the sheet edge, leaving the stile end 0.420
    from it, and ``validate_layouts`` then rejected the whole layout.
    """
    pad = slot_end_clearance(spec.part_number, config) - config.part_gap
    if pad <= 0.0:
        return spec.width, spec.height
    return spec.width, spec.height + 2.0 * pad


def _pack_pad(part_number: str, config: "NestingConfig") -> float:
    """How much :func:`_pack_dims` added at EACH end of the height axis."""
    return max(0.0, slot_end_clearance(part_number, config) - config.part_gap)


def _edge_inset(part_number: str, config: "NestingConfig") -> float:
    """How far a part's RESERVED rectangle must stay off the SHEET EDGES.

    Zero for everything the shop cuts except a WDC, and even then only at
    the two ends of the axis its stiles run along — the same directional
    story as :func:`slot_end_clearance`, seen from the sheet edge instead
    of from a neighbour.

    The number is forced, not chosen: ``validate_layouts`` enforces (hard)
    that a WDC stile END sits at least ``slot_end_clearance`` from the
    sheet edge along the slot axis, the reserved rectangle stands
    :func:`_pack_pad` outside that stile end, so the rectangle itself must
    keep ``slot_end_clearance - _pack_pad`` — i.e. exactly ``part_gap`` —
    off the edge.  Written as the difference so the invariant is visible:
    whatever the two settings are, rectangle inset + reserved pad always
    equals what the validator demands.

    2026-08-04 review.  Before it the packer relied on ``_pack_dims``
    alone, which is a valid argument between two parts (``part_gap`` tops
    the pad up) and no argument at all against a sheet edge, where the only
    thing standing between a stile end and thin air was the SOFT
    ``edge_cushion`` — compressible to zero by design.  The packer could
    therefore emit a layout its own validator refused, and the user got an
    optimized job that could never reach Generate.
    """
    pad = _pack_pad(part_number, config)
    if pad <= 0.0:
        return 0.0
    return max(0.0, slot_end_clearance(part_number, config) - pad)


def _fmt(value: float) -> str:
    """Round to 1e-4 and format stably (no ``-0.0000``)."""
    rounded = round(value, 4) + 0.0
    return f"{rounded:.4f}"


def _canonical_placement(p: Placement) -> str:
    kids = ";".join(sorted(_canonical_placement(c) for c in p.children))
    return (
        f"{p.part_number}@{_fmt(p.x)},{_fmt(p.y)}"
        f":{_fmt(p.width)}x{_fmt(p.height)}"
        f":{'R' if p.rotated else 'U'}"
        f"[{kids}]"
    )


@dataclass
class SheetLayout:
    """The contents of one physical sheet picture."""

    placements: list[Placement] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.placements)

    def canonical(self) -> str:
        """Stable serialization used to group identical sheet pictures.

        Placements are sorted and coordinates rounded to 1e-4, so two sheets
        that look the same produce the same string regardless of the order
        the packer happened to emit them in.
        """
        return "|".join(sorted(_canonical_placement(p) for p in self.placements))

    def part_counts(self) -> dict[str, int]:
        """Top-level part number -> count on this sheet (children included)."""
        counts: dict[str, int] = {}

        def walk(items: list) -> None:
            for p in items:
                counts[p.part_number] = counts.get(p.part_number, 0) + 1
                walk(p.children)

        walk(self.placements)
        return dict(sorted(counts.items()))

    def used_area(self) -> float:
        total = 0.0

        def walk(items: list) -> None:
            nonlocal total
            for p in items:
                total += p.area
                walk(p.children)

        walk(self.placements)
        return total

    def footprint_area(self) -> float:
        """Area this sheet actually gives up: top-level footprints only.

        A nested frame is cut from a host's interior waste, so it yields
        product without consuming any more sheet — which is exactly why
        ``used_area`` (everything, nested included) and this are different
        numbers once inside nesting is on.
        """
        return sum(p.area for p in self.placements)

    def child_count(self) -> int:
        """Frames nested inside another frame on this sheet, at any depth."""
        total = 0

        def walk(items: list) -> None:
            nonlocal total
            for p in items:
                total += len(p.children)
                walk(p.children)

        walk(self.placements)
        return total

    def nested_area(self) -> float:
        """Footprint area recovered from the hosts' interior waste."""
        total = 0.0

        def walk(items: list, depth: int) -> None:
            nonlocal total
            for p in items:
                if depth > 0:
                    total += p.area
                walk(p.children, depth + 1)

        walk(self.placements, 0)
        return total


@dataclass
class NestingResult:
    """Optimizer output.

    ``unique_sheets`` pairs each distinct sheet picture with the number of
    physical sheets cut from it; the run quantities sum to ``total_sheets``.
    ``demand`` is the (normalised) input, retained so the independent
    validator can re-derive everything it needs.
    """

    unique_sheets: list[tuple[SheetLayout, int]]
    total_sheets: int
    demand: list[PartSpec]
    config: NestingConfig
    #: Physical frames placed inside another frame's opening (spec 4b),
    #: counted per sheet cut, i.e. already multiplied by each run quantity.
    inside_placements: int = 0
    #: Sheets the same order needs with inside nesting turned off.  ``None``
    #: when no baseline pass was run (inside nesting off, or
    #: ``inside_baseline`` disabled).
    baseline_sheets: int | None = None

    @property
    def unique_sheet_count(self) -> int:
        return len(self.unique_sheets)

    @property
    def sheets_saved(self) -> int | None:
        """Sheets saved versus the no-inside-nesting baseline (spec 5)."""
        if self.baseline_sheets is None:
            return None
        return self.baseline_sheets - self.total_sheets

    @property
    def total_parts(self) -> int:
        return sum(spec.qty for spec in self.demand)

    @property
    def total_part_area(self) -> float:
        return sum(spec.area * spec.qty for spec in self.demand)

    @property
    def nested_part_area(self) -> float:
        """Ordered area that came out of hosts' waste, not out of new sheet."""
        return sum(
            layout.nested_area() * run for layout, run in self.unique_sheets
        )

    @property
    def area_lower_bound_sheets(self) -> int:
        """Absolute floor on sheet count: sheet-consuming area / sheet area.

        Frames nested inside a host are cut from interior waste the host
        already paid for, so they do not consume sheet area and are excluded.
        With inside nesting off nothing is nested and this is simply the
        total part area over the sheet area.
        """
        sheet_area = self.config.sheet_area
        if sheet_area <= 0:
            return 0
        consuming = self.total_part_area - self.nested_part_area
        return math.ceil(max(0.0, consuming) / sheet_area - 1e-9)

    def fill_fraction(self, layout: SheetLayout) -> float:
        """How much of the sheet the footprints consume (nested frames are
        free riders in a host's waste, so they are not counted)."""
        area = self.config.sheet_area
        return layout.footprint_area() / area if area > 0 else 0.0

    @property
    def overall_fill_fraction(self) -> float:
        denom = self.total_sheets * self.config.sheet_area
        if denom <= 0:
            return 0.0
        return (self.total_part_area - self.nested_part_area) / denom

    def edge_contact_parts(self) -> int:
        """Placements (weighted by run qty) that fail the soft edge cushion."""
        cfg = self.config
        cushion = cfg.edge_cushion
        total = 0
        for layout, run in self.unique_sheets:
            for p in layout.placements:
                if (
                    p.x < cushion - EPS
                    or p.y < cushion - EPS
                    or p.x + p.width > cfg.sheet_width - cushion + EPS
                    or p.y + p.height > cfg.sheet_height - cushion + EPS
                ):
                    total += run
        return total

    def summary(self) -> str:
        lines = [
            f"parts={self.total_parts} "
            f"part_area={self.total_part_area:.1f}in2 "
            f"sheet_area={self.config.sheet_area:.1f}in2",
            f"total_sheets={self.total_sheets} "
            f"(area lower bound {self.area_lower_bound_sheets}) "
            f"unique_sheets={self.unique_sheet_count} "
            f"overall_fill={self.overall_fill_fraction * 100:.1f}%",
        ]
        if self.config.inside_nesting:
            saved = self.sheets_saved
            baseline = (
                f" baseline={self.baseline_sheets} saved={saved}"
                if saved is not None
                else ""
            )
            lines.append(f"inside_placements={self.inside_placements}{baseline}")
        for i, (layout, run) in enumerate(self.unique_sheets, start=1):
            contents = ", ".join(
                f"{n}x{pn}" for pn, n in sorted(layout.part_counts().items())
            )
            nested = layout.child_count()
            inside = f" inside={nested}" if nested else ""
            lines.append(
                f"  sheet {i:>2}: run={run:<3} fill={self.fill_fraction(layout) * 100:5.1f}% "
                f"parts={len(layout):<2}{inside} [{contents}]"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Input normalisation
# --------------------------------------------------------------------------


def _check_config(config: NestingConfig) -> None:
    problems = []
    if not (math.isfinite(config.sheet_width) and config.sheet_width > 0):
        problems.append(f"sheet_width must be positive and finite, got {config.sheet_width!r}")
    if not (math.isfinite(config.sheet_height) and config.sheet_height > 0):
        problems.append(f"sheet_height must be positive and finite, got {config.sheet_height!r}")
    if not (math.isfinite(config.part_gap) and config.part_gap >= 0):
        problems.append(f"part_gap must be >= 0 and finite, got {config.part_gap!r}")
    if not (math.isfinite(config.inner_clearance) and config.inner_clearance >= 0):
        problems.append(
            f"inner_clearance must be >= 0 and finite, got {config.inner_clearance!r}"
        )
    if not (math.isfinite(config.edge_cushion) and config.edge_cushion >= 0):
        problems.append(f"edge_cushion must be >= 0 and finite, got {config.edge_cushion!r}")
    if not (math.isfinite(config.front_margin) and config.front_margin >= 0):
        problems.append(f"front_margin must be >= 0 and finite, got {config.front_margin!r}")
    if problems:
        raise NestingError("invalid NestingConfig: " + "; ".join(problems))


def _normalize_demand(parts, config: NestingConfig) -> list[PartSpec]:
    """Validate, merge duplicates, drop zero quantities, sort deterministically."""
    merged: dict[str, PartSpec] = {}
    for spec in parts:
        pn = spec.part_number.strip()
        if not pn:
            raise NestingError("every part needs a non-empty part_number")
        if not (math.isfinite(spec.width) and spec.width > 0):
            raise NestingError(f"{pn}: width must be positive and finite, got {spec.width!r}")
        if not (math.isfinite(spec.height) and spec.height > 0):
            raise NestingError(f"{pn}: height must be positive and finite, got {spec.height!r}")
        if int(spec.qty) != spec.qty or spec.qty < 0:
            raise NestingError(f"{pn}: qty must be a non-negative integer, got {spec.qty!r}")
        if pn in merged:
            prev = merged[pn]
            if abs(prev.width - spec.width) > EPS or abs(prev.height - spec.height) > EPS:
                raise NestingError(
                    f"{pn}: duplicate part number with conflicting dimensions "
                    f"({prev.width}x{prev.height} vs {spec.width}x{spec.height})"
                )
            merged[pn] = PartSpec(pn, prev.width, prev.height, prev.qty + int(spec.qty))
        else:
            merged[pn] = PartSpec(pn, float(spec.width), float(spec.height), int(spec.qty))

    demand = [s for _, s in sorted(merged.items()) if s.qty > 0]

    sw, sh = config.sheet_width, config.sheet_height
    for spec in demand:
        # A WDC has to fit with the room its slot cuts, not just with its
        # footprint, or the packer would place a part the post must refuse.
        # Between the SHEET EDGES that room is the full reach at both ends
        # (nothing tops up an edge — see ``_edge_inset``), so the rectangle
        # tested here is the footprint plus 2 x slot_end_clearance along the
        # slot axis.  A WDC taller than 97 - 1.75 = 95.25 therefore cannot be
        # made at all, and saying so here is the only honest answer: the
        # alternative is a layout the validator will refuse (2026-08-04).
        pw, ph = _pack_dims(spec, config)
        inset = _edge_inset(spec.part_number, config)
        ew, eh = pw, ph + 2.0 * inset
        upright = ew <= sw + EPS and eh <= sh + EPS
        turned = eh <= sw + EPS and ew <= sh + EPS
        if not (upright or turned):
            reserved = (
                ""
                if (ew, eh) == (spec.width, spec.height)
                else (
                    f" (its T17 stile slot needs {ew:g}x{eh:g} clear of the "
                    f"sheet edges — see NestingConfig.part_gap and "
                    f"nesting.slot_end_clearance)"
                )
            )
            raise NestingError(
                f"{spec.part_number}: {spec.width}x{spec.height} does not fit on a "
                f"{sw}x{sh} sheet in either orientation "
                f"(rotated it would be {spec.height}x{spec.width}){reserved}; "
                f"the part cannot be nested and dimensions must never be altered"
            )
    return demand


# --------------------------------------------------------------------------
# Packing internals
# --------------------------------------------------------------------------


def _pick_scale(values) -> int:
    for scale in _SCALE_CANDIDATES:
        if all(abs(v * scale - round(v * scale)) < 1e-9 for v in values):
            return scale
    return _SCALE_CANDIDATES[-1]


def _solo_cost(
    width: float, height: float, config: NestingConfig, edge_inset: float = 0.0
) -> float:
    """Inches of sheet length one part eats when nested only with its clones.

    Closed form: in the better of the two orientations, a shelf holds
    ``per_row`` copies and costs ``h + gap`` of sheet length, so one part
    costs ``(h + gap) / per_row``.  A 30x30 frame costs its whole 30.375"
    shelf; an 18x30 costs half of one (two fit across 49").

    A row's ``sum(solo_cost) - (row_height + gap)`` is therefore the sheet
    length that row saves versus packing its parts separately — the quantity
    the optimizer is really trying to maximise, since total sheet length
    divided by 97" is the sheet count.

    ``width``/``height`` are the RESERVED rectangle (``_pack_dims``), and
    ``edge_inset`` is the distance that rectangle has to keep off the sheet
    edges at the two ends of its slot axis (:func:`_edge_inset`, nonzero for
    a WDC only).  Since the slot axis lies along the sheet's x exactly when
    the part is turned 90°, the inset narrows the row for the turned
    orientation and can rule an orientation out altogether — which is the
    honest cost model, because the packer will not use one either.
    """
    gap = config.part_gap
    best = None
    # (placed w, placed h, does the slot axis lie along the sheet's x?)
    for w, h, along_x in ((width, height, False), (height, width, True)):
        avail_w = config.sheet_width - (2.0 * edge_inset if along_x else 0.0)
        avail_h = config.sheet_height - (0.0 if along_x else 2.0 * edge_inset)
        if w > avail_w + EPS or h > avail_h + EPS:
            continue
        per_row = int((avail_w + gap + EPS) // (w + gap))
        if per_row < 1:
            continue
        cost = (h + gap) / per_row
        if best is None or cost < best:
            best = cost
    return best if best is not None else 0.0


class _Context:
    """Precomputed, immutable packing context for one :func:`nest` call."""

    def __init__(self, demand: list[PartSpec], config: NestingConfig):
        self.config = config
        self.specs = {s.part_number: s for s in demand}
        self.part_numbers = sorted(self.specs)

        #: What the packer reserves per part type, as ordered: the footprint
        #: for everything but a WDC, whose T17 stile slot needs room past
        #: its ends (see ``_pack_dims``).  EVERY packing decision below —
        #: shelf heights, knapsack widths, solo costs — uses these; the real
        #: footprint reappears only when a Placement is emitted.
        self.pack_dims = {s.part_number: _pack_dims(s, config) for s in demand}
        self.pack_pad = {
            s.part_number: _pack_pad(s.part_number, config) for s in demand
        }
        #: How far each part's reserved rectangle must stay off the sheet
        #: edges at the two ends of its slot axis — zero for everything but a
        #: WDC (:func:`_edge_inset`, 2026-08-04).  Consulted twice below: to
        #: rule out an orientation that can never satisfy it
        #: (``_legal_orientations``), and to seat rows and shelf stacks off
        #: the edge in ``_build_sheet``.
        self.edge_inset = {
            s.part_number: _edge_inset(s.part_number, config) for s in demand
        }
        #: True when ANY part on this job demands an edge inset, i.e. when the
        #: rule can bite at all.  Every WDC-specific branch below is skipped
        #: outright otherwise, so a job with no WDC on it packs down exactly
        #: the code path it always did.
        self.has_edge_insets = any(v > 0.0 for v in self.edge_inset.values())

        #: Which of the two orientations a part may be placed in at all:
        #: ``rotated`` flags, upright first.  An orientation is out when the
        #: reserved rectangle plus its edge insets cannot fit between the
        #: sheet edges — a WDC2452 (18 x 52) turned 90° would need
        #: 18 x 53.75 across a 49" sheet, so it is upright-only, and the
        #: packer must know that before it starts filling shelves.
        self.orientations = {
            s.part_number: self._legal_orientations(s.part_number) for s in demand
        }

        dims = []
        for pw, ph in self.pack_dims.values():
            dims.extend((pw, ph))
        self.scale = _pick_scale(dims + [config.sheet_width])

        # Conservative quantisation: capacity rounded down, item widths up.
        # A row of n parts occupies ``sum(w) + gap * (n - 1)``, modelled as
        # ``sum(w + gap) <= sheet_width + gap``, so the gap is carried once
        # per item and once in the capacity.  It gets its own rounding — UP
        # onto the grid, i.e. the row is asked for slightly MORE space than
        # it needs — because the production gap (0.455) is not a dyadic
        # fraction: quantising ``w + gap`` as one number would spend the
        # rounding allowance of every single item on the gap's remainder and
        # leave a full-sheet-width part unable to fit its own sheet.
        self.gap_units = math.ceil(config.part_gap * self.scale - 1e-9)
        self.capacity = (
            math.floor(config.sheet_width * self.scale + 1e-9) + self.gap_units
        )
        # Distinct candidate shelf heights, tallest first (deterministic).
        self.height_candidates = sorted({d for d in dims}, reverse=True)
        self._shelf_cache: dict[tuple, tuple | None] = {}
        #: Knapsack DP element-operations spent so far.  A deterministic
        #: stand-in for elapsed time (see ``_OPS_BUDGET``).
        self.dp_ops = 0

        self.solo_cost = {
            s.part_number: _solo_cost(
                *self.pack_dims[s.part_number],
                config,
                self.edge_inset[s.part_number],
            )
            for s in demand
        }
        self.solo_units = {
            pn: int(round(c * _SOLO_SCALE)) for pn, c in self.solo_cost.items()
        }
        # Seed order for candidate sheets: the part that wastes the most
        # sheet on its own (worst solo area efficiency) gets first pick of
        # partners while they are still in stock.
        self.seed_order = sorted(
            self.part_numbers,
            key=lambda pn: (
                round(
                    self.specs[pn].area / (self.solo_cost[pn] * config.sheet_width), 9
                )
                if self.solo_cost[pn] > 0
                else 0.0,
                -self.specs[pn].area,
                pn,
            ),
        )

    def _legal_orientations(self, part_number: str) -> tuple[bool, ...]:
        """``rotated`` flags this part may actually be placed with.

        Only an edge inset can rule one out, so this is ``(False, True)`` for
        every ordinary part; ``_normalize_demand`` has already refused
        anything with no legal orientation at all, so the result is never
        empty.
        """
        cfg = self.config
        pw, ph = self.pack_dims[part_number]
        inset = self.edge_inset[part_number]
        legal = []
        # Upright: the slot axis (the ordered height) runs up the sheet.
        if pw <= cfg.sheet_width + EPS and ph + 2.0 * inset <= cfg.sheet_height + EPS:
            legal.append(False)
        # Turned 90°: the slot axis runs across the sheet width instead.
        if ph + 2.0 * inset <= cfg.sheet_width + EPS and pw <= cfg.sheet_height + EPS:
            legal.append(True)
        return tuple(legal)

    def charged_width(
        self, part_number: str, placed_width: float, rotated: bool, inflate: bool
    ) -> float:
        """Row width to CHARGE a part, which may exceed the width it occupies.

        Normally the reserved rectangle itself.  Under ``inflate`` a part whose
        slot axis runs across the sheet — a turned WDC — is charged its two
        edge insets as well, which is how ``_best_shelf`` returns a row with
        guaranteed side room without shrinking the sheet for everyone else.
        The inflation is only ever a charge: the emitted placement still
        occupies ``placed_width``, so the surplus lands in the row's slack
        where :func:`_seat` can spend it on the edge the part actually needs.
        """
        if not inflate or wdc_slot_axis_is_height(rotated):
            return placed_width
        return placed_width + 2.0 * self.edge_inset[part_number]

    def width_units(self, placed_width: float) -> int:
        return math.ceil(placed_width * self.scale - 1e-9) + self.gap_units


def _orient_for_shelf(
    width: float, height: float, shelf_h: float, legal: tuple[bool, ...] = (False, True)
):
    """Best orientation for a shelf of height ``shelf_h``.

    Returns ``(placed_width, placed_height, rotated)`` for the orientation
    whose placed height is the largest value <= ``shelf_h``, or ``None`` when
    neither orientation fits.  That orientation is also the narrower one, so
    it dominates on both height fill and width consumption.

    ``legal`` restricts the choice to the orientations the part may be placed
    in at all (:meth:`_Context._legal_orientations`) — a WDC whose slot reach
    would hang off the sheet if it were turned must not be offered the turn,
    or the packer builds a row the validator refuses.
    """
    best = None
    if False in legal and height <= shelf_h + EPS:
        best = (width, height, False)
    if True in legal and width <= shelf_h + EPS and (best is None or width > best[1] + EPS):
        best = (height, width, True)
    return best


def _area_units(w: float, h: float) -> int:
    return int(round(w * h * _AREA_SCALE))


def _bounded_knapsack(entries, capacity: int):
    """Exact bounded knapsack via binary splitting.

    ``entries`` is a list of ``(weight, value, count)``.  Returns a list of
    chosen counts, one per entry, maximising total value subject to total
    weight <= ``capacity``.
    """
    bundles = []  # (entry_index, take, weight, value)
    for idx, (weight, value, count) in enumerate(entries):
        remaining, step = count, 1
        while remaining > 0:
            take = step if step < remaining else remaining
            bundles.append((idx, take, weight * take, value * take))
            remaining -= take
            step *= 2

    dp = [0] * (capacity + 1)
    layers = [dp]
    for _idx, _take, bw, bv in bundles:
        if bw > capacity:
            layers.append(dp)
            continue
        lifted = [x + bv for x in dp[: capacity + 1 - bw]]
        dp = dp[:bw] + [b if b > a else a for a, b in zip(dp[bw:], lifted)]
        layers.append(dp)

    chosen = [0] * len(entries)
    cap = capacity
    for i in range(len(bundles) - 1, -1, -1):
        # dp strictly improved at this capacity only if the bundle was used;
        # ties resolve to "not taken", which is always achievable and keeps
        # reconstruction deterministic.
        if layers[i + 1][cap] > layers[i][cap]:
            idx, take, bw, _bv = bundles[i]
            chosen[idx] += take
            cap -= bw
    return chosen


def _best_shelf(
    ctx: _Context,
    avail: dict[str, int],
    shelf_h: float,
    seed: str | None = None,
    inflate_edges: bool = False,
):
    """Best row of parts for a shelf of height ``shelf_h``.

    When ``seed`` is given, one part of that type is forced into the row (and
    the row is rejected if that type cannot be placed at this shelf height).
    Seeding is how the pattern loop explores sheets built around each part
    family instead of only the locally densest one.

    ``inflate_edges`` charges every part whose slot axis would run ACROSS the
    sheet — a turned WDC — the width of its two edge insets on top of its own
    (:meth:`_Context.charged_width`), so whatever row comes back is guaranteed
    to have the side room its ends need.  ``_build_sheet`` turns it on only
    when the free-for-all row pinned a stile end against a side edge
    (2026-08-04); charging per part rather than shrinking the sheet is what
    keeps a plain 48"-wide frame placeable while the correction is in force.

    Returns ``(actual_height, row_width, area, items, saving)`` where
    ``items`` is a deterministic left-to-right list of
    ``(part_number, w, h, rotated)``, or ``None`` when nothing fits.  The
    ``w``/``h`` carried in ``items`` are the RESERVED rectangle
    (``_Context.pack_dims``), so a WDC's slot room is spent here and given
    back when the placement is emitted; ``area`` is the real footprint area,
    since padding is a cost, not value.
    """
    cfg = ctx.config
    capacity = ctx.capacity
    forced = None
    if seed is not None:
        if avail.get(seed, 0) <= 0:
            return None
        oriented = _orient_for_shelf(
            *ctx.pack_dims[seed], shelf_h, ctx.orientations[seed]
        )
        if oriented is None:
            return None
        pw, ph, rotated = oriented
        wu = ctx.width_units(ctx.charged_width(seed, pw, rotated, inflate_edges))
        if wu > capacity:
            return None
        forced = (seed, pw, ph, rotated, wu)
        capacity -= wu

    entries = []
    meta = []
    for pn in ctx.part_numbers:
        n = avail.get(pn, 0)
        if forced is not None and pn == seed:
            n -= 1
        if n <= 0:
            continue
        oriented = _orient_for_shelf(*ctx.pack_dims[pn], shelf_h, ctx.orientations[pn])
        if oriented is None:
            continue
        pw, ph, rotated = oriented
        wu = ctx.width_units(ctx.charged_width(pn, pw, rotated, inflate_edges))
        if wu <= 0 or wu > capacity:
            continue
        # No row can hold more than capacity // wu of anything; capping here
        # shrinks the knapsack and makes the cache key hit far more often.
        n_eff = min(n, capacity // wu)
        if n_eff <= 0:
            continue
        spec = ctx.specs[pn]
        value = (
            _area_units(spec.width, spec.height) * _AREA_WEIGHT
            + ctx.solo_units[pn] * _SOLO_WEIGHT
            + min(n, _ABUNDANCE_CAP)
        )
        entries.append((wu, value, n_eff))
        meta.append((pn, pw, ph, rotated))

    if not entries and forced is None:
        return None

    key = (round(shelf_h, 9), capacity, forced, tuple(zip(meta, entries)))
    cached = ctx._shelf_cache.get(key)
    if cached is not None:
        return cached[0]

    items = []
    if forced is not None:
        items.append(forced[:4])
    if entries:
        ctx.dp_ops += capacity * sum(max(1, e[2].bit_length()) for e in entries)
        chosen = _bounded_knapsack(entries, capacity)
        for count, item in zip(chosen, meta):
            items.extend([item] * count)
    if not items:
        ctx._shelf_cache[key] = (None,)
        return None

    # Tallest first, then widest, then by name: purely cosmetic but stable.
    items.sort(key=lambda it: (-it[2], -it[1], it[0], it[3]))
    actual_h = max(it[2] for it in items)
    row_width = sum(it[1] for it in items) + cfg.part_gap * (len(items) - 1)
    area = sum(ctx.specs[it[0]].area for it in items)
    # Sheet length this row saves versus nesting its parts on their own.
    saving = sum(ctx.solo_cost[it[0]] for it in items) - (actual_h + cfg.part_gap)
    result = (actual_h, row_width, area, items, saving)
    ctx._shelf_cache[key] = (result,)
    return result


def _cushion_score(layout: SheetLayout, cfg: NestingConfig) -> int:
    """How many placements honour the SOFT edge cushion (higher is better)."""
    cushion = cfg.edge_cushion
    good = 0
    for p in layout.placements:
        if (
            p.x >= cushion - EPS
            and p.y >= cushion - EPS
            and p.x + p.width <= cfg.sheet_width - cushion + EPS
            and p.y + p.height <= cfg.sheet_height - cushion + EPS
        ):
            good += 1
    return good


def _row_edge_needs(ctx: _Context, items) -> tuple[float, float]:
    """``(left, right)`` inset this row owes the sheet's two SIDE edges.

    Only the row's two END items can touch a side edge — every other item has
    at least a neighbour and a ``part_gap`` between it and the edge — so only
    they are asked, and only when their slot axis runs across the sheet, i.e.
    when they are turned 90° (:func:`wdc_slot_axis_is_height`).  Both are zero
    for any row without a turned WDC at an end, which is the overwhelming
    majority and why the layouts of such sheets are untouched by this rule.
    """
    if not items or not ctx.has_edge_insets:
        return 0.0, 0.0
    needs = []
    for pn, _pw, _ph, rotated in (items[0], items[-1]):
        needs.append(0.0 if wdc_slot_axis_is_height(rotated) else ctx.edge_inset[pn])
    return needs[0], needs[1]


def _stack_edge_needs(ctx: _Context, shelves) -> tuple[float, float]:
    """``(bottom, top)`` inset the shelf stack owes the sheet's END edges.

    Items sit bottom-aligned in their shelf, so only the BOTTOM shelf's items
    reach the bottom of the stack — and only the TOP shelf's can reach its
    top, discounted by whatever headroom a shorter reserved rectangle already
    leaves inside a taller shelf.  Both are zero unless an upright WDC is in
    the shelf concerned.
    """
    if not shelves or not ctx.has_edge_insets:
        return 0.0, 0.0
    _bottom_h, _bw, bottom_items = shelves[0]
    bottom = max(
        (
            ctx.edge_inset[pn]
            for pn, _pw, _ph, rotated in bottom_items
            if wdc_slot_axis_is_height(rotated)
        ),
        default=0.0,
    )
    top_h, _tw, top_items = shelves[-1]
    top = max(
        (
            max(0.0, ctx.edge_inset[pn] - (top_h - ph))
            for pn, _pw, ph, rotated in top_items
            if wdc_slot_axis_is_height(rotated)
        ),
        default=0.0,
    )
    return bottom, top


def _any_row_pinned(ctx: _Context, shelves) -> bool:
    """Does any row lack the side room its end items owe the sheet edges?

    The vertical rule has no counterpart here because :func:`_select_shelves`
    charges the stack for its front and back insets as it goes and so cannot
    produce a pinned stack.  A row is different: the knapsack decides its
    contents, and which items land at the ends is only known afterwards — so
    the answer is checked, and a pinned row costs a whole second selection
    rather than a nudge, because the row that is too wide may be the one the
    rest of the sheet was built around.
    """
    for _shelf_h, row_width, items in shelves:
        need_left, need_right = _row_edge_needs(ctx, items)
        if need_left + need_right > ctx.config.sheet_width - row_width + EPS:
            return True
    return False


def _seat(preferred: float, slack: float, need_low: float, need_high: float) -> float:
    """Where to start a row or stack: the soft preference, edge rule obeyed.

    ``preferred`` is what the soft cushion or front margin would like,
    ``slack`` is the total spare room on that axis, and the two ``need_*``
    are the HARD insets the two ends owe the sheet edges.  Returns the
    preference pulled into the legal window ``[need_low, slack - need_high]``,
    with the low end winning a collision so the answer is never below zero.
    With nothing to honour it returns ``preferred`` untouched, which is why
    every sheet that has no WDC against an edge is laid out exactly as it was
    before 2026-08-04.
    """
    return min(max(preferred, need_low), max(need_low, slack - need_high))


def _select_shelves(
    ctx: _Context,
    remaining: dict[str, int],
    seed: str | None,
    shelf_saving: bool,
    inflate_edges: bool = False,
):
    """Greedily stack shelves up the sheet; the loop of one sheet.

    Returns ``(shelves, used_height)`` where a shelf is
    ``(actual_height, row_width, items)``.

    The stack pays the FRONT and BACK edge insets as it goes rather than
    afterwards (2026-08-04), which makes the vertical half of the edge rule
    exact and constructive: only the first shelf can reach the front of the
    stack, so its inset is reserved the moment that shelf is committed, and
    only the last shelf can reach the back, so every candidate is asked to fit
    with the inset it would owe IF it turned out to be last.  Nothing is lost
    by charging that early — a shelf stacked on top of it needs strictly more
    room than the inset it releases — and nothing is wasted, because the
    charge is exactly what :func:`_stack_edge_needs` will ask for at placement
    time.  Compare ``inflate_edges``, which is the row (side-edge) half and
    cannot be exact for the same reason a knapsack is not a queue.
    """
    cfg = ctx.config
    avail = {pn: n for pn, n in sorted(remaining.items()) if n > 0}
    shelves = []  # (actual_height, row_width, items)
    used_height = 0.0
    front_reserve = 0.0

    while True:
        lead_gap = cfg.part_gap if shelves else 0.0
        space = cfg.sheet_height - front_reserve - used_height - lead_gap
        if space <= EPS:
            break

        shelf_seed = seed if avail.get(seed or "", 0) > 0 else None
        best = None
        best_key = None
        best_front = 0.0
        for shelf_h in ctx.height_candidates:
            if shelf_h > space + EPS:
                continue
            found = _best_shelf(ctx, avail, shelf_h, shelf_seed, inflate_edges)
            if found is None:
                continue
            actual_h, row_width, area, items, saving = found
            if actual_h > space + EPS or row_width > cfg.sheet_width + EPS:
                continue
            own_front = 0.0
            if ctx.has_edge_insets:
                # What this shelf would owe the sheet's two END edges: the
                # front only if it is the first, the back on the assumption
                # that it is last.
                front, back = _stack_edge_needs(ctx, [(actual_h, row_width, items)])
                own_front = 0.0 if shelves else front
                if own_front + actual_h + back > space + EPS:
                    continue
            density = area / (cfg.sheet_width * actual_h)
            cushioned = 1 if row_width <= cfg.sheet_width - 2 * cfg.edge_cushion + EPS else 0
            key = (
                round(density, 9),
                round(saving / (actual_h + cfg.part_gap), 6) if shelf_saving else 0,
                round(area, 6),
                cushioned,
                round(actual_h, 6),
            )
            if best_key is None or key > best_key:
                best_key = key
                best = found
                best_front = own_front
        if best is None:
            break

        actual_h, row_width, _area, items, _saving = best
        for pn, _pw, _ph, _rot in items:
            avail[pn] -= 1
        if not shelves:
            front_reserve = best_front
        shelves.append((actual_h, row_width, items))
        used_height += lead_gap + actual_h

    return shelves, used_height


def _build_sheet(
    ctx: _Context,
    remaining: dict[str, int],
    seed: str | None = None,
    shelf_saving: bool = True,
):
    """Greedily build one sheet layout from the remaining demand.

    Shelves are chosen by density (placed area / sheet_width * shelf height),
    i.e. the value extracted per inch of the sheet's scarce vertical run.
    Equally dense shelves are ranked by neediness per inch — at equal
    efficiency, serve the parts that would pack worst on their own first,
    while their partners are still in stock.  Then more area, then room for
    the soft edge cushion, then the taller shelf.

    ``seed``, when given, forces that part type into every shelf it can
    occupy, so the caller can generate one candidate sheet per part family.

    The sheet EDGE rule (2026-08-04) is enforced in two halves.  The front and
    back edges are handled inside :func:`_select_shelves`, exactly.  The two
    SIDE edges cannot be: which items land at a row's ends is a knapsack's
    verdict, not a decision, so the free-for-all row is checked afterwards
    (:func:`_any_row_pinned`) and, if any row put a stile end where the slot
    would cut off the side of the sheet, the whole selection is redone with
    every turned WDC charged its two insets on top of its width
    (:meth:`_Context.charged_width`).  That second pass cannot come back
    pinned — a charged row leaves at least the room its ends need — so one
    retry settles it, and charging the guilty part rather than shrinking the
    sheet keeps every other part exactly as placeable as it was.  On a job
    with no WDC on it nothing here fires and the layouts are identical to
    before.
    """
    cfg = ctx.config
    shelves, used_height = _select_shelves(ctx, remaining, seed, shelf_saving)
    if ctx.has_edge_insets and _any_row_pinned(ctx, shelves):
        shelves, used_height = _select_shelves(
            ctx, remaining, seed, shelf_saving, inflate_edges=True
        )

    layout = SheetLayout()
    counts: dict[str, int] = {}
    if not shelves:
        return layout, counts

    # Soft front-edge margin (2026-08-03 amendment): spend up to
    # front_margin of whatever vertical slack is left on the front (Y=0)
    # side of the stack; any slack beyond that goes to the back edge.
    # The WDC end insets (2026-08-04) are HARD and win where they collide.
    slack_v = cfg.sheet_height - used_height
    need_bottom, need_top = _stack_edge_needs(ctx, shelves)
    y = _seat(
        min(cfg.front_margin, max(0.0, slack_v)),
        max(0.0, slack_v),
        need_bottom,
        need_top,
    )

    for shelf_h, row_width, items in shelves:
        slack_h = cfg.sheet_width - row_width
        need_left, need_right = _row_edge_needs(ctx, items)
        x = _seat(
            min(cfg.edge_cushion, max(0.0, slack_h) / 2.0),
            max(0.0, slack_h),
            need_left,
            need_right,
        )
        for pn, pw, ph, rotated in items:
            # ``pw``/``ph`` are the RESERVED rectangle.  The real footprint
            # sits centred in it, which for a WDC leaves the slot's reach
            # free at both ends of its stiles and nowhere else.
            pad = ctx.pack_pad[pn]
            dx = pad if (pad and rotated) else 0.0
            dy = pad if (pad and not rotated) else 0.0
            layout.placements.append(
                Placement(
                    part_number=pn,
                    x=round(x + dx, 9),
                    y=round(y + dy, 9),
                    width=pw - 2.0 * dx,
                    height=ph - 2.0 * dy,
                    rotated=rotated,
                )
            )
            counts[pn] = counts.get(pn, 0) + 1
            x += pw + cfg.part_gap
        y += shelf_h + cfg.part_gap

    return layout, dict(sorted(counts.items()))


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def _run_strategy(ctx: _Context, demand: list[PartSpec], strategy: _Strategy):
    """Pattern loop for one strategy: build a sheet, stamp out its run, repeat."""
    cfg = ctx.config
    remaining = {s.part_number: s.qty for s in demand}
    patterns: list[tuple[SheetLayout, int]] = []

    # Every iteration places at least one part, so this cannot spin forever;
    # the guard just turns a hypothetical bug into a clear error.
    max_iterations = sum(remaining.values()) + 1
    while any(v > 0 for v in remaining.values()):
        if len(patterns) > max_iterations:
            raise NestingError("internal error: nesting failed to converge")

        # Generate one candidate sheet per leading part family (plus the
        # unseeded greedy sheet) and keep the best.  Seeding lets a part the
        # locally greedy packer would keep deferring get a sheet built around
        # it while its partner parts are still in stock.
        best = None
        best_key = None
        live = [pn for pn in ctx.seed_order if remaining.get(pn, 0) > 0]
        seeds: list[str | None] = [None]
        seeds.extend(live[: strategy.seed_breadth])
        for seed in seeds:
            layout, counts = _build_sheet(ctx, remaining, seed, strategy.shelf_saving)
            if not counts:
                continue
            # Stamp this picture out as many times as demand allows: this is
            # what turns a good layout into a run of identical sheets.
            run = max(1, min(remaining[pn] // n for pn, n in counts.items()))
            area = layout.used_area()
            # The sheet costs one sheet and retires ``solo`` inches of the
            # sheet length the job would otherwise still need, so the sheet
            # retiring the most is the greedy-optimal next pattern.
            solo = sum(ctx.solo_cost[pn] * n for pn, n in counts.items())
            key = (
                round(solo if strategy.sheet_key == "saving" else area, 6),
                round(area, 6),                      # 1. fewest sheets
                run,                                 # 3. repeated sheet pictures
                _cushion_score(layout, cfg),         # 4. keep off the sheet edges
                -len(layout.placements),
            )
            if best_key is None or key > best_key:
                best_key = key
                best = (layout, counts, run)

        if best is None:
            leftovers = ", ".join(f"{pn} x{n}" for pn, n in sorted(remaining.items()) if n > 0)
            raise NestingError(f"internal error: could not place remaining demand ({leftovers})")

        layout, counts, run = best
        for pn, n in counts.items():
            remaining[pn] -= n * run
        patterns.append((layout, run))

    # Group identical sheet pictures, keeping first-appearance order.
    grouped: list[tuple[SheetLayout, int]] = []
    index: dict[str, int] = {}
    for layout, run in patterns:
        key = layout.canonical()
        if key in index:
            i = index[key]
            grouped[i] = (grouped[i][0], grouped[i][1] + run)
        else:
            index[key] = len(grouped)
            grouped.append((layout, run))
    return grouped


def _pack(
    demand: list[PartSpec],
    cfg: NestingConfig,
    strategies: tuple[_Strategy, ...] = _STRATEGIES,
) -> NestingResult:
    """Footprint-pack an already-normalised demand list (spec 4a / 4c).

    Every strategy in ``strategies`` is run (until the deterministic work
    budget is spent) and the best result kept, ranked by the spec's
    objectives in order: fewest total sheets, then fewest unique sheet
    pictures, then the fewest parts sitting on a sheet edge.  Fully
    deterministic — the final tie-break is the layouts' canonical form.
    """
    ctx = _Context(demand, cfg)
    best_result = None
    best_key = None
    for strategy in strategies:
        if best_result is not None and ctx.dp_ops > _OPS_BUDGET:
            break
        grouped = _run_strategy(ctx, demand, strategy)
        result = NestingResult(grouped, sum(r for _, r in grouped), demand, cfg)
        key = (
            result.total_sheets,
            result.unique_sheet_count,
            result.edge_contact_parts(),
            "||".join(f"{layout.canonical()}#{run}" for layout, run in grouped),
        )
        if best_key is None or key < best_key:
            best_key = key
            best_result = result
    assert best_result is not None
    return best_result


#: Separator for the synthetic "host carrying an inner" part numbers fed to
#: the packer.  ASCII unit separator: not whitespace (so ``strip()`` leaves
#: it alone) and impossible in a real part number off a spreadsheet.
_SYNTHETIC_SEP = "\x1f"

#: How many "give this inner type up" assignments :func:`nest` packs
#: alongside the maximum-count one.
#:
#: Nesting a frame is not automatically a sheet-count win.  A B18 (18 x 30)
#: rides for free in the 18" of width left over beside a 30"-wide frame, so
#: hiding it inside a host's opening recovers nothing — while the host slot
#: it occupied could have swallowed a frame that does NOT pack for free.  On
#: the 7-21-26 order the flat-out maximum nests 92 frames and needs 41
#: sheets; giving up B18 nests 80 and needs 40.  Spec section 4 ranks fewest
#: sheets above most inside placements, so the packer decides.  The cap keeps
#: a many-family order bounded; the screening pass below keeps the cost of
#: the extra candidates down to a fraction of a pack each.
_INSIDE_PORTFOLIO = 8

#: Candidates are screened with the cheapest single strategy (about a tenth
#: of the cost of a full pack) and only the leaders get the full portfolio.
#: On the 7-21-26 order the screen predicts the full pack's sheet count
#: exactly for every candidate, so this is a pure speed-up; three finalists
#: leave room for it to be wrong by a sheet without changing the answer.
_SCREEN_STRATEGIES = _STRATEGIES[:1]
_INSIDE_FINALISTS = 3


def place_inner(
    host: Placement,
    host_spec: PartSpec,
    inner_spec: PartSpec,
    config: NestingConfig,
) -> Placement | None:
    """Centre ``inner_spec`` in one of ``host``'s openings, in sheet coords.

    Returns ``None`` when the inner does not fit any of the host's openings
    with the required clearance, so a GUI drag can reject the drop.  "The
    required clearance" is ``inner_clearance`` on three sides and, for a WDC
    inner, the reach of its T17 stile slot beyond the two ends of its
    stiles (:func:`faceframe_cnc.inside.end_clearance_for`) — a WDC nests
    inside W2742 and W2442, where the host's rail is what the slot would
    otherwise cut into.

    The host's own openings come from the geometry engine in frame-local
    coordinates; this maps them onto the sheet.  Rotation convention (used
    identically by :func:`validate_layouts`, and the one the NC post must
    follow): a rotated placement is the frame turned 90° COUNTER-CLOCKWISE,
    so a frame-local point ``(lx, ly)`` lands at sheet offset
    ``(ordered_height - ly, lx)`` inside the placed footprint — and
    ``ordered_height`` is just ``host.width`` once the host is rotated.
    """
    fit = best_fit(
        host_spec.part_number,
        host_spec.width,
        host_spec.height,
        inner_spec.width,
        inner_spec.height,
        config.inner_clearance,
        inner_spec.part_number,
    )
    if fit is None:
        return None

    if host.rotated:
        x = host.x + (host.width - fit.local_y - fit.local_height)
        y = host.y + fit.local_x
        width, height = fit.local_height, fit.local_width
    else:
        x = host.x + fit.local_x
        y = host.y + fit.local_y
        width, height = fit.local_width, fit.local_height

    return Placement(
        part_number=inner_spec.part_number,
        x=round(x, 9),
        y=round(y, 9),
        width=width,
        height=height,
        # The child's flag is absolute: turned inside a turned host is upright.
        rotated=(fit.inner_rotated != host.rotated),
    )


def _pack_demand_with_inners(demand: list[PartSpec], assignment):
    """Rewrite demand so each assigned host arrives carrying its passenger.

    Returns ``(pack_demand, decode)``.  A host type that takes an inner
    becomes a synthetic type ``HOST\\x1fINNER`` with the host's footprint and
    the assigned count; leftovers of that host, and of every inner not
    nested, stay under their real part numbers.  ``decode`` maps each
    synthetic name back to ``(host, inner)``.

    Nesting does not consume extra units: a W3012 sitting inside a WDC2436
    is one of the thirty W3012s the customer ordered, which is why the
    inner's own quantity is decremented here rather than added to.
    """
    by_name = {s.part_number: s for s in demand}
    remaining = {s.part_number: s.qty for s in demand}
    pack: list[PartSpec] = []
    decode: dict[str, tuple[str, str]] = {}

    for host, inner, count in assignment.pairs:
        if count <= 0:
            continue
        name = f"{host}{_SYNTHETIC_SEP}{inner}"
        spec = by_name[host]
        pack.append(PartSpec(name, spec.width, spec.height, count))
        decode[name] = (host, inner)
        remaining[host] -= count
        remaining[inner] -= count

    for pn in sorted(remaining):
        left = remaining[pn]
        if left < 0:
            raise NestingError(
                f"internal error: inside-nesting assignment used {-left} more "
                f"{pn} than were ordered"
            )
        if left > 0:
            spec = by_name[pn]
            pack.append(PartSpec(pn, spec.width, spec.height, left))

    pack.sort(key=lambda s: s.part_number)
    return pack, decode


def _expand_children(
    packed: NestingResult,
    decode: dict[str, tuple[str, str]],
    demand: list[PartSpec],
    cfg: NestingConfig,
    baseline_sheets: int | None,
) -> NestingResult:
    """Turn synthetic host types back into real hosts with real children.

    Run grouping already happened on the synthetic names, and the mapping
    ``HOST\\x1fINNER -> (host, inner)`` is injective, so two sheet pictures
    that grouped together really do carry the same inner in the same place
    and two that did not really are different pictures (spec 4c).
    """
    by_name = {s.part_number: s for s in demand}
    sheets: list[tuple[SheetLayout, int]] = []
    inside_placements = 0

    for layout, run in packed.unique_sheets:
        placements: list[Placement] = []
        for p in layout.placements:
            pair = decode.get(p.part_number)
            if pair is None:
                placements.append(
                    Placement(p.part_number, p.x, p.y, p.width, p.height, p.rotated, [])
                )
                continue
            host_name, inner_name = pair
            host = Placement(host_name, p.x, p.y, p.width, p.height, p.rotated, [])
            child = place_inner(host, by_name[host_name], by_name[inner_name], cfg)
            if child is None:
                raise NestingError(
                    f"internal error: {inner_name} was assigned to {host_name} but "
                    f"does not fit its openings with {cfg.inner_clearance} clearance"
                )
            host.children.append(child)
            inside_placements += run
            placements.append(host)
        sheets.append((SheetLayout(placements), run))

    return NestingResult(
        unique_sheets=sheets,
        total_sheets=packed.total_sheets,
        demand=demand,
        config=cfg,
        inside_placements=inside_placements,
        baseline_sheets=baseline_sheets,
    )


def nest(parts, config: NestingConfig | None = None) -> NestingResult:
    """Nest ``parts`` onto sheets and group identical sheet pictures.

    ``parts`` is an iterable of :class:`PartSpec`.  Raises
    :class:`NestingError` for invalid input or for any part that cannot fit
    on a sheet in either orientation.

    With ``config.inside_nesting`` off this is plain footprint packing.  With
    it on, the frame-inside-frame pairing phase runs first, and a small
    portfolio of assignments is packed so the result can be ranked by spec
    section 4's objectives in their stated order — fewest total sheets
    FIRST, then most inside placements (see ``_INSIDE_PORTFOLIO``).  The
    no-inside baseline is also computed (``config.inside_baseline``) so the
    summary can quote the delta, which is the app's headline value.
    """
    cfg = config if config is not None else NestingConfig()
    _check_config(cfg)
    demand = _normalize_demand(list(parts), cfg)
    if not demand:
        return NestingResult([], 0, [], cfg)

    if not cfg.inside_nesting:
        return _pack(demand, cfg)

    baseline_sheets = _pack(demand, cfg).total_sheets if cfg.inside_baseline else None

    unbarred = assign_inners(demand, cfg.inner_clearance)
    if unbarred.total == 0:
        result = _pack(demand, cfg)
        result.baseline_sheets = baseline_sheets
        return result

    # Candidate assignments: the flat-out maximum, plus one per inner type
    # that gives that type up.  Ranked by how many frames each type nests, so
    # a truncated portfolio still covers the types that matter most.
    ranked = sorted(unbarred.inners_used().items(), key=lambda kv: (-kv[1], kv[0]))
    bars: list[frozenset] = [frozenset()]
    bars.extend(frozenset([name]) for name, _n in ranked[:_INSIDE_PORTFOLIO])

    screened = []
    seen: set[tuple] = set()
    for bar in bars:
        assignment = unbarred if not bar else assign_inners(demand, cfg.inner_clearance, bar)
        if assignment.total == 0:
            continue
        pack_demand, decode = _pack_demand_with_inners(demand, assignment)
        signature = tuple((s.part_number, s.qty) for s in pack_demand)
        if signature in seen:
            continue
        seen.add(signature)
        estimate = _pack(pack_demand, cfg, _SCREEN_STRATEGIES).total_sheets
        screened.append(
            (estimate, -assignment.total, sorted(bar), pack_demand, decode)
        )
    screened.sort(key=lambda entry: entry[:3])

    best = None
    best_key = None
    for _estimate, _neg_total, _bar, pack_demand, decode in screened[:_INSIDE_FINALISTS]:
        candidate = _expand_children(
            _pack(pack_demand, cfg), decode, demand, cfg, baseline_sheets
        )
        key = (
            candidate.total_sheets,               # 1. minimise total sheets
            -candidate.inside_placements,         # 2. maximise inside placements
            candidate.unique_sheet_count,         # 3. repeated sheet pictures
            candidate.edge_contact_parts(),       # 4. keep off the sheet edges
            "||".join(f"{l.canonical()}#{r}" for l, r in candidate.unique_sheets),
        )
        if best_key is None or key < best_key:
            best_key = key
            best = candidate

    if best is None:
        result = _pack(demand, cfg)
        result.baseline_sheets = baseline_sheets
        return result
    return best


# --------------------------------------------------------------------------
# Independent validator
# --------------------------------------------------------------------------


#: Depth at which the validator stops descending and reports a malformed
#: layout instead.  Real nests are depth 1, or 2 with ``inside_recursion``;
#: anything past this is a cycle in the placement tree, not a cabinet.
_MAX_NEST_DEPTH = 8


def _flatten(placements: list) -> tuple[list[tuple[Placement, int]], bool]:
    """Depth-first ``[(placement, parent_index)]`` plus a "too deep" flag.

    Top-level placements get parent index -1.  The depth cap means a
    self-referential ``children`` list degrades into a reported problem
    rather than a stack overflow inside the NC verifier.
    """
    nodes: list[tuple[Placement, int]] = []
    too_deep = False

    def walk(items: list, parent_index: int, depth: int) -> None:
        nonlocal too_deep
        if depth > _MAX_NEST_DEPTH:
            too_deep = True
            return
        for p in items:
            index = len(nodes)
            nodes.append((p, parent_index))
            walk(p.children, index, depth + 1)

    walk(placements, -1, 0)
    return nodes, too_deep


def _related(nodes: list[tuple[Placement, int]], a_idx: int, b_idx: int) -> bool:
    """True when one of the two placements is an ancestor of the other."""
    for lower, upper in ((a_idx, b_idx), (b_idx, a_idx)):
        cursor = nodes[lower][1]
        while cursor >= 0:
            if cursor == upper:
                return True
            cursor = nodes[cursor][1]
    return False


def _ordered_dims(p: Placement, ordered: dict[str, PartSpec]) -> tuple[float, float]:
    """The part's as-ordered ``(width, height)``.

    Prefers the order line, falling back to undoing the placement's own
    rotation.  Any disagreement between the two is already reported by the
    "dimensions must never be altered" check, so this never hides one.
    """
    spec = ordered.get(p.part_number)
    if spec is not None:
        return spec.width, spec.height
    return (p.height, p.width) if p.rotated else (p.width, p.height)


def _sheet_openings(parent: Placement, ordered: dict[str, PartSpec]):
    """The parent's routed openings as ``(x, y, w, h)`` in SHEET coordinates.

    Recomputed from the geometry engine, never from anything the packer
    stored.  Returns ``(rects, error)``; ``error`` is a human-readable reason
    when the part has no usable openings at all.

    Rotation convention (shared with :func:`place_inner`): a rotated
    placement is the frame turned 90° counter-clockwise, so frame-local
    ``(lx, ly, w, h)`` becomes ``(ordered_h - ly - h, lx, h, w)`` inside the
    placed footprint, and ``ordered_h`` equals the placed width.
    """
    width, height = _ordered_dims(parent, ordered)
    geom = compute_geometry(parent.part_number, width, height)
    if geom.errors:
        return [], f"geometry is invalid ({geom.errors[0]})"
    if not geom.openings:
        return [], "the part has no routed openings"

    rects = []
    for opening in geom.openings:
        if parent.rotated:
            rects.append(
                (
                    parent.x + (parent.width - opening.y - opening.height),
                    parent.y + opening.x,
                    opening.height,
                    opening.width,
                )
            )
        else:
            rects.append(
                (
                    parent.x + opening.x,
                    parent.y + opening.y,
                    opening.width,
                    opening.height,
                )
            )
    return rects, None


def _check_containment(
    sheet: int,
    child: Placement,
    parent: Placement,
    ordered: dict[str, PartSpec],
    config: NestingConfig,
) -> list[str]:
    """Child footprint + clearance must lie wholly inside ONE parent opening.

    The clearance is ``inner_clearance`` on every side except the two ends
    of a WDC child's stiles, which need the reach of its T17 slot: the cut
    runs past the frame, and what is past the frame here is the host's own
    rail.
    """
    clearance = config.inner_clearance
    ends = end_clearance_for(child.part_number, clearance)
    if wdc_slot_axis_is_height(child.rotated):
        need_x, need_y = clearance, ends
    else:
        need_x, need_y = ends, clearance

    rects, error = _sheet_openings(parent, ordered)
    if error is not None:
        return [
            f"sheet {sheet}: {child.part_number} is nested inside "
            f"{parent.part_number} but {error}"
        ]

    need_x0 = child.x - need_x
    need_y0 = child.y - need_y
    need_x1 = child.x + child.width + need_x
    need_y1 = child.y + child.height + need_y

    best_shortfall = None
    for ox, oy, ow, oh in rects:
        shortfall = max(
            ox - need_x0,
            oy - need_y0,
            need_x1 - (ox + ow),
            need_y1 - (oy + oh),
        )
        if shortfall <= EPS:
            return []
        if best_shortfall is None or shortfall < best_shortfall:
            best_shortfall = shortfall

    wanted = (
        f"{clearance} clearance"
        if ends <= clearance
        else (
            f"{clearance} clearance, and {ends} beyond its stile ends for the "
            f"T17 slot"
        )
    )
    return [
        f"sheet {sheet}: {child.part_number} @({child.x:.4f},{child.y:.4f}) "
        f"{child.width:.4f}x{child.height:.4f} does not fit inside any single "
        f"opening of {parent.part_number} @({parent.x:.4f},{parent.y:.4f}) with "
        f"{wanted} — closest opening is short by "
        f"{best_shortfall:.4f}"
    ]


def validate_layouts(result: NestingResult, config: NestingConfig) -> list[str]:
    """Re-check a :class:`NestingResult` from scratch; [] means valid.

    Deliberately independent of the packing code above — it recomputes every
    piece of geometry from the placement coordinates rather than trusting any
    flag or cached value the packer left behind, because this routine becomes
    part of the NC safety verifier.

    Checks, applied to nested (frame-inside-frame) placements as well as
    top-level ones: parts fully on the sheet, minimum edge-to-edge part gap,
    placed dimensions matching the ordered dimensions (allowing only a
    rotation swap), run quantities summing to the physical sheet count, and
    every ordered part placed exactly the ordered number of times.

    Frame-inside-frame adds (spec 4b):

    *   every child's footprint plus ``inner_clearance`` on all four sides
        must lie wholly within ONE opening of its parent — its own setting
        since 2026-08-03, no longer an alias of ``part_gap``.  The openings
        are recomputed
        from :func:`~faceframe_cnc.geometry.compute_geometry` using the
        parent's part number and ORDERED dimensions and then transformed by
        the parent's own placement — nothing the packer recorded about the
        child is trusted.  This one check is what rejects a child on a part
        that has no openings, a child straddling a cross bar or rail, and a
        child that overhangs its opening;
    *   children of children only when ``config.inside_recursion`` is on;
    *   the ordinary part-gap rule between any two placements that are not
        ancestor and descendant of each other — so siblings sharing an
        opening are checked, while a child is not reported for "overlapping"
        the host it is by definition inside.

    The WDC 45-degree stile slot adds one DIRECTIONAL rule (2026-08-03), on
    top of and independent of everything the packer does to satisfy it:
    beyond the two ends of a WDC frame's stiles, both the gap to any other
    part and the distance to the sheet edge must clear the slot's full
    reach, :func:`slot_end_clearance`.  Everywhere else a WDC is an ordinary
    part.  This is the one place a HARD rule applies to the sheet edge — the
    edge cushion is a preference, so a part sitting on the sheet edge is
    never otherwise reported here.
    """
    problems: list[str] = []

    sw = config.sheet_width
    sh = config.sheet_height
    gap = config.part_gap

    if not (math.isfinite(sw) and sw > 0) or not (math.isfinite(sh) and sh > 0):
        return [f"invalid sheet size {sw}x{sh}"]

    # --- run quantities -----------------------------------------------
    run_total = 0
    for i, (_layout, run) in enumerate(result.unique_sheets, start=1):
        if not isinstance(run, int) or isinstance(run, bool):
            problems.append(f"sheet {i}: run quantity {run!r} is not an integer")
            continue
        if run < 1:
            problems.append(f"sheet {i}: run quantity {run} must be at least 1")
        run_total += run
    if run_total != result.total_sheets:
        problems.append(
            f"run quantities sum to {run_total} but total_sheets is {result.total_sheets}"
        )

    # --- demand, needed below for the parents' ORDERED dimensions -------
    ordered: dict[str, PartSpec] = {}
    for spec in result.demand:
        if spec.part_number in ordered:
            problems.append(f"demand lists {spec.part_number} more than once")
            continue
        ordered[spec.part_number] = spec

    # --- per-sheet geometry -------------------------------------------
    placed: dict[str, int] = {}
    for i, (layout, run) in enumerate(result.unique_sheets, start=1):
        if not layout.placements:
            problems.append(f"sheet {i}: empty sheet layout")
        nodes, cycle = _flatten(layout.placements)
        if cycle:
            problems.append(
                f"sheet {i}: nesting is deeper than {_MAX_NEST_DEPTH} levels — "
                f"a placement is probably its own descendant"
            )

        multiplier = run if isinstance(run, int) and not isinstance(run, bool) and run > 0 else 0
        for p, parent_index in nodes:
            placed[p.part_number] = placed.get(p.part_number, 0) + multiplier

        # -- each placement on its own ---------------------------------
        sane = [True] * len(nodes)
        for index, (p, _parent_index) in enumerate(nodes):
            if not (math.isfinite(p.x) and math.isfinite(p.y)):
                problems.append(f"sheet {i}: {p.part_number} has non-finite position")
                sane[index] = False
                continue
            if not (math.isfinite(p.width) and p.width > 0) or not (
                math.isfinite(p.height) and p.height > 0
            ):
                problems.append(
                    f"sheet {i}: {p.part_number} has non-positive placed size "
                    f"{p.width}x{p.height}"
                )
                sane[index] = False
                continue
            if p.x < -EPS or p.y < -EPS or p.x + p.width > sw + EPS or p.y + p.height > sh + EPS:
                problems.append(
                    f"sheet {i}: {p.part_number} is off the sheet — occupies "
                    f"x[{p.x:.4f}, {p.x + p.width:.4f}] y[{p.y:.4f}, {p.y + p.height:.4f}] "
                    f"on a {sw}x{sh} sheet"
                )

            # A WDC's slot cuts past its stile ends, so those ends need
            # room against the SHEET EDGE too - the only hard edge rule.
            ends = slot_end_clearance(p.part_number, config)
            if ends > gap:
                if wdc_slot_axis_is_height(p.rotated):
                    low, high, limit, axis = p.y, p.y + p.height, sh, "y"
                else:
                    low, high, limit, axis = p.x, p.x + p.width, sw, "x"
                if low < ends - EPS or high > limit - ends + EPS:
                    problems.append(
                        f"sheet {i}: {p.part_number} @({p.x:.4f},{p.y:.4f}) has its "
                        f"stile ends at {axis}[{low:.4f}, {high:.4f}], within "
                        f"{ends} of the edge of the {sw}x{sh} sheet — its 45-degree "
                        f"T17 slot cuts that far past each end, so the cut would "
                        f"run off the sheet"
                    )

            spec = ordered.get(p.part_number)
            if spec is not None:
                same = (
                    abs(p.width - spec.width) <= EPS and abs(p.height - spec.height) <= EPS
                )
                swapped = (
                    abs(p.width - spec.height) <= EPS and abs(p.height - spec.width) <= EPS
                )
                if not ((swapped and p.rotated) or (same and not p.rotated)):
                    problems.append(
                        f"sheet {i}: {p.part_number} placed as {p.width}x{p.height} "
                        f"(rotated={p.rotated}) but was ordered {spec.width}x{spec.height} — "
                        f"part dimensions must never be altered"
                    )

        # -- pairwise gap, skipping ancestor/descendant pairs -----------
        for a_idx in range(len(nodes)):
            if not sane[a_idx]:
                continue
            a = nodes[a_idx][0]
            for b_idx in range(a_idx + 1, len(nodes)):
                if not sane[b_idx]:
                    continue
                if _related(nodes, a_idx, b_idx):
                    # A child sits inside its host's footprint by design; the
                    # containment check below is what polices that pair.
                    continue
                b = nodes[b_idx][0]
                # Two parts are far enough apart when EITHER axis separates
                # them by what that axis demands.  The demand is normally
                # part_gap both ways; a WDC raises it along the axis its
                # stiles - and so its slot - run.
                a_need = clearance_needs(a, config)
                b_need = clearance_needs(b, config)
                need_x = max(a_need[0], b_need[0])
                need_y = max(a_need[1], b_need[1])
                clear_x = max(a.x, b.x) - min(a.x + a.width, b.x + b.width)
                clear_y = max(a.y, b.y) - min(a.y + a.height, b.y + b.height)
                if clear_x < need_x - EPS and clear_y < need_y - EPS:
                    if max(clear_x, clear_y) < 0:
                        detail = "footprints overlap"
                    elif clear_x >= clear_y:
                        detail = f"clearance {clear_x:.4f} < required {need_x:g} in x"
                    else:
                        detail = f"clearance {clear_y:.4f} < required {need_y:g} in y"
                    if max(need_x, need_y) > gap:
                        detail += (
                            " (a WDC frame's 45-degree stile slot cuts past its "
                            "stile ends)"
                        )
                    problems.append(
                        f"sheet {i}: gap violation between {a.part_number} "
                        f"@({a.x:.4f},{a.y:.4f}) and {b.part_number} "
                        f"@({b.x:.4f},{b.y:.4f}) — {detail}"
                    )

        # -- frame-inside-frame containment (spec 4b) -------------------
        for index, (child, parent_index) in enumerate(nodes):
            if parent_index < 0:
                continue
            parent, grandparent_index = nodes[parent_index]
            if grandparent_index >= 0 and not config.inside_recursion:
                problems.append(
                    f"sheet {i}: {child.part_number} is nested inside "
                    f"{parent.part_number}, which is itself nested inside "
                    f"{nodes[grandparent_index][0].part_number} — recursive "
                    f"frame-inside-frame is disabled (inside_recursion=False)"
                )
            if not sane[index] or not sane[parent_index]:
                continue
            problems.extend(
                _check_containment(i, child, parent, ordered, config)
            )

    for pn in sorted(set(ordered) | set(placed)):
        want = ordered[pn].qty if pn in ordered else 0
        got = placed.get(pn, 0)
        if pn not in ordered:
            problems.append(f"{pn}: {got} placed but the part was never ordered")
        elif got < want:
            problems.append(f"{pn}: only {got} of {want} ordered parts were placed")
        elif got > want:
            problems.append(f"{pn}: {got} placed but only {want} were ordered")

    return problems
