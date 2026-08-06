"""Where the holding tabs are: pure, deterministic profile geometry.

The 2026-08-05 amendment (Scott, job R0805,
``CLAUDE_CODE_PROMPT_Tabs_and_Groove_Clamp.md`` §3a/§3b) in one module.  Two
frames came off the sheet broken because every opening dropout was cut free
before the perimeter was touched, so the T11 finished the job on a loose MDF
ring; the fix is that **nothing is fully separated** until a final release
section — everything stays attached by tabs, and every pass that cuts below
the tab top rises over them instead of cutting through.

This module answers exactly one question — WHERE the tabs on a profile are —
and answers it as pure geometry: no coordinates go in or come out, no G-code,
no feed, no wall clock and no randomness (same input, same tabs, always).  The
emitter (:mod:`~.generator`) turns a zone into motion; the release section and
the verifier's hold invariant are milestone 3 and re-derive what they need
themselves.  Nothing here imports :mod:`~.verifier`, and the verifier must
never import this: it is the independent authority on what the generator
emitted, which it cannot be if it shares the generator's arithmetic.

Zones live on the FINISHED profile, not on a pass's path
--------------------------------------------------------
A :class:`TabZone` names a side of the profile rectangle and a signed offset
from that side's MIDPOINT — never an X or a Y.  That is what makes spec §3b's
"one tab block spans both kerfs" true by construction: the T11 opening pass
(tool centre 0.1975 inside the finished line) and the T12 detail pass (0.1
inside it) run on two different rectangles, but both rectangles are concentric
with the finished opening, so the same midpoint-relative offset is the same
place on the profile for both.  :func:`zone_point` is the mapping, and it is
exact for any zone that stays on one side — which the corner clearance
guarantees, since it is 2" against pass offsets of at most 0.2.

The travel direction is the sign
--------------------------------
Every loop in this post is traversed counter-clockwise
(:func:`~.generator.loop_points`), so each side has one fixed travel
direction: bottom +X, right +Y, top -X, left -Y.  A zone's ``centre`` is
measured along THAT direction, which makes the lead-in span asymmetric in the
same natural way the machine sees it (the ramp arrives from -, the overshoot
leaves towards +) and makes "the zones of this move, in the order the tool
meets them" a plain sort.

The placement rule (spec §3a), in one place
-------------------------------------------
Per side, with ``f`` the worst-case tab footprint among the passes that will
lift (0.75 plus a ramp at each end, ≈1.774 for the Z-0.006 through pass):

1.  A tab centre may lie in ``[-L/2 + c + f/2, +L/2 - c - f/2]``, where ``c``
    is :attr:`~.model.TabSpec.corner_clearance` — that is the ≥2" corner rule
    applied to the whole footprint, ramps included.
2.  On the pass's ENTRY side that interval loses the lead-in/lead-out span
    (:func:`entry_exclusion`: from one ramp length before the entry point to
    one tool diameter plus one ramp length after it, all measured with the same
    numbers :func:`~.generator.loop_points` uses — the ramp is ≈4", never a
    hardcoded 4).  A tab is never shrunk to fit beside it; it is RELOCATED, by
    the interval it may live in being split in two.
3.  Each usable interval gets ``1 + ceil(span / (max_gap + f))`` tabs, capped
    at the ``1 + floor(span / f)`` that physically fit, spread evenly with the
    outermost pair sitting exactly on the interval's ends.  The count formula
    is the spacing target read backwards: with ``n`` tabs spread over ``span``
    the free run between two footprints is ``span/(n-1) - f``, so the formula
    is the smallest ``n`` that keeps that at or under
    :attr:`~.model.TabSpec.max_gap`.  Pushing the outermost tabs onto the
    clearance limit is deliberate: the corners are the stiffest place to hold a
    frame, and for a given count it makes every interior gap as short as it can
    be.
4.  On a single-interval side (any side but the entry one) the result is
    therefore symmetric about the side's midpoint, and the minimum of two tabs
    per side falls out of the formula for every side long enough to hold two.
    On the entry side symmetry yields to the lead-in, which is asymmetric; each
    of the two intervals is placed by the same rule, so the side still gets its
    two.

Degenerate sides — the fallback chain (PROPOSAL, flagged for review)
--------------------------------------------------------------------
Scott's numbers cover the sides the shop actually cuts.  For sides too short
for them this module falls back, in order:

1.  fewer tabs than the spacing target asks for, down to two (the capacity cap
    in rule 3);
2.  ONE tab, centred, still clear of the corners by ``c``;
3.  the corner clearance gives way — one tab per usable interval, placed as
    close to the side's midpoint as the lead-in leaves room for, which is the
    position with the most corner clearance still available.  The tab itself is
    never shrunk, because a shrunk tab is not a tab.  Two sides need this: one
    shorter than ``2c + f`` (1.8" to 5.8" for the through pass), which gets a
    single centred tab; and an ENTRY side shorter than about 15.6", where the
    ≈8.4" lead-in span plus two 2" clearances do not fit — a 12x30 part's 12"
    right edge, for instance, which the shop really does cut.
4.  ZERO tabs on that side — it is shorter than one tab's footprint, or it is
    an entry side the lead-in span swallows whole (under about 6").  The
    profile's other three sides hold the piece.

Steps 3 and 4 are this module's proposal, not Scott's ruling — his numbers do
not say what to give up when they cannot all be met, and this gives up the
corner clearance first and the tab last.  They are tested as such
(``tests/test_tabs.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PERIMETER,
    SIDES,
    Box,
    PassSpec,
    PostConfig,
    TabSpec,
    ToolSpec,
)

__all__ = [
    "TabZone",
    "opening_cuts",
    "perimeter_cuts",
    "lifting_cuts",
    "lifts_over_tabs",
    "tab_ramp",
    "tab_footprint",
    "worst_footprint",
    "entry_exclusion",
    "place_tabs",
    "side_length",
    "travel_offset",
    "zone_point",
    "assign_zones",
    "entry_conflict",
    "release_ramp",
    "release_span",
    "travel_sequence",
]

#: Geometric tolerance, the emitter's own 1e-9 (coordinates in this post are
#: exact multiples of 0.0001, so nothing real is ever this close to nothing).
EPS = 1e-9


@dataclass(frozen=True)
class TabZone:
    """One tab, as a position on a profile rectangle's side.

    ``side`` is one of :data:`~.model.SIDES` in the loop's own vocabulary, and
    ``centre`` is the signed offset of the tab's middle from that side's
    midpoint, positive in the side's counter-clockwise travel direction (module
    docstring).  ``length`` is the run at full tab height — the ramps at either
    end are NOT in it, because how long they are depends on how deep the pass
    cutting past this tab goes (:func:`tab_ramp`).

    Deliberately not here: any X or Y, any Z, any feed, and which profile the
    zone belongs to.  The plan holds zones under the profile's own key
    (:attr:`~.model.CutPlan.tabs`), and the emitter supplies the rest from the
    post table.
    """

    side: str
    centre: float
    length: float

    def span(self, ramp: float = 0.0) -> tuple[float, float]:
        """``(start, end)`` in travel offsets, with ``ramp`` at each end.

        ``ramp`` 0 gives the full-height span; :func:`tab_ramp` for a pass
        gives that pass's whole footprint.
        """
        reach = self.length / 2.0 + ramp
        return (self.centre - reach, self.centre + reach)


# --------------------------------------------------------------------------
# Which passes cut a profile, and how far a tab reaches on each
# --------------------------------------------------------------------------


def opening_cuts(config: PostConfig) -> tuple[tuple[PassSpec, ToolSpec], ...]:
    """The ``(pass, tool)`` pairs that cut an opening profile: T11 then T12.

    Every configured T11 depth pass, then the one T12 detail pass.  A generated
    sheet's T11 runs two since the 2026-08-05 max-bite amendment
    (:func:`~.from_layout.generated_opening_passes`) and the references run one;
    only the passes that reach BELOW the tab top get a vote in placement
    (:func:`lifting_cuts`), and the shallow rung of the ladder — Z0.45, well
    above the 0.25 tab top — is not one of them, so the tabs on an opening are
    where they were before the ladder existed.
    """
    pairs = [(spec, SECTION_OPENINGS) for spec in config.openings_passes]
    pairs.append((config.detail_pass, SECTION_DETAIL))
    return tuple(
        (spec, config.tools[section]) for spec, section in pairs if section in config.tools
    )


def perimeter_cuts(config: PostConfig) -> tuple[tuple[PassSpec, ToolSpec], ...]:
    """The ``(pass, tool)`` pairs that cut a part footprint: every depth pass.

    One pair on a generated sheet since the 2026-08-05 amendment
    (:func:`~.from_layout.generated_post_passes`), two on the references.
    """
    tool = config.tools.get(SECTION_PERIMETER)
    if tool is None:  # pragma: no cover - a post table with no perimeter tool
        return ()
    return tuple((spec, tool) for spec in config.perimeter_passes)


def lifts_over_tabs(z_cut: float, config: PostConfig) -> bool:
    """Does a pass at ``z_cut`` have to rise over the tabs? (spec §3b)

    Only if it cuts BELOW the tab top: the T13 groove floor (0.55) and both
    T17 slot passes (0.4062 / 0.3125) are above 0.25 and so leave a tab
    untouched — which is also why a groove may cross a tab zone with no special
    handling at all (spec §3a, last bullet).
    """
    return z_cut < config.tabs.top_z - EPS


def lifting_cuts(
    cuts, config: PostConfig
) -> tuple[tuple[PassSpec, ToolSpec], ...]:
    """The subset of ``cuts`` that will lift over a tab.

    A pass at or above the tab top neither forms nor damages one, so it has no
    say in where the tabs go — which is what makes an air cut
    (:func:`~.job.dry_run_config`, every depth mirrored above the stock) place
    no tabs and lift over none.
    """
    return tuple((spec, tool) for spec, tool in cuts if lifts_over_tabs(spec.z_cut, config))


def tab_ramp(z_cut: float, config: PostConfig) -> float:
    """Length along the path of ONE ramp on/off a tab, for a pass at ``z_cut``.

    The post's own measured :attr:`~.model.PostConfig.ramp_ratio` (2 of travel
    per 1 of Z, R710101N 112/167/222), so no new geometry is invented: from
    Z-0.006 up to a 0.25 tab top is 0.512, from Z0.15 it is 0.2.
    """
    return (config.tabs.top_z - z_cut) * config.ramp_ratio


def tab_footprint(z_cut: float, config: PostConfig) -> float:
    """How much of the path one tab consumes on a pass at ``z_cut``.

    The full-height length plus a ramp at each end: ≈1.774 for the Z-0.006
    through pass, 1.15 for the Z0.15 opening pass.
    """
    return config.tabs.length + 2.0 * tab_ramp(z_cut, config)


def worst_footprint(cuts, config: PostConfig) -> float:
    """The largest footprint among ``cuts`` — what placement must clear.

    Placement is one answer for a whole profile while the passes crossing it
    are several depths, so it reserves room for the deepest one; a shallower
    pass then lifts inside a space already proven big enough.
    """
    return max(tab_footprint(spec.z_cut, config) for spec, _ in cuts)


def entry_exclusion(cuts, config: PostConfig) -> tuple[float, float]:
    """The travel offsets a tab may not touch on a loop's ENTRY side.

    Built from the same three numbers :func:`~.generator.loop_points` builds
    the loop out of, worst case over ``cuts``, so it cannot drift from the
    motion it is protecting:

    *   the lead-in ramp arrives at the entry point from
        ``(approach_z - z_cut) * ramp_ratio`` before it — ≈4.012 for the
        Z-0.006 through pass, which is the "perimeter lead-ins are ~4 inches
        long" of spec §3a and the reason that number is derived here rather
        than written down;
    *   the loop closes on the entry point and overshoots one tool DIAMETER
        past it;
    *   the lead-out ramp then climbs for another ramp length.

    The lateral lead (:attr:`~.model.PassSpec.lateral_lead`) is deliberately
    absent: it moves the ramp sideways off the profile line, not along it, so
    it changes nothing about which part of the side is spoken for.
    """
    ramp = max((config.approach_z - spec.z_cut) * config.ramp_ratio for spec, _ in cuts)
    over = max(tool.diameter for _, tool in cuts)
    return (-ramp, over + ramp)


# --------------------------------------------------------------------------
# Placement (spec §3a)
# --------------------------------------------------------------------------


def side_length(box: Box, side: str) -> float:
    """The length of ``side`` of ``box`` along its travel direction."""
    if side in ("bottom", "top"):
        return box.width
    if side in ("right", "left"):
        return box.height
    raise ValueError(f"unknown side {side!r}")


def place_tabs(
    box: Box,
    entry_side: str,
    cuts,
    config: PostConfig,
) -> tuple[TabZone, ...]:
    """Where the tabs on one profile go — the whole of spec §3a.

    ``box`` is the FINISHED profile rectangle (a part's footprint for a
    perimeter, the finished opening for an opening), never a pass's offset
    path.  ``cuts`` are the ``(pass, tool)`` pairs that will cut it
    (:func:`opening_cuts` / :func:`perimeter_cuts`), and only the ones that cut
    below the tab top get a vote (:func:`lifting_cuts`) — an air-cut table
    therefore places nothing.  ``entry_side`` is the side those passes lead in
    on, whose lead-in span is excluded.

    Pure and deterministic: floats in, the same zones out, every time, in a
    fixed order (:data:`~.model.SIDES`, then travel order along each side).

    A note for whoever wires this into a plan (milestone 3): all the passes
    crossing one profile must lead in on the SAME side, or the zones this
    returns are clear of one pass's lead-in and not another's.  The emitter
    refuses that case loudly rather than emitting a tab a ramp has already cut
    through (:func:`entry_conflict`).
    """
    if entry_side not in SIDES:
        raise ValueError(f"unknown entry side {entry_side!r}")
    lifting = lifting_cuts(cuts, config)
    if not lifting:
        return ()
    footprint = worst_footprint(lifting, config)
    exclusion = entry_exclusion(lifting, config)
    # A zone has to fit on EVERY lifting pass's path, and where the offset
    # points inward that path is shorter than the profile: an opening's T11 path
    # is 0.395 shorter per side than the finished opening.  It only ever matters
    # on a side small enough to be in the fallback chain — the 2" corner
    # clearance is ten times the largest offset — but a zone running off the end
    # of one pass's side would be a refusal at emission time, so it is ruled out
    # here instead.
    inward = min([0.0, *(spec.offset for spec, _ in lifting)])
    zones: list[TabZone] = []
    for side in SIDES:
        length = side_length(box, side)
        zones.extend(
            _place_side(
                side,
                length,
                length + 2.0 * inward,
                footprint,
                exclusion if side == entry_side else None,
                config.tabs,
            )
        )
    return tuple(zones)


def _place_side(
    side: str,
    length: float,
    path_length: float,
    footprint: float,
    exclusion: tuple[float, float] | None,
    spec: TabSpec,
) -> list[TabZone]:
    """One side's tabs, by the rule in the module docstring."""
    reach = footprint / 2.0
    half = length / 2.0
    #: The outermost centre whose whole footprint still fits on the shortest
    #: path along this side, with something left over: a tab whose ramp ended
    #: exactly on the corner would leave the loop a zero-length move to make.
    limit = path_length / 2.0 - reach
    if limit < EPS:
        return []  # fallback step 4: not even one tab fits on this side
    lo = max(-half + spec.corner_clearance + reach, -limit)
    hi = min(half - spec.corner_clearance - reach, limit)
    out: list[TabZone] = []
    if hi >= lo - EPS:
        for start, end in _usable_intervals(lo, hi, exclusion, reach):
            out.extend(
                TabZone(side=side, centre=centre, length=spec.length)
                for centre in _spread(start, end, footprint, spec.max_gap)
            )
    if out:
        return out
    # Fallback step 3: nothing fits with the full corner clearance, so the
    # clearance gives way and the tab does not.  One tab per interval the
    # lead-in leaves, at the allowed position CLOSEST TO THE MIDPOINT, which is
    # the one with the most corner clearance left; on a side with no lead-in
    # that is the centre, which is Scott's own "a side too short to fit two gets
    # one, centered".
    for start, end in _usable_intervals(-limit, limit, exclusion, reach):
        out.append(
            TabZone(side=side, centre=min(max(0.0, start), end), length=spec.length)
        )
    return out


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Do two closed intervals share more than a touching endpoint?"""
    return a[0] < b[1] - EPS and b[0] < a[1] - EPS


def _usable_intervals(
    lo: float,
    hi: float,
    exclusion: tuple[float, float] | None,
    reach: float,
) -> list[tuple[float, float]]:
    """``[lo, hi]`` minus the centres whose footprint would hit ``exclusion``.

    One interval on every side but the entry one; up to two there, which is
    what "relocate the tab, don't shrink it" (spec §3a) means in practice.
    """
    if exclusion is None:
        return [(lo, hi)]
    blocked = (exclusion[0] - reach, exclusion[1] + reach)
    out = []
    for start, end in ((lo, min(hi, blocked[0])), (max(lo, blocked[1]), hi)):
        if end >= start - EPS:
            out.append((start, end))
    return out


def _spread(
    start: float, end: float, footprint: float, max_gap: float
) -> list[float]:
    """Tab centres in ``[start, end]``: how many, and where (rule 3)."""
    span = end - start
    if span <= EPS:
        return [(start + end) / 2.0]
    by_gap = 1 + math.ceil(span / (max_gap + footprint) - EPS)
    capacity = 1 + int(math.floor(span / footprint + EPS))
    count = max(1, min(by_gap, capacity))
    if count == 1:
        return [(start + end) / 2.0]
    step = span / (count - 1)
    return [start + i * step for i in range(count)]


# --------------------------------------------------------------------------
# Mapping a zone onto a pass's actual path
# --------------------------------------------------------------------------


def travel_offset(box: Box, side: str, point: tuple[float, float]) -> float:
    """``point``'s offset along ``side``, signed by the travel direction.

    The inverse of :func:`zone_point`.  Only the coordinate along the side is
    read, so a point standing off the profile line (a ramp's lateral lead)
    projects onto it.
    """
    if side == "bottom":
        return point[0] - box.mid_x
    if side == "top":
        return box.mid_x - point[0]
    if side == "right":
        return point[1] - box.mid_y
    if side == "left":
        return box.mid_y - point[1]
    raise ValueError(f"unknown side {side!r}")


def zone_point(box: Box, side: str, offset: float) -> tuple[float, float]:
    """The point on ``side`` of ``box`` at travel ``offset`` from its midpoint.

    ``box`` here is the PASS's rectangle (the finished profile grown by
    :attr:`~.model.PassSpec.offset`), which is concentric with the finished one
    — so a zone placed on the finished profile lands on this path exactly, at
    the same place on the profile for every pass (module docstring).
    """
    if side == "bottom":
        return (box.mid_x + offset, box.y0)
    if side == "top":
        return (box.mid_x - offset, box.y1)
    if side == "right":
        return (box.x1, box.mid_y + offset)
    if side == "left":
        return (box.x0, box.mid_y - offset)
    raise ValueError(f"unknown side {side!r}")


def assign_zones(
    zones,
    spans,
    ramp: float,
) -> tuple[tuple[TabZone, ...], ...]:
    """Which zones each move of a loop has to lift over, in travel order.

    ``spans`` is one ``(side, from_offset, to_offset)`` per at-depth move of
    the loop, in emission order (:func:`~.generator.loop_spans`); ``ramp`` is
    this pass's ramp length, so the footprint judged is the one that will
    actually be emitted.

    Every zone must fall wholly inside exactly one move, with room to spare at
    both ends.  Placement guarantees that (2" of corner clearance against a
    footprint under 1.8, and the entry span excluded), so anything else is a
    contradiction between a plan and the geometry it was built for, and this
    raises ``ValueError`` rather than emitting a tab that a corner or the
    lead-in point cuts in half.
    """
    out: list[list[TabZone]] = [[] for _ in spans]
    for zone in zones:
        low, high = zone.span(ramp)
        hits = [
            index
            for index, (side, start, end) in enumerate(spans)
            if side == zone.side and start < low - EPS and high < end - EPS
        ]
        if len(hits) != 1:
            raise ValueError(
                f"tab zone {zone} spans travel offsets {low:.4f}..{high:.4f} on "
                f"the {zone.side} side, which is not inside exactly one cut move "
                f"of this loop ({len(hits)} candidates among "
                f"{[(s, round(a, 4), round(b, 4)) for s, a, b in spans]}) - a "
                f"zone may not cross a corner or the lead-in point"
            )
        out[hits[0]].append(zone)
    for index, assigned in enumerate(out):
        assigned.sort(key=lambda zone: zone.centre)
        for first, second in zip(assigned, assigned[1:]):
            if _overlaps(first.span(ramp), second.span(ramp)):
                raise ValueError(
                    f"tab zones {first} and {second} overlap on the "
                    f"{first.side} side once their {ramp:.4f} ramps are counted"
                )
    return tuple(tuple(assigned) for assigned in out)


# --------------------------------------------------------------------------
# The release cut (spec §3c) — still pure geometry, still no coordinates
# --------------------------------------------------------------------------


def release_ramp(config: PostConfig) -> float:
    """The longest ramp a pass can leave standing beside a tab.

    A tab is not a 0.75 block: every pass that lifted over it left a wedge at
    each end, and the deeper the pass, the longer its wedge.  The release has to
    mill all of it away, so it reserves for the WORST case the post table admits
    — a pass at the Z floor (:attr:`~.model.PostConfig.z_min`, the deepest cut
    this post is allowed to make) — rather than for the passes a particular plan
    happens to run.

    That is deliberately not "the deepest pass in this table", for one decisive
    reason: the dry-run twin (:func:`~.job.dry_run_config`) mirrors every cut
    depth ABOVE the stock, where "deeper" is meaningless and nothing lifts at
    all, and an air cut has to trace the production program's XY path exactly
    (the same problem :attr:`~.model.WdcSlotSpec.overruns` exists to solve, and
    here it solves itself).  ``z_min`` is not mirrored — it is a machine limit,
    not a cut — so this number is the same in both tables, and one release span
    serves every tab on the sheet.

    The cost of the worst case is that the release runs 0.008 further into
    already-open kerf at each end of an OPENING's tab than that opening's own
    deepest pass strictly needed (its T12 pass reaches Z-0.002, the floor
    -0.006).  It removes no extra material.
    """
    return (config.tabs.top_z - config.z_min) * config.ramp_ratio


def release_span(zone: TabZone, config: PostConfig) -> tuple[float, float]:
    """The travel offsets one release cut runs between (spec §3c).

    The full-height 0.75, plus BOTH ramps (:func:`release_ramp`), plus
    :attr:`~.model.ReleaseSpec.overlap` at each end.  Both ends therefore
    start and finish in kerf an earlier pass already cut right through, which is
    what makes the plunge at the start of this span a plunge into open air and
    the ~0.252 of standing tab the only material the release touches.

    Raises ``ValueError`` when the post table configures no release pass: the
    overlap is that table's number and there is nothing honest to guess.
    """
    if config.release is None:
        raise ValueError(
            "this post table configures no release pass, so it says nothing "
            "about how far past a tab the release cut runs"
        )
    reach = zone.length / 2.0 + release_ramp(config) + config.release.overlap
    return (zone.centre - reach, zone.centre + reach)


def travel_sequence(zones, entry_side: str) -> tuple[TabZone, ...]:
    """``zones`` in the order the tool meets them going round the loop.

    The loop is counter-clockwise from the midpoint of ``entry_side`` (module
    docstring), so the order is: this side's zones ahead of the entry point,
    then the next three sides in :data:`~.model.SIDES` order, then this side's
    zones BEHIND the entry point — which the loop only reaches on its way back
    to close.  It is the order :func:`assign_zones` puts them in for the pass
    that formed them, written as a plain sort because the release section cuts
    each tab on its own and has no loop to hang them off.
    """
    if entry_side not in SIDES:
        raise ValueError(f"unknown entry side {entry_side!r}")
    first = SIDES.index(entry_side)

    def key(zone: TabZone) -> tuple[int, float]:
        if zone.side == entry_side:
            return (0 if zone.centre >= -EPS else 4, zone.centre)
        return ((SIDES.index(zone.side) - first) % 4, zone.centre)

    return tuple(sorted(zones, key=key))


def entry_conflict(
    zones,
    entry_side: str,
    exclusion: tuple[float, float],
    ramp: float,
) -> TabZone | None:
    """The first zone that fouls this pass's lead-in span, or ``None``.

    The one thing :func:`place_tabs` cannot know on its own: it is told which
    side the passes lead in on, and a pass that in the event leads in somewhere
    else (a different entry override, or the short-part fallback in
    :func:`~.generator.entry_side_for` choosing another edge for a deeper pass)
    would ramp straight through a tab.  The emitter asks this before it lifts,
    so that case is a refusal with the zone named instead of a tab the machine
    quietly cuts away.
    """
    for zone in zones:
        if zone.side == entry_side and _overlaps(zone.span(ramp), exclusion):
            return zone
    return None
