"""Headless application model for the faceframe GUI (spec section 5).

Everything the desktop app can DO lives here; the Qt layer only renders
this object and forwards gestures to it.  No Qt import appears in this
module, on purpose — the whole workflow (load an order, tick lines on and
off, resolve a needs-attention line, optimize, then drag / rotate / nest
parts by hand) is exercised by ``tests/test_gui_session.py`` with no
display and, for the editing half, without pandas either.

Editing model
-------------
Manual edits never mutate the live :class:`~faceframe_cnc.nesting.NestingResult`.
Every edit is applied to a deep copy, re-checked with the independent
:func:`~faceframe_cnc.nesting.validate_layouts`, and only then swapped in.
An illegal edit therefore leaves the model untouched and returns the
violated rule, so the GUI's "snap back" behaviour is free: it simply
redraws the state it already had.

Spec 4c is honoured on the way in and on the way out:

*   editing one sheet of a run of N splits ONE physical sheet off into its
    own unique picture (the original run drops to N-1, the copy starts at
    1), because the user edited one sheet, not all N;
*   after the edit the pictures are re-grouped, so an edit that makes two
    pictures identical merges them back into a single run — and a sheet
    left with nothing on it disappears, taking its physical sheet with it.

Coordinates are the nesting module's: sheet origin at the lower-left, x
across the 49" width, y up the 97" length, and a rotated placement is the
frame turned 90 degrees COUNTER-CLOCKWISE (:func:`sheet_openings` below
reproduces exactly the transform :func:`faceframe_cnc.nesting.place_inner`
uses, and a test pins the two together).
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..geometry import FrameType, compute_geometry, infer_frame_type
from ..inside import candidate_fits
from ..nesting import (
    EPS,
    NestingConfig,
    NestingError,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    nest,
    place_inner,
    validate_layouts,
)

__all__ = [
    "SETTINGS_FILENAME",
    "AppSettings",
    "SessionError",
    "RowStatus",
    "OrderRow",
    "EditResult",
    "OpeningRect",
    "Session",
    "default_settings_path",
    "load_settings",
    "save_settings",
    "sheet_openings",
    "suggest_dimensions",
]

#: Part path: indices from the sheet's top-level placement list down through
#: ``children``.  ``(2,)`` is the third part on the sheet; ``(2, 0)`` is the
#: frame nested inside it.  Paths are recomputed after every edit (an edit
#: can renumber siblings), which is why each edit returns the new path.
PartPath = tuple[int, ...]


# --------------------------------------------------------------------------
# Settings (persisted to a local JSON file — no network, ever)
# --------------------------------------------------------------------------

SETTINGS_FILENAME = "faceframe_settings.json"


def default_settings_path() -> Path:
    """Settings file next to the app (the project/install root)."""
    return Path(__file__).resolve().parents[2] / SETTINGS_FILENAME


@dataclass
class AppSettings:
    """User-adjustable settings, persisted between runs.

    The nesting library defaults ``inside_nesting`` to off so Milestone 2
    behaviour is unchanged for library callers; the APP turns it on, since
    frame-inside-frame is the entire reason this program exists.
    """

    sheet_width: float = 49.0
    sheet_height: float = 97.0
    part_gap: float = 0.375
    edge_cushion: float = 0.5
    #: Spec 4a amendment (2026-08-03): soft front-edge target — see
    #: ``NestingConfig.front_margin``.
    front_margin: float = 1.0
    inside_nesting: bool = True
    #: Spec 4b: allow a nested frame to host a frame of its own.  Off by
    #: default; when off the validator rejects hand-built depth-2 nests.
    inside_recursion: bool = False
    last_order_path: Optional[str] = None

    def to_config(self) -> NestingConfig:
        """The optimizer config this app runs with.

        ``inside_baseline`` is always on: the summary panel's headline
        "sheets saved vs no-inside baseline" (spec 5) needs it.
        """
        return NestingConfig(
            sheet_width=float(self.sheet_width),
            sheet_height=float(self.sheet_height),
            part_gap=float(self.part_gap),
            edge_cushion=float(self.edge_cushion),
            front_margin=float(self.front_margin),
            inside_nesting=bool(self.inside_nesting),
            inside_recursion=bool(self.inside_recursion),
            inside_baseline=True,
        )

    def to_dict(self) -> dict:
        return {
            "sheet_width": float(self.sheet_width),
            "sheet_height": float(self.sheet_height),
            "part_gap": float(self.part_gap),
            "edge_cushion": float(self.edge_cushion),
            "front_margin": float(self.front_margin),
            "inside_nesting": bool(self.inside_nesting),
            "inside_recursion": bool(self.inside_recursion),
            "last_order_path": self.last_order_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        """Build from (possibly hand-edited, possibly stale) JSON.

        Unknown keys are ignored and unusable values fall back to the
        default, so a corrupt settings file can never stop the app from
        starting on the shop floor.
        """
        defaults = cls()
        if not isinstance(data, dict):
            return defaults

        def number(key: str, minimum: float) -> float:
            value = data.get(key, getattr(defaults, key))
            try:
                value = float(value)
            except (TypeError, ValueError):
                return float(getattr(defaults, key))
            if not math.isfinite(value) or value < minimum:
                return float(getattr(defaults, key))
            return value

        def flag(key: str) -> bool:
            value = data.get(key, getattr(defaults, key))
            return bool(value) if isinstance(value, (bool, int)) else bool(getattr(defaults, key))

        path = data.get("last_order_path")
        return cls(
            sheet_width=number("sheet_width", 1e-6),
            sheet_height=number("sheet_height", 1e-6),
            part_gap=number("part_gap", 0.0),
            edge_cushion=number("edge_cushion", 0.0),
            front_margin=number("front_margin", 0.0),
            inside_nesting=flag("inside_nesting"),
            inside_recursion=flag("inside_recursion"),
            last_order_path=path if isinstance(path, str) and path else None,
        )

    def validate(self) -> list[str]:
        """Human-readable problems with these settings, ``[]`` when usable."""
        problems: list[str] = []
        if not (math.isfinite(self.sheet_width) and self.sheet_width > 0):
            problems.append("sheet width must be a positive number")
        if not (math.isfinite(self.sheet_height) and self.sheet_height > 0):
            problems.append("sheet height must be a positive number")
        if not (math.isfinite(self.part_gap) and self.part_gap >= 0):
            problems.append("part gap must be zero or more")
        if not (math.isfinite(self.edge_cushion) and self.edge_cushion >= 0):
            problems.append("edge cushion must be zero or more")
        if not (math.isfinite(self.front_margin) and self.front_margin >= 0):
            problems.append("front edge margin must be zero or more")
        return problems


def load_settings(path: Optional[Path | str] = None) -> AppSettings:
    """Read settings from disk; defaults on a missing or unreadable file."""
    target = Path(path) if path is not None else default_settings_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            return AppSettings.from_dict(json.load(handle))
    except (OSError, ValueError):
        return AppSettings()


def save_settings(settings: AppSettings, path: Optional[Path | str] = None) -> bool:
    """Write settings to disk.  ``False`` when the file could not be written.

    Never raises: a read-only install directory is a nuisance, not a reason
    to lose the user's session.
    """
    target = Path(path) if path is not None else default_settings_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(settings.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Order rows
# --------------------------------------------------------------------------


class SessionError(RuntimeError):
    """A request the session cannot carry out, with a message fit for the UI."""


class RowStatus(Enum):
    READY = "ready"
    NEEDS_ATTENTION = "needs attention"
    #: 2026-08-03 amendment ("SD1212 / no-faceframe lines"): QTY > 0 with
    #: BOTH frame dims missing -- e.g. SD1212, a sample door whose order
    #: form shows N/A for the faceframe. Auto-excluded and shown
    #: informationally, never prompted for; still manually resolvable.
    NO_FRAME = "no faceframe"
    INVALID = "invalid"


_DIGITS = re.compile(r"(\d{2})(\d{2})$")


def suggest_dimensions(part_number: str) -> tuple[Optional[float], Optional[float]]:
    """Best guess at (width, height) encoded in a part number, or ``(None, None)``.

    ``W3036`` encodes a 30 x 36 frame, so offering that as a prefill saves
    typing.  WDC is deliberately excluded: its name encodes the DIAGONAL
    CORNER CABINET size, not the frame (WDC2436 is an 18 x 36 frame — the
    2026-08-03 amendment), and a wrong prefill on the one part the shop
    already gets wrong would be worse than no prefill at all.
    """
    normalized = part_number.strip().upper()
    if infer_frame_type(normalized) is FrameType.WDC:
        return None, None
    match = _DIGITS.search(normalized)
    if match is None:
        return None, None
    width, height = float(match.group(1)), float(match.group(2))
    if width <= 0 or height <= 0:
        return None, None
    return width, height


@dataclass
class OrderRow:
    """One line of the order as the GUI shows it.

    Rows the parser could not fully read (``missing`` non-empty) start
    excluded and cannot be included until the user supplies the dimension —
    spec section 2's "do NOT silently guess".
    """

    key: str
    part_number: str
    qty: int
    frame_width: Optional[float] = None
    frame_height: Optional[float] = None
    included: bool = True
    #: Which of ``("width", "height")`` the spreadsheet did not supply.
    missing: tuple[str, ...] = ()
    #: Free-text explanation shown beside a needs-attention row.
    reason: str = ""
    #: Source spreadsheet row, for tracing back to the order.
    row_index: int = -1
    #: Set once the user has typed the missing dimension.
    resolved: bool = False

    @property
    def frame_type(self) -> FrameType:
        return infer_frame_type(self.part_number)

    @property
    def status(self) -> RowStatus:
        if self.missing:
            # 2026-08-03 amendment: missing BOTH dims is "no faceframe
            # required" (e.g. SD1212), not "needs attention" -- only a
            # row missing exactly ONE dim (e.g. WDC2436) is prompted for.
            if len(self.missing) >= 2:
                return RowStatus.NO_FRAME
            return RowStatus.NEEDS_ATTENTION
        if self.geometry_error is not None:
            return RowStatus.INVALID
        return RowStatus.READY

    @property
    def geometry_error(self) -> Optional[str]:
        """Why this frame's geometry is unusable, or ``None``.

        Spec section 3: a frame too short for its pattern is flagged in the
        UI instead of quietly producing garbage openings.
        """
        if self.frame_width is None or self.frame_height is None:
            return None
        geometry = compute_geometry(self.part_number, self.frame_width, self.frame_height)
        return geometry.errors[0] if geometry.errors else None

    @property
    def can_include(self) -> bool:
        return self.status is RowStatus.READY

    @property
    def size_text(self) -> str:
        if self.status is RowStatus.NO_FRAME:
            # 2026-08-03 amendment: the order form says N/A -- show that,
            # not "? x ?" (which reads as a data-entry gap to chase down).
            return "n/a"

        def one(value: Optional[float]) -> str:
            if value is None:
                return "?"
            return f"{value:g}"

        return f"{one(self.frame_width)} x {one(self.frame_height)}"

    @property
    def type_text(self) -> str:
        return self.frame_type.value

    @property
    def hint(self) -> str:
        """Guidance for the needs-attention editor."""
        if not self.missing:
            return ""
        if self.status is RowStatus.NO_FRAME:
            return (
                "no faceframe — order form says N/A (sample door); resolve "
                "only if the form is wrong"
            )
        if self.frame_type is FrameType.WDC:
            return (
                "WDC part names encode the diagonal-corner CABINET size, not the "
                "frame: WDC2436 is an 18 x 36 frame with 2\" stiles."
            )
        width, height = suggest_dimensions(self.part_number)
        if width is not None:
            return f"the part number suggests {width:g} x {height:g}; confirm before using"
        return "no dimension can be inferred from the part number; ask the office"


# --------------------------------------------------------------------------
# Geometry helpers shared by the canvas and the editing rules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OpeningRect:
    """One routed opening of a placed part, in SHEET coordinates."""

    x: float
    y: float
    width: float
    height: float
    label: str
    index: int

    @property
    def area(self) -> float:
        return self.width * self.height

    def contains(self, x: float, y: float) -> bool:
        return (
            self.x - EPS <= x <= self.x + self.width + EPS
            and self.y - EPS <= y <= self.y + self.height + EPS
        )


def ordered_dims(placement: Placement, ordered: dict[str, PartSpec]) -> tuple[float, float]:
    """The part's as-ordered ``(width, height)`` (undoing any rotation)."""
    spec = ordered.get(placement.part_number)
    if spec is not None:
        return spec.width, spec.height
    return (
        (placement.height, placement.width) if placement.rotated else (placement.width, placement.height)
    )


def sheet_openings(
    placement: Placement, ordered: dict[str, PartSpec] | None = None
) -> list[OpeningRect]:
    """A placed part's routed openings, transformed onto the sheet.

    This is the same transform :func:`faceframe_cnc.nesting.place_inner` and
    the validator apply: a rotated placement is the frame turned 90 degrees
    counter-clockwise, so frame-local ``(lx, ly, w, h)`` lands at
    ``(ordered_height - ly - h, lx, h, w)`` within the placed footprint —
    and ``ordered_height`` is simply the placed width once rotated.  A test
    pins this against ``place_inner`` so the two can never drift.
    """
    width, height = ordered_dims(placement, ordered or {})
    geometry = compute_geometry(placement.part_number, width, height)
    if geometry.errors:
        return []
    rects: list[OpeningRect] = []
    for index, opening in enumerate(geometry.openings):
        if placement.rotated:
            rects.append(
                OpeningRect(
                    x=placement.x + (placement.width - opening.y - opening.height),
                    y=placement.y + opening.x,
                    width=opening.height,
                    height=opening.width,
                    label=opening.label,
                    index=index,
                )
            )
        else:
            rects.append(
                OpeningRect(
                    x=placement.x + opening.x,
                    y=placement.y + opening.y,
                    width=opening.width,
                    height=opening.height,
                    label=opening.label,
                    index=index,
                )
            )
    return rects


def _walk(placements: Sequence[Placement], prefix: PartPath = ()):
    """Yield ``(path, placement)`` depth-first, parents before children."""
    for index, placement in enumerate(placements):
        path = prefix + (index,)
        yield path, placement
        yield from _walk(placement.children, path)


def _at(layout: SheetLayout, path: PartPath) -> Placement:
    items: Sequence[Placement] = layout.placements
    placement: Optional[Placement] = None
    for index in path:
        if index < 0 or index >= len(items):
            raise SessionError(f"no part at path {path}")
        placement = items[index]
        items = placement.children
    if placement is None:
        raise SessionError("empty part path")
    return placement


def _siblings(layout: SheetLayout, path: PartPath) -> list[Placement]:
    if len(path) == 1:
        return layout.placements
    return _at(layout, path[:-1]).children


def _path_of(layout: SheetLayout, target: Placement) -> Optional[PartPath]:
    """Locate a placement, by identity first then by value.

    The value fallback matters after a re-group: when the edited picture
    turns out to be identical to an existing one it is merged away, and the
    surviving layout holds an equal-but-not-identical placement.
    """
    for path, placement in _walk(layout.placements):
        if placement is target:
            return path
    for path, placement in _walk(layout.placements):
        if (
            placement.part_number == target.part_number
            and placement.rotated == target.rotated
            and abs(placement.x - target.x) <= 1e-7
            and abs(placement.y - target.y) <= 1e-7
            and abs(placement.width - target.width) <= 1e-7
            and abs(placement.height - target.height) <= 1e-7
        ):
            return path
    return None


def _translate(placement: Placement, dx: float, dy: float) -> None:
    placement.x = round(placement.x + dx, 9)
    placement.y = round(placement.y + dy, 9)
    for child in placement.children:
        _translate(child, dx, dy)


def _rotate_about(placement: Placement, cx: float, cy: float) -> None:
    """Turn a placement (and everything nested in it) 90 degrees CCW about a point.

    Rotating the whole subtree about the HOST's centre is what keeps a
    nested frame centred in its opening: the opening rotates by exactly the
    same transform, so a legal nest stays legal through a rotation.
    """
    new_x = cx - (placement.y + placement.height - cy)
    new_y = cy + (placement.x - cx)
    children = list(placement.children)
    placement.x = round(new_x, 9)
    placement.y = round(new_y, 9)
    placement.width, placement.height = placement.height, placement.width
    placement.rotated = not placement.rotated
    for child in children:
        _rotate_about(child, cx, cy)


def _conflicts(a: Placement, bx: float, by: float, bw: float, bh: float, gap: float) -> bool:
    half = gap / 2.0
    overlap_x = min(a.x + a.width + half, bx + bw + half) - max(a.x - half, bx - half)
    overlap_y = min(a.y + a.height + half, by + bh + half) - max(a.y - half, by - half)
    return overlap_x > EPS and overlap_y > EPS


class _Picture:
    """A unique sheet picture and how many physical sheets are cut from it.

    A mutable, identity-compared stand-in for the
    ``(SheetLayout, run)`` tuples in a :class:`NestingResult`, used while an
    edit is being trialled so the code can follow one particular sheet
    through a split and a re-group without juggling indices.
    """

    __slots__ = ("layout", "run")

    def __init__(self, layout: SheetLayout, run: int):
        self.layout = layout
        self.run = run


@dataclass
class EditResult:
    """Outcome of one manual layout edit.

    Falsy when the edit was rejected, in which case ``message`` is the
    violated rule, verbatim from
    :func:`~faceframe_cnc.nesting.validate_layouts`, and the session is
    byte-for-byte unchanged — which is all the GUI needs to "snap back".
    """

    ok: bool
    message: str = ""
    sheet_index: int = -1
    path: PartPath = ()
    #: True when spec 4c split one physical sheet out of a run for this edit.
    split: bool = False
    #: True when the edit made two pictures identical and they merged.
    merged: bool = False

    def __bool__(self) -> bool:
        return self.ok


def _strip_sheet_number(problem: str) -> str:
    """Drop the "sheet N: " prefix so problems compare across a re-numbering."""
    return re.sub(r"^sheet \d+: ", "", problem)


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class Session:
    """The whole application state, minus the pixels."""

    def __init__(self, settings: Optional[AppSettings] = None):
        self.settings = settings if settings is not None else AppSettings()
        self.order_path: Optional[str] = None
        self.rows: list[OrderRow] = []
        self.result: Optional[NestingResult] = None
        #: Rows changed since the last optimize (the GUI greys out the layout).
        self.dirty: bool = False
        #: True once the layout has been edited by hand since the last optimize.
        self.edited: bool = False
        self.skipped_rows: int = 0
        self._problems: list[str] = []

    # -- order -----------------------------------------------------------

    def load_order(self, path: str) -> None:
        """Parse an order spreadsheet into :attr:`rows` (needs pandas + xlrd).

        Imported lazily so a machine without pandas can still run every
        other part of the model — and so the GUI can report a missing
        dependency as a message box instead of an import crash.
        """
        try:
            from ..order_parser import parse_order
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise SessionError(
                "reading .xls order files needs pandas and xlrd installed "
                f"({exc})"
            ) from exc

        try:
            parsed = parse_order(path)
        except Exception as exc:  # noqa: BLE001 - any parse failure is user-facing
            raise SessionError(f"could not read {path}: {exc}") from exc

        rows: list[OrderRow] = []
        for line in parsed.lines:
            rows.append(
                OrderRow(
                    key=f"r{line.row_index}:{line.part_number}",
                    part_number=line.part_number,
                    qty=line.qty,
                    frame_width=line.frame_width,
                    frame_height=line.frame_height,
                    included=True,
                    row_index=line.row_index,
                )
            )
        for line in parsed.needs_attention:
            rows.append(
                OrderRow(
                    key=f"r{line.row_index}:{line.part_number}",
                    part_number=line.part_number,
                    qty=line.qty,
                    frame_width=line.frame_width,
                    frame_height=line.frame_height,
                    included=False,
                    missing=tuple(line.missing),
                    reason=line.reason,
                    row_index=line.row_index,
                )
            )
        for line in parsed.no_frame:
            # 2026-08-03 amendment: both dims missing -- "no faceframe
            # required" (spec: shown informationally, never prompted for).
            rows.append(
                OrderRow(
                    key=f"r{line.row_index}:{line.part_number}",
                    part_number=line.part_number,
                    qty=line.qty,
                    frame_width=line.frame_width,
                    frame_height=line.frame_height,
                    included=False,
                    missing=tuple(line.missing),
                    reason=line.reason,
                    row_index=line.row_index,
                )
            )
        rows.sort(key=lambda row: (row.row_index, row.part_number))

        self.rows = rows
        self.skipped_rows = parsed.skipped_rows
        self.order_path = path
        self.settings.last_order_path = path
        self.result = None
        self._problems = []
        self.dirty = True
        self.edited = False

    def set_rows(self, rows: Iterable[OrderRow]) -> None:
        """Install rows directly — used by tests and by fixture orders."""
        self.rows = list(rows)
        self.result = None
        self._problems = []
        self.dirty = True
        self.edited = False

    def row(self, key: str) -> OrderRow:
        for row in self.rows:
            if row.key == key:
                return row
        raise SessionError(f"no order row {key!r}")

    def ready_rows(self) -> list[OrderRow]:
        return [row for row in self.rows if row.status is RowStatus.READY]

    def needs_attention_rows(self) -> list[OrderRow]:
        return [row for row in self.rows if row.status is RowStatus.NEEDS_ATTENTION]

    def no_frame_rows(self) -> list[OrderRow]:
        """Rows with QTY > 0 and BOTH frame dims missing (2026-08-03 amendment).

        Shown informationally elsewhere in the UI -- never in the
        needs-attention list, and never prompted for.
        """
        return [row for row in self.rows if row.status is RowStatus.NO_FRAME]

    def invalid_rows(self) -> list[OrderRow]:
        return [row for row in self.rows if row.status is RowStatus.INVALID]

    def included_rows(self) -> list[OrderRow]:
        return [row for row in self.rows if row.included and row.status is RowStatus.READY]

    def set_included(self, key: str, included: bool) -> None:
        """Tick a line in or out of the cut list (spec 5, order panel)."""
        row = self.row(key)
        if included and not row.can_include:
            raise SessionError(
                f"{row.part_number} cannot be included until its "
                f"{' and '.join(row.missing) or 'geometry'} is resolved"
            )
        if row.included != included:
            row.included = included
            self.dirty = True

    def set_all_included(self, included: bool) -> None:
        for row in self.rows:
            if included and not row.can_include:
                continue
            if row.included != included:
                row.included = included
                self.dirty = True

    def resolve_row(
        self,
        key: str,
        *,
        width: Optional[float] = None,
        height: Optional[float] = None,
        include: bool = True,
    ) -> OrderRow:
        """Supply the dimension the spreadsheet was missing (spec section 2).

        Only the missing dimensions are taken; a value the sheet already
        had is never overwritten here.  Raises :class:`SessionError` when
        the row is still incomplete or the value is not a usable size.
        """
        row = self.row(key)
        if not row.missing:
            raise SessionError(f"{row.part_number} is not missing any dimension")

        def coerce(name: str, value: object) -> Optional[float]:
            if value is None or value == "":
                return None
            try:
                number = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise SessionError(f"{name} {value!r} is not a number") from None
            if not math.isfinite(number) or number <= 0:
                raise SessionError(f"{name} must be a positive number, got {value!r}")
            return number

        new_width = coerce("width", width)
        new_height = coerce("height", height)
        resolved_width = row.frame_width if row.frame_width is not None else new_width
        resolved_height = row.frame_height if row.frame_height is not None else new_height

        still_missing = [
            name
            for name, value in (("width", resolved_width), ("height", resolved_height))
            if value is None
        ]
        if still_missing:
            raise SessionError(
                f"{row.part_number} still needs a frame {' and '.join(still_missing)}"
            )

        before = (row.frame_width, row.frame_height, row.missing, row.reason, row.resolved)
        row.frame_width = resolved_width
        row.frame_height = resolved_height
        row.missing = ()
        row.resolved = True
        row.reason = ""

        error = row.geometry_error
        if error is not None:
            # Leave the row exactly as it was rather than let a frame that
            # cannot produce openings reach the optimizer (spec section 3).
            (row.frame_width, row.frame_height, row.missing, row.reason, row.resolved) = before
            raise SessionError(
                f"{row.part_number} {resolved_width:g} x {resolved_height:g}: {error}"
            )

        row.included = bool(include)
        self.dirty = True
        return row

    # -- demand ----------------------------------------------------------

    def demand(self) -> list[PartSpec]:
        """The included lines as optimizer input, merged by part number."""
        merged: dict[str, PartSpec] = {}
        for row in self.included_rows():
            assert row.frame_width is not None and row.frame_height is not None
            name = row.part_number.strip()
            existing = merged.get(name)
            if existing is None:
                merged[name] = PartSpec(name, row.frame_width, row.frame_height, row.qty)
                continue
            if (
                abs(existing.width - row.frame_width) > EPS
                or abs(existing.height - row.frame_height) > EPS
            ):
                raise SessionError(
                    f"{name} appears twice in the order with different sizes "
                    f"({existing.width:g} x {existing.height:g} and "
                    f"{row.frame_width:g} x {row.frame_height:g}) — "
                    f"exclude one of the lines or correct the order file"
                )
            merged[name] = PartSpec(
                name, existing.width, existing.height, existing.qty + row.qty
            )
        return [merged[name] for name in sorted(merged)]

    @property
    def total_frames(self) -> int:
        return sum(row.qty for row in self.included_rows())

    # -- optimize --------------------------------------------------------

    def optimize(self) -> NestingResult:
        """Run the optimizer over the included lines (discards manual edits)."""
        config = self.settings.to_config()
        problems = self.settings.validate()
        if problems:
            raise SessionError("; ".join(problems))
        demand = self.demand()
        try:
            result = nest(demand, config)
        except NestingError as exc:
            raise SessionError(str(exc)) from exc
        self.result = result
        self._problems = validate_layouts(result, config) if demand else []
        self.dirty = False
        self.edited = False
        return result

    def set_result(self, result: NestingResult) -> None:
        """Install a layout the optimizer did not produce.

        The seam tests use to start from a known layout instead of whatever
        the packer happens to emit; it re-runs the validator so the session
        knows which problems (if any) it inherited.
        """
        self.result = result
        self._problems = validate_layouts(result, result.config)
        self.dirty = False
        self.edited = False

    # -- layout access ---------------------------------------------------

    @property
    def config(self) -> NestingConfig:
        return self.result.config if self.result is not None else self.settings.to_config()

    @property
    def sheets(self) -> list[tuple[SheetLayout, int]]:
        return list(self.result.unique_sheets) if self.result is not None else []

    @property
    def unique_sheet_count(self) -> int:
        return len(self.sheets)

    @property
    def total_sheets(self) -> int:
        return self.result.total_sheets if self.result is not None else 0

    def sheet(self, index: int) -> tuple[SheetLayout, int]:
        sheets = self.sheets
        if not sheets:
            raise SessionError("no layout — run the optimizer first")
        if index < 0 or index >= len(sheets):
            raise SessionError(f"sheet {index + 1} does not exist")
        return sheets[index]

    def ordered_specs(self) -> dict[str, PartSpec]:
        if self.result is None:
            return {}
        return {spec.part_number: spec for spec in self.result.demand}

    def sheet_openings(self, placement: Placement) -> list[OpeningRect]:
        return sheet_openings(placement, self.ordered_specs())

    def sheet_contents(self, index: int) -> str:
        """One-line contents summary for the unique-sheet list."""
        layout, _run = self.sheet(index)
        parts = ", ".join(f"{n}x{pn}" for pn, n in sorted(layout.part_counts().items()))
        nested = layout.child_count()
        return f"{parts} ({nested} nested)" if nested else parts

    def sheet_title(self, index: int) -> str:
        """Header text for the sheet preview (spec 5)."""
        if self.result is None or not self.sheets:
            return "No layout yet"
        _layout, run = self.sheet(index)
        return f"Sheet {index + 1} of {self.unique_sheet_count} — run quantity {run}"

    def problems(self) -> list[str]:
        return list(self._problems)

    # -- hit testing (pure geometry, so the canvas stays dumb) -----------

    def hit_test(self, sheet_index: int, x: float, y: float) -> Optional[PartPath]:
        """Deepest part whose footprint contains the point, or ``None``.

        Deepest-first so clicking a nested frame grabs the frame, not the
        host it is sitting in.
        """
        layout, _run = self.sheet(sheet_index)
        best: Optional[PartPath] = None
        best_depth = -1
        for path, placement in _walk(layout.placements):
            if (
                placement.x - EPS <= x <= placement.x + placement.width + EPS
                and placement.y - EPS <= y <= placement.y + placement.height + EPS
            ):
                if len(path) > best_depth:
                    best_depth = len(path)
                    best = path
        return best

    def opening_at(
        self, sheet_index: int, x: float, y: float, *, exclude: PartPath = ()
    ) -> Optional[tuple[PartPath, OpeningRect]]:
        """Smallest routed opening containing the point, and whose part it is.

        ``exclude`` suppresses a part and everything nested inside it, so a
        part being dragged is never offered its own opening as a target.
        """
        layout, _run = self.sheet(sheet_index)
        ordered = self.ordered_specs()
        best: Optional[tuple[PartPath, OpeningRect]] = None
        for path, placement in _walk(layout.placements):
            if exclude and path[: len(exclude)] == exclude:
                continue
            for rect in sheet_openings(placement, ordered):
                if rect.contains(x, y) and (best is None or rect.area < best[1].area):
                    best = (path, rect)
        return best

    def plan_drop(
        self, sheet_index: int, path: PartPath, x: float, y: float
    ) -> tuple[str, Optional[PartPath]]:
        """What a drop at ``(x, y)`` means: ``("move"|"nest"|"unnest", host)``.

        The dragged part's CENTRE decides, not the cursor: dropping a frame
        so that it visually sits in an opening is the gesture the user
        thinks they are making, and validation still has the last word on
        whether it actually fits.
        """
        layout, _run = self.sheet(sheet_index)
        placement = _at(layout, path)
        centre = (x + placement.width / 2.0, y + placement.height / 2.0)
        target = self.opening_at(sheet_index, centre[0], centre[1], exclude=path)
        parent = path[:-1] if len(path) > 1 else None
        if target is not None:
            host_path, _rect = target
            if parent is not None and host_path == parent:
                return "move", host_path
            return "nest", host_path
        if parent is not None:
            return "unnest", None
        return "move", None

    def preview_drop(
        self, sheet_index: int, path: PartPath, x: float, y: float
    ) -> EditResult:
        """Ask what :meth:`apply_drop` WOULD do, changing nothing.

        Used for the live drag feedback (spec 5: "live collision/clearance
        checking").  Safe because every edit builds a fresh result object
        instead of mutating the current one, so putting the old object back
        is a complete undo.
        """
        saved_result = self.result
        saved_problems = list(self._problems)
        saved_edited = self.edited
        try:
            return self.apply_drop(sheet_index, path, x, y)
        finally:
            self.result = saved_result
            self._problems = saved_problems
            self.edited = saved_edited

    def apply_drop(
        self, sheet_index: int, path: PartPath, x: float, y: float
    ) -> EditResult:
        """Carry out whatever gesture :meth:`plan_drop` says this drop is."""
        action, host_path = self.plan_drop(sheet_index, path, x, y)
        if action == "nest":
            assert host_path is not None
            return self.nest_part(sheet_index, path, host_path, x=x, y=y)
        if action == "unnest":
            return self.unnest_part(sheet_index, path, x=x, y=y)
        return self.move_part(sheet_index, path, x, y)

    # -- editing ---------------------------------------------------------

    def _work(self) -> list[_Picture]:
        if self.result is None:
            raise SessionError("no layout — run the optimizer first")
        return [
            _Picture(copy.deepcopy(layout), run) for layout, run in self.result.unique_sheets
        ]

    @staticmethod
    def _split(work: list[_Picture], index: int) -> tuple[_Picture, bool]:
        """Spec 4c: peel ONE physical sheet off a run so it can be edited alone.

        The user edited one sheet on the shop floor, not all N of them, so a
        run of 5 becomes a run of 4 plus a new unique picture of 1.  The new
        picture is inserted right after the original, keeping the preview's
        sheet order stable around the edit.
        """
        picture = work[index]
        if picture.run > 1:
            picture.run -= 1
            clone = _Picture(copy.deepcopy(picture.layout), 1)
            work.insert(index + 1, clone)
            return clone, True
        return picture, False

    def _finish(
        self,
        work: list[_Picture],
        focus: _Picture,
        target: Placement,
        split: bool,
        description: str,
    ) -> EditResult:
        """Re-group, validate on the trial copy, and commit only if legal."""
        assert self.result is not None
        config = self.config

        # Spec 4c, the other direction: an edit can make two pictures
        # identical, and identical pictures are one NC program and one run.
        # A picture emptied by the edit stops existing at all, and its
        # physical sheets stop being cut.
        grouped: list[_Picture] = []
        by_canonical: dict[str, _Picture] = {}
        merged = False
        landed = focus
        for picture in work:
            if not picture.layout.placements:
                continue
            key = picture.layout.canonical()
            existing = by_canonical.get(key)
            if existing is None:
                by_canonical[key] = picture
                grouped.append(picture)
            else:
                existing.run += picture.run
                if picture is focus:
                    landed = existing
                    merged = True
                elif existing is focus:
                    merged = True

        trial = NestingResult(
            unique_sheets=[(p.layout, p.run) for p in grouped],
            total_sheets=sum(p.run for p in grouped),
            demand=self.result.demand,
            config=config,
            inside_placements=sum(p.layout.child_count() * p.run for p in grouped),
            baseline_sheets=self.result.baseline_sheets,
        )

        problems = validate_layouts(trial, config)
        introduced = self._new_problems(problems)
        if introduced:
            return EditResult(False, introduced[0])

        self.result = trial
        self._problems = problems
        self.edited = True
        # ``landed`` is always still in ``grouped`` (every edit leaves at
        # least the edited part on its picture); the fallback only keeps a
        # future edit that empties the focused sheet from raising here.
        if landed in grouped:
            sheet_index = grouped.index(landed)
            new_path = _path_of(landed.layout, target) or ()
        else:
            sheet_index = 0 if grouped else -1
            new_path = ()
        return EditResult(
            True,
            description,
            sheet_index=sheet_index,
            path=new_path,
            split=split,
            merged=merged,
        )

    def _new_problems(self, problems: list[str]) -> list[str]:
        """Problems this edit introduced, ignoring any it inherited.

        The optimizer's own output validates clean, so this is normally just
        ``problems``; the diff exists so that a layout loaded in an odd state
        cannot freeze the user out of editing it.
        """
        before = Counter(_strip_sheet_number(p) for p in self._problems)
        introduced: list[str] = []
        for problem in problems:
            key = _strip_sheet_number(problem)
            if before[key] > 0:
                before[key] -= 1
            else:
                introduced.append(problem)
        return introduced

    def move_part(self, sheet_index: int, path: PartPath, x: float, y: float) -> EditResult:
        """Move a part (and anything nested in it) to a new lower-left corner."""
        work = self._work()
        self.sheet(sheet_index)
        picture, split = self._split(work, sheet_index)
        target = _at(picture.layout, path)
        _translate(target, x - target.x, y - target.y)
        return self._finish(
            work, picture, target, split, f"moved {target.part_number}"
        )

    def nudge_part(
        self, sheet_index: int, path: PartPath, dx: float, dy: float
    ) -> EditResult:
        placement = _at(self.sheet(sheet_index)[0], path)
        return self.move_part(sheet_index, path, placement.x + dx, placement.y + dy)

    def rotate_part(self, sheet_index: int, path: PartPath) -> EditResult:
        """Turn a part 90 degrees about its own centre (spec 5: the R key)."""
        work = self._work()
        self.sheet(sheet_index)
        picture, split = self._split(work, sheet_index)
        target = _at(picture.layout, path)
        cx = target.x + target.width / 2.0
        cy = target.y + target.height / 2.0
        _rotate_about(target, cx, cy)
        return self._finish(
            work, picture, target, split, f"rotated {target.part_number}"
        )

    def move_part_to_sheet(
        self,
        sheet_index: int,
        path: PartPath,
        destination_index: int,
        *,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> EditResult:
        """Move a part onto another sheet, at ``(x, y)`` or the first free spot."""
        if destination_index == sheet_index:
            raise SessionError("that part is already on this sheet")
        self.sheet(sheet_index)
        self.sheet(destination_index)
        work = self._work()

        # Split BOTH sheets before touching either: each is one physical
        # sheet being changed, so each owes spec 4c a picture of its own.
        source, split_source = self._split(work, sheet_index)
        destination_index_shifted = destination_index + (
            1 if split_source and destination_index > sheet_index else 0
        )
        destination, split_destination = self._split(work, destination_index_shifted)

        target = _at(source.layout, path)
        if x is None or y is None:
            spot = self._free_spot(destination.layout, target.width, target.height)
            if spot is None:
                return EditResult(
                    False,
                    f"no free space on sheet {destination_index + 1} for "
                    f"{target.part_number} ({target.width:g} x {target.height:g})",
                )
            x, y = spot
        _siblings(source.layout, path).remove(target)
        _translate(target, x - target.x, y - target.y)
        destination.layout.placements.append(target)
        return self._finish(
            work,
            destination,
            target,
            split_source or split_destination,
            f"moved {target.part_number} to sheet {destination_index + 1}",
        )

    def nest_part(
        self,
        sheet_index: int,
        path: PartPath,
        host_path: PartPath,
        *,
        x: Optional[float] = None,
        y: Optional[float] = None,
        opening_index: Optional[int] = None,
    ) -> EditResult:
        """Make a part a child of a host's opening (spec 4b, manual drag).

        With ``x`` / ``y`` the dropped position is kept — the user placed it
        there — and validation decides whether it clears the opening.
        Without them the part is CENTRED in the best-fitting opening, the
        placement rule spec 4b gives for automatic nesting; pass
        ``opening_index`` to insist on a particular opening.

        Manual nesting deliberately allows more than one inner per host:
        spec 4b prefers one, but permits multiples "if the user drags them
        in manually".  The clearance rules are not relaxed — the two inners
        must clear each other and the opening like any other parts.
        """
        if host_path[: len(path)] == path:
            raise SessionError("a part cannot be nested inside itself")
        work = self._work()
        self.sheet(sheet_index)
        picture, split = self._split(work, sheet_index)
        target = _at(picture.layout, path)
        host = _at(picture.layout, host_path)

        if x is None or y is None:
            spot = self._centre_spot(host, target, opening_index)
            if spot is None:
                return EditResult(
                    False,
                    f"{target.part_number} ({target.width:g} x {target.height:g}) does not "
                    f"fit any opening of {host.part_number} with "
                    f"{self.config.inner_clearance:g}\" clearance",
                )
            x, y = spot

        _siblings(picture.layout, path).remove(target)
        _translate(target, x - target.x, y - target.y)
        host.children.append(target)
        return self._finish(
            work,
            picture,
            target,
            split,
            f"nested {target.part_number} inside {host.part_number}",
        )

    def centre_in_opening(
        self, sheet_index: int, path: PartPath, opening_index: Optional[int] = None
    ) -> EditResult:
        """Re-centre an already-nested part in its host's opening (spec 4b)."""
        if len(path) < 2:
            raise SessionError("that part is not nested inside anything")
        work = self._work()
        self.sheet(sheet_index)
        picture, split = self._split(work, sheet_index)
        target = _at(picture.layout, path)
        host = _at(picture.layout, path[:-1])
        spot = self._centre_spot(host, target, opening_index)
        if spot is None:
            return EditResult(
                False,
                f"{target.part_number} does not fit any opening of "
                f"{host.part_number} with {self.config.inner_clearance:g}\" clearance",
            )
        _translate(target, spot[0] - target.x, spot[1] - target.y)
        return self._finish(
            work, picture, target, split, f"centred {target.part_number}"
        )

    def unnest_part(
        self,
        sheet_index: int,
        path: PartPath,
        *,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> EditResult:
        """Lift a nested frame back out onto the sheet as its own footprint."""
        if len(path) < 2:
            raise SessionError("that part is not nested inside anything")
        work = self._work()
        self.sheet(sheet_index)
        picture, split = self._split(work, sheet_index)
        target = _at(picture.layout, path)
        if x is None or y is None:
            spot = self._free_spot(
                picture.layout, target.width, target.height, ignore=target
            )
            if spot is None:
                return EditResult(
                    False,
                    f"no free space on this sheet for {target.part_number} "
                    f"({target.width:g} x {target.height:g})",
                )
            x, y = spot
        _siblings(picture.layout, path).remove(target)
        _translate(target, x - target.x, y - target.y)
        picture.layout.placements.append(target)
        return self._finish(
            work, picture, target, split, f"un-nested {target.part_number}"
        )

    # -- placement helpers ----------------------------------------------

    def _centre_spot(
        self, host: Placement, inner: Placement, opening_index: Optional[int]
    ) -> Optional[tuple[float, float]]:
        """Sheet coordinates that centre ``inner`` in one of ``host``'s openings.

        Built on :func:`~faceframe_cnc.nesting.place_inner` so the automatic
        and the manual paths centre a frame identically; when a specific
        opening is asked for, the same centring maths is applied to that one.
        """
        ordered = self.ordered_specs()
        host_w, host_h = ordered_dims(host, ordered)
        inner_w, inner_h = (
            (inner.height, inner.width) if inner.rotated else (inner.width, inner.height)
        )
        clearance = self.config.inner_clearance

        if opening_index is None:
            child = place_inner(
                host,
                PartSpec(host.part_number, host_w, host_h, 1),
                PartSpec(inner.part_number, inner_w, inner_h, 1),
                self.config,
            )
            if child is None:
                return None
            # place_inner may pick the other orientation; keep the user's.
            if abs(child.width - inner.width) > EPS or abs(child.height - inner.height) > EPS:
                rects = self.sheet_openings(host)
                return self._centre_in_rects(rects, inner, clearance)
            return child.x, child.y

        rects = [r for r in self.sheet_openings(host) if r.index == opening_index]
        return self._centre_in_rects(rects, inner, clearance)

    @staticmethod
    def _centre_in_rects(
        rects: Sequence[OpeningRect], inner: Placement, clearance: float
    ) -> Optional[tuple[float, float]]:
        for rect in rects:
            if (
                inner.width + 2 * clearance <= rect.width + EPS
                and inner.height + 2 * clearance <= rect.height + EPS
            ):
                return (
                    round(rect.x + (rect.width - inner.width) / 2.0, 9),
                    round(rect.y + (rect.height - inner.height) / 2.0, 9),
                )
        return None

    def _free_spot(
        self,
        layout: SheetLayout,
        width: float,
        height: float,
        *,
        ignore: Optional[Placement] = None,
    ) -> Optional[tuple[float, float]]:
        """First bottom-left position with room for a ``width x height`` part.

        Only top-level placements are considered: a nested frame is inside
        its host's footprint, so clearing the hosts clears the passengers.
        The soft edge cushion (spec 4a) is honoured on the first pass and
        given up on the second, so a part lands against the sheet edge only
        when that is the only way to fit it.
        """
        config = self.config
        gap = config.part_gap
        cushion = config.edge_cushion
        others = [p for p in layout.placements if p is not ignore]

        xs = {cushion}
        ys = {cushion}
        for placement in others:
            xs.add(round(placement.x + placement.width + gap, 9))
            ys.add(round(placement.y + placement.height + gap, 9))
            xs.add(round(placement.x - gap - width, 9))
            ys.add(round(placement.y - gap - height, 9))

        for margin in (cushion, 0.0):
            candidates = sorted(
                (y, x)
                for y in ys | ({0.0} if margin == 0.0 else set())
                for x in xs | ({0.0} if margin == 0.0 else set())
                if x >= margin - EPS
                and y >= margin - EPS
                and x + width <= config.sheet_width - margin + EPS
                and y + height <= config.sheet_height - margin + EPS
            )
            for y, x in candidates:
                if all(not _conflicts(other, x, y, width, height, gap) for other in others):
                    return round(x, 9), round(y, 9)
        return None

    # -- summary ---------------------------------------------------------

    def summary(self) -> dict:
        """Numbers for the summary panel (spec 5)."""
        result = self.result
        return {
            "frames": self.total_frames,
            "lines": len(self.included_rows()),
            "total_sheets": result.total_sheets if result else 0,
            "unique_sheets": len(result.unique_sheets) if result else 0,
            "baseline_sheets": result.baseline_sheets if result else None,
            "sheets_saved": result.sheets_saved if result else None,
            "inside_placements": result.inside_placements if result else 0,
            "fill": result.overall_fill_fraction if result else 0.0,
            "area_floor": result.area_lower_bound_sheets if result else 0,
        }
