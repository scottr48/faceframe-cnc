"""Sheet-nesting optimizer — footprint packing (spec sections 4a and 4c).

Milestone 2 scope: pack whole faceframe footprints (outside W x H) onto
49 x 97 sheets, honouring the 0.375" edge-to-edge part gap, allowing 90
degree rotation of every part, and grouping identical sheet pictures into
runs.  Frame-inside-frame placement (spec 4b) is Milestone 3 and is NOT
implemented here; :class:`Placement` carries an empty ``children`` list so
that milestone can hang inner frames off a host without a schema change.

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

__all__ = [
    "EPS",
    "NestingError",
    "NestingConfig",
    "PartSpec",
    "Placement",
    "SheetLayout",
    "NestingResult",
    "nest",
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

#: Grid resolutions tried when quantising widths for the knapsack.  Inch
#: dimensions plus a 3/8" gap are exact at 8; the larger values cover odd
#: user-entered sizes.  Anything not representable falls back to the largest
#: scale with conservative rounding (item widths up, capacity down), which
#: can only ever under-fill a shelf, never overfill it — at worst a row is
#: judged 1/16" wider than it is.  Capping the grid at 16 keeps the DP small
#: for orders with awkward fractional dimensions.
_SCALE_CANDIDATES = (1, 2, 4, 8, 16)


class NestingError(ValueError):
    """A nesting request that cannot be satisfied (bad input or a part that
    does not fit on a sheet in either orientation)."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class NestingConfig:
    """Sheet and spacing settings.  All values are inches and configurable."""

    sheet_width: float = 49.0
    sheet_height: float = 97.0
    #: Minimum edge-to-edge distance between any two parts on a sheet.
    part_gap: float = 0.375
    #: SOFT preference: keep parts this far from the sheet edges when the
    #: packing allows it.  Parts may go all the way to the edge when they
    #: must; edge contact is scored as a last resort, never as an error.
    edge_cushion: float = 0.5

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

    ``children`` is reserved for Milestone 3 (frame-inside-frame): inner
    frames placed inside this part's routed opening.  Milestone 2 always
    leaves it empty.
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

    @property
    def unique_sheet_count(self) -> int:
        return len(self.unique_sheets)

    @property
    def total_parts(self) -> int:
        return sum(spec.qty for spec in self.demand)

    @property
    def total_part_area(self) -> float:
        return sum(spec.area * spec.qty for spec in self.demand)

    @property
    def area_lower_bound_sheets(self) -> int:
        """Absolute floor on sheet count: total part area / sheet area."""
        sheet_area = self.config.sheet_area
        if sheet_area <= 0:
            return 0
        return math.ceil(self.total_part_area / sheet_area - 1e-9)

    def fill_fraction(self, layout: SheetLayout) -> float:
        area = self.config.sheet_area
        return layout.used_area() / area if area > 0 else 0.0

    @property
    def overall_fill_fraction(self) -> float:
        denom = self.total_sheets * self.config.sheet_area
        return self.total_part_area / denom if denom > 0 else 0.0

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
        for i, (layout, run) in enumerate(self.unique_sheets, start=1):
            contents = ", ".join(
                f"{n}x{pn}" for pn, n in sorted(layout.part_counts().items())
            )
            lines.append(
                f"  sheet {i:>2}: run={run:<3} fill={self.fill_fraction(layout) * 100:5.1f}% "
                f"parts={len(layout):<2} [{contents}]"
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
    if not (math.isfinite(config.edge_cushion) and config.edge_cushion >= 0):
        problems.append(f"edge_cushion must be >= 0 and finite, got {config.edge_cushion!r}")
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
        upright = spec.width <= sw + EPS and spec.height <= sh + EPS
        turned = spec.height <= sw + EPS and spec.width <= sh + EPS
        if not (upright or turned):
            raise NestingError(
                f"{spec.part_number}: {spec.width}x{spec.height} does not fit on a "
                f"{sw}x{sh} sheet in either orientation "
                f"(rotated it would be {spec.height}x{spec.width}); "
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


def _solo_cost(width: float, height: float, config: NestingConfig) -> float:
    """Inches of sheet length one part eats when nested only with its clones.

    Closed form: in the better of the two orientations, a shelf holds
    ``per_row`` copies and costs ``h + gap`` of sheet length, so one part
    costs ``(h + gap) / per_row``.  A 30x30 frame costs its whole 30.375"
    shelf; an 18x30 costs half of one (two fit across 49").

    A row's ``sum(solo_cost) - (row_height + gap)`` is therefore the sheet
    length that row saves versus packing its parts separately — the quantity
    the optimizer is really trying to maximise, since total sheet length
    divided by 97" is the sheet count.
    """
    gap = config.part_gap
    best = None
    for w, h in ((width, height), (height, width)):
        if w > config.sheet_width + EPS or h > config.sheet_height + EPS:
            continue
        per_row = int((config.sheet_width + gap + EPS) // (w + gap))
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

        dims = []
        for s in demand:
            dims.extend((s.width, s.height))
        self.scale = _pick_scale(dims + [config.part_gap, config.sheet_width])

        # Conservative quantisation: capacity rounded down, item widths up.
        self.capacity = math.floor(
            (config.sheet_width + config.part_gap) * self.scale + 1e-9
        )
        # Distinct candidate shelf heights, tallest first (deterministic).
        self.height_candidates = sorted({d for d in dims}, reverse=True)
        self._shelf_cache: dict[tuple, tuple | None] = {}
        #: Knapsack DP element-operations spent so far.  A deterministic
        #: stand-in for elapsed time (see ``_OPS_BUDGET``).
        self.dp_ops = 0

        self.solo_cost = {
            s.part_number: _solo_cost(s.width, s.height, config) for s in demand
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

    def width_units(self, placed_width: float) -> int:
        return math.ceil((placed_width + self.config.part_gap) * self.scale - 1e-9)


def _orient_for_shelf(width: float, height: float, shelf_h: float):
    """Best orientation for a shelf of height ``shelf_h``.

    Returns ``(placed_width, placed_height, rotated)`` for the orientation
    whose placed height is the largest value <= ``shelf_h``, or ``None`` when
    neither orientation fits.  That orientation is also the narrower one, so
    it dominates on both height fill and width consumption.
    """
    best = None
    if height <= shelf_h + EPS:
        best = (width, height, False)
    if width <= shelf_h + EPS and (best is None or width > best[1] + EPS):
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


def _best_shelf(ctx: _Context, avail: dict[str, int], shelf_h: float, seed: str | None = None):
    """Best row of parts for a shelf of height ``shelf_h``.

    When ``seed`` is given, one part of that type is forced into the row (and
    the row is rejected if that type cannot be placed at this shelf height).
    Seeding is how the pattern loop explores sheets built around each part
    family instead of only the locally densest one.

    Returns ``(actual_height, row_width, area, items, saving)`` where
    ``items`` is a deterministic left-to-right list of
    ``(part_number, w, h, rotated)``, or ``None`` when nothing fits.
    """
    cfg = ctx.config
    capacity = ctx.capacity
    forced = None
    if seed is not None:
        if avail.get(seed, 0) <= 0:
            return None
        spec = ctx.specs[seed]
        oriented = _orient_for_shelf(spec.width, spec.height, shelf_h)
        if oriented is None:
            return None
        pw, ph, rotated = oriented
        wu = ctx.width_units(pw)
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
        spec = ctx.specs[pn]
        oriented = _orient_for_shelf(spec.width, spec.height, shelf_h)
        if oriented is None:
            continue
        pw, ph, rotated = oriented
        wu = ctx.width_units(pw)
        if wu <= 0 or wu > capacity:
            continue
        # No row can hold more than capacity // wu of anything; capping here
        # shrinks the knapsack and makes the cache key hit far more often.
        n_eff = min(n, capacity // wu)
        if n_eff <= 0:
            continue
        value = (
            _area_units(pw, ph) * _AREA_WEIGHT
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
    area = sum(it[1] * it[2] for it in items)
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
    """
    cfg = ctx.config
    avail = {pn: n for pn, n in sorted(remaining.items()) if n > 0}
    shelves = []  # (actual_height, items)
    used_height = 0.0

    while True:
        lead_gap = cfg.part_gap if shelves else 0.0
        space = cfg.sheet_height - used_height - lead_gap
        if space <= EPS:
            break

        shelf_seed = seed if avail.get(seed or "", 0) > 0 else None
        best = None
        best_key = None
        for shelf_h in ctx.height_candidates:
            if shelf_h > space + EPS:
                continue
            found = _best_shelf(ctx, avail, shelf_h, shelf_seed)
            if found is None:
                continue
            actual_h, row_width, area, items, saving = found
            if actual_h > space + EPS or row_width > cfg.sheet_width + EPS:
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
        if best is None:
            break

        actual_h, row_width, _area, items, _saving = best
        for pn, _pw, _ph, _rot in items:
            avail[pn] -= 1
        shelves.append((actual_h, row_width, items))
        used_height += lead_gap + actual_h

    layout = SheetLayout()
    counts: dict[str, int] = {}
    if not shelves:
        return layout, counts

    # Soft edge cushion: spend whatever vertical slack is left on a bottom
    # margin (up to the cushion, balanced against the top margin).
    slack_v = cfg.sheet_height - used_height
    y = min(cfg.edge_cushion, max(0.0, slack_v) / 2.0)

    for shelf_h, row_width, items in shelves:
        slack_h = cfg.sheet_width - row_width
        x = min(cfg.edge_cushion, max(0.0, slack_h) / 2.0)
        for pn, pw, ph, rotated in items:
            layout.placements.append(
                Placement(
                    part_number=pn,
                    x=round(x, 9),
                    y=round(y, 9),
                    width=pw,
                    height=ph,
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


def nest(parts, config: NestingConfig | None = None) -> NestingResult:
    """Nest ``parts`` onto sheets and group identical sheet pictures.

    ``parts`` is an iterable of :class:`PartSpec`.  Raises
    :class:`NestingError` for invalid input or for any part that cannot fit
    on a sheet in either orientation.

    Every strategy in ``_STRATEGIES`` is run (until the deterministic work
    budget is spent) and the best result kept, ranked by the spec's
    objectives in order: fewest total sheets, then fewest unique sheet
    pictures, then the fewest parts sitting on a sheet edge.  Fully
    deterministic — the final tie-break is the layouts' canonical form.
    """
    cfg = config if config is not None else NestingConfig()
    _check_config(cfg)
    demand = _normalize_demand(list(parts), cfg)
    if not demand:
        return NestingResult([], 0, [], cfg)

    ctx = _Context(demand, cfg)
    best_result = None
    best_key = None
    for strategy in _STRATEGIES:
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


# --------------------------------------------------------------------------
# Independent validator
# --------------------------------------------------------------------------


def validate_layouts(result: NestingResult, config: NestingConfig) -> list[str]:
    """Re-check a :class:`NestingResult` from scratch; [] means valid.

    Deliberately independent of the packing code above — it recomputes every
    piece of geometry from the placement coordinates rather than trusting any
    flag or cached value the packer left behind, because this routine becomes
    part of the NC safety verifier.

    Checks: parts fully on the sheet, minimum edge-to-edge part gap, placed
    dimensions matching the ordered dimensions (allowing only a rotation
    swap), run quantities summing to the physical sheet count, and every
    ordered part placed exactly the ordered number of times.

    The soft edge cushion is a preference, not a rule, so a part sitting on
    the sheet edge is never reported here.  Child (frame-inside-frame)
    placements are Milestone 3 and are not inspected.
    """
    problems: list[str] = []

    sw = config.sheet_width
    sh = config.sheet_height
    gap = config.part_gap
    half = gap / 2.0

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

    # --- per-sheet geometry -------------------------------------------
    for i, (layout, run) in enumerate(result.unique_sheets, start=1):
        placements = list(layout.placements)
        if not placements:
            problems.append(f"sheet {i}: empty sheet layout")
        for p in placements:
            if not (math.isfinite(p.x) and math.isfinite(p.y)):
                problems.append(f"sheet {i}: {p.part_number} has non-finite position")
                continue
            if not (math.isfinite(p.width) and p.width > 0) or not (
                math.isfinite(p.height) and p.height > 0
            ):
                problems.append(
                    f"sheet {i}: {p.part_number} has non-positive placed size "
                    f"{p.width}x{p.height}"
                )
                continue
            if p.x < -EPS or p.y < -EPS or p.x + p.width > sw + EPS or p.y + p.height > sh + EPS:
                problems.append(
                    f"sheet {i}: {p.part_number} is off the sheet — occupies "
                    f"x[{p.x:.4f}, {p.x + p.width:.4f}] y[{p.y:.4f}, {p.y + p.height:.4f}] "
                    f"on a {sw}x{sh} sheet"
                )

        for a_idx in range(len(placements)):
            a = placements[a_idx]
            for b_idx in range(a_idx + 1, len(placements)):
                b = placements[b_idx]
                overlap_x = min(a.x + a.width + half, b.x + b.width + half) - max(
                    a.x - half, b.x - half
                )
                overlap_y = min(a.y + a.height + half, b.y + b.height + half) - max(
                    a.y - half, b.y - half
                )
                if overlap_x > EPS and overlap_y > EPS:
                    clear_x = max(a.x, b.x) - min(a.x + a.width, b.x + b.width)
                    clear_y = max(a.y, b.y) - min(a.y + a.height, b.y + b.height)
                    clearance = max(clear_x, clear_y)
                    if clearance < 0:
                        detail = "footprints overlap"
                    else:
                        detail = f"clearance {clearance:.4f} < required {gap}"
                    problems.append(
                        f"sheet {i}: gap violation between {a.part_number} "
                        f"@({a.x:.4f},{a.y:.4f}) and {b.part_number} "
                        f"@({b.x:.4f},{b.y:.4f}) — {detail}"
                    )

    # --- demand accounting --------------------------------------------
    ordered: dict[str, PartSpec] = {}
    for spec in result.demand:
        if spec.part_number in ordered:
            problems.append(f"demand lists {spec.part_number} more than once")
            continue
        ordered[spec.part_number] = spec

    placed: dict[str, int] = {}
    for i, (layout, run) in enumerate(result.unique_sheets, start=1):
        multiplier = run if isinstance(run, int) and not isinstance(run, bool) and run > 0 else 0
        for p in layout.placements:
            placed[p.part_number] = placed.get(p.part_number, 0) + multiplier
            spec = ordered.get(p.part_number)
            if spec is None:
                continue
            same = (
                abs(p.width - spec.width) <= EPS and abs(p.height - spec.height) <= EPS
            )
            swapped = (
                abs(p.width - spec.height) <= EPS and abs(p.height - spec.width) <= EPS
            )
            ok = (swapped and p.rotated) or (same and not p.rotated)
            if not ok:
                problems.append(
                    f"sheet {i}: {p.part_number} placed as {p.width}x{p.height} "
                    f"(rotated={p.rotated}) but was ordered {spec.width}x{spec.height} — "
                    f"part dimensions must never be altered"
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
