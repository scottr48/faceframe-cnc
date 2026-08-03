"""Frame-inside-frame pairing (spec section 4b) — Milestone 3.

This module answers two questions and nothing else:

1.  **Eligibility** — can part type *I* sit inside one of the routed
    openings of part type *H*?  The rule (spec 4b, hard minimum) is
    ``inner + 2 * clearance`` must fit the opening in at least one of the
    inner's two orientations, with ``clearance = 0.375"`` per side.  The
    openings come from :mod:`faceframe_cnc.geometry`, so WDC's 2" stiles
    (2026-08-03 amendment) and the sub-divided openings of BASE / 3DB
    frames are handled for free — every opening of every frame family is a
    candidate host opening.

2.  **Assignment** — given the ordered quantities, which inner instances go
    into which host instances?  Objective 2 of spec section 4 is "maximise
    frame-inside-frame placements", so this is an exact integer
    transportation problem on part TYPES (a handful of nodes), solved as a
    min-cost max-flow.  Max flow gives the most inners placed; the cost
    function is purely a deterministic tie-break that implements the spec's
    preferences ("prefer the smallest inner", then "prefer larger residual
    clearance").

Placement geometry (centring the inner in the opening) is returned as an
:class:`InnerFit` in HOST-LOCAL coordinates; turning that into sheet
coordinates needs the host's placement and therefore lives in
:mod:`faceframe_cnc.nesting`.

Scope notes:

*   **One inner per host.**  The optimizer never puts two frames in one
    opening; the spec reserves that for a manual GUI drag.  Every host
    instance therefore has capacity 1.
*   **Depth 1 only.**  The optimizer emits hosts at the top level with a
    single inner each, so an instance that is used as an inner can never
    also be a host (that would be recursion).  This module always enforces
    that disjointness, independently of ``inside_recursion`` — the config
    flag relaxes the *validator* so hand-built depth-2 layouts from the GUI
    are legal, it does not ask the optimizer to build them.

Stdlib only, fully deterministic.  No dependency on the nesting module:
callers pass anything with ``part_number`` / ``width`` / ``height`` / ``qty``
attributes, which keeps the import graph one-way
(``nesting -> inside -> geometry``).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .geometry import compute_geometry

__all__ = [
    "DEFAULT_CLEARANCE",
    "UPRIGHT",
    "ROTATED",
    "InnerFit",
    "InsideAssignment",
    "host_openings",
    "candidate_fits",
    "best_fit",
    "fitting_orientations",
    "eligibility_table",
    "assign_inners",
]

#: Hard minimum clearance between an inner frame and the opening it sits in,
#: on every side (spec 4b).  Equal to the sheet-level part gap; callers pass
#: ``NestingConfig.part_gap`` so the two can never drift apart.
DEFAULT_CLEARANCE = 0.375

#: Orientation labels used by :func:`eligibility_table`.
UPRIGHT = "upright"
ROTATED = "rotated"

_EPS = 1e-9


@dataclass(frozen=True)
class InnerFit:
    """One legal way to centre an inner frame in one opening of a host.

    All coordinates are HOST-LOCAL (origin at the host frame's lower-left
    corner, x across the host's ordered width, y up its ordered height) and
    already describe the inner CENTRED in the opening, which is what spec 4b
    asks for: the clearance rule is a validation floor, not a target.

    ``inner_rotated`` is relative to the host — the inner is turned 90° in
    host-local space.  Its absolute orientation on the sheet also depends on
    whether the host itself was rotated by the packer.
    """

    opening_index: int
    opening_label: str
    inner_rotated: bool
    local_x: float
    local_y: float
    local_width: float
    local_height: float
    #: Smallest residual gap between the inner and the opening edge.  Always
    #: >= the required clearance; bigger is better (spec objective 4, and
    #: more residual web means better vacuum hold on the host).
    clearance: float


@dataclass(frozen=True)
class InsideAssignment:
    """Which inner TYPE goes into which host TYPE, and how many times.

    ``pairs`` is sorted by ``(host, inner)`` so the result is stable, and
    ``total`` is the number of frames recovered from footprint packing.
    """

    pairs: tuple[tuple[str, str, int], ...]
    total: int

    def hosts_used(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for host, _inner, count in self.pairs:
            out[host] = out.get(host, 0) + count
        return out

    def inners_used(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _host, inner, count in self.pairs:
            out[inner] = out.get(inner, 0) + count
        return out


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


def host_openings(part_number: str, width: float, height: float) -> list:
    """Routed openings of a frame, or ``[]`` when the geometry is invalid.

    A frame with no usable openings can never host, which is exactly what an
    empty list means to every caller here.
    """
    geom = compute_geometry(part_number, width, height)
    if geom.errors:
        return []
    return list(geom.openings)


def _orientations(inner_w: float, inner_h: float):
    """The inner's distinct orientations: ``(local_w, local_h, rotated)``.

    A square inner has only one — reporting the 90° turn as a separate
    option would make the eligibility table ambiguous and the tie-breaks
    non-deterministic for no geometric gain.
    """
    yield (inner_w, inner_h, False)
    if abs(inner_w - inner_h) > _EPS:
        yield (inner_h, inner_w, True)


def candidate_fits(
    openings,
    inner_w: float,
    inner_h: float,
    clearance: float = DEFAULT_CLEARANCE,
) -> list[InnerFit]:
    """Every legal centred placement of an inner across ``openings``.

    The fit rule is the spec's, verbatim: ``inner + 2 * clearance`` must fit
    the opening on both axes.  Nothing here ever alters the inner's
    dimensions — rotation only swaps which one runs along the host's x axis.
    """
    fits: list[InnerFit] = []
    for index, opening in enumerate(openings):
        for local_w, local_h, rotated in _orientations(inner_w, inner_h):
            if local_w + 2.0 * clearance > opening.width + _EPS:
                continue
            if local_h + 2.0 * clearance > opening.height + _EPS:
                continue
            slack_x = (opening.width - local_w) / 2.0
            slack_y = (opening.height - local_h) / 2.0
            fits.append(
                InnerFit(
                    opening_index=index,
                    opening_label=opening.label,
                    inner_rotated=rotated,
                    local_x=opening.x + slack_x,
                    local_y=opening.y + slack_y,
                    local_width=local_w,
                    local_height=local_h,
                    clearance=min(slack_x, slack_y),
                )
            )
    return fits


def _pick(fits: list[InnerFit]) -> InnerFit | None:
    """Best of several legal fits: most residual clearance, then stable.

    Ties fall back to the top-down opening order and then to the unrotated
    orientation, so the choice never depends on dict or set iteration order.
    """
    if not fits:
        return None
    return min(
        fits,
        key=lambda f: (-round(f.clearance, 9), f.opening_index, f.inner_rotated),
    )


def best_fit(
    host_part_number: str,
    host_w: float,
    host_h: float,
    inner_w: float,
    inner_h: float,
    clearance: float = DEFAULT_CLEARANCE,
) -> InnerFit | None:
    """Where an inner of ``inner_w x inner_h`` should sit inside a host frame.

    Returns ``None`` when it does not fit any of the host's openings in
    either orientation.
    """
    return _pick(
        candidate_fits(
            host_openings(host_part_number, host_w, host_h),
            inner_w,
            inner_h,
            clearance,
        )
    )


def fitting_orientations(
    host_part_number: str,
    host_w: float,
    host_h: float,
    inner_w: float,
    inner_h: float,
    clearance: float = DEFAULT_CLEARANCE,
) -> tuple[str, ...]:
    """Orientation labels in which the inner fits the host, ``()`` if none.

    Ordered ``upright`` before ``rotated`` so the result is directly
    comparable in tests and stable in reports.
    """
    fits = candidate_fits(
        host_openings(host_part_number, host_w, host_h), inner_w, inner_h, clearance
    )
    found = {f.inner_rotated for f in fits}
    labels = []
    if False in found:
        labels.append(UPRIGHT)
    if True in found:
        labels.append(ROTATED)
    return tuple(labels)


def eligibility_table(
    specs, clearance: float = DEFAULT_CLEARANCE
) -> dict[str, dict[str, tuple[str, ...]]]:
    """The spec 4b host -> inner candidate table for one order.

    Every part number in ``specs`` appears as a key, mapping to the inners
    that fit inside it (an empty dict means "hosts nothing from this
    order").  Quantities are ignored — this is a pure type-level question.
    """
    by_name = {s.part_number: s for s in specs}
    table: dict[str, dict[str, tuple[str, ...]]] = {}
    for host in sorted(by_name):
        h = by_name[host]
        openings = host_openings(host, h.width, h.height)
        row: dict[str, tuple[str, ...]] = {}
        for inner in sorted(by_name):
            i = by_name[inner]
            fits = candidate_fits(openings, i.width, i.height, clearance)
            found = {f.inner_rotated for f in fits}
            if not found:
                continue
            labels = []
            if False in found:
                labels.append(UPRIGHT)
            if True in found:
                labels.append(ROTATED)
            row[inner] = tuple(labels)
        table[host] = row
    return table


# --------------------------------------------------------------------------
# Assignment (exact integer min-cost max-flow on the type graph)
# --------------------------------------------------------------------------


class _MinCostFlow:
    """Successive-shortest-path min-cost max-flow (SPFA, integer costs).

    The graph is tiny — one node per inner type plus one per host type, so
    six by four on the test order — which is why an exact solver is worth
    having here instead of a greedy rule that could silently leave frames on
    the sheet.  Deterministic: the queue order is fixed by insertion order
    and every cost is a distinct integer (see :func:`_edge_cost`).
    """

    def __init__(self, node_count: int):
        self.node_count = node_count
        # Each edge is [to, residual_capacity, cost, index_of_reverse_edge].
        self.graph: list[list[list]] = [[] for _ in range(node_count)]

    def add_edge(self, src: int, dst: int, capacity: int, cost: int) -> list:
        forward = [dst, capacity, cost, len(self.graph[dst])]
        backward = [src, 0, -cost, len(self.graph[src])]
        self.graph[src].append(forward)
        self.graph[dst].append(backward)
        return forward

    def run(self, source: int, sink: int) -> tuple[int, int]:
        total_flow = 0
        total_cost = 0
        inf = float("inf")
        while True:
            dist = [inf] * self.node_count
            in_queue = [False] * self.node_count
            prev_node = [-1] * self.node_count
            prev_edge = [-1] * self.node_count
            dist[source] = 0
            queue = deque([source])
            in_queue[source] = True
            while queue:
                node = queue.popleft()
                in_queue[node] = False
                base = dist[node]
                for edge_index, edge in enumerate(self.graph[node]):
                    dst, capacity, cost, _rev = edge
                    if capacity <= 0:
                        continue
                    candidate = base + cost
                    if candidate < dist[dst]:
                        dist[dst] = candidate
                        prev_node[dst] = node
                        prev_edge[dst] = edge_index
                        if not in_queue[dst]:
                            in_queue[dst] = True
                            queue.append(dst)
            if dist[sink] == inf:
                return total_flow, total_cost

            # Push as much as this shortest path can carry.
            push = None
            node = sink
            while node != source:
                edge = self.graph[prev_node[node]][prev_edge[node]]
                push = edge[1] if push is None else min(push, edge[1])
                node = prev_node[node]
            assert push is not None and push > 0
            node = sink
            while node != source:
                edge = self.graph[prev_node[node]][prev_edge[node]]
                edge[1] -= push
                self.graph[node][edge[3]][1] += push
                node = prev_node[node]
            total_flow += push
            total_cost += push * dist[sink]


#: Cost weights.  The three terms never interfere: the clearance term is at
#: most 1e6 and the tie-break at most 1e4, so each is strictly dominated by
#: the one above it.  Costs are integers, so the flow is exact.
_AREA_WEIGHT = 10**11
_CLEAR_WEIGHT = 10**4
_CLEAR_BASE = 1_000_000


def _edge_cost(inner_area: float, clearance: float, tie_break: int) -> int:
    """Lexicographic preference, packed into one integer.

    1.  Smallest inner first (spec: "when several inners fit a host, prefer
        the smallest" — bigger residual web, better vacuum hold).  Note this
        term only bites when not every inner can be placed: when the flow
        seats all of them, each inner contributes its area exactly once no
        matter where it goes, so the total is constant and term 2 decides.
    2.  Largest residual clearance (spec objective 4).
    3.  Edge index, purely so no two edges can ever tie.
    """
    area_term = int(round(inner_area * 1000.0))
    clear_term = max(0, _CLEAR_BASE - int(round(clearance * 1000.0)))
    return area_term * _AREA_WEIGHT + clear_term * _CLEAR_WEIGHT + tie_break


#: How many role splits (see ``assign_inners``) may be enumerated before the
#: search degrades from exhaustive to coordinate ascent.  Every real order
#: has at most one or two dual-role types, so the exhaustive branch is what
#: actually runs; the cap only stops a pathological input from hanging.
_SPLIT_BUDGET = 20_000


def _solve_transport(edges, by_name, inner_cap, host_cap):
    """Min-cost max-flow for one fixed set of per-type capacities.

    Returns ``(flow, cost, {(host, inner): count})``.  Max flow first (spec
    objective 2: as many inners nested as possible), min cost only as a
    tie-break between equally large assignments.
    """
    inner_names = sorted({inner for inner, _host, _fit in edges if inner_cap.get(inner, 0) > 0})
    host_names = sorted({host for _inner, host, _fit in edges if host_cap.get(host, 0) > 0})
    if not inner_names or not host_names:
        return 0, 0, {}

    inner_index = {name: 1 + n for n, name in enumerate(inner_names)}
    host_index = {name: 1 + len(inner_names) + n for n, name in enumerate(host_names)}
    source = 0
    sink = 1 + len(inner_names) + len(host_names)

    flow = _MinCostFlow(sink + 1)
    for name in inner_names:
        flow.add_edge(source, inner_index[name], inner_cap[name], 0)
    for name in host_names:
        flow.add_edge(host_index[name], sink, host_cap[name], 0)

    # (inner, host, live_edge, original_capacity) — the solver mutates an
    # edge's residual capacity in place, so the flow it carried is the
    # original capacity minus what is left.
    tracked: list[tuple[str, str, list, int]] = []
    for tie_break, (inner, host, fit) in enumerate(edges):
        if inner not in inner_index or host not in host_index:
            continue
        i = by_name[inner]
        capacity = min(inner_cap[inner], host_cap[host])
        cost = _edge_cost(i.width * i.height, fit.clearance, tie_break)
        handle = flow.add_edge(inner_index[inner], host_index[host], capacity, cost)
        tracked.append((inner, host, handle, capacity))

    total_flow, total_cost = flow.run(source, sink)

    assigned: dict[tuple[str, str], int] = {}
    for inner, host, handle, capacity in tracked:
        used = capacity - handle[1]
        if used > 0:
            assigned[(host, inner)] = assigned.get((host, inner), 0) + used
    return total_flow, total_cost, assigned


def _role_split_ranges(edges, qty, dual_role):
    """For each dual-role type, the useful values of "units kept as inners".

    Capping the range at the number of host slots that actually accept the
    type keeps the exhaustive search small: WDC2436 can only ever be an inner
    inside W2742 and W2442, so its 30 units give 21 splits, not 31.
    """
    ranges = {}
    for name in dual_role:
        accepting = sum(
            qty[host] for inner, host, _fit in edges if inner == name
        )
        ranges[name] = list(range(0, min(qty[name], accepting) + 1))
    return ranges


def _score(candidate):
    """Rank two solver outcomes: more inners nested, then lower cost."""
    total_flow, total_cost, _assigned = candidate
    return (-total_flow, total_cost)


def assign_inners(
    specs,
    clearance: float = DEFAULT_CLEARANCE,
    blocked_inners=(),
) -> InsideAssignment:
    """Assign inner instances to host instances, maximising the count.

    ``specs`` is an iterable of part types with ``part_number`` / ``width`` /
    ``height`` / ``qty``.  Each host instance takes at most one inner (spec
    4b: multiples only via a manual drag), so the host side's capacity is its
    ordered quantity.

    ``blocked_inners`` bars those part numbers from the inner role (they can
    still host).  Nesting a frame is not always a sheet-count win — a part
    that would otherwise ride free in the wasted width beside a wide frame
    costs nothing at the top level — so :func:`~faceframe_cnc.nesting.nest`
    packs a few assignments that each bar one type and keeps whichever needs
    the fewest sheets, spec section 4's first objective.

    **Dual-role types.**  A type can be eligible as an inner AND as a host —
    on the 7-21-26 order WDC2436 (18 x 36 after the amendment) is exactly
    that: small enough to sit inside W2742/W2442, and with a 14 x 33 opening
    that takes a rotated W3012.  Since the optimizer only ever emits depth-1
    layouts, one physical frame cannot do both jobs, and that coupling is not
    expressible as an arc capacity in a plain flow network.  It is, however,
    trivially expressible once the split is FIXED, so the solver enumerates
    every way of dividing each dual-role type's quantity between the two
    roles and keeps the best — which is exhaustive, hence exact, whenever the
    number of combinations stays inside ``_SPLIT_BUDGET`` (always, in
    practice: one dual-role type here means 21 solves of a 10-node graph).
    Beyond the budget it falls back to coordinate ascent, which stays
    feasible but is no longer provably optimal.

    Returns an empty assignment when nothing fits inside anything.
    """
    by_name = {s.part_number: s for s in specs}
    qty = {name: int(s.qty) for name, s in by_name.items()}
    names = sorted(by_name)
    blocked = frozenset(blocked_inners)

    # --- build the type graph ------------------------------------------
    edges: list[tuple[str, str, InnerFit]] = []  # (inner, host, fit)
    for host in names:
        if qty[host] <= 0:
            continue
        h = by_name[host]
        openings = host_openings(host, h.width, h.height)
        if not openings:
            continue
        for inner in names:
            if qty[inner] <= 0 or inner in blocked:
                continue
            i = by_name[inner]
            fit = _pick(candidate_fits(openings, i.width, i.height, clearance))
            if fit is None:
                continue
            edges.append((inner, host, fit))

    if not edges:
        return InsideAssignment((), 0)

    inner_side = {inner for inner, _host, _fit in edges}
    host_side = {host for _inner, host, _fit in edges}
    dual_role = sorted(inner_side & host_side)

    base_inner_cap = {name: qty[name] for name in inner_side}
    base_host_cap = {name: qty[name] for name in host_side}

    def solve(split: dict[str, int]):
        inner_cap = dict(base_inner_cap)
        host_cap = dict(base_host_cap)
        for name, kept_as_inner in split.items():
            inner_cap[name] = kept_as_inner
            host_cap[name] = qty[name] - kept_as_inner
        return _solve_transport(edges, by_name, inner_cap, host_cap)

    if not dual_role:
        best = solve({})
    else:
        ranges = _role_split_ranges(edges, qty, dual_role)
        combinations = 1
        for name in dual_role:
            combinations *= len(ranges[name])

        if combinations <= _SPLIT_BUDGET:
            best = None
            best_key = None
            counters = [0] * len(dual_role)
            while True:
                split = {
                    name: ranges[name][counters[n]]
                    for n, name in enumerate(dual_role)
                }
                candidate = solve(split)
                key = _score(candidate)
                if best_key is None or key < best_key:
                    best_key = key
                    best = candidate
                # Odometer over the split ranges, least-significant last.
                position = len(counters) - 1
                while position >= 0:
                    counters[position] += 1
                    if counters[position] < len(ranges[dual_role[position]]):
                        break
                    counters[position] = 0
                    position -= 1
                if position < 0:
                    break
        else:
            # Coordinate ascent from "never used as an inner", which is the
            # conservative starting point: hosting is what recovers a
            # footprint for a frame that also has an opening to offer.
            split = {name: 0 for name in dual_role}
            best = solve(split)
            best_key = _score(best)
            for _pass in range(2):
                improved = False
                for name in dual_role:
                    for value in ranges[name]:
                        if value == split[name]:
                            continue
                        trial = dict(split)
                        trial[name] = value
                        candidate = solve(trial)
                        key = _score(candidate)
                        if key < best_key:
                            best_key = key
                            best = candidate
                            split = trial
                            improved = True
                if not improved:
                    break

    _flow, _cost, assigned = best
    pairs = tuple(
        (host, inner, count) for (host, inner), count in sorted(assigned.items())
    )
    total = sum(count for _h, _i, count in pairs)

    # Belt and braces: the capacities above already make dual roles
    # impossible, but this is safety-critical output, so prove it.
    hosts_used: dict[str, int] = {}
    inners_used: dict[str, int] = {}
    for host, inner, count in pairs:
        hosts_used[host] = hosts_used.get(host, 0) + count
        inners_used[inner] = inners_used.get(inner, 0) + count
    for name in sorted(set(hosts_used) | set(inners_used)):
        used = hosts_used.get(name, 0) + inners_used.get(name, 0)
        if used > qty.get(name, 0):
            raise AssertionError(
                f"inside-nesting assignment over-commits {name}: "
                f"{hosts_used.get(name, 0)} hosting + {inners_used.get(name, 0)} "
                f"nested > {qty.get(name, 0)} ordered"
            )

    return InsideAssignment(pairs, total)
