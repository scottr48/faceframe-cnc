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
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from ..geometry import (
    MEMBER,
    FrameType,
    WDC_SLOT_DEPTH,
    WDC_SLOT_END_REACH,
    WDC_SLOT_INSET_FROM_INSIDE_EDGE,
    WDC_STILE_INSET,
    compute_geometry,
    infer_frame_type,
)
from ..inside import candidate_fits
from ..nesting import (
    EPS,
    MIN_PART_GAP,
    NestingConfig,
    NestingError,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
    clearance_needs,
    nest,
    place_inner,
    validate_layouts,
)

if TYPE_CHECKING:  # names used only in annotations, so no import cost at run
    # Everything the 3D simulation needs comes out of the post and sim
    # packages, and both are imported LAZILY inside
    # :meth:`Session.simulation_inputs` -- the same rule the rest of this
    # module follows (see :func:`opening_tool_inset`), so a machine that only
    # ever loads an order still pays for nothing it does not use.
    from ..post.model import CutPlan, PostConfig, ProgramHeader, SheetProgram
    from ..post.verifier import ExpectedWork
    from ..sim import FindingSet, SimTimeline

__all__ = [
    "SETTINGS_FILENAME",
    "SIM_CREATED",
    "SIM_JOB_PREFIX",
    "AppSettings",
    "SessionError",
    "RowStatus",
    "OrderRow",
    "EditResult",
    "OpeningRect",
    "Session",
    "SimulationInputs",
    "SimulationRefused",
    "default_settings_path",
    "frame_problem",
    "load_settings",
    "min_millable_opening",
    "opening_tool_inset",
    "save_settings",
    "sheet_openings",
    "suggest_dimensions",
    "wdc_detail",
    "whole_qty",
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
    #: 0.455 (owner decision 2026-08-03), matching
    #: ``NestingConfig.part_gap``: it is the spacing the shop's own
    #: R710101N.anc uses and the least the NC post can cut without the
    #: perimeter lead-in sweeping into the neighbouring part.  That makes
    #: :data:`~faceframe_cnc.nesting.MIN_PART_GAP` a HARD FLOOR here: a
    #: persisted settings file from before the 0.455 decision is migrated up
    #: on load (:meth:`from_dict`, with a note in :attr:`migration_notes`),
    #: :meth:`validate` refuses anything below it, and so does the settings
    #: dialog — otherwise the optimizer happily packs sheets the NC verifier
    #: must then refuse at Generate time, which is how the owner found out.
    #: The frame-inside-frame clearance is deliberately NOT this setting and
    #: is not exposed here: it stays at the 0.375 proven by R720101N (see
    #: ``NestingConfig.inner_clearance``).
    part_gap: float = 0.455
    edge_cushion: float = 0.5
    #: Spec 4a amendment (2026-08-03): soft front-edge target — see
    #: ``NestingConfig.front_margin``.
    front_margin: float = 1.0
    inside_nesting: bool = True
    #: Spec 4b: allow a nested frame to host a frame of its own.  Off by
    #: default; when off the validator rejects hand-built depth-2 nests.
    inside_recursion: bool = False
    last_order_path: Optional[str] = None
    #: Milestone 5: where the last NC job was written, and the digit prefix
    #: it used (the file name is ``R<prefix><sheet index>N.anc``, spec
    #: section 6).  Empty prefix means "offer a date-derived one".  The
    #: dry-run and one-file-per-physical-sheet toggles are deliberately NOT
    #: persisted: a dry run silently left on from yesterday would send an
    #: operator to the machine with an air cut.
    last_output_dir: Optional[str] = None
    job_prefix: str = ""
    #: Human-readable record of anything :meth:`from_dict` had to change to
    #: make a persisted settings file safe to run (today: a part gap below
    #: the NC post's floor raised to it).  NEVER persisted (``to_dict``
    #: omits it) and excluded from equality — it describes the load, not the
    #: settings — but the GUI must show it somewhere honest, because a
    #: silently-altered setting is the same sin as a silently-refused sheet.
    migration_notes: list = field(default_factory=list, compare=False)

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
            "last_output_dir": self.last_output_dir,
            "job_prefix": str(self.job_prefix or ""),
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

        # The part gap has a hard floor (2026-08-03): a stale file persisted
        # before the 0.455 decision would pack sheets the NC verifier must
        # refuse at Generate time, so it is raised here — with a note the
        # GUI shows, because a silent correction is not a correction the
        # user can trust.  A gap the file never had (missing key, junk
        # value) falls back to the compliant default with no note.
        migration_notes: list = []
        part_gap = number("part_gap", 0.0)
        if part_gap < MIN_PART_GAP:
            migration_notes.append(
                f"part gap {part_gap:g} from saved settings raised to "
                f"{MIN_PART_GAP:g} - the perimeter lead-in sweeps 0.425 past "
                f"the part edge, closer parts would be cut into"
            )
            part_gap = MIN_PART_GAP

        path = data.get("last_order_path")
        out_dir = data.get("last_output_dir")
        prefix = data.get("job_prefix")
        return cls(
            sheet_width=number("sheet_width", 1e-6),
            sheet_height=number("sheet_height", 1e-6),
            part_gap=part_gap,
            migration_notes=migration_notes,
            edge_cushion=number("edge_cushion", 0.0),
            front_margin=number("front_margin", 0.0),
            inside_nesting=flag("inside_nesting"),
            inside_recursion=flag("inside_recursion"),
            last_order_path=path if isinstance(path, str) and path else None,
            last_output_dir=out_dir if isinstance(out_dir, str) and out_dir else None,
            job_prefix=prefix if isinstance(prefix, str) and prefix.isdigit() else "",
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
        elif self.part_gap < MIN_PART_GAP - 1e-12:
            # Hard machine floor (2026-08-03): the T11 perimeter lead-in
            # sweeps 0.425 past the part edge, so a closer neighbour gets
            # cut into and the NC verifier refuses the sheet.  Refusing here
            # is the backstop for any path that dodges the settings dialog
            # and the load-time migration.
            problems.append(
                f"part gap must be at least {MIN_PART_GAP:g} in - the NC "
                f"perimeter lead-in sweeps 0.425 in past the part edge, so "
                f"parts spaced closer would be cut into; raise the part gap "
                f"to {MIN_PART_GAP:g} or more"
            )
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


#: Spreadsheet formats the .xls parser (pandas + xlrd) cannot read at all.
_MODERN_EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xlsb")


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


#: The WDC name encodes the diagonal-corner CABINET width; the frame is this
#: much narrower (2026-08-03 amendment: WDC2436 = cabinet 24 x 36, frame
#: 18 x 36 — the 2" stiles' geometry against the corner cabinet).  Kept
#: equal to ``order_parser._WDC_FRAME_WIDTH_REDUCTION``, which does the
#: actual deriving; a test pins the two together.  It cannot simply be
#: imported from there because the parser needs pandas and this module must
#: run without it.
WDC_CABINET_WIDTH_REDUCTION = 6.0


def wdc_detail(
    part_number: str,
    frame_width: Optional[float] = None,
    frame_height: Optional[float] = None,
) -> str:
    """Human-readable fact sheet for a WDC row, ``""`` for anything else.

    Owner request (2026-08-03): "for WDC I'm nervous to put in 18 inches
    for the width.  How do I know that it has the 2 inch stiles and the
    special T17 routing?"  This is the answer — what the machine will
    actually do to a WDC frame, in words, next to the order line.

    Every number is DERIVED: stile and rail widths and the opening from
    :mod:`faceframe_cnc.geometry` (``WDC_STILE_INSET``, ``MEMBER``,
    ``compute_geometry``), the slot facts from the same module's
    ``WDC_SLOT_*`` constants, and the frame-vs-cabinet size from the part
    number.  Nothing here restates a value from anywhere else, so the
    display can never drift from what the optimizer and the post cut.

    ``frame_width`` / ``frame_height`` are the row's dimensions when known;
    either may be ``None`` (an unresolved row), in which case the value the
    part number encodes fills in, clearly attributed to the name.
    """
    if infer_frame_type(part_number) is not FrameType.WDC:
        return ""
    normalized = part_number.strip().upper()

    width, height = frame_width, frame_height
    match = _DIGITS.search(normalized)
    lines: list[str] = []
    if match is not None:
        cabinet_w, cabinet_h = float(match.group(1)), float(match.group(2))
        encoded_w = cabinet_w - WDC_CABINET_WIDTH_REDUCTION
        if width is None and encoded_w > 0:
            width = encoded_w
        if height is None:
            height = cabinet_h
        size = (
            f"frame {width:g} x {height:g}"
            if width is not None and height is not None
            else "frame size not yet known"
        )
        lines.append(
            f"{normalized}: {size} — the name encodes the diagonal-corner "
            f"CABINET ({cabinet_w:g} x {cabinet_h:g}); the frame is "
            f'{WDC_CABINET_WIDTH_REDUCTION:g}" narrower'
        )
    elif width is not None and height is not None:
        lines.append(f"{normalized}: frame {width:g} x {height:g}")
    else:
        lines.append(f"{normalized}: frame size not yet known")

    stiles = (
        f'stiles {WDC_STILE_INSET:g}" wide (not the standard {MEMBER:g}"), '
        f'rails {MEMBER:g}"'
    )
    if width is not None and height is not None:
        geometry = compute_geometry(normalized, width, height)
        if not geometry.errors and geometry.openings:
            opening = geometry.openings[0]
            stiles += f" — single opening {opening.width:g} x {opening.height:g}"
    lines.append(stiles)

    lines.append(
        f"T17 45-degree V-slot down BOTH stiles: "
        f'{WDC_SLOT_DEPTH:g}" deep, centreline '
        f'{WDC_SLOT_INSET_FROM_INSIDE_EDGE:g}" '
        f"({WDC_SLOT_INSET_FROM_INSIDE_EDGE * 25.4:.0f} mm) from the stile's "
        f"inside edge, two passes; WDC frames get NO standard T13 stile "
        f"grooves"
    )
    lines.append(
        f'the slot cuts {WDC_SLOT_END_REACH:g}" past each stile end, so the '
        f"optimizer reserves that much clearance around WDC stile ends"
    )
    return "\n".join(lines)


def _dims_equal(a: Optional[float], b: Optional[float]) -> bool:
    """``True`` for equal numbers, equal ``None``, but never one of each."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9


def _format_dim(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:g}"


def whole_qty(value: object) -> Optional[int]:
    """``value`` as a whole quantity, or ``None`` when it is not one.

    The order parser deliberately keeps a QTY cell that is a number but not
    a whole one (``2.9``) exactly as the sheet wrote it (2026-08-04 review
    fix 3) rather than flooring it, so everything downstream has to be able
    to ask "is this a real quantity yet?" without guessing.  ``None`` here
    means "a human still has to say what the quantity is".
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    return int(number)


# --------------------------------------------------------------------------
# What the machine can actually cut (2026-08-04 external review)
# --------------------------------------------------------------------------
#
# A frame can pass compute_geometry and still be uncuttable: a wall frame
# 3.2 wide has a positive 0.2 opening, which nests and previews happily and
# is then refused by the NC post, because the opening pass runs the tool
# CENTRE inside each opening edge and 0.2 has no room for that.  The offsets
# are read out of the post's measured table instead of being typed here, so
# this module can never disagree with what the machine does.

#: Cached tool-centre inset (see :func:`opening_tool_inset`).
_OPENING_INSET: Optional[float] = None


def opening_tool_inset() -> float:
    """How far inside each opening edge the NC post runs its tool centre.

    READ-ONLY from :func:`faceframe_cnc.post.model.default_config`: the T11
    opening pass runs 0.1975 inside the finished edge (0.1875 tool radius +
    0.010 of finish stock left for T12) and the T12 detail pass 0.1.  The
    deepest of those insets decides whether an opening has any tool path
    left at all.  Imported lazily and cached — this is the only number the
    order model needs out of the post package.
    """
    global _OPENING_INSET
    if _OPENING_INSET is None:
        from ..post.model import default_config

        post = default_config()
        _OPENING_INSET = max(
            -float(post.openings_pass.offset), -float(post.detail_pass.offset), 0.0
        )
    return _OPENING_INSET


def min_millable_opening() -> float:
    """Smallest routed opening the NC post can cut — twice the tool inset.

    An opening this size or smaller collapses to nothing once the offset is
    applied, which is exactly the refusal
    :mod:`faceframe_cnc.post.generator` raises at Generate time ("collapses
    to 0x... once the tool offset is applied").  Frames like that are
    refused here instead — at include / edit / optimize time — so a sheet
    can never be nested, previewed and only then refused at the machine.
    """
    return 2.0 * opening_tool_inset()


def _too_small_to_cut(geometry) -> Optional[str]:
    """The first opening too small for the cutter, worded for the shop, or ``None``."""
    floor = min_millable_opening()
    inset = opening_tool_inset()
    for opening in geometry.openings:
        if min(opening.width, opening.height) <= floor + EPS:
            # Most frames have one opening simply labelled "opening"; the
            # drawer stacks name theirs (top / middle / bottom / door).
            which = "the" if opening.label == "opening" else f"the {opening.label}"
            return (
                f"{which} opening would be {opening.width:g} x "
                f"{opening.height:g} in, which the machine cannot cut: the NC "
                f"opening pass runs the tool centre {inset:g} in inside each "
                f"edge, so an opening must be more than {floor:g} in wide and "
                f"tall"
            )
    return None


def frame_problem(part_number: str, width: float, height: float) -> Optional[str]:
    """Why this frame cannot be cut, or ``None`` when it can.

    Two gates, in the order the work meets them:

    1.  :func:`~faceframe_cnc.geometry.compute_geometry` — the frame's own
        geometry (spec section 3).  A part family the geometry engine
        deliberately refuses to lay out (an unsupported drawer base —
        2026-08-04 parser fix 4) raises out of it; that becomes a problem
        message here rather than an exception, because this is called from
        :attr:`OrderRow.status`, i.e. from the order table's paint path.
    2.  the NC post's tool geometry (:func:`min_millable_opening`) — a
        positive opening can still be too small for the cutter.
    """
    try:
        geometry = compute_geometry(part_number, width, height)
    except ValueError:  # geometry.py refuses this part family outright
        return (
            f"{part_number}: this app cannot compute openings for this part "
            f"family - check the line against the order form"
        )
    if geometry.errors:
        return geometry.errors[0]
    return _too_small_to_cut(geometry)


@dataclass
class OrderRow:
    """One line of the order as the GUI shows it.

    Rows the parser could not fully read (``missing`` non-empty) start
    excluded and cannot be included until the user supplies the dimension —
    spec section 2's "do NOT silently guess".
    """

    key: str
    part_number: str
    #: Whole in every ordinary case.  A QTY cell the order form wrote as
    #: something other than a whole number (``2.9``) arrives here as the raw
    #: float the parser refused to floor (2026-08-04 review fix 3); see
    #: :attr:`qty_problem`, which keeps such a row off the cut list until a
    #: human says what the quantity really is.
    qty: "int | float"
    frame_width: Optional[float] = None
    frame_height: Optional[float] = None
    included: bool = True
    #: Which of ``("width", "height")`` the spreadsheet did not supply.
    missing: tuple[str, ...] = ()
    #: Free-text explanation shown beside a needs-attention row.
    reason: str = ""
    #: The parser held this row back for something that is NOT expressible
    #: as a missing dimension or a bad quantity: a WDC row whose entered
    #: dimensions contradict its part number, an unsupported drawer-base
    #: family, a catalogue accessory (2026-08-04 parser fixes 1 and 4).
    #: Those rows arrive with ``missing == ()`` and a whole ``qty``, so
    #: nothing in their own data would stop :attr:`status` reading them as
    #: READY — this flag does.  Cleared by :meth:`Session.resolve_row` (the
    #: user confirming the line), restored by :meth:`Session.revert_row`.
    needs_attention: bool = False
    #: Provenance remark on a READY row (2026-08-03 amendment): the parser
    #: sets it when it derived a dimension the spreadsheet left blank (a
    #: WDC frame width from the part number), so the GUI can show where the
    #: number came from instead of presenting it as typed-in data.
    note: str = ""
    #: Source spreadsheet row, for tracing back to the order.
    row_index: int = -1
    #: Set once the user has typed the missing dimension.
    resolved: bool = False

    #: As-loaded provenance for :meth:`Session.edit_row` /
    #: :meth:`Session.revert_row` (2026-08-03, "edit a line" amendment).
    #: ``None`` here is a sentinel meaning "not yet captured" -- resolved to
    #: whatever the field held at construction by :meth:`__post_init__`, so
    #: every row built the ordinary way (parser, tests, fixtures) gets a
    #: correct baseline for free, with no separate call required.  A field
    #: legitimately being ``None`` (an unresolved dimension) and the
    #: sentinel collapse to the same value on purpose: "the original was
    #: None" and "capture whatever is there" mean the same thing here.
    original_qty: Optional[int] = None
    original_width: Optional[float] = None
    original_height: Optional[float] = None
    #: The row's ``missing``/``reason`` as loaded, kept so
    #: :meth:`Session.revert_row` can put an auto-resolved-by-hand row (one
    #: that was NEEDS_ATTENTION or NO_FRAME until :meth:`Session.edit_row`
    #: supplied the missing dimension) back into that state instead of
    #: leaving it READY with a dimension quietly wiped back to ``None``.
    original_missing: Optional[tuple[str, ...]] = None
    original_reason: Optional[str] = None
    #: :attr:`needs_attention` as loaded, so :meth:`Session.revert_row` can
    #: put a confirmed-by-hand row back into the state the parser gave it.
    original_needs_attention: Optional[bool] = None
    #: The note as loaded (a WDC derivation remark, or ``""``) -- the part
    #: :meth:`OrderRow._compose_note` never overwrites, only appends to.
    base_note: Optional[str] = None

    def __post_init__(self) -> None:
        if self.original_qty is None:
            self.original_qty = self.qty
        if self.original_width is None:
            self.original_width = self.frame_width
        if self.original_height is None:
            self.original_height = self.frame_height
        if self.original_missing is None:
            self.original_missing = self.missing
        if self.original_reason is None:
            self.original_reason = self.reason
        if self.original_needs_attention is None:
            self.original_needs_attention = self.needs_attention
        if self.base_note is None:
            self.base_note = self.note

    @property
    def edited(self) -> bool:
        """True once qty/width/height differ from the order-form originals."""
        return (
            self.qty != self.original_qty
            or not _dims_equal(self.frame_width, self.original_width)
            or not _dims_equal(self.frame_height, self.original_height)
        )

    def _compose_note(self) -> str:
        """The note :meth:`Session.edit_row` installs after applying a change.

        Always recomputed from scratch against the order-form originals
        (never against whatever the previous edit's note said), so two
        edits in a row describe the NET change, not a diff of diffs -- and
        an edit that lands back on the originals collapses to
        :attr:`base_note` with no "edited" text left over.  A pre-existing
        note (the WDC part-number derivation) is a prefix, never overwritten.
        """
        changes: list[str] = []
        if self.qty != self.original_qty:
            changes.append(f"qty {self.original_qty} -> {self.qty}")
        if not _dims_equal(self.frame_width, self.original_width):
            changes.append(
                f"width {_format_dim(self.original_width)} -> {_format_dim(self.frame_width)}"
            )
        if not _dims_equal(self.frame_height, self.original_height):
            changes.append(
                f"height {_format_dim(self.original_height)} -> {_format_dim(self.frame_height)}"
            )
        if not changes:
            return self.base_note or ""
        edit_text = f"edited: {', '.join(changes)} (order form values kept for reference)"
        return f"{self.base_note}; {edit_text}" if self.base_note else edit_text

    @property
    def frame_type(self) -> FrameType:
        return infer_frame_type(self.part_number)

    @property
    def qty_problem(self) -> bool:
        """True while the quantity is not a whole number (parser fix 3).

        Derived from the data, not from a flag: the moment a whole quantity
        is supplied the problem is gone.  Such a row must never read READY —
        a fractional demand cannot be cut, and before this was checked the
        row looked ready to cut with "2.9" printed in its Qty column.
        """
        return whole_qty(self.qty) is None

    @property
    def status(self) -> RowStatus:
        if self.missing:
            # 2026-08-03 amendment: missing BOTH dims is "no faceframe
            # required" (e.g. SD1212), not "needs attention" -- only a
            # row missing exactly ONE dim (e.g. WDC2436) is prompted for.
            if len(self.missing) >= 2:
                return RowStatus.NO_FRAME
            return RowStatus.NEEDS_ATTENTION
        if self.qty_problem or self.needs_attention:
            return RowStatus.NEEDS_ATTENTION
        if self.geometry_error is not None:
            return RowStatus.INVALID
        return RowStatus.READY

    @property
    def geometry_error(self) -> Optional[str]:
        """Why this frame cannot be cut, or ``None``.

        Spec section 3: a frame too short for its pattern is flagged in the
        UI instead of quietly producing garbage openings.  Since the
        2026-08-04 external review this also covers a frame whose openings
        are positive but too small for the NC post's tool offsets (see
        :func:`frame_problem`) — the same "flag it, never cut it" channel,
        because "0.2 wide opening" is no more cuttable than "no opening".
        """
        if self.frame_width is None or self.frame_height is None:
            return None
        return frame_problem(self.part_number, self.frame_width, self.frame_height)

    @property
    def unmillable_reason(self) -> Optional[str]:
        """Why the NC post could not cut this frame's OPENINGS, or ``None``.

        Deliberately narrower than :attr:`geometry_error`: only the
        tool-offset floor (2026-08-04 external review), so
        :meth:`Session.optimize` can refuse a ticked-on row for it by name
        without changing what happens to rows the geometry engine already
        refuses.
        """
        if self.frame_width is None or self.frame_height is None:
            return None
        try:
            geometry = compute_geometry(
                self.part_number, self.frame_width, self.frame_height
            )
        except ValueError:
            return None  # an unlayoutable family: geometry_error's business
        if geometry.errors:
            return None  # ditto
        return _too_small_to_cut(geometry)

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
        if self.qty_problem:
            return (
                f"the order form's quantity is {self.qty!r}, which is not a "
                f"whole number of frames — type the quantity to cut"
            )
        if not self.missing:
            if self.needs_attention:
                return "check this line against the order form, then confirm it"
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


def _conflicts(
    a: Placement,
    bx: float,
    by: float,
    bw: float,
    bh: float,
    gap: float,
    need: tuple[float, float] | None = None,
) -> bool:
    """True when a ``bw x bh`` part at ``(bx, by)`` would crowd ``a``.

    ``need`` is the ``(x, y)`` clearance the MOVING part demands, which is
    only ever more than ``gap`` for a WDC frame (its 45-degree stile slot
    cuts past its stile ends).  Mirrors
    :func:`faceframe_cnc.nesting.validate_layouts`, which has the final say
    — this one only picks where to offer a drop.
    """
    need_x, need_y = need if need is not None else (gap, gap)
    clear_x = max(a.x, bx) - min(a.x + a.width, bx + bw)
    clear_y = max(a.y, by) - min(a.y + a.height, by + bh)
    return clear_x < need_x - EPS and clear_y < need_y - EPS


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
# 3D cut simulation (Milestone 5 of the simulation work)
# --------------------------------------------------------------------------
#
# The simulation judges a sheet with the SAME inputs the Generate path judges
# it with: the same :func:`~faceframe_cnc.post.from_layout.plan_sheet` call,
# the same post table, and the same expected-work manifest
# (:func:`~faceframe_cnc.post.verifier.expected_work`) that
# :func:`faceframe_cnc.post.job.build_job` hands the verifier.  So a missing
# cut shows up in 3D exactly as it would refuse at Generate, and the operator
# is never shown a sheet running clean that Generate will then refuse.
#
# Nothing here writes a file and nothing here is a second opinion: the
# planner refuses, the verifier judges, this only assembles their inputs.

#: ``(CREATED ON ...)`` text the simulated program carries.  FIXED, not a
#: clock reading: simulating one sheet twice must judge the same bytes, and
#: the created line is the one header line a clock would move.  It says what
#: it is, because this program is NOT the one the operator carries to the
#: machine — that one is named, dated and written by
#: :mod:`faceframe_cnc.post.job` at Generate time.
SIM_CREATED = "SIMULATION - NO FILE IS WRITTEN"

#: Job prefix the simulated program's NAME is built from when the settings
#: hold none yet.  The name is only read back by the verifier's header rule
#: and shown in the window title, so the convention is the real one (spec
#: section 6, :func:`faceframe_cnc.post.job.sheet_filename`) with a prefix
#: that cannot be mistaken for a job somebody generated.
SIM_JOB_PREFIX = "0000"


class SimulationRefused(SessionError):
    """The post refuses to cut the sheet the user asked to simulate.

    A :class:`SessionError`, so every ``except SessionError`` in the Qt layer
    still catches it, and its ``str()`` is the refusal's own words verbatim.
    What it adds is the STRUCTURE the 3D refusal view needs and a message
    cannot carry:

    :attr:`error`
        the original exception, preserved whole (and as ``__cause__``): a
        :class:`~faceframe_cnc.post.from_layout.SheetPlanError`, which carries
        ``part_number`` and ``box``, or the ``ValueError`` a later gate raised;
    :attr:`part_number` / :attr:`box`
        mirrored off it, so this object can go straight to
        :class:`~faceframe_cnc.gui.sim3d.refusal.RefusalView`, which reads
        both with ``getattr``;
    :attr:`program`
        the :class:`~faceframe_cnc.post.model.SheetProgram` when one could be
        built — the refusal view draws the sheet from it and outlines the part
        the refusal names.  ``None`` when the refusal came before there was a
        program (an empty sheet, a frame the geometry engine rejects), which is
        that view's banner-only case;
    :attr:`post_config`
        the post table the sheet was planned against, so the view's envelopes
        are the machine's real reach rather than a default.

    Wrapping instead of propagating follows this module's own convention —
    everything the Qt layer catches is a :class:`SessionError` carrying a
    message fit for a message box — and nothing is lost, because the original
    exception is right here on the object.
    """

    def __init__(
        self,
        error: BaseException,
        program: "SheetProgram | None" = None,
        post_config: "PostConfig | None" = None,
        sheet_index: int = -1,
    ):
        super().__init__(str(error))
        self.error = error
        self.program = program
        self.post_config = post_config
        self.sheet_index = int(sheet_index)
        self.part_number: Optional[str] = getattr(error, "part_number", None)
        self.box = getattr(error, "box", None)


@dataclass(frozen=True)
class SimulationInputs:
    """One unique sheet, planned, emitted and judged, ready for the 3D window.

    Everything :class:`~faceframe_cnc.gui.sim3d.window.Sim3DWindow` needs and
    nothing it has to derive: :attr:`timeline` is the emitted program indexed
    for playback and :attr:`findings` the verifier's verdict on that same
    text, located on it.  The rest is kept because a caller (and a test) has
    to be able to see WHAT was judged: the sheet it came from, the header and
    post table it was emitted with, and the manifest of cuts the sheet owed.
    """

    #: Index into :attr:`Session.sheets` — the sheet the user was looking at.
    sheet_index: int
    layout: SheetLayout
    #: Physical sheets cut from this picture (banner information only here).
    run_quantity: int
    header: "ProgramHeader"
    post_config: "PostConfig"
    program: "SheetProgram"
    plan: "CutPlan"
    timeline: "SimTimeline"
    #: The manifest :func:`faceframe_cnc.post.job.build_job` would hand
    #: :func:`~faceframe_cnc.post.verifier.verify` for this sheet.
    expected: "ExpectedWork"
    findings: "FindingSet"

    @property
    def clean(self) -> bool:
        """True when the verifier found nothing wrong with this program."""
        return not self.findings

    @property
    def program_name(self) -> str:
        return self.header.name


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

    # -- settings --------------------------------------------------------

    def set_settings(self, settings: AppSettings) -> None:
        """Install new optimizer settings, invalidating any layout on screen.

        The ONLY way the GUI is allowed to change the settings a layout was
        packed with (2026-08-04 review): the previous layout was nested for
        the OLD sheet size / part gap / inside-nesting rules, so leaving it
        in place would leave a layout that is generate-able under settings
        it was never checked against — the same governing rule as
        :meth:`edit_row`'s and :meth:`set_included`'s, and the same
        invalidation (``result = None``, problems cleared, the manual-edit
        flag reset).

        Guarded, and guarded BEFORE anything is stored: unusable settings
        raise :class:`SessionError` with the same messages
        :meth:`AppSettings.validate` gives and the session keeps the
        settings it had, so a half-applied change is impossible.  Settings
        that do not change the optimizer's input at all (a remembered output
        folder, a job prefix) are stored without touching the layout, the
        same no-op rule :meth:`set_included` follows.
        """
        problems = settings.validate()
        if problems:
            raise SessionError("; ".join(problems))
        layout_changed = settings.to_config() != self.settings.to_config()
        self.settings = settings
        if layout_changed:
            self.result = None
            self._problems = []
            self.edited = False
            self.dirty = True

    # -- order -----------------------------------------------------------

    def load_order(self, path: str) -> None:
        """Parse an order spreadsheet into :attr:`rows` (needs pandas + xlrd).

        Imported lazily so a machine without pandas can still run every
        other part of the model — and so the GUI can report a missing
        dependency as a message box instead of an import crash.
        """
        # The parser is xlrd-only, i.e. legacy .xls only.  A modern .xlsx
        # would otherwise come back as a generic "could not read" (the file
        # dialog even used to offer *.xlsx), which tells the operator
        # nothing about what to do next.
        suffix = os.path.splitext(path)[1].lower()
        if suffix in _MODERN_EXCEL_SUFFIXES:
            raise SessionError(
                f"{os.path.basename(path)} is a modern Excel file ({suffix}); "
                f"this program reads the shop's Excel 97-2003 order form "
                f"(.xls).  Open it in Excel and use File > Save As > "
                f"\"Excel 97-2003 Workbook (*.xls)\", then open that file."
            )
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
            row = OrderRow(
                key=f"r{line.row_index}:{line.part_number}",
                part_number=line.part_number,
                qty=line.qty,
                frame_width=line.frame_width,
                frame_height=line.frame_height,
                included=True,
                # 2026-08-03 amendment: a WDC dimension the parser
                # derived from the part number arrives annotated, and
                # the annotation must survive to the order panel.
                note=getattr(line, "note", "") or "",
                row_index=line.row_index,
            )
            # A line the parser read cleanly can still be uncuttable — a
            # frame too short for its pattern, or (2026-08-04 external
            # review) openings too small for the NC post's tool offsets.
            # Such a row is INVALID, so it starts UNTICKED: leaving it
            # ticked would show the operator a locked, unticked checkbox
            # while the session still counted the line as wanted.
            row.included = row.can_include
            rows.append(row)
        for line in parsed.needs_attention:
            missing = tuple(line.missing)
            rows.append(
                OrderRow(
                    key=f"r{line.row_index}:{line.part_number}",
                    part_number=line.part_number,
                    qty=line.qty,
                    frame_width=line.frame_width,
                    frame_height=line.frame_height,
                    included=False,
                    missing=missing,
                    reason=line.reason,
                    # Rows held back for something neither a missing
                    # dimension nor a bad quantity (a WDC contradiction, an
                    # unsupported drawer-base family, an accessory) need the
                    # flag, or status would read them as READY -- their own
                    # data looks complete.  See OrderRow.needs_attention.
                    needs_attention=not missing and whole_qty(line.qty) is not None,
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
        """Tick a line in or out of the cut list (spec 5, order panel).

        A layout already on screen was optimized against whatever set of
        lines was ticked at the time; ticking a line in or out changes
        :meth:`demand` out from under that layout, so an actual change here
        invalidates the current result exactly the way :meth:`edit_row`
        does (``result = None``, problems cleared) -- the same governing
        rule as edit_row's: a layout built from the pre-change lines must
        never be reachable from Generate.  A no-op call (the box already
        matched) leaves an existing result alone.
        """
        row = self.row(key)
        if included and not row.can_include:
            if row.missing:
                detail = f"its {' and '.join(row.missing)} is missing"
            elif row.qty_problem:
                detail = f"its quantity ({row.qty!r}) is not a whole number"
            else:
                detail = row.reason or row.geometry_error or "its geometry is unusable"
            raise SessionError(f"{row.part_number} cannot be included: {detail}")
        if row.included != included:
            row.included = included
            self.dirty = True
            self.result = None
            self._problems = []

    def set_all_included(self, included: bool) -> None:
        """Tick every includable line in or out at once (the "Cut all" /
        "Cut none" buttons).  Same invalidation rule as :meth:`set_included`
        -- applied once for the whole batch, not per row, so a call that
        changes nothing (everything already at the target state) leaves an
        existing result alone.
        """
        changed = False
        for row in self.rows:
            if included and not row.can_include:
                continue
            if row.included != included:
                row.included = included
                self.dirty = True
                changed = True
        if changed:
            self.result = None
            self._problems = []

    def resolve_row(
        self,
        key: str,
        *,
        width: Optional[float] = None,
        height: Optional[float] = None,
        qty: object = None,
        include: bool = True,
    ) -> OrderRow:
        """Supply what the order form did not give this row (spec section 2).

        Takes whatever the user typed and validates THAT combination, whole:

        *   a dimension supplied here wins over the one the sheet had (a
            present-but-dubious value — a WDC width that contradicts the
            part number, 2026-08-04 parser fix 1 — has to be correctable;
            the same rule :func:`faceframe_cnc.order_parser.resolve` follows);
        *   ``qty`` is REQUIRED when the row's quantity is not a whole number
            (2026-08-04 parser fix 3: the sheet said ``2.9``) and optional
            otherwise, and must be a whole number of at least 1;
        *   nothing at all is required for a row the parser merely wants
            confirmed (an unsupported family, an accessory) — those are
            resolved by confirming them, and the geometry gate below still
            has the last word.

        Raises :class:`SessionError` when the row would still be incomplete,
        when a value is not usable, or when the resulting frame cannot be
        cut — and in every one of those cases the row is left exactly as it
        was, never half-resolved.  On success the row moves onto the cut
        list (a demand that did not exist before), so the current layout is
        invalidated the same way :meth:`edit_row` invalidates it --
        ``result = None``, problems cleared -- the same governing rule: a
        layout built before this row was resolved must never be reachable
        from Generate.
        """
        row = self.row(key)
        if not (row.missing or row.needs_attention or row.qty_problem):
            raise SessionError(f"{row.part_number} has nothing that needs resolving")

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
        resolved_width = new_width if new_width is not None else row.frame_width
        resolved_height = new_height if new_height is not None else row.frame_height

        still_missing = [
            name
            for name, value in (("width", resolved_width), ("height", resolved_height))
            if value is None
        ]
        if still_missing:
            raise SessionError(
                f"{row.part_number} still needs a frame {' and '.join(still_missing)}"
            )

        resolved_qty = row.qty
        if qty is not None and qty != "":
            resolved_qty = qty
        whole = whole_qty(resolved_qty)
        if whole is None:
            raise SessionError(
                f"{row.part_number}: quantity {resolved_qty!r} is not a whole "
                f"number of frames — type the quantity to cut"
            )
        if whole <= 0:
            raise SessionError(
                f"{row.part_number}: quantity must be at least 1 (got "
                f"{resolved_qty!r}) - use the Cut checkbox to leave a line out"
            )

        # Validate the whole candidate BEFORE touching the row, so a refusal
        # leaves no partial state behind (spec section 3: a frame that cannot
        # produce openings never reaches the optimizer).
        error = frame_problem(row.part_number, resolved_width, resolved_height)
        if error is not None:
            raise SessionError(
                f"{row.part_number} {resolved_width:g} x {resolved_height:g}: {error}"
            )

        row.frame_width = resolved_width
        row.frame_height = resolved_height
        row.qty = whole
        row.missing = ()
        row.needs_attention = False
        row.resolved = True
        row.reason = ""
        row.note = row._compose_note()
        row.included = bool(include)
        self.dirty = True
        self.result = None
        self._problems = []
        return row

    def edit_row(
        self,
        key: str,
        *,
        qty: Optional[int] = None,
        width: Optional[float] = None,
        height: Optional[float] = None,
    ) -> OrderRow:
        """Change a line's quantity and/or frame dimensions (owner request,
        2026-08-03: "after the change a save button or some other form of
        'are you sure?'" -- that confirmation lives in the GUI dialog; this
        is where the change actually gets checked and applied).

        ``None`` leaves that field alone.  Validation mirrors
        :meth:`resolve_row`'s coercion: quantity must be a positive
        integer -- zero is refused, since excluding a line is what the Cut
        checkbox is for, and the error says so -- and a supplied dimension
        must be a positive finite number.

        NOTHING is written until every check has passed, and the checks are
        run against exactly the combination the caller asked for -- the new
        width WITH the new height (2026-08-04 review): the previous version
        validated the full candidate but then delegated the commit to
        :meth:`resolve_row`, which re-validated the new dimension against
        the OLD one and could refuse a perfectly good edit while quoting a
        size the user never typed ("30 x 2" for a row whose junk height was
        2 and which the user had just replaced).  A refusal therefore leaves
        the row byte-for-byte as it was, the same discipline the layout
        edits use.

        A row still missing a dimension is completed here when this call
        supplies whatever it is still missing (and is then ticked onto the
        cut list, since it now has something to cut); supplying only part of
        what an incomplete row needs is refused with the same message
        :meth:`resolve_row` gives.  A qty-only edit is allowed on an
        incomplete row without resolving it, and a whole quantity typed here
        also clears a row the parser held back for a fractional QTY.

        On success the note is rewritten to say what changed from the
        order-form originals (never losing a pre-existing derivation note —
        see :meth:`OrderRow._compose_note`), and the current layout is
        invalidated exactly like :meth:`load_order` invalidates it
        (``result = None``, problems cleared): a layout built from the
        pre-edit numbers must never be reachable from Generate.
        """
        row = self.row(key)
        was_blocked = row.status is not RowStatus.READY

        def coerce_dim(name: str, value: object) -> float:
            try:
                number = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise SessionError(f"{name} {value!r} is not a number") from None
            if not math.isfinite(number) or number <= 0:
                raise SessionError(f"{name} must be a positive number, got {value!r}")
            return number

        new_qty = row.qty
        if qty is not None:
            new_qty = whole_qty(qty)
            if new_qty is None:
                raise SessionError(f"quantity {qty!r} is not a whole number")
            if new_qty <= 0:
                raise SessionError(
                    f"quantity must be at least 1 (got {qty!r}) - use the Cut "
                    f"checkbox to exclude a line instead of setting its "
                    f"quantity to zero"
                )

        new_width = row.frame_width if width is None else coerce_dim("width", width)
        new_height = row.frame_height if height is None else coerce_dim("height", height)

        # -- validate the whole candidate, then commit it in one go --------
        completing = bool(row.missing)
        if new_width is not None and new_height is not None:
            error = frame_problem(row.part_number, new_width, new_height)
            if error is not None:
                raise SessionError(
                    f"{row.part_number} {new_width:g} x {new_height:g}: {error}"
                )
        elif completing and (width is not None or height is not None):
            still_missing = [
                name
                for name, value in (("width", new_width), ("height", new_height))
                if value is None
            ]
            raise SessionError(
                f"{row.part_number} still needs a frame {' and '.join(still_missing)}"
            )
        else:
            # The row is still missing a dimension and this call did not
            # supply one -- a qty-only edit on an incomplete row.
            completing = False

        row.qty = new_qty
        if new_width is not None and new_height is not None:
            row.frame_width, row.frame_height = new_width, new_height
            if completing:
                row.missing = ()
                row.resolved = True
                row.reason = ""
        # A reason the row is no longer guilty of (a fractional quantity the
        # user has just typed as a whole number) must not stay on screen as
        # a stale amber warning.
        if row.reason and not row.missing and not row.needs_attention and not row.qty_problem:
            row.reason = ""
        if was_blocked and row.status is RowStatus.READY:
            # The edit unblocked the line: it has something to cut now, so
            # it joins the cut list the way a resolved row does.
            row.included = True
        row.note = row._compose_note()
        self.dirty = True
        self.result = None
        self._problems = []
        return row

    def revert_row(self, key: str) -> OrderRow:
        """Undo every :meth:`edit_row` change, back to the order-form values.

        Restores qty/width/height and the original note (the WDC derivation
        note when there was one) verbatim.  If the row had been NEEDS_
        ATTENTION or NO_FRAME before an edit supplied its missing
        dimension, that incomplete state comes back too -- a dimension
        :meth:`edit_row` filled in does not get to survive as a floating
        READY value once its own provenance is reverted away.  Raises
        :class:`SessionError` if the row was never edited: there is nothing
        to revert to that is not already showing.  Invalidates the current
        layout the same way :meth:`edit_row` does.
        """
        row = self.row(key)
        if not row.edited:
            raise SessionError(f"{row.part_number} has not been edited")
        row.qty = row.original_qty
        row.frame_width = row.original_width
        row.frame_height = row.original_height
        row.note = row.base_note or ""
        if row.original_missing or row.original_needs_attention:
            row.missing = row.original_missing or ()
            row.needs_attention = bool(row.original_needs_attention)
            row.reason = row.original_reason or ""
            row.resolved = False
            row.included = False
        self.dirty = True
        self.result = None
        self._problems = []
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
        """Run the optimizer over the included lines (discards manual edits).

        Refuses, before packing anything, a line whose openings the NC post
        cannot cut (2026-08-04 external review: a 3.2 x 10 wall frame yields
        a positive 0.2 opening that nested and previewed fine and was only
        refused at Generate, after the operator had built a layout around
        it).  Named, with the size and the minimum, and left to be corrected
        or unticked -- never a crash.
        """
        config = self.settings.to_config()
        problems = self.settings.validate()
        if problems:
            raise SessionError("; ".join(problems))
        blocked = [
            (row, row.unmillable_reason)
            for row in self.rows
            if row.included and row.unmillable_reason is not None
        ]
        if blocked:
            raise SessionError(
                "; ".join(
                    f"{row.part_number} {row.frame_width:g} x {row.frame_height:g}: "
                    f"{reason}"
                    for row, reason in blocked
                )
                + " - correct the frame size or untick the line"
            )
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

        # ``landed`` is always still in ``grouped`` (every edit leaves at
        # least the edited part on its picture); the fallback only keeps a
        # future edit that empties the focused sheet from raising here.
        if landed in grouped:
            sheet_index = grouped.index(landed)
            new_path = _path_of(landed.layout, target) or ()
        else:
            sheet_index = 0 if grouped else -1
            new_path = ()

        # A gesture that changed NOTHING is not an edit (2026-08-04 review):
        # a press and release with no movement -- the click an operator makes
        # to read a part's label -- used to commit a "move" to the part's own
        # position, which set ``edited`` (arming the "Discard manual edits?"
        # prompt on the next Optimize) and, on a sheet cut more than once,
        # split one sheet out of its run and merged it straight back.  The
        # comparison is canonical, the same identity the packer groups runs
        # by, so a sub-1e-4 twitch of the mouse is a no-op too, and it covers
        # every gesture (rotating a square part, re-nesting a child where it
        # already sits) rather than just the click.
        before = [(layout.canonical(), run) for layout, run in self.result.unique_sheets]
        after = [(picture.layout.canonical(), picture.run) for picture in grouped]
        if after == before:
            return EditResult(
                True,
                f"{target.part_number} unchanged",
                sheet_index=sheet_index,
                path=new_path,
            )

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
            spot = self._free_spot(
                destination.layout, target.width, target.height, moving=target
            )
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
                return self._centre_in_rects(rects, inner, clearance, self.config)
            return child.x, child.y

        rects = [r for r in self.sheet_openings(host) if r.index == opening_index]
        return self._centre_in_rects(rects, inner, clearance, self.config)

    @staticmethod
    def _centre_in_rects(
        rects: Sequence[OpeningRect],
        inner: Placement,
        clearance: float,
        config: Optional[NestingConfig] = None,
    ) -> Optional[tuple[float, float]]:
        need_x, need_y = clearance, clearance
        if config is not None:
            # A WDC inner needs its slot's reach past its stile ends, which
            # inside a host opening means past the host's rails.
            wants = clearance_needs(inner, config)
            need_x = max(clearance, wants[0] if wants[0] > config.part_gap else 0.0)
            need_y = max(clearance, wants[1] if wants[1] > config.part_gap else 0.0)
        for rect in rects:
            if (
                inner.width + 2 * need_x <= rect.width + EPS
                and inner.height + 2 * need_y <= rect.height + EPS
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
        moving: Optional[Placement] = None,
    ) -> Optional[tuple[float, float]]:
        """First bottom-left position with room for a ``width x height`` part.

        Only top-level placements are considered: a nested frame is inside
        its host's footprint, so clearing the hosts clears the passengers.
        The soft edge cushion (spec 4a) is honoured on the first pass and
        given up on the second, so a part lands against the sheet edge only
        when that is the only way to fit it.

        ``moving`` is the part being placed, needed only for the one part
        type whose clearance is directional: a WDC frame's 45-degree stile
        slot cuts past its stile ends, so it needs more room there — and
        hard room against the sheet edge, which is otherwise a preference.
        """
        config = self.config
        gap = config.part_gap
        cushion = config.edge_cushion
        others = [p for p in layout.placements if p is not ignore]

        subject = moving if moving is not None else ignore
        need_x, need_y = gap, gap
        if subject is not None:
            need_x, need_y = clearance_needs(subject, config)
        edge_x = need_x if need_x > gap else 0.0
        edge_y = need_y if need_y > gap else 0.0

        xs = {cushion}
        ys = {cushion}
        for placement in others:
            other_x, other_y = clearance_needs(placement, config)
            step_x = max(need_x, other_x)
            step_y = max(need_y, other_y)
            xs.add(round(placement.x + placement.width + step_x, 9))
            ys.add(round(placement.y + placement.height + step_y, 9))
            xs.add(round(placement.x - step_x - width, 9))
            ys.add(round(placement.y - step_y - height, 9))

        for margin in (cushion, 0.0):
            low_x = max(margin, edge_x)
            low_y = max(margin, edge_y)
            candidates = sorted(
                (y, x)
                for y in ys | ({0.0} if margin == 0.0 else set())
                for x in xs | ({0.0} if margin == 0.0 else set())
                if x >= low_x - EPS
                and y >= low_y - EPS
                and x + width <= config.sheet_width - low_x + EPS
                and y + height <= config.sheet_height - low_y + EPS
            )
            for y, x in candidates:
                if all(
                    not _conflicts(
                        other,
                        x,
                        y,
                        width,
                        height,
                        gap,
                        (
                            max(need_x, clearance_needs(other, config)[0]),
                            max(need_y, clearance_needs(other, config)[1]),
                        ),
                    )
                    for other in others
                ):
                    return round(x, 9), round(y, 9)
        return None

    # -- NC generation (Milestone 5 phase 2) -----------------------------

    def can_generate(self) -> bool:
        """True when there is a validated layout to write NC for."""
        return self.result is not None and bool(self.sheets) and not self._problems

    def generate_blocker(self) -> Optional[str]:
        """Why :meth:`generate_nc` would refuse right now, or ``None``."""
        if self.result is None or not self.sheets:
            return "Optimize a layout first"
        if self._problems:
            return f"The layout has {len(self._problems)} unresolved problem(s)"
        return None

    def default_job_prefix(self, today: Optional[str] = None) -> str:
        """The digit prefix to offer in the Generate dialog.

        The saved one if there is one, otherwise today's date as ``MMDD`` —
        spec section 6 describes the prefix as "job/date digits", and the
        shop's own numbering is not derivable from the reference file names.
        ``today`` is injectable so the dialog and its tests agree.
        """
        saved = str(self.settings.job_prefix or "")
        if saved.isdigit():
            return saved
        if today is not None:
            return today
        import time as _time

        return _time.strftime("%m%d")

    def generate_nc(
        self,
        output_dir: str,
        *,
        prefix: str,
        dry_run: bool = False,
        per_physical_sheet: bool = False,
        pdf_report: bool = True,
        created: Optional[str] = None,
        remember: bool = True,
    ):
        """Write one ``.anc`` per sheet and return the
        :class:`~faceframe_cnc.post.job.JobResult`.

        Every gate lives in :mod:`faceframe_cnc.post.job` — this method only
        turns the session's state into a job and its failures into a
        :class:`SessionError` the UI can show.  Sheets refused individually
        (one the verifier rejects, or a WDC frame with something inside the
        reach of its T17 slot) come back inside the result; only a whole-job
        failure raises.

        Milestone 6: with ``pdf_report`` on (the default) the printable
        cut-sheet report is written beside the programs as
        ``R<prefix>_report.pdf``.  It is written LAST and its failure can
        never take the NC with it — the operator can cut from a folder with
        no paperwork in it, but paperwork with no programs is useless — so
        the report is reported as its own problem on the returned job:

        ``job.report_path``
            Where the PDF landed, or ``None`` if none was asked for or
            written.
        ``job.report_problem``
            Why there is no PDF, or ``None``.

        Those two live on the job object rather than in
        :class:`~faceframe_cnc.post.job.JobResult` itself because the report
        is this layer's business: the NC post neither writes it nor depends
        on it.
        """
        from ..post.job import JobError, JobOptions, write_job

        blocker = self.generate_blocker()
        if blocker is not None:
            raise SessionError(blocker)

        options = JobOptions(
            output_dir=str(output_dir),
            prefix=str(prefix).strip(),
            dry_run=bool(dry_run),
            per_physical_sheet=bool(per_physical_sheet),
            created=created,
        )
        problems = options.validate()
        if problems:
            raise SessionError("; ".join(problems))

        try:
            job = write_job(self.result, options)
        except JobError as exc:
            raise SessionError(str(exc)) from exc

        job.report_path = None
        job.report_problem = None
        if pdf_report:
            self._write_report(job, options)

        if remember:
            self.settings.last_output_dir = job.output_dir
            self.settings.job_prefix = options.prefix
        return job

    def _write_report(self, job, options) -> None:
        """Write the PDF cut sheets beside the programs, never fatally.

        Deliberately catches everything: a report is paperwork, and no
        failure to produce paperwork may unwrite a verified NC program that
        is already on disk.  The reason comes back on the job so the UI can
        say so out loud rather than leave the user to notice the missing
        file.
        """
        path = os.path.join(job.output_dir, self.report_filename(options.prefix))
        try:
            from ..report.cutsheet import write_report

            job.report_path = write_report(
                self.result, job, path, created=options.created
            )
        except Exception as exc:  # noqa: BLE001 - see the docstring
            job.report_path = None
            job.report_problem = (
                f"the PDF cut-sheet report could not be written ({exc}); the NC "
                f"programs are unaffected"
            )

    @staticmethod
    def report_filename(prefix: str) -> str:
        """``R<prefix>_report.pdf``, the report that goes with this job."""
        from ..report.cutsheet import report_filename

        return report_filename(str(prefix).strip())

    # -- 3D cut simulation -----------------------------------------------

    def can_simulate(self, sheet_index: int) -> bool:
        """True when :meth:`simulation_inputs` has a sheet to work on.

        STRUCTURAL only — is there a current layout, and is this one of its
        unique sheets.  Deliberately NOT ``can_generate``: a sheet the post
        REFUSES is precisely the sheet the 3D refusal view exists for, so
        gating on cleanliness would hide the one picture that explains the
        refusal.  Staleness is not cleanliness, and staleness is covered
        anyway — every path that invalidates a layout clears :attr:`result`
        (:meth:`edit_row`, :meth:`resolve_row`, :meth:`set_included`,
        :meth:`set_all_included`, :meth:`set_settings`, :meth:`load_order`),
        so a stale layout offers no sheets here either.
        :meth:`simulation_inputs` still has the last word and gives the reason
        in words.
        """
        return self.result is not None and 0 <= sheet_index < self.unique_sheet_count

    def simulation_inputs(self, sheet_index: int) -> SimulationInputs:
        """Plan, emit and judge ONE unique sheet for the 3D simulation.

        The same steps :func:`faceframe_cnc.post.job.build_job` takes for the
        sheet it is about to write, in the same order and against the same
        tables:

        1.  the gate first.  :meth:`generate_blocker` — no layout, or a layout
            that does not pass its own validator — is reused verbatim rather
            than re-derived, so there is exactly ONE notion in this app of
            whether the layout on screen may be worked with (the 2026-08-04
            rule: a stale layout must never reach Generate, and it must not
            reach the simulation either).  Then :meth:`sheet`, which is what
            refuses an index that is not on the screen's layout;
        2.  ``post_config_for(result.config, None)`` — the same post table
            Generate uses, with the optimizer's sheet size in it;
        3.  :func:`~faceframe_cnc.post.from_layout.plan_sheet` on the sheet's
            layout, ``result.demand`` and ``result.config`` — the same call
            with the same arguments;
        4.  :meth:`~faceframe_cnc.sim.SimTimeline.build`, then
            :func:`~faceframe_cnc.post.verifier.expected_work` on the LAYOUT
            (not on the plan) and :func:`~faceframe_cnc.sim.run_verifier` with
            that manifest, located by
            :meth:`~faceframe_cnc.sim.FindingSet.build`.  Judging with the
            manifest is the whole point: without it the verifier can only see
            what the file says, never what the sheet owed, so a dropped
            through-cut or an unrouted opening would play back looking fine
            and refuse at Generate.

        Two deliberate differences from the job builder, neither of which can
        change the verdict:

        *   the header is a fixed simulation identity (:data:`SIM_CREATED`,
            :data:`SIM_JOB_PREFIX` when the settings hold no prefix yet), so
            simulating one sheet twice judges the same bytes.  The real
            O-number, file name and date belong to the run the operator
            starts in the Generate dialog, and they do not exist yet;
        *   no ``banner_lines``.  The banner states the job's sheet numbering
            and run quantity, which likewise do not exist yet; the verifier
            skips header comments, and
            :func:`~faceframe_cnc.post.verifier.expected_work` never looks at
            them, so their absence is invisible to every rule that judges a
            cut.

        Raises :class:`SessionError` for a state with nothing to simulate
        (gate 1) and :class:`SimulationRefused` — a ``SessionError`` carrying
        the planner's own exception, the part it named and the program when
        there is one — when the post refuses the sheet.  A refusal is a
        RESULT here, not a bug: the caller shows it in 3D.
        """
        blocker = self.generate_blocker()
        if blocker is not None:
            raise SessionError(blocker)
        layout, run = self.sheet(sheet_index)

        from ..post.from_layout import plan_sheet, post_config_for
        from ..post.model import ProgramHeader
        from ..post.verifier import expected_work
        from ..sim import FindingSet, SimTimeline, run_verifier

        result = self.result
        assert result is not None  # generate_blocker has already refused None
        post_config = post_config_for(result.config, None)
        header = ProgramHeader(
            name=self._sim_program_name(sheet_index),
            o_number=sheet_index + 1,
            created=SIM_CREATED,
        )

        try:
            program, plan = plan_sheet(
                layout, header, result.demand, result.config, post_config
            )
        except ValueError as exc:
            # SheetPlanError is a ValueError; so is anything the geometry
            # engine raises on the way through.  Both are refusals, and both
            # keep whatever structure they came with (SheetPlanError.part_number
            # / .box).  The program is rebuilt best-effort so the refusal view
            # can still draw the sheet the refusal is about.
            raise SimulationRefused(
                exc,
                program=self._sim_program_only(layout, header, result),
                post_config=post_config,
                sheet_index=sheet_index,
            ) from exc

        try:
            timeline = SimTimeline.build(program, plan, post_config)
            expected = expected_work(layout, timeline.config)
        except ValueError as exc:
            # The emitter refusing the program (a sheet size that does not
            # match its table) or the manifest refusing to be stated at all
            # (job.py's "no honest statement of the work is possible").  Both
            # are the same kind of refusal as above, and there IS a program to
            # draw this time.
            raise SimulationRefused(
                exc,
                program=program,
                post_config=post_config,
                sheet_index=sheet_index,
            ) from exc

        findings = FindingSet.build(timeline, run_verifier(timeline, expected))
        return SimulationInputs(
            sheet_index=sheet_index,
            layout=layout,
            run_quantity=int(run),
            header=header,
            post_config=post_config,
            program=program,
            plan=plan,
            timeline=timeline,
            expected=expected,
            findings=findings,
        )

    def _sim_program_name(self, sheet_index: int) -> str:
        """``R<prefix><NN>N`` for the sheet being simulated (spec section 6).

        The naming convention is :func:`faceframe_cnc.post.job.sheet_filename`
        itself, not a copy of it, so the simulated program cannot come to be
        called something a generated one never would.  The sheet index is
        1-based to match :attr:`~faceframe_cnc.post.job.JobOptions.first_sheet_index`.
        """
        from ..post.job import sheet_filename

        prefix = str(self.settings.job_prefix or "")
        if not prefix.isdigit():
            prefix = SIM_JOB_PREFIX
        return sheet_filename(prefix, sheet_index + 1)[: -len(".anc")]

    @staticmethod
    def _sim_program_only(layout, header, result) -> "SheetProgram | None":
        """The sheet's program alone, for a refusal that has no plan.

        A refusal from :func:`~faceframe_cnc.post.from_layout.plan_sheet` is
        either about the PROGRAM (an empty sheet, a part that is not in the
        order, a frame the geometry engine rejects — there is nothing to draw)
        or about the PLAN (a WDC slot with a neighbour inside its reach — the
        program built fine and is exactly what the operator needs to look at).
        Rebuilding just the program says which, without the caller having to
        know how :func:`plan_sheet` is put together.  Any failure means "no
        program", never an exception on top of an exception.
        """
        from ..post.from_layout import sheet_program_from_layout

        try:
            return sheet_program_from_layout(
                layout, header, result.demand, result.config
            )
        except ValueError:  # SheetPlanError included: it is a ValueError
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
