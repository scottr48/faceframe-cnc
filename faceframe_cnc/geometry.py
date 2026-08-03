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
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "FrameType",
    "Opening",
    "FrameGeometry",
    "infer_frame_type",
    "compute_geometry",
]

MEMBER = 1.5
STILE_INSET = 1.5
WDC_STILE_INSET = 2.0

_TOP_DRAWER_HEIGHT = 5.0
_MIDDLE_DRAWER_HEIGHT = 9.875


class FrameType(Enum):
    """The faceframe families this shop cuts (spec section 2, as amended)."""

    WALL = "wall"
    BASE = "base"
    THREE_DRAWER = "three_drawer"
    WDC = "wdc"


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


def infer_frame_type(part_number: str) -> FrameType:
    """Infer frame family from a part number prefix (spec section 2, as amended).

    Case-insensitive, whitespace-stripped. ``3DB...`` is a three-drawer
    frame. ``B...`` is a base (drawer-over-door) frame, EXCEPT ``BBC...``
    which is a plain wall-style frame despite starting with "B". ``WDC...``
    is a special wall-style frame with 2" stiles instead of the usual 1.5"
    (2026-08-03 amendment; the part name encodes the diagonal-corner
    cabinet's size, not the frame's). Everything else (``W``, ``LS``,
    ``MC``, ``SB``, ``V``, ``OVD``, ...) is a wall-style single-opening
    frame with the standard 1.5" stiles.
    """
    normalized = part_number.strip().upper()
    if normalized.startswith("3DB"):
        return FrameType.THREE_DRAWER
    if normalized.startswith("BBC"):
        return FrameType.WALL
    if normalized.startswith("WDC"):
        return FrameType.WDC
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
    raise ValueError(f"unhandled frame type: {frame_type!r}")


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
