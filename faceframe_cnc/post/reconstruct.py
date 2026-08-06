"""Read an existing ``.anc`` back into a sheet layout and a cut plan.

This is the other half of the round-trip proof: it recovers, from nothing
but the cut coordinates, what the sheet HELD (part footprints, rotations,
routed openings, which frame is nested in which) and the order the CAM cut
it in.  Feeding that back through :func:`~.generator.generate` has to
reproduce the original file line for line.

What is recovered is deliberately narrow.  A plan carries integers and
enums only — sequence, which opening, which edge the tool led in on.  Every
coordinate, feed, speed, Z level and G/M word in the regenerated file is
recomputed from :class:`~.model.PostConfig` and the part geometry; none of
it is copied out of the source file.

Recovery rules (all measured, see :mod:`~faceframe_cnc.post.model`):

*   part footprint  = perimeter loop shrunk by that pass's offset;
*   routed opening  = T11 opening loop grown by 0.1975 (radius + finish
    stock), cross-checked against the T12 loop grown by 0.1.  Since the
    2026-08-05 max-bite amendment the T11 section can cut one opening on
    several depth passes (Z0.45 then Z0.15 on a generated sheet), so the loops
    are sorted into one list per configured rung and the rungs are held to
    each other before the deepest one is read as the opening;
*   rotation        = which pair of T13 grooves sits 0.5625 in from the
    edge (the stile pair) — or, for a WDC frame, which pair of T17 slots
    sits 0.6614 in, since a WDC has no T13 stile grooves to vote with;
*   T13 groove      = matched against BOTH stile-groove shapes, the clamped
    one this post writes since the 2026-08-05 amendment and the legacy 0.375
    overrun every reference file contains (:func:`_groove_ref`);
*   WDC slot        = two passes on one centreline, matched against the
    emitter's own per-pass overrun;
*   nesting         = a part whose footprint lies inside another part's
    opening is that part's child (spec 4b).

Tabbed programs (2026-08-05 amendment, Scott, job R0805)
-------------------------------------------------------
A generated sheet now holds every part and every dropout with tabs and cuts
them free in a final T12 release section (spec §3b/§3c).  Two things about that
have to be read back, and one deliberately is not:

*   a loop that rises over a tab has four extra moves per tab, so a profile loop
    is no longer eight moves — it is ``8 + 4n``.  :func:`_loop_corners` finds the
    four corners by walking the loop rather than by counting, so it reads either
    shape;
*   a program then has TWO T12 sections, the detail pass and the release, which
    :func:`_split_sections` tells apart by what they contain: the detail section
    is closed loops, the release section is straight two-point cuts.  The
    release cuts are attributed to the profiles they release
    (:func:`_release_refs`), so a reader can see which part each one frees.

What is NOT recovered is WHERE the tabs are.  A plan carries the zones
(:attr:`~.model.CutPlan.tabs`) and a zone is a position on a profile, so
recovering them means measuring the lift positions back off the text and
inverting the placement arithmetic — and a byte-exact round trip of a TABBED
program was explicitly not asked for at this milestone.  So a reconstructed plan
is untabbed and regenerating it produces the same program WITHOUT the tabs and
the release section: faithful about the sheet, honest about the holding.
:func:`reconstruct_text` records what it saw on
:attr:`~.model.CutPlan.release` — the profiles the file releases — so a caller
that needs to know whether the source was tab-held can ask, and the byte-exact
round trips (the reference programs, which contain no tab) are untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .model import (
    Box,
    CutPlan,
    DEFAULT_SECTIONS,
    FeatureRef,
    PartProgram,
    PostConfig,
    ProgramHeader,
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_RELEASE,
    SECTION_WDC_SLOT,
    SheetProgram,
    default_config,
)
from .generator import (
    default_entry_side,
    entry_side_for,
    groove_segment,
    release_path,
    wdc_slot_segment,
)
from .tabs import TabZone, travel_offset

__all__ = ["ReconstructionError", "reconstruct", "reconstruct_text"]

TOL = 1e-6

#: Decimals a coordinate reaches this module with.  The post prints four and
#: strips trailing zeros, so nothing read out of a file is more precise than
#: that.
PLACES = 4

#: Tolerance for comparing a value this module COMPUTED against one the file
#: PRINTED (2026-08-04 review, fix 9).  ``TOL`` is right for printed-against-
#: printed — both sides are then on the same 0.0001 grid — but wrong the
#: moment an exact midpoint meets a rounded one: a 30.0625" frame's opening has
#: its mid-x at 16.03125, the post can only print ``X16.0312``, and the
#: half-of-the-last-digit difference made :func:`_entry_side` refuse a file the
#: post had just written.  5e-5 is the largest error a four-decimal rounding
#: can introduce, so anything above it is enough; 1e-4 keeps the margin
#: visible and is still four orders of magnitude tighter than the smallest
#: distance between two different edges of a real frame.
PRINTED_TOL = 1e-4

_WORD_RE = re.compile(r"([A-Za-z])(-?\d*\.?\d+)")
_TOOL_RE = re.compile(r"^\(ROUTE TOOL #(\d+):")
_O_RE = re.compile(r"^O(\d+) \((.*)\)$")
_CREATED_RE = re.compile(r"^\(CREATED ON (.*)\)$")


class ReconstructionError(ValueError):
    """The file does not match the grammar this post knows how to write."""


@dataclass
class _Feature:
    """One G1 run: a groove (2 points) or a closed profile loop (8)."""

    pre: tuple[float, float]
    points: list[tuple[float, float, float]]
    xy_in_first_line: bool

    @property
    def is_groove(self) -> bool:
        return not self.xy_in_first_line

    @property
    def z_cut(self) -> float:
        return self.points[0][2]


def _parse_words(line: str) -> list[tuple[str, str]]:
    code = re.sub(r"\([^)]*\)", "", line).strip()
    return [(letter.upper(), value) for letter, value in _WORD_RE.findall(code)]


def _split_sections(lines: list[str]) -> list[tuple[int, int, int]]:
    """Return ``(tool_number, start, end)`` for each ``(ROUTE TOOL #n`` block."""
    heads = [i for i, line in enumerate(lines) if line.startswith("(ROUTE TOOL")]
    out = []
    for pos, head in enumerate(heads):
        match = _TOOL_RE.match(lines[head])
        if not match:
            raise ReconstructionError(f"unreadable tool header: {lines[head]!r}")
        end = heads[pos + 1] if pos + 1 < len(heads) else len(lines)
        out.append((int(match.group(1)), head, end))
    return out


def _scan_features(lines: list[str]) -> list[_Feature]:
    """Walk one section's lines and pull out its G1 runs."""
    modal = 0
    x = y = z = 0.0
    features: list[_Feature] = []
    current: _Feature | None = None

    for raw in lines:
        words = _parse_words(raw)
        if not words:
            continue
        line_g = None
        moved_xy = False
        new_x, new_y, new_z = x, y, z
        for letter, value in words:
            if letter == "G" and value in ("0", "1"):
                line_g = int(value)
            elif letter == "X":
                new_x = float(value)
                moved_xy = True
            elif letter == "Y":
                new_y = float(value)
                moved_xy = True
            elif letter == "Z":
                new_z = float(value)
        if line_g is not None:
            if line_g == 0 and modal == 1 and current is not None:
                features.append(current)
                current = None
            modal = line_g
        if modal == 1:
            if current is None:
                current = _Feature(pre=(x, y), points=[], xy_in_first_line=moved_xy)
            current.points.append((new_x, new_y, new_z))
        x, y, z = new_x, new_y, new_z

    if current is not None:
        features.append(current)
    return features


def _close_index(feature: _Feature) -> int:
    """Which move of a loop returns to the lead-in point and closes it.

    Everything before it is on the rectangle; the two moves after it are the
    one-tool-diameter overshoot past the lead-in point and the lead-out ramp,
    both of which leave the rectangle (the ramp also steps 0.05 sideways off the
    profile on a perimeter), so neither may be measured.
    """
    start = (round(feature.points[0][0], 6), round(feature.points[0][1], 6))
    for index, point in enumerate(feature.points[1:], start=1):
        if (round(point[0], 6), round(point[1], 6)) == start:
            if index < 4:
                raise ReconstructionError(
                    f"a profile loop returns to its lead-in point after only "
                    f"{index} move(s) - a closed rectangle takes four"
                )
            if len(feature.points) != index + 3:
                raise ReconstructionError(
                    f"a profile loop closes on move {index} but the run has "
                    f"{len(feature.points)} moves, not the {index + 3} this post "
                    f"writes (the loop, its overshoot and its lead-out ramp)"
                )
            return index
    raise ReconstructionError(
        f"a profile loop of {len(feature.points)} moves never returns to its "
        f"lead-in point, so it is not a closed rectangle this post wrote"
    )


def _loop_box(feature: _Feature) -> Box:
    """The rectangle a profile loop cut.

    The extent of every point from the lead-in up to and including the move that
    closes the loop.  Not "points 1..4", which is what this measured before the
    2026-08-05 amendment: a loop that rises over a holding tab has four extra
    moves per tab (spec §3b), all of them ON the rectangle, so counting moves is
    no longer the way to find the corners — and taking the extent of the closed
    part of the loop finds them whether there are tabs or not.
    """
    close = _close_index(feature)
    points = feature.points[: close + 1]
    xs = sorted({round(p[0], 6) for p in points})
    ys = sorted({round(p[1], 6) for p in points})
    if len(xs) < 2 or len(ys) < 2:
        raise ReconstructionError(f"loop points are not a rectangle: {points}")
    return Box(xs[0], ys[0], xs[-1], ys[-1])


def _loop_zones(feature: _Feature, box: Box, config: PostConfig):
    """The holding tabs this loop rose over, as :class:`~.tabs.TabZone`.

    The 2026-08-05 amendment read backwards.  A tab lift is four moves — cut on
    to the foot of the climb, climb to :attr:`~.model.TabSpec.top_z`, traverse
    the full-height length at that Z, descend back to depth — so in the point
    list a tab is the one place where TWO consecutive points sit at the tab top.
    Those two points are the tab's crests, and a zone is where their midpoint
    falls on the profile:

    *   the side is the side of ``box`` the crests run along;
    *   ``centre`` is that midpoint's offset from the side's midpoint, which is
        the same number on every one of a profile's concentric paths — the T11
        kerf, the T12 kerf and the flush release path alike — and so is what a
        zone means (:mod:`~.tabs`);
    *   ``length`` is the distance between the crests, snapped to the post
        table's own :attr:`~.model.TabSpec.length` when it matches to within a
        printed ten-thousandth: the crest coordinates reach this module rounded
        to four decimals, and a zone carrying 0.7501 instead of 0.75 would be a
        rounding artefact masquerading as a decision.
    """
    top = config.tabs.top_z
    zones: list[TabZone] = []
    points = feature.points[: _close_index(feature) + 1]
    for first, second in zip(points, points[1:]):
        if abs(first[2] - top) > PRINTED_TOL or abs(second[2] - top) > PRINTED_TOL:
            continue
        side = _side_of(box, first[:2], second[:2])
        low = travel_offset(box, side, first[:2])
        high = travel_offset(box, side, second[:2])
        length = abs(high - low)
        if abs(length - config.tabs.length) <= PRINTED_TOL:
            length = config.tabs.length
        zones.append(
            TabZone(side=side, centre=(low + high) / 2.0, length=length)
        )
    return tuple(zones)


def _side_of(box: Box, start, end) -> str:
    """Which side of ``box`` the segment ``start`` -> ``end`` runs along."""
    if abs(start[1] - end[1]) <= abs(start[0] - end[0]):
        across = (start[1] + end[1]) / 2.0
        candidates = (("bottom", box.y0), ("top", box.y1))
    else:
        across = (start[0] + end[0]) / 2.0
        candidates = (("left", box.x0), ("right", box.x1))
    side, line = min(candidates, key=lambda item: abs(across - item[1]))
    if abs(across - line) > PRINTED_TOL:
        raise ReconstructionError(
            f"the segment {start} -> {end} does not run along any side of {box}"
        )
    return side


def _entry_side(feature: _Feature, box: Box) -> str:
    """Which edge's midpoint the lead-in landed on.

    An edge MIDPOINT is the one place in this module where an exact value meets
    a printed one — halving a 0.0001-grid coordinate lands off the grid every
    time the span is an odd number of ten-thousandths — so this comparison uses
    :data:`PRINTED_TOL` rather than :data:`TOL` (2026-08-04 review, fix 9).
    """
    ex, ey = feature.points[0][0], feature.points[0][1]
    if abs(ey - box.y0) < PRINTED_TOL and abs(ex - box.mid_x) < PRINTED_TOL:
        return "bottom"
    if abs(ex - box.x1) < PRINTED_TOL and abs(ey - box.mid_y) < PRINTED_TOL:
        return "right"
    if abs(ey - box.y1) < PRINTED_TOL and abs(ex - box.mid_x) < PRINTED_TOL:
        return "top"
    if abs(ex - box.x0) < PRINTED_TOL and abs(ey - box.mid_y) < PRINTED_TOL:
        return "left"
    raise ReconstructionError(
        f"lead-in point ({ex}, {ey}) is not the midpoint of any edge of {box}"
    )


def _match_pass(z_cut: float, passes, what: str):
    """Which configured depth pass a loop at ``z_cut`` is.

    One function for both ladders (the perimeter's, and — since the 2026-08-05
    max-bite amendment — the T11 openings'), because reading a program back means
    the same thing in both cases: the Z word says which rung, and a Z that is not
    a configured rung is not something to guess at.
    """
    for index, spec in enumerate(passes):
        if abs(spec.z_cut - z_cut) < 1e-9:
            return index, spec
    raise ReconstructionError(
        f"{what} loop cuts at Z{z_cut}, which is not one of the configured "
        f"passes {[p.z_cut for p in passes]} - refusing to guess"
    )


def reconstruct(path: str, config: PostConfig | None = None):
    """Read ``path`` and return ``(SheetProgram, CutPlan)``."""
    with open(path, "r", newline="") as handle:
        return reconstruct_text(handle.read(), config)


def reconstruct_text(text: str, config: PostConfig | None = None):
    """Reconstruct a sheet layout and cut plan from ``.anc`` text."""
    cfg = config or default_config()
    lines = text.split("\r\n")
    if not lines or lines[0] != "%":
        raise ReconstructionError("file does not start with a '%' line")

    o_match = _O_RE.match(lines[1])
    created_match = _CREATED_RE.match(lines[2])
    if not o_match or not created_match:
        raise ReconstructionError("missing O-number and/or (CREATED ON ...) line")
    header = ProgramHeader(
        name=o_match.group(2),
        o_number=int(o_match.group(1)),
        created=created_match.group(1),
        material_comment=lines[3],
        load_comment=lines[4],
    )

    sections: dict[str, list[_Feature]] = {}
    order: list[str] = []
    for tool_number, start, end in _split_sections(lines):
        body = lines[start:end]
        features = _scan_features(body)
        if tool_number == 13:
            name = SECTION_PANEL
        elif tool_number == 17:
            name = SECTION_WDC_SLOT
        elif tool_number == 12:
            # T12 appears TWICE since the 2026-08-05 amendment: the detail pass
            # and the release section, at the same depth with the same tool.  What
            # tells them apart is what they contain — the detail pass is closed
            # loops, a release cut is the straight plunge-and-move grammar (spec
            # §3c) — which is the same structural distinction the verifier makes.
            name = (
                SECTION_RELEASE
                if features and all(item.is_groove for item in features)
                else SECTION_DETAIL
            )
        elif tool_number == 11:
            # T11 cuts two operations, and since the 2026-08-05 max-bite
            # amendment each of them can be a LADDER of depth passes, so the
            # question is which operation's ladder this section's first loop
            # belongs to.  The two ladders never share a Z (openings run
            # 0.45/0.15, perimeters 0.372/-0.006, and the measured table's single
            # 0.15 against 0.06/-0.006), which is what makes the first loop
            # enough to name the section.
            name = (
                SECTION_OPENINGS
                if any(
                    abs(features[0].z_cut - spec.z_cut) < 1e-9
                    for spec in cfg.openings_passes
                )
                else SECTION_PERIMETER
            )
        else:
            raise ReconstructionError(
                f"tool T{tool_number} is not supported by this post (the T2 "
                f"roughing style of R620101N is deliberately not replicated)"
            )
        if name in sections:
            raise ReconstructionError(f"two {name} sections in one program")
        sections[name] = features
        order.append(name)

    if SECTION_PERIMETER not in sections:
        raise ReconstructionError("no perimeter section: cannot recover footprints")

    # --- parts, from the perimeter section --------------------------------
    passes: list[list[tuple[Box, str, tuple[TabZone, ...]]]] = [
        [] for _ in cfg.perimeter_passes
    ]
    for feature in sections[SECTION_PERIMETER]:
        index, spec = _match_pass(feature.z_cut, cfg.perimeter_passes, "perimeter")
        box = _loop_box(feature)
        passes[index].append(
            (
                box.grow(-spec.offset).rounded(),
                _entry_side(feature, box),
                _loop_zones(feature, box, cfg),
            )
        )

    if not passes[0]:
        raise ReconstructionError("perimeter section cut nothing at the first depth")
    for index, entries in enumerate(passes[1:], start=1):
        if len(entries) != len(passes[0]):
            raise ReconstructionError(
                f"perimeter pass {index + 1} cuts {len(entries)} parts, "
                f"pass 1 cuts {len(passes[0])}"
            )

    boxes = [box for box, _, _ in passes[0]]
    parts = [PartProgram(part_number=f"PART{i + 1}", box=box) for i, box in enumerate(boxes)]

    # --- openings ---------------------------------------------------------
    # One list per configured T11 depth pass, exactly as the perimeter above:
    # since the 2026-08-05 max-bite amendment a generated sheet cuts every
    # opening twice (Z0.45 then Z0.15) and the measured table cuts it once, so
    # the rungs are collected by their Z word and then held to each other.  The
    # DEEPEST rung is the one the opening list is read off, because it is the pass
    # the plan and the measured offset were always about.
    if not cfg.openings_passes:
        raise ReconstructionError(
            "this post table configures no T11 opening pass, so nothing here can "
            "say what an opening loop in the file was cutting"
        )
    opening_passes: list[list[tuple[Box, str, tuple[TabZone, ...]]]] = [
        [] for _ in cfg.openings_passes
    ]
    for feature in sections.get(SECTION_OPENINGS, []):
        index, spec = _match_pass(feature.z_cut, cfg.openings_passes, "opening")
        box = _loop_box(feature)
        opening_passes[index].append(
            (
                box.grow(-spec.offset).rounded(),
                _entry_side(feature, box),
                _loop_zones(feature, box, cfg),
            )
        )
    for index, entries in enumerate(opening_passes[:-1]):
        if [box for box, _, _ in entries] != [
            box for box, _, _ in opening_passes[-1]
        ]:
            raise ReconstructionError(
                f"T11 opening pass {index + 1} does not rough the same openings, in "
                f"the same order, as the deepest pass "
                f"(Z{cfg.openings_passes[-1].z_cut})"
            )
    opening_boxes = [box for box, _, _ in opening_passes[-1]]
    opening_sides = [side for _, side, _ in opening_passes[-1]]
    opening_zones = [zones for _, _, zones in opening_passes[-1]]

    detail_boxes: list[Box] = []
    detail_sides: list[str] = []
    detail_zones: list[tuple[TabZone, ...]] = []
    for feature in sections.get(SECTION_DETAIL, []):
        box = _loop_box(feature)
        detail_boxes.append(box.grow(-cfg.detail_pass.offset).rounded())
        detail_sides.append(_entry_side(feature, box))
        detail_zones.append(_loop_zones(feature, box, cfg))
    if detail_boxes and detail_boxes != opening_boxes:
        raise ReconstructionError(
            "the T12 detail section does not finish the same openings, in the "
            "same order, as the T11 opening section"
        )

    for box in opening_boxes:
        owner = _innermost(parts, box)
        if owner is None:
            raise ReconstructionError(f"opening {box} lies outside every part")
        owner.openings.append(box)
    for part in parts:
        part.openings.sort(key=lambda b: (-b.y1, b.x0))

    # --- nesting ----------------------------------------------------------
    top_level: list[PartProgram] = []
    for part in parts:
        host = _host_of(parts, part)
        if host is None:
            top_level.append(part)
        else:
            host.children.append(part)

    program = SheetProgram(
        header=header,
        parts=top_level,
        sheet_width=cfg.sheet_width,
        sheet_length=cfg.sheet_length,
    )
    flat = program.flat_parts()
    index_of = {id(part): i for i, part in enumerate(flat)}

    # --- rotation, from the T13 grooves and the T17 slots ------------------
    grooves = _straight_runs(sections.get(SECTION_PANEL, []), "T13 groove")
    slots = _straight_runs(sections.get(SECTION_WDC_SLOT, []), "T17 slot")
    if grooves or slots:
        _apply_rotation(parts, grooves, slots, cfg)

    # --- plan -------------------------------------------------------------
    panel_refs: list[FeatureRef] = []
    for start, end in grooves:
        panel_refs.append(_groove_ref(parts, index_of, start, end, cfg))

    slot_refs = _slot_refs(parts, index_of, slots, cfg)

    #: The tabs the file's own lifts state, per profile.  Openings vote first
    #: and the detail pass fills in anything the T11 section did not (which is
    #: nothing on a program this post wrote — spec §3b puts the two kerfs' lifts
    #: at the same positions on purpose).
    zones: dict[tuple[int, str, int], tuple[TabZone, ...]] = {}

    opening_refs: list[FeatureRef] = []
    for position, (box, side) in enumerate(zip(opening_boxes, opening_sides)):
        owner = _innermost(parts, box)
        assert owner is not None
        ref_index = owner.openings.index(box)
        opening_refs.append(
            FeatureRef(
                part=index_of[id(owner)],
                kind="opening",
                index=ref_index,
                entry=None
                if side
                == _effective_entry_side(
                    box.grow(cfg.openings_passes[-1].offset),
                    "opening",
                    cfg.tools[SECTION_OPENINGS],
                    cfg.openings_passes[-1],
                    cfg,
                )
                else side,
            )
        )
        for source in (opening_zones, detail_zones):
            if position < len(source) and source[position]:
                zones.setdefault(opening_refs[-1].profile, source[position])

    detail_refs: list[FeatureRef] | None = None
    if detail_sides and detail_sides != opening_sides:
        detail_refs = [
            FeatureRef(ref.part, "opening", ref.index, entry=side)
            for ref, side in zip(opening_refs, detail_sides)
        ]

    perimeter_refs: list[list[FeatureRef]] = []
    for pass_index, entries in enumerate(passes):
        spec = cfg.perimeter_passes[pass_index]
        pass_refs: list[FeatureRef] = []
        for box, side, pass_zones in entries:
            owner = next((p for p in parts if p.box == box), None)
            if owner is None:
                raise ReconstructionError(
                    f"perimeter pass cuts {box}, which pass 1 never cut"
                )
            pass_refs.append(
                FeatureRef(
                    part=index_of[id(owner)],
                    kind="perimeter",
                    entry=None
                    if side
                    == _effective_entry_side(
                        box.grow(spec.offset),
                        "perimeter",
                        cfg.tools[SECTION_PERIMETER],
                        spec,
                        cfg,
                    )
                    else side,
                )
            )
            if pass_zones:
                zones.setdefault(pass_refs[-1].profile, pass_zones)
        perimeter_refs.append(pass_refs)

    release_refs = _release_refs(
        parts,
        index_of,
        _straight_runs(sections.get(SECTION_RELEASE, []), "T12 release cut"),
        zones,
        cfg,
    )

    plan = CutPlan(
        panel=panel_refs,
        wdc_slot=slot_refs,
        openings=opening_refs,
        perimeter=perimeter_refs,
        detail=detail_refs,
        sections=tuple(order) if order else DEFAULT_SECTIONS,
        tabs=zones or None,
        release=release_refs,
    )
    return program, plan


def _release_refs(parts, index_of, runs, zones, cfg: PostConfig) -> list[FeatureRef]:
    """One :class:`FeatureRef` per PROFILE the release section frees, in order.

    Each straight T12 cut is attributed to the profile whose flush release path
    (:func:`~.generator.release_path`) it runs on — the same numeric test the
    verifier's hold invariant makes, and the reason a release cut can be traced
    back to the part it frees at all.  The plan then names each profile once, in
    the order the file first releases it, because one entry stands for all of
    that profile's tabs: the emitter cuts one release move per zone.

    Refuses a release cut that matches no tabbed profile rather than guessing:
    a cut nothing on the sheet was standing in the way of is not something this
    post wrote, and reading it as if it were would hide it.
    """
    if not runs:
        return []
    if cfg.release is None:
        raise ReconstructionError(
            "this program has a T12 release section but the post table configures "
            "no release pass, so nothing here says what depth or feeds it should "
            "have had - read it with post_config_for()'s table"
        )
    candidates: list[tuple[FeatureRef, Box]] = []
    for part in parts:
        index = index_of[id(part)]
        for opening_index, opening in enumerate(part.openings):
            candidates.append(
                (
                    FeatureRef(index, "opening", opening_index),
                    release_path(opening, "opening", cfg.tool(SECTION_RELEASE).radius),
                )
            )
        candidates.append(
            (
                FeatureRef(index, "perimeter"),
                release_path(part.box, "perimeter", cfg.tool(SECTION_RELEASE).radius),
            )
        )

    refs: list[FeatureRef] = []
    for start, end in runs:
        for ref, path in candidates:
            if ref.profile not in zones:
                continue
            try:
                side = _side_of(path, start, end)
            except ReconstructionError:
                continue
            low = min(travel_offset(path, side, start), travel_offset(path, side, end))
            high = max(travel_offset(path, side, start), travel_offset(path, side, end))
            if any(
                zone.side == side and low <= zone.centre <= high
                for zone in zones[ref.profile]
            ):
                if ref not in refs:
                    refs.append(ref)
                break
        else:
            raise ReconstructionError(
                f"the T12 release cut {start} -> {end} is not flush with any tabbed "
                f"profile's finished edge over one of the tabs that profile's own "
                f"loops left standing - this post only writes a release cut on the "
                f"flush path of a tab it made"
            )
    return refs


def _effective_entry_side(cut: Box, kind: str, tool, spec, cfg: PostConfig) -> str:
    """What :func:`~.generator.generate` would choose, left to itself.

    A :attr:`~.model.FeatureRef.entry` is recorded only when the file did
    something the emitter would NOT have done by default, so this has to be the
    emitter's own rule and not a copy of half of it: since the 2026-08-04
    entry-side fallback (fix 6) the default depends on whether the lead-in fits
    the sheet, and a reconstruction that asked only
    :func:`~.generator.default_entry_side` would drop the override on exactly
    the parts the fallback exists for — and regenerate a different file.

    A cut with no fitting edge at all cannot be the emitter's default by
    definition, so the actual side is recorded explicitly.
    """
    try:
        return entry_side_for(cut, kind, tool, spec, cfg)
    except ValueError:
        return ""


def _innermost(parts: list[PartProgram], box: Box) -> PartProgram | None:
    """The smallest part footprint containing ``box``."""
    best: PartProgram | None = None
    for part in parts:
        if part.box == box:
            continue
        if part.box.contains(box, TOL):
            if best is None or part.box.width * part.box.height < best.box.width * best.box.height:
                best = part
    return best


def _host_of(parts: list[PartProgram], part: PartProgram) -> PartProgram | None:
    """The part whose OPENING this part sits inside (spec 4b nesting)."""
    for other in parts:
        if other is part:
            continue
        for opening in other.openings:
            if opening.contains(part.box, TOL):
                return other
    return None


def _straight_runs(features: list[_Feature], what: str):
    """``[(start, end)]`` for a section of straight two-point cuts."""
    runs = []
    for feature in features:
        if not feature.is_groove or len(feature.points) != 2:
            raise ReconstructionError(
                f"a {what} is not a straight cut (plunge then one move); this "
                f"post only knows that grammar"
            )
        runs.append((feature.pre, (feature.points[-1][0], feature.points[-1][1])))
    return runs


def _apply_rotation(
    parts: list[PartProgram], grooves, slots, cfg: PostConfig
) -> None:
    """Set each part's ``rotated`` flag from its stile-cut pattern.

    An upright part's STILES are its left and right edges, so the cuts that
    run down them are vertical.  Which cut that is depends on the frame: an
    ordinary part has a T13 groove 0.5625 in from each stile edge, while a
    WDC has no T13 stile groove at all and a T17 slot 0.6614 in instead
    (2026-08-03 amendment).  Both vote here, and a part cannot have both.
    """
    candidates = [(cfg.panel.stile_inset, cfg.panel.overrun, grooves)]
    if slots:
        candidates.append(
            (
                cfg.wdc_slot.inset_from_outside_edge,
                max(
                    cfg.wdc_slot_reach(position)
                    for position in range(len(cfg.wdc_slot.z_cuts))
                ),
                slots,
            )
        )

    for part in parts:
        votes = {False: 0, True: 0}
        box = part.box
        for inset, overrun, runs in candidates:
            for start, end in runs:
                horizontal = abs(start[1] - end[1]) < TOL
                if horizontal:
                    y = start[1]
                    if abs(y - (box.y0 + inset)) < TOL or abs(y - (box.y1 - inset)) < TOL:
                        if box.x0 - TOL <= min(start[0], end[0]) + overrun <= box.x1 + TOL:
                            votes[True] += 1
                else:
                    x = start[0]
                    if abs(x - (box.x0 + inset)) < TOL or abs(x - (box.x1 - inset)) < TOL:
                        if box.y0 - TOL <= min(start[1], end[1]) + overrun <= box.y1 + TOL:
                            votes[False] += 1
        if votes[True] and votes[False]:
            raise ReconstructionError(
                f"part {part.box} has stile cuts on both axes - cannot tell "
                f"its rotation"
            )
        part.rotated = votes[True] > 0


def _slot_refs(parts, index_of, slots, cfg: PostConfig) -> list[FeatureRef]:
    """One :class:`FeatureRef` per WDC slot, from its depth passes.

    The emitter writes every configured depth pass of one slot back to back
    on a single centreline, so the section reads as consecutive groups of
    ``len(z_cuts)`` cuts.  Each group is matched against the geometry the
    emitter would produce for some part and stile; anything else is a file
    this post did not write, and is refused rather than guessed at.
    """
    if not slots:
        return []
    spec = cfg.wdc_slot
    stride = len(spec.z_cuts)
    if len(slots) % stride:
        raise ReconstructionError(
            f"the T17 section has {len(slots)} cuts, which is not a whole number "
            f"of {stride}-pass slots"
        )

    refs: list[FeatureRef] = []
    for base in range(0, len(slots), stride):
        group = slots[base : base + stride]
        for part in parts:
            for index in (0, 1):
                if all(
                    _matches(
                        wdc_slot_segment(
                            part, index, spec, cfg.wdc_slot_reach(position)
                        ),
                        group[position],
                    )
                    for position in range(stride)
                ):
                    refs.append(
                        FeatureRef(index_of[id(part)], "wdc_slot", index)
                    )
                    break
            else:
                continue
            break
        else:
            raise ReconstructionError(
                f"the T17 cuts starting {group[0][0]} -> {group[0][1]} do not match "
                f"any part's stile slot (centreline "
                f"{spec.inset_from_inside_edge:g} from the inside edge of a "
                f"{spec.stile_width:g}\" stile, one pass per configured depth)"
            )
    return refs


def _matches(want, got) -> bool:
    return _same(want[0], got[0]) and _same(want[1], got[1])


def _groove_ref(parts, index_of, start, end, cfg: PostConfig) -> FeatureRef:
    """Identify which of a part's four grooves this segment is.

    TWO shapes are accepted for a stile groove, and both map to the same
    :class:`FeatureRef` (2026-08-05 amendment, job R0805):

    *   the CLAMPED one this post writes now — endpoints one tool radius
        inside the part ends, so the cut stops flush with the part;
    *   the LEGACY one, ``panel.overrun`` (0.375) PAST both part ends, which
        is what every reference ``.anc`` in the repo contains and what the
        shop's own CAM wrote for years.

    Reading a file is not the same act as writing one: a program the shop
    already cut has to be readable whatever the post emits today, or the
    reference files stop being usable evidence.  Which shape a file used is
    not recorded on the ref, because a ref carries no geometry at all — so
    regenerating a legacy file produces the clamped groove, which is exactly
    the intended behaviour change and exactly what the byte-for-byte
    round-trip goldens have to be re-blessed for (spec §5).
    """
    radius = cfg.tools[SECTION_PANEL].radius
    # The legacy shape is the clamp run backwards: an effective reach of MINUS
    # the measured overrun puts the endpoint 0.375 OUTSIDE the part instead of
    # a tool radius inside it.  Expressed through the same function on purpose,
    # so the two shapes can never disagree about the insets, the long axis or
    # the rotation convention — only about where the groove stops.
    legacy = replace(cfg.panel, end_inset=-(radius + cfg.panel.overrun))
    for part in parts:
        for index in range(4):
            for panel in (cfg.panel, legacy):
                want_start, want_end = groove_segment(part, index, panel, radius)
                if _same(want_start, start) and _same(want_end, end):
                    return FeatureRef(
                        index_of[id(part)], "groove", index, reverse=False
                    )
                if _same(want_start, end) and _same(want_end, start):
                    return FeatureRef(
                        index_of[id(part)], "groove", index, reverse=True
                    )
    raise ReconstructionError(
        f"T13 groove {start} -> {end} does not match any part's measured groove "
        f"pattern (0.5625 stile / 0.9375 rail insets; a stile groove ends either "
        f"flush with the part edge or {cfg.panel.overrun:g} past it)"
    )


def _same(a, b) -> bool:
    """Do a segment endpoint this module computed and one the file printed match?

    Computed-against-printed, so :data:`PRINTED_TOL` (see fix 9): a groove or
    slot centreline is derived from a footprint that is itself a printed
    coordinate plus an exact inset, and any of those arithmetic steps can land
    a half-ten-thousandth off what the post was able to print.
    """
    return abs(a[0] - b[0]) < PRINTED_TOL and abs(a[1] - b[1]) < PRINTED_TOL
