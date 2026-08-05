"""Faceframe geometry engine.

Implements spec sections 2 (frame-type inference) and 3 (opening layout
rules) from ``docs/CLAUDE_CODE_PROMPT_Faceframe_Optimizer.md``. Pure
Python, stdlib only — no optimizer, GUI, spreadsheet parsing, or NC
generation lives here.

Coordinate system: frame-local, origin at the frame's lower-left corner,
x increasing across the frame width, y increasing up the frame height.
All members (stiles, rails, cross bars) are 1.5" wide/tall, EXCEPT the
stiles on a WDC frame, which are 2" wide (2026-08-03 amendment — see the
"Amendments" section of docs/CLAUDE_CODE_PROMPT_Faceframe_Optimizer.md).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "FrameType",
    "Opening",
    "FrameGeometry",
    "infer_frame_type",
    "compute_geometry",
    "WDC_SLOT_INSET_FROM_INSIDE_EDGE",
    "WDC_SLOT_DEPTH",
    "WDC_SLOT_END_REACH",
    "wdc_slot_axis_is_height",
]

MEMBER = 1.5
STILE_INSET = 1.5
WDC_STILE_INSET = 2.0

# --------------------------------------------------------------------------
# The WDC 45-degree stile slot (2026-08-03 amendment)
# --------------------------------------------------------------------------
#
# A WDC frame's 2" stiles do NOT take the standard T13 stile grooves; each
# gets a straight 45-degree V groove down its length so the frame can meet a
# diagonal-corner cabinet.  The three numbers below are the FRAME-side facts
# — where the slot runs and how far its cut reaches past the part — and they
# are what the optimizer needs to leave room for.  The machine-side table
# (tool, feeds, the two depth passes) lives in
# :class:`faceframe_cnc.post.model.WdcSlotSpec`, which derives the same reach
# from its own numbers; ``tests/test_nc_job.py`` cross-checks the two, so the
# optimizer cannot start reserving room for a slot the post no longer cuts.

#: Centreline distance from the stile's INSIDE (opening-side) edge: 34 mm.
#: The amendment's earlier 15/16" was a tape measurement and is superseded.
WDC_SLOT_INSET_FROM_INSIDE_EDGE = 1.3386

#: Total slot depth, 7/16".  Cut from the face-down back like the T13
#: grooves, so the slot bottom sits at machine Z0.3125 in 3/4" stock.
WDC_SLOT_DEPTH = 0.4375

#: How far the cut reaches past each END of a WDC stile, at the surface.
#:
#: The bit is 45 degrees per side, so at depth ``d`` its cutting surface has
#: radius ``d``.  The deepest pass runs its CENTRE ``d`` past the part end
#: (the amendment's "overrun by the tool's effective radius at that pass's
#: depth"), and the cone reaches a further ``d`` past that centre where it
#: breaks the surface — so the swept material ends ``2 * d`` = 0.875" beyond
#: the stile.  Anything within that of a WDC stile end gets carved, which is
#: why WDC parts need directional clearance the ordinary ``part_gap`` does
#: not give (see :class:`faceframe_cnc.nesting.NestingConfig`).
WDC_SLOT_END_REACH = 2.0 * WDC_SLOT_DEPTH


def wdc_slot_axis_is_height(rotated: bool) -> bool:
    """True when a WDC's slots (and so their end reach) run along sheet Y.

    The stiles run along the frame's HEIGHT axis, which the packer's 90
    degree counter-clockwise rotation maps onto the sheet's X axis — so an
    upright WDC reaches past its ends in Y and a rotated one in X.
    """
    return not rotated

_TOP_DRAWER_HEIGHT = 5.0
_MIDDLE_DRAWER_HEIGHT = 9.875


class FrameType(Enum):
    """The faceframe families this shop cuts (spec section 2, as amended).

    ``UNSUPPORTED_DRAWER_BASE`` (2026-08-04 review fix 4) is deliberately
    NOT a family this app knows how to cut: it marks a drawer-base part
    number (``2DB24``, ``4DB18``, ``MICRO3DB24``, ...) this app recognizes
    by name but whose cross-bar layout it does not implement. It exists so
    :func:`infer_frame_type` never has to fall back to ``WALL`` for one of
    these -- which would silently produce a single-opening frame with no
    drawer cross-bars, wrong for the real part -- and so
    :func:`compute_geometry` refuses (see ``_stack_template``'s existing
    "unhandled frame type" raise) instead of inventing geometry for it.
    ``order_parser.parse_order`` intercepts rows of this type before they
    ever reach :func:`compute_geometry` and routes them to
    ``needs_attention`` instead.
    """

    WALL = "wall"
    BASE = "base"
    THREE_DRAWER = "three_drawer"
    WDC = "wdc"
    UNSUPPORTED_DRAWER_BASE = "unsupported_drawer_base"


@dataclass(frozen=True)
class Opening:
    """A single routed-through opening in frame-local coordinates.

    ``x``/``y`` locate the opening's lower-left corner; ``width``/``height``
    are its size. ``label`` identifies the opening's role (e.g. "opening",
    "drawer", "door", "top", "middle", "bottom") so callers can tell which
    opening is which without relying on list position alone.
    """

    x: float
    y: float
    width: float
    height: float
    label: str


@dataclass
class FrameGeometry:
    """Computed geometry for one faceframe part.

    ``openings`` is ordered top-down (matching the order the spec describes
    the vertical stack), regardless of the openings' actual y coordinates.
    When ``errors`` is non-empty, ``openings`` is always empty — callers
    must never receive partial or garbage geometry for an invalid frame.
    """

    part_number: str
    frame_type: FrameType
    width: float
    height: float
    openings: list[Opening] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


#: A drawer-base family the shop's catalogue carries but whose cross-bar
#: layout this app does not implement (2026-08-04 review, fix 4): a
#: leading single digit + "DB" (``2DB24``, ``2DB30``, ``2DB33``, ``2DB36``,
#: ``4DB18``) or "MICRO" + digits + "DB" (``MICRO3DB24``, ``MICRO3DB27``,
#: ``MICRO3DB30``). ``3DB...`` is NOT one of these -- it is checked first,
#: above, and returns ``THREE_DRAWER``, a layout this app DOES know (spec
#: section 3). These families show up in the reference orders' "Drawer
#: Bases" catalogue section, always at qty 0 or "*" in both files on hand
#: -- never actually ordered yet, but nothing stops a future order line
#: from setting one to a real quantity.
_UNSUPPORTED_DRAWER_BASE = re.compile(r"^(?:\dDB|MICRO\d+DB)")


def infer_frame_type(part_number: str) -> FrameType:
    """Infer frame family from a part number prefix (spec section 2, as amended).

    Case-insensitive, whitespace-stripped. ``3DB...`` is a three-drawer
    frame. ``B...`` is a base (drawer-over-door) frame, EXCEPT ``BBC...``
    which is a plain wall-style frame despite starting with "B". ``WDC...``
    is a special wall-style frame with 2" stiles instead of the usual 1.5"
    (2026-08-03 amendment; the part name encodes the diagonal-corner
    cabinet's size, not the frame's). A leading digit + "DB", or "MICRO" +
    digits + "DB" (``2DB24``, ``4DB18``, ``MICRO3DB24``, ...), is a
    drawer-base family this app recognizes by name but cannot lay out
    (2026-08-04 review fix 4) -- returns ``UNSUPPORTED_DRAWER_BASE``,
    never ``WALL`` (a plain single-opening frame would be the wrong
    geometry: these have drawer cross-bars ``WALL`` doesn't model).
    Everything else (``W``, ``LS``, ``MC``, ``SB``, ``V``, ``OVD``, ...)
    is a wall-style single-opening frame with the standard 1.5" stiles.
    """
    normalized = part_number.strip().upper()
    if normalized.startswith("3DB"):
        return FrameType.THREE_DRAWER
    if normalized.startswith("BBC"):
        return FrameType.WALL
    if normalized.startswith("WDC"):
        return FrameType.WDC
    if _UNSUPPORTED_DRAWER_BASE.match(normalized):
        return FrameType.UNSUPPORTED_DRAWER_BASE
    if normalized.startswith("B"):
        return FrameType.BASE
    return FrameType.WALL


def _stack_template(frame_type: FrameType) -> list[tuple[str, str | None, float | None]]:
    """Top-down list of stack items: (kind, label, fixed_height).

    ``kind`` is "member" or "opening". A fixed_height of ``None`` marks the
    single opening in the stack that fills the remaining height.
    """
    if frame_type is FrameType.WALL or frame_type is FrameType.WDC:
        return [
            ("member", None, MEMBER),
            ("opening", "opening", None),
            ("member", None, MEMBER),
        ]
    if frame_type is FrameType.BASE:
        return [
            ("member", None, MEMBER),
            ("opening", "drawer", _TOP_DRAWER_HEIGHT),
            ("member", None, MEMBER),
            ("opening", "door", None),
            ("member", None, MEMBER),
        ]
    if frame_type is FrameType.THREE_DRAWER:
        return [
            ("member", None, MEMBER),
            ("opening", "top", _TOP_DRAWER_HEIGHT),
            ("member", None, MEMBER),
            ("opening", "middle", _MIDDLE_DRAWER_HEIGHT),
            ("member", None, MEMBER),
            ("opening", "bottom", None),
            ("member", None, MEMBER),
        ]
    raise ValueError(
        f"unhandled frame type: {frame_type!r} -- geometry is not implemented for it "
        "(2026-08-04 review fix 4: this is deliberate for UNSUPPORTED_DRAWER_BASE, "
        "which order_parser.parse_order should have already routed to needs_attention "
        "before it got here)"
    )


def compute_geometry(part_number: str, width: float, height: float) -> FrameGeometry:
    """Compute a faceframe's routed openings from its outside dimensions.

    Opening width is ``width - 2 * stile_width``, at x = stile_width. Stile
    width is 1.5" for every frame type except WDC, whose stiles are 2" wide
    (2026-08-03 amendment) — so a WDC opening is ``width - 4`` at x = 2.0.
    Opening heights follow the top-down vertical stack for the inferred
    frame type (spec section 3); WDC uses the same single-opening vertical
    stack as WALL (1.5" rail / opening / 1.5" rail). The stack's one
    flexible opening absorbs whatever height remains so the stack sums
    exactly to ``height``.

    Like WALL, WDC is a single-large-opening frame: for Milestone 3
    (frame-inside-frame nesting) it is eligible to host an inner frame in
    its opening, subject to the usual clearance rule.

    On any invalid input — non-finite/non-positive dimensions, an opening
    width or height that would be <= 0, or the stack failing to sum to
    ``height`` — returns a ``FrameGeometry`` with no openings and a
    human-readable message in ``errors``. Never returns partial geometry.
    """
    frame_type = infer_frame_type(part_number)
    errors: list[str] = []

    if not (math.isfinite(width) and width > 0):
        errors.append(f"invalid frame width {width!r}: must be a positive, finite number")
    if not (math.isfinite(height) and height > 0):
        errors.append(f"invalid frame height {height!r}: must be a positive, finite number")
    if errors:
        return FrameGeometry(part_number, frame_type, width, height, [], errors)

    stile_width = WDC_STILE_INSET if frame_type is FrameType.WDC else STILE_INSET
    opening_width = width - 2 * stile_width
    if opening_width <= 0:
        errors.append(
            f"frame width {width} is too narrow: opening width would be "
            f"{opening_width} (must be > 0)"
        )
        return FrameGeometry(part_number, frame_type, width, height, [], errors)

    template = _stack_template(frame_type)
    fixed_height_sum = sum(h for kind, _, h in template if h is not None)
    fill_height = height - fixed_height_sum
    if not (math.isfinite(fill_height) and fill_height > 0):
        errors.append(
            f"frame height {height} is too short for a {frame_type.value} frame: "
            f"remaining height for its last opening would be {fill_height} (must be > 0)"
        )
        return FrameGeometry(part_number, frame_type, width, height, [], errors)

    resolved = [
        (kind, label, (fill_height if h is None else h)) for kind, label, h in template
    ]
    for kind, label, h in resolved:
        if kind == "opening" and h <= 0:
            errors.append(f"opening {label!r} would have non-positive height {h}")
    if errors:
        return FrameGeometry(part_number, frame_type, width, height, [], errors)

    openings_bottom_up: list[Opening] = []
    cursor = 0.0
    for kind, label, h in reversed(resolved):
        if kind == "opening":
            assert label is not None
            openings_bottom_up.append(Opening(x=stile_width, y=cursor, width=opening_width, height=h, label=label))
        cursor += h

    if not math.isclose(cursor, height, rel_tol=0.0, abs_tol=1e-9):
        errors.append(
            f"internal invariant failed: vertical stack sums to {cursor}, expected {height}"
        )
        return FrameGeometry(part_number, frame_type, width, height, [], errors)

    openings_top_down = list(reversed(openings_bottom_up))
    return FrameGeometry(part_number, frame_type, width, height, openings_top_down, [])
