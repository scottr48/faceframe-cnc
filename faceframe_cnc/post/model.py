"""Data model and measured post tables for the ``.anc`` NC post.

EVERY constant in this module was measured from the production files in
``reference/nc_files``; the docstrings cite the line evidence.  Nothing is
taken from the prose spec where the two disagree (rule zero).

Coordinate system (matches :mod:`faceframe_cnc.nesting`)
--------------------------------------------------------
Sheet origin at the lower-left corner, X across the 49" width, Y along the
97" length, G54 work offset, inch (G20), absolute (G90).

Z semantics, measured from the files
------------------------------------
Z is positive UP.  Z0 is the top of the spoilboard, i.e. the BOTTOM of the
3/4" stock; the stock's top face is therefore Z0.75.  Evidence:

*   ``R620101N.anc`` line 22 starts its T2 opening-roughing spiral at
    ``Z0.7367`` and steps down 0.0133 per pass — the first pass is one step
    below a Z0.75 surface, so the stock top is Z0.75.
*   ``R710101N.anc`` line 268 (``G1 X30.2675 Y15. Z-0.006``) is the final
    perimeter pass, which must free the part: 0.006 BELOW the bottom of the
    stock, i.e. a 0.006 relief scratch into the spoilboard.
*   ``R710101N.anc`` line 167 (``G1 X15. Z-0.002``) is the T12 finishing
    pass on the openings — likewise 0.002 through.

Depth of cut per section then falls out as ``0.75 - Z``:

===================  =======  =============  ==========================
Section              Z        depth of cut   what is left
===================  =======  =============  ==========================
T13 panel groove     0.55     0.20           a 0.20-deep groove only
T11 opening through  0.15     0.60           0.15 of skin under the slug
T12 opening detail   -0.002   0.752          through (finish to size)
T11 perimeter pass1  0.06     0.69           0.06 ONION SKIN holding part
T11 perimeter pass2  -0.006   0.756          through — part is free
===================  =======  =============  ==========================

``Z2.`` is the ramp/approach plane and ``Z2.5`` the rapid/clearance plane
(every ``G0`` between features goes to Z2.5; every feature drops to Z2.
before feeding down).  Section 8's claim that "Z0.55 ... is aimed at a
target depth of ~0.75 into the stock" is contradicted by the files: Z0.55
is a shallow 0.20" groove and the deepest cut in every reference file is
Z-0.006.  The files win.

The T13 "panel cutter" groove
-----------------------------
T13 (0.6299 dia = 16 mm) does NOT separate parts.  In every reference file
it cuts a 0.20-deep, 16 mm-wide groove in the back face of each part (the
stock runs face DOWN), four grooves per part, forming a rectangle inset
from the part's own edges:

*   0.5625 in from the two STILE edges, running the full part length with a
    0.375 overrun past both ends;
*   0.9375 in from the two RAIL edges, running between the two stile-groove
    centre lines.

Which pair is which is set by the part's rotation, and that is how this
module recovers rotation from a file: in ``R730101N.anc`` the two parts the
shop confirmed as rotated (3DB30, B30) are exactly the two whose 0.5625
grooves are horizontal.  What the groove is FOR (it looks like a 16 mm
panel dado in the frame back) is not documented anywhere in the reference
material, so the insets are carried as measured constants, not derived.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

__all__ = [
    "Box",
    "ToolSpec",
    "PassSpec",
    "PanelSpec",
    "WdcSlotSpec",
    "PostConfig",
    "ProgramHeader",
    "PartProgram",
    "SheetProgram",
    "FeatureRef",
    "CutPlan",
    "default_config",
    "program_from_placements",
    "SECTION_PANEL",
    "SECTION_WDC_SLOT",
    "SECTION_OPENINGS",
    "SECTION_DETAIL",
    "SECTION_PERIMETER",
    "DEFAULT_SECTIONS",
    "SIDES",
    "T17",
]

#: Geometric tolerance.  Coordinates in the reference files are exact
#: multiples of 0.0001, so 1e-6 is generous.
EPS = 1e-6

SECTION_PANEL = "panel"
SECTION_WDC_SLOT = "wdc_slot"
SECTION_OPENINGS = "openings"
SECTION_DETAIL = "detail"
SECTION_PERIMETER = "perimeter"

#: Section order used by every modern reference file (R710101N, R720101N,
#: R730101N): T13 -> T11 (openings) -> T12 (detail) -> T11 (perimeters),
#: with the T17 WDC slot section between T13 and the first T11 (2026-08-03
#: amendment: "the T13 and T17 groove routing runs FIRST", and T17 leads
#: RFK0101N exactly as T13 leads every modern frame file).  A section with
#: no features is skipped, so a sheet with no WDC frame emits the same four
#: sections it always did.
#:
#: R620101N/R620102N replace the T13 section with a T2 roughing section;
#: spec section 6 says prefer the newer sequence, so that is the default.
DEFAULT_SECTIONS = (
    SECTION_PANEL,
    SECTION_WDC_SLOT,
    SECTION_OPENINGS,
    SECTION_DETAIL,
    SECTION_PERIMETER,
)

#: Legal entry sides for a cut loop.  The loop itself is ALWAYS traversed
#: counter-clockwise in every reference file; only the edge whose midpoint
#: the tool leads in on varies.
SIDES = ("bottom", "right", "top", "left")


@dataclass(frozen=True)
class Box:
    """An axis-aligned rectangle in sheet coordinates (inches)."""

    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_size(cls, x: float, y: float, width: float, height: float) -> "Box":
        return cls(x, y, x + width, y + height)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def mid_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def mid_y(self) -> float:
        return (self.y0 + self.y1) / 2.0

    def grow(self, d: float) -> "Box":
        """Offset every edge outward by ``d`` (negative shrinks)."""
        return Box(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def contains(self, other: "Box", tol: float = EPS) -> bool:
        return (
            other.x0 >= self.x0 - tol
            and other.y0 >= self.y0 - tol
            and other.x1 <= self.x1 + tol
            and other.y1 <= self.y1 + tol
        )

    def overlaps(self, other: "Box", tol: float = EPS) -> bool:
        """True when the two rectangles share interior area (touching is not
        an overlap)."""
        return (
            other.x0 < self.x1 - tol
            and other.x1 > self.x0 + tol
            and other.y0 < self.y1 - tol
            and other.y1 > self.y0 + tol
        )

    def rounded(self, digits: int = 4) -> "Box":
        return Box(
            round(self.x0, digits),
            round(self.y0, digits),
            round(self.x1, digits),
            round(self.y1, digits),
        )


# --------------------------------------------------------------------------
# Tool / pass tables (measured)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One tool exactly as the reference files announce and drive it.

    ``header_comment`` and ``diameter_comment`` are stored VERBATIM (double
    spaces and all) so a generated section header is byte-identical to the
    references.
    """

    number: int
    header_comment: str
    diameter_comment: str
    diameter: float
    speed: int

    @property
    def radius(self) -> float:
        return self.diameter / 2.0


@dataclass(frozen=True)
class PassSpec:
    """One depth pass of a profiling (closed-loop) section.

    ``offset`` is the signed distance from the nominal feature edge to the
    TOOL CENTRE path, positive = outward (away from the feature's interior).
    ``lateral_lead`` shifts the lead-in/lead-out ramp sideways off the
    profile line (perimeter passes only).
    """

    z_cut: float
    offset: float
    entry_feed: float
    cut_feed: float
    lateral_lead: float = 0.0


@dataclass(frozen=True)
class PanelSpec:
    """The T13 groove pass (straight cuts, no ramp)."""

    z_cut: float
    entry_feed: float
    cut_feed: float
    stile_inset: float
    rail_inset: float
    overrun: float


@dataclass(frozen=True)
class WdcSlotSpec:
    """The T17 45-degree slot down each stile of a WDC frame.

    Grammar and tool measured from ``reference/nc_files/RFK0101N.anc``
    (owner-supplied 2026-08-03): a straight ``G1 Z<depth> F150.`` plunge with
    no lateral ramp, one straight cut at F400, ``G0 Z2.5`` between — the same
    shape as the T13 panel groove, at T17's own feeds.

    Geometry from the 2026-08-03 amendment: a STRAIGHT V groove (the machine
    is 3-axis; the 45 degrees are the bit's own flanks) run down the
    centreline 34 mm from the stile's INSIDE edge to 7/16" total depth, in
    two passes on that one centreline so no single bite is deeper than
    RFK0101N demonstrates.

    ``z_cuts`` are machine Z, not depths of cut: the stock top is Z0.75, so
    the passes take 0.3438 and then the full 0.4375.
    """

    z_cuts: tuple[float, ...] = (0.4062, 0.3125)
    entry_feed: float = 150.0
    cut_feed: float = 400.0
    #: How far past each part end each pass runs its tool centre.  ``None``
    #: means "derive it from ``z_cuts``", which is what the measured table
    #: does — the amendment defines the overrun as the bit's effective
    #: radius at that pass's depth, so it is never a free parameter.  The
    #: dry-run twin (:func:`~.job.dry_run_config`) is the one caller that
    #: pins it: its cut Z levels are mirrored ABOVE the stock, where "depth
    #: of cut" is meaningless, and an air cut has to trace the production
    #: program's XY path exactly.
    overruns: tuple[float, ...] | None = None
    #: Centreline distance from the stile's INSIDE (opening-side) edge.
    inset_from_inside_edge: float = 1.3386
    #: Width of the stile the slot runs down (WDC frames only).
    stile_width: float = 2.0
    #: Rise over run of the bit's flank.  45 degrees per side means the
    #: cutting surface radius grows one inch per inch of depth, so the
    #: effective radius of a pass EQUALS its depth of cut.
    flank_slope: float = 1.0

    @property
    def inset_from_outside_edge(self) -> float:
        """The same centreline, measured from the stile's outer edge."""
        return self.stile_width - self.inset_from_inside_edge

    def surface_radius(self, depth_of_cut: float, tool_radius: float) -> float:
        """Radius of the cut where the bit breaks the surface.

        Capped at the tool's own radius: past that depth the cone has run
        out of flank and the bit is cutting at its full diameter, which this
        slot never does (see :func:`~.generator._check_config`).
        """
        return min(depth_of_cut * self.flank_slope, tool_radius)


@dataclass(frozen=True)
class PostConfig:
    """Every number the emitter needs, all measured from the references.

    Z limits (spec section 8) are machine protection: ``z_min`` is the
    deepest legal cut and ``z_max`` the highest legal move.  The factory
    defaults are the deepest cut (-0.006) and the rapid plane (2.5) found
    in every reference file.
    """

    tools: dict[str, ToolSpec]
    openings_pass: PassSpec
    detail_pass: PassSpec
    perimeter_passes: tuple[PassSpec, ...]
    panel: PanelSpec
    wdc_slot: WdcSlotSpec = WdcSlotSpec()

    rapid_z: float = 2.5
    approach_z: float = 2.0
    #: Lead-in/out ramps in every file descend 1 unit of Z per 2 units of
    #: travel: R710101N line 112 ramps 3.7 in X for 1.85 of Z; line 167
    #: ramps 4.004 for 2.002; line 222 ramps 3.88 for 1.94.
    ramp_ratio: float = 2.0

    material_thickness: float = 0.75
    #: Z of the top face of the stock (see the module docstring).
    stock_top_z: float = 0.75

    sheet_width: float = 49.0
    sheet_length: float = 97.0

    #: How far a cut may run outside a part edge into the trim margin.
    #: Measured: T13 groove overruns are exactly 0.375 past the part edge
    #: (R710101N line 44/47: a groove on the part at Y0..30 runs X-0.375 to
    #: X30.375), and that is also the largest excursion past the SHEET edge
    #: (R720101N line 94: Y97.285 on a 97" sheet = 0.285).
    overhang: float = 0.375

    z_min: float = -0.006
    z_max: float = 2.5

    #: All four reference files emit a bare ``M59`` + ``G0 Z2.5`` after the
    #: FIRST loop of the final perimeter section and nowhere else
    #: (R710101N 230-232, R720101N 230-232, R730101N 362-364,
    #: R620101N 2178-2180).  Replicated, not explained.
    perimeter_marker_after_first_loop: bool = True

    #: Optional generated-by banner comment lines (spec section 6 safety
    #: requirement).  EMPTY for replication of the references — any content
    #: here changes the header and so the round-trip diff.
    banner_lines: tuple[str, ...] = ()

    #: This table describes an AIR CUT (spec section 6 "dry-run mode"): every
    #: cut depth has been lifted above :attr:`stock_top_z` so a first article
    #: can be watched without touching material.  The only thing the flag
    #: itself changes is that the verifier additionally requires every
    #: CUTTING move to stay at or above the stock top — the Z floor
    #: (:attr:`z_min`) still has to admit the ``G28 Z0`` homing moves in the
    #: fixed header and footer, so it cannot carry that meaning.
    #: :func:`faceframe_cnc.post.job.dry_run_config` builds one of these
    #: from the measured table; nothing else sets it.
    dry_run: bool = False

    def tool(self, section: str) -> ToolSpec:
        return self.tools[section]

    def wdc_slot_reach(self, position: int) -> float:
        """Surface radius of the V bit on WDC slot pass ``position``.

        One number, used twice, which is why it lives here rather than in
        two callers: it is how far past the part end that pass runs its tool
        CENTRE (the amendment's per-pass overrun) AND how far past the
        centre the cone reaches where it breaks the surface.  The swept
        material of a pass therefore ends ``2 * reach`` past the part.
        """
        spec = self.wdc_slot
        if spec.overruns is not None:
            return spec.overruns[position]
        tool = self.tools.get(SECTION_WDC_SLOT)
        radius = tool.radius if tool is not None else float("inf")
        return spec.surface_radius(self.stock_top_z - spec.z_cuts[position], radius)


T13 = ToolSpec(
    number=13,
    header_comment="(ROUTE TOOL #13: T13 - 3/8 PANEL CUTTER)",
    diameter_comment="(DIAMETER: 0.6299)",
    diameter=0.6299,
    speed=17500,
)
T11 = ToolSpec(
    number=11,
    header_comment="(ROUTE TOOL #11: T11 3/8  COMP - 1.375  LONG)",
    diameter_comment="(DIAMETER: 0.375)",
    diameter=0.375,
    speed=16700,
)
T12 = ToolSpec(
    number=12,
    header_comment="(ROUTE TOOL #12: T12  0.200 DOWNSHEAR)",
    diameter_comment="(DIAMETER: 0.2)",
    diameter=0.2,
    speed=17000,
)
#: The 45-degree V bit that cuts the WDC stile slot, verbatim from
#: ``reference/nc_files/RFK0101N.anc`` lines 13-18.  0.96 is the diameter at
#: the bit's shoulder, i.e. the widest cut it can make and the cap on the
#: cone geometry in :meth:`WdcSlotSpec.surface_radius`.
T17 = ToolSpec(
    number=17,
    header_comment="(ROUTE TOOL #17: T17 45 VTIP 158-562SC.026-1W-A)",
    diameter_comment="(DIAMETER: 0.96)",
    diameter=0.96,
    speed=16000,
)


def default_config(**overrides) -> PostConfig:
    """The post table measured from R710101N/R720101N/R730101N.

    Offsets:

    *   openings, T11 (-0.1975): tool centre 0.1975 INSIDE the finished
        opening edge = 0.1875 tool radius + 0.010 of finish stock left for
        T12 (confirmed against R730101N in Milestone 1);
    *   openings, T12 (-0.1): tool centre one tool radius inside the edge,
        i.e. finishing exactly to the finished opening line;
    *   perimeter pass 1 (+0.1895): 0.1875 radius + 0.002 of spring stock;
    *   perimeter pass 2 (+0.1875): tool tangent to the finished part edge.
    """
    config = PostConfig(
        tools={
            SECTION_PANEL: T13,
            SECTION_WDC_SLOT: T17,
            SECTION_OPENINGS: T11,
            SECTION_DETAIL: T12,
            SECTION_PERIMETER: T11,
        },
        openings_pass=PassSpec(
            z_cut=0.15, offset=-0.1975, entry_feed=150.0, cut_feed=545.0
        ),
        detail_pass=PassSpec(
            z_cut=-0.002, offset=-0.1, entry_feed=100.0, cut_feed=293.0
        ),
        perimeter_passes=(
            PassSpec(
                z_cut=0.06,
                offset=0.1895,
                entry_feed=150.0,
                cut_feed=498.2,
                lateral_lead=0.05,
            ),
            PassSpec(
                z_cut=-0.006,
                offset=0.1875,
                entry_feed=150.0,
                cut_feed=498.2,
                lateral_lead=0.05,
            ),
        ),
        panel=PanelSpec(
            z_cut=0.55,
            entry_feed=150.0,
            cut_feed=490.0,
            stile_inset=0.5625,
            rail_inset=0.9375,
            overrun=0.375,
        ),
    )
    if overrides:
        config = replace(config, **overrides)
    return config


# --------------------------------------------------------------------------
# Program header / sheet contents
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgramHeader:
    """The five identity lines at the top of a program.

    ``created`` is the free text inside ``(CREATED ON ...)`` — the one line
    a round-trip diff is allowed to normalise, along with ``o_number``.
    """

    name: str
    o_number: int = 1
    created: str = ""
    material_comment: str = "(MATERIAL: MDF 3/4 )"
    load_comment: str = "(LOAD: Material face DOWN)"


@dataclass
class PartProgram:
    """One faceframe on the sheet, in sheet coordinates.

    ``box`` is the finished part footprint (what the perimeter pass cuts
    to).  ``openings`` are the finished routed openings.  ``children`` are
    whole frames nested inside this part's openings (spec 4b) — their
    coordinates are SHEET coordinates, exactly as in
    :class:`faceframe_cnc.nesting.Placement`.

    ``rotated`` is the part's absolute orientation on the sheet and is used
    for one thing only: deciding which pair of T13 grooves is the 0.5625
    "stile" pair.
    """

    part_number: str
    box: Box
    rotated: bool = False
    openings: list[Box] = field(default_factory=list)
    children: list["PartProgram"] = field(default_factory=list)

    def solid_boxes(self) -> list[Box]:
        """This part's footprint minus its openings, as up to four bands.

        Used by the verifier to answer "did a cut enter a part that is not
        the one being cut?" without treating an opening's void — where a
        nested inner frame legitimately lives and is legitimately cut — as
        part material.
        """
        bands = [self.box]
        for opening in self.openings:
            nxt: list[Box] = []
            for band in bands:
                nxt.extend(_subtract(band, opening))
            bands = nxt
        return bands


def _subtract(box: Box, hole: Box) -> list[Box]:
    """``box`` minus ``hole`` as a list of disjoint boxes."""
    if not box.overlaps(hole):
        return [box]
    pieces: list[Box] = []
    if hole.y0 > box.y0 + EPS:
        pieces.append(Box(box.x0, box.y0, box.x1, hole.y0))
    if hole.y1 < box.y1 - EPS:
        pieces.append(Box(box.x0, hole.y1, box.x1, box.y1))
    mid_y0 = max(box.y0, hole.y0)
    mid_y1 = min(box.y1, hole.y1)
    if mid_y1 > mid_y0 + EPS:
        if hole.x0 > box.x0 + EPS:
            pieces.append(Box(box.x0, mid_y0, hole.x0, mid_y1))
        if hole.x1 < box.x1 - EPS:
            pieces.append(Box(hole.x1, mid_y0, box.x1, mid_y1))
    return pieces


@dataclass
class SheetProgram:
    """The contents of one sheet: the input the generator turns into NC."""

    header: ProgramHeader
    parts: list[PartProgram] = field(default_factory=list)
    sheet_width: float = 49.0
    sheet_length: float = 97.0

    def flat_parts(self) -> list[PartProgram]:
        """Depth-first (host before its passengers) flattening.

        :class:`FeatureRef` part indices are indices into THIS list, so the
        order is part of the contract.
        """
        out: list[PartProgram] = []

        def walk(items: list[PartProgram]) -> None:
            for part in items:
                out.append(part)
                walk(part.children)

        walk(self.parts)
        return out


def program_from_placements(
    placements,
    header: ProgramHeader,
    sheet_width: float = 49.0,
    sheet_length: float = 97.0,
) -> SheetProgram:
    """Build a :class:`SheetProgram` from :mod:`faceframe_cnc.nesting`
    placements, computing each part's openings from the geometry engine.

    The rotation convention is the packer's: a rotated placement is the
    frame turned 90 degrees COUNTER-CLOCKWISE, so a frame-local opening at
    (x, y, w, h) lands at (X + (W - y - h), Y + x, h, w) — the same
    transform :func:`faceframe_cnc.nesting.validate_layouts` uses to
    re-derive openings.
    """
    from ..geometry import compute_geometry

    def convert(placement) -> PartProgram:
        ordered_w = placement.height if placement.rotated else placement.width
        ordered_h = placement.width if placement.rotated else placement.height
        geom = compute_geometry(placement.part_number, ordered_w, ordered_h)
        if geom.errors:
            raise ValueError(
                f"cannot post {placement.part_number}: {geom.errors[0]}"
            )
        openings: list[Box] = []
        for opening in geom.openings:
            if placement.rotated:
                openings.append(
                    Box.from_size(
                        placement.x + (placement.width - opening.y - opening.height),
                        placement.y + opening.x,
                        opening.height,
                        opening.width,
                    )
                )
            else:
                openings.append(
                    Box.from_size(
                        placement.x + opening.x,
                        placement.y + opening.y,
                        opening.width,
                        opening.height,
                    )
                )
        return PartProgram(
            part_number=placement.part_number,
            box=Box.from_size(
                placement.x, placement.y, placement.width, placement.height
            ),
            rotated=placement.rotated,
            openings=openings,
            children=[convert(child) for child in placement.children],
        )

    return SheetProgram(
        header=header,
        parts=[convert(p) for p in placements],
        sheet_width=sheet_width,
        sheet_length=sheet_length,
    )


# --------------------------------------------------------------------------
# Sequencing plan
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureRef:
    """One cut, named by WHAT it is — never by where it is.

    A plan is pure sequencing: part index, feature kind, feature index, and
    the two stylistic choices the reference CAM makes per cut (which edge
    the tool leads in on, and which end of a groove it starts from).  No
    coordinate, no G-code and no feed ever travels in a plan; those all
    come from :class:`PostConfig` and the part geometry.

    ``kind`` is ``"perimeter"``, ``"opening"``, ``"groove"`` or
    ``"wdc_slot"``.  For ``"opening"``, ``index`` selects the part's
    opening.  For ``"groove"``, ``index`` is 0..3 = stile-low, rail-low,
    stile-high, rail-high in the part's own orientation.  For
    ``"wdc_slot"``, ``index`` is 0 or 1 = the low-side then high-side stile
    in sheet coordinates; ONE reference emits both depth passes, because the
    two bites of one slot are a property of the post table
    (:class:`WdcSlotSpec`), not a sequencing choice.
    """

    part: int
    kind: str
    index: int = 0
    #: Entry-edge override; ``None`` means "use the measured default rule".
    entry: str | None = None
    #: Cut the groove from its high end toward its low end.
    reverse: bool = False


@dataclass
class CutPlan:
    """Which feature is cut when, per section.

    ``perimeter`` holds ONE ordered list of parts PER DEPTH PASS, which is
    exactly the freedom the 2026-08-03 onion-skin amendment needs: pass 1
    (Z0.06, onion skin) may run every perimeter in one order and pass 2
    (Z-0.006, through) in another — inners before hosts.  Every reference
    file happens to use the same order for both passes.

    ``detail`` defaults to ``openings``: in all four reference files the
    T12 section repeats the T11 opening section's order AND its entry
    sides, cut for cut.
    """

    panel: list[FeatureRef] = field(default_factory=list)
    #: T17 stile slots, empty unless the sheet holds a WDC frame.
    wdc_slot: list[FeatureRef] = field(default_factory=list)
    openings: list[FeatureRef] = field(default_factory=list)
    perimeter: list[list[FeatureRef]] = field(default_factory=list)
    detail: list[FeatureRef] | None = None
    sections: tuple[str, ...] = DEFAULT_SECTIONS

    def detail_order(self) -> list[FeatureRef]:
        return self.openings if self.detail is None else self.detail
