"""Order spreadsheet parser.

Implements spec section 2 ("Order spreadsheet parsing") from
``docs/CLAUDE_CODE_PROMPT_Faceframe_Optimizer.md``.

The shop's order files are legacy ``.xls`` spreadsheets with no usable
header row, so every column is read by fixed 0-based index. Read with
pandas + xlrd (both were confirmed to read the real sample files correctly
during development — ``pd.read_excel(path, header=None, engine="xlrd")``
reproduces the raw grid exactly, so there was no need to fall back to
calling xlrd directly).

This module is NOT imported by ``faceframe_cnc/__init__.py`` so that
``import faceframe_cnc`` keeps working on a bare stdlib Python without
pandas/xlrd installed (see Milestone 1a). Importing *this* module without
those dependencies raises the normal ``ImportError`` at import time.

Amendment (2026-08-03, "SD1212 / no-faceframe lines", see the Amendments
section of ``docs/CLAUDE_CODE_PROMPT_Faceframe_Optimizer.md`` — it
supersedes section 2's "exclude SD1212 after prompting"): a row with QTY > 0
and BOTH frame dimensions missing (e.g. ``SD1212``, a sample door whose
order form shows N/A for the faceframe — no faceframe is cut for it) is a
"no faceframe required" line, not a "needs attention" line. Those rows are
collected in :attr:`ParseResult.no_frame` instead of
:attr:`ParseResult.needs_attention`, are never prompted for, and are
auto-excluded (shown informationally, still manually resolvable — a user
can type dimensions and include the line if the order form was actually
wrong). ``needs_attention`` now only ever holds rows missing exactly ONE
frame dimension.

Amendment (2026-08-03, "WDC single-missing-dim auto-resolution", see the
Amendments section of the spec doc): a WDC part name encodes the
DIAGONAL-CORNER CABINET size, and the frame is 6" narrower than the cabinet
(2" stiles: WDC2436 = cabinet 24 x 36, frame 18 x 36).  The shop's order
forms routinely leave the frame width blank on WDC lines, which used to
prompt the owner to type 18 on every single load.  When a WDC row is
missing exactly ONE dimension and the dimension it DOES have matches what
the part number encodes, the missing one is derived from the name
(:func:`derive_wdc_dimensions`) and the row is emitted as a READY line
carrying a :attr:`OrderLine.note` saying so.  A present dimension that
CONTRADICTS the part number is never guessed over — the row stays in
``needs_attention`` exactly as before — and a WDC row missing BOTH
dimensions stays a ``no_frame`` row like anything else.

Amendment (2026-08-04, external review, four verified findings): (1) the
above only ran when a WDC dimension was MISSING -- a WDC row with BOTH
cells full skipped it, and the shop's order template routinely prefills
FRAME W with the CABINET width (the part number's diagonal-corner size),
not the true 6"-narrower frame width, so such a row parsed READY and
wrong.  :func:`check_wdc_dimensions` closes this with the same
auto-apply-and-show-the-note policy, raising for a real contradiction so
:func:`parse_order` can route it to ``needs_attention``.  (2)
:func:`safe_float` let ``inf``/``-inf``/``1e999``/``nan`` through from its
string branch; both branches now require ``math.isfinite``.  (3)
:func:`safe_int` used to floor a fractional QTY (``"0.9"`` -> ``0``, then
silently skipped as non-positive -- the row vanished with no record it
existed); it now refuses to floor and routes anything non-integral or
non-finite to ``needs_attention`` with the raw entered value.  (4) a row
whose part number is a drawer-base family this app cannot lay out (see
``geometry.FrameType.UNSUPPORTED_DRAWER_BASE``) or a "B"-prefixed
catalogue accessory (``BSK``, ``BFD24``, ``BPP9``/``BPP12``, ``BES12``,
``BF3`` -- verified qty 0 or ``"*"``, never actually ordered, in every
reference file on hand) now routes to ``needs_attention`` instead of
silently becoming a fabricated line.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .geometry import FrameType, infer_frame_type

__all__ = [
    "DrawerFaces",
    "OrderLine",
    "NeedsAttentionLine",
    "ParseResult",
    "safe_float",
    "safe_int",
    "derive_wdc_dimensions",
    "check_wdc_dimensions",
    "parse_order",
    "resolve",
]

# 0-indexed column layout (spec section 2). Columns not listed here (door
# size, quantity-of-doors/drawers, etc.) are not needed by this milestone.
COL_QTY = 2
COL_PART = 3
COL_FRAME_W = 7
COL_FRAME_H = 8
COL_TOP_W = 13
COL_TOP_H = 14
COL_MID_W = 16
COL_MID_H = 17
COL_BOT_W = 19
COL_BOT_H = 20

_QUANTITY_TOTAL = "quantity total"


def safe_float(value: object) -> Optional[float]:
    """Best-effort float parse that never raises.

    Handles ``int``/``float`` (including pandas ``NaN``), strips
    whitespace on ``str`` inputs, and returns ``None`` for ``None``, empty
    strings, and any junk that isn't actually numeric (e.g. ``"?"``).

    2026-08-04 review fix: a dimension cell must be a genuine, finite
    measurement. The numeric branch always filtered ``NaN`` but let
    ``inf``/``-inf`` through unchanged, and the string branch filtered
    NEITHER -- ``safe_float("inf")``, ``safe_float("1e999")`` (overflows
    to ``inf``) and ``safe_float("nan")`` all returned a non-finite float
    a caller could go on to do arithmetic with. Both branches now require
    ``math.isfinite`` before returning a value.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass; it is never a meaningful spreadsheet cell.
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            f = float(stripped)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _raw_number(value: object) -> Optional[float]:
    """Parse ``value`` to a float with NO finiteness or integrality check.

    Used only by :func:`safe_int` (and, directly, by :func:`parse_order`'s
    QTY handling) to tell "not a number at all" (``None``: a blank cell,
    ``None``, junk text like ``"?"`` or ``"*"`` -- a row like this is
    silently skipped, unchanged from before) apart from "a number, but
    not usable as a whole quantity" (``inf``, a fraction like ``2.9`` --
    2026-08-04 review fix 3: this must surface to a human, never be
    floored or dropped). Deliberately NOT exported: everything outside
    this module goes through :func:`safe_float`/:func:`safe_int`, which
    apply the finiteness/integrality rules those two are documented to
    apply.

    A ``NaN`` input (pandas' representation of a genuinely blank numeric
    cell) is treated the same as "not a number at all" -- ``None`` -- so a
    blank QTY cell keeps being silently skipped rather than newly routed
    to ``needs_attention``; that distinction is exactly why this does not
    just reuse the pre-fix ``safe_float`` behavior.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if math.isnan(f) else f
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            f = float(stripped)
        except ValueError:
            return None
        return None if math.isnan(f) else f
    return None


def safe_int(value: object) -> Optional[int]:
    """Parse a QTY cell to a whole number; never floors a fraction.

    2026-08-04 review fix 3: this used to be ``int(safe_float(value))``,
    which floors any float -- ``"2.9"`` silently became ``2``, and
    ``"0.9"`` became ``0`` and then vanished as a non-positive qty with no
    record the row ever existed. An exactly-integral value (``2``,
    ``2.0``, ``"2"``) still parses exactly as before. Anything else --
    non-integral (``2.9``) or non-finite (``inf``, ``1e999``) -- now
    returns ``None`` instead of being rounded, so it can never silently
    reach the optimizer as the wrong quantity; :func:`parse_order` calls
    :func:`_raw_number` itself to tell that case apart from "not a number
    at all" and route it to ``needs_attention`` with the real value shown.
    """
    f = _raw_number(value)
    if f is None or not math.isfinite(f):
        return None
    if f != math.floor(f):
        return None
    return int(f)


def _safe_str(value: object) -> Optional[str]:
    """Best-effort string parse: ``None``/``NaN``/blank all become ``None``."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text if text else None


@dataclass(frozen=True)
class DrawerFaces:
    """Informational drawer-face sizes (spec section 2 cols 13/14, 16/17, 19/20).

    These are the *applied front* sizes, never used to size routed
    openings (spec section 3) — only to help identify drawer lines. Any
    or all fields may be ``None`` when the sheet leaves them blank.
    """

    top_width: Optional[float] = None
    top_height: Optional[float] = None
    middle_width: Optional[float] = None
    middle_height: Optional[float] = None
    bottom_width: Optional[float] = None
    bottom_height: Optional[float] = None


@dataclass
class OrderLine:
    """One good, fully-specified order row.

    ``note`` is a human-readable provenance remark, empty for a row the
    spreadsheet fully specified.  The one producer today is the WDC
    single-missing-dim auto-resolution (2026-08-03 amendment), which
    records that a dimension was derived from the part number rather than
    read off the order form, so the GUI can show WHERE the number came
    from instead of silently inventing it.
    """

    row_index: int
    part_number: str
    qty: int
    frame_width: float
    frame_height: float
    frame_type: FrameType
    drawer_faces: DrawerFaces = field(default_factory=DrawerFaces)
    note: str = ""


@dataclass
class NeedsAttentionLine:
    """A row with QTY > 0 but something a human needs to look at.

    ``missing`` lists which of ``("width", "height")`` are absent so a UI
    can prompt for exactly those; ``reason`` is a ready-to-display message.
    ``missing`` is ``()`` for a row that has both dimensions but is stuck
    here for another reason (a WDC contradiction, an unsupported
    drawer-base family, a non-integral QTY -- 2026-08-04 review fixes 1/3/4
    below) -- check ``reason`` for those.

    This dataclass is reused for :attr:`ParseResult.no_frame` rows (the
    2026-08-03 "SD1212 / no-faceframe lines" amendment): a row missing BOTH
    dimensions has ``missing == ("width", "height")`` and lands in
    ``no_frame`` instead of ``needs_attention``, but is otherwise the same
    shape — :func:`resolve` works on either.

    ``qty`` is typed ``int`` for the common case but 2026-08-04 review fix
    3 (a QTY cell that is not a whole number, e.g. ``2.9``) stores the raw
    ``float`` as parsed off the sheet instead of silently flooring it --
    callers that need to display "what did the sheet actually say" (a GUI
    resolve dialog) read it as-is; nothing in this module ever floors it
    for them. KNOWN GAP (flagged for the GUI owner, not fixed here): the
    GUI's row-resolve editor only edits dimensions today, not quantity --
    a qty-needs-attention row has nothing in that editor to fix it with
    yet.
    """

    row_index: int
    part_number: str
    qty: "int | float"
    frame_width: Optional[float]
    frame_height: Optional[float]
    frame_type: FrameType
    drawer_faces: DrawerFaces
    missing: tuple[str, ...]
    reason: str


@dataclass
class ParseResult:
    """Outcome of parsing one order spreadsheet.

    ``needs_attention`` holds rows missing exactly ONE frame dimension
    (the user is prompted for it). ``no_frame`` holds rows missing BOTH
    frame dimensions — "no faceframe required" lines (2026-08-03
    amendment) such as ``SD1212`` — which are shown informationally and
    never prompted for.
    """

    lines: list[OrderLine] = field(default_factory=list)
    needs_attention: list[NeedsAttentionLine] = field(default_factory=list)
    no_frame: list[NeedsAttentionLine] = field(default_factory=list)
    skipped_rows: int = 0


#: A WDC part name in the shop's catalogue form: WDC + two digits of
#: cabinet width + two digits of cabinet height (WDC2436).  Anything else
#: (a suffix, a three-digit size) is not a name this parser will read a
#: dimension out of.
_WDC_CABINET = re.compile(r"^WDC(\d\d)(\d\d)$")

#: The frame is this much narrower than the cabinet the WDC name encodes:
#: the 2026-08-03 amendment's 2" stiles take 6" off the cabinet width
#: (WDC2436 = cabinet 24 x 36, frame 18 x 36).  The height carries over
#: unchanged.
_WDC_FRAME_WIDTH_REDUCTION = 6.0

#: How exactly the present dimension must match the encoded one before the
#: missing one is derived.  Order-form cells are whole or half inches, so
#: this only has to absorb float noise — a real disagreement (36 vs 35) is
#: a contradiction to surface, never a rounding artefact.
_WDC_MATCH_TOLERANCE = 1e-6


def _decode_wdc_cabinet(part_number: str) -> Optional[tuple[float, float]]:
    """Decode a ``WDC<ww><hh>`` part number to ``(cabinet_width, cabinet_height)``.

    Shared by :func:`derive_wdc_dimensions` (a WDC row missing ONE
    dimension) and :func:`check_wdc_dimensions` (2026-08-04 review fix 1, a
    WDC row with BOTH dimensions present but possibly wrong) so the two
    never decode the name two different ways.  Returns ``None`` for
    anything that is not a plain ``WDC`` + 2-digit-width + 2-digit-height
    name (a suffix, a three-digit size, ...) — not a name this parser
    reads a dimension out of.
    """
    match = _WDC_CABINET.match(part_number.strip().upper())
    if match is None:
        return None
    return float(match.group(1)), float(match.group(2))


def derive_wdc_dimensions(
    part_number: str,
    frame_width: Optional[float],
    frame_height: Optional[float],
) -> Optional[tuple[float, float, str]]:
    """Fill in a WDC row's single missing dimension from its part number.

    2026-08-03 amendment ("WDC single-missing-dim auto-resolution"): a WDC
    name encodes the diagonal-corner CABINET size, and the frame is the
    cabinet width minus 6" by the cabinet height.  Returns
    ``(frame_width, frame_height, note)`` when exactly one dimension is
    missing AND the present one equals what the name encodes — the only
    situation in which deriving is not guessing.  Returns ``None`` in every
    other case, spec section 2's "do NOT silently guess" intact:

    - not a plain ``WDC<ww><hh>`` name, or the encoded frame width would
      not be positive;
    - both dimensions present (nothing to derive — see
      :func:`check_wdc_dimensions` for the 2026-08-04 fix that checks THAT
      case) or both missing (a no-faceframe line, per the SD1212
      amendment);
    - the present dimension contradicts the part number, which means the
      order form and the name disagree and a human has to look.
    """
    decoded = _decode_wdc_cabinet(part_number)
    if decoded is None:
        return None
    cabinet_width, cabinet_height = decoded
    encoded_width = cabinet_width - _WDC_FRAME_WIDTH_REDUCTION
    if encoded_width <= 0:
        return None

    if frame_width is None and frame_height is not None:
        if abs(frame_height - cabinet_height) > _WDC_MATCH_TOLERANCE:
            return None
        note = (
            f"width {encoded_width:g} derived from part number (WDC cabinet "
            f"{cabinet_width:g}x{cabinet_height:g}, 2in-stile frame is 6in narrower)"
        )
        return encoded_width, float(frame_height), note

    if frame_height is None and frame_width is not None:
        if abs(frame_width - encoded_width) > _WDC_MATCH_TOLERANCE:
            return None
        note = (
            f"height {cabinet_height:g} derived from part number (WDC cabinet "
            f"{cabinet_width:g}x{cabinet_height:g}, 2in-stile frame is 6in narrower)"
        )
        return float(frame_width), cabinet_height, note

    return None


def check_wdc_dimensions(
    part_number: str,
    frame_width: float,
    frame_height: float,
) -> tuple[float, float, str]:
    """Validate/auto-correct a WDC row whose FRAME W and FRAME H are BOTH present.

    2026-08-04 review fix 1 ("WDC rows with both dims present bypass the
    contradiction check", order_parser.py:336 as filed): the function
    above, :func:`derive_wdc_dimensions`, only ever ran when a dimension
    was MISSING — a WDC row that arrived with both cells full skipped the
    check entirely. The shop's order template routinely prefills the
    FRAME W column with the CABINET width the part number encodes (the
    diagonal-corner size), not the true 6"-narrower frame width, so a real
    order line (``WDC2436`` qty 30, W=24 H=36) parsed as a READY 24x36
    frame — wrong; the frame is 18x36.

    Returns ``(frame_width, frame_height, note)``:

    - not a decodable ``WDC<ww><hh>`` name, or the encoded frame width
      would not be positive -> ``(frame_width, frame_height, "")``
      unchanged — exactly the prior behavior for anything this parser
      cannot read a dimension out of.
    - the entered width already equals the encoded frame width (cabinet
      width minus 6") and the entered height matches the cabinet height
      -> unchanged, empty note (correct as entered, nothing to prove).
    - the entered width equals the RAW cabinet width (the template
      prefill mistake) and the height matches -> the corrected width, with
      a non-empty provenance note in the same style as
      :func:`derive_wdc_dimensions`'s (2026-08-03's "auto-apply derived
      values AND show them" policy — no silent magic).

    Raises ``ValueError`` for anything else: the entered width matches
    neither reading, or the entered height contradicts the name outright.
    :func:`parse_order` catches this and routes the row to
    ``needs_attention`` with the message as the reason — a real
    contradiction between the order form and the part number is never
    guessed over, spec section 2's rule intact.
    """
    decoded = _decode_wdc_cabinet(part_number)
    if decoded is None:
        return frame_width, frame_height, ""
    cabinet_width, cabinet_height = decoded
    encoded_width = cabinet_width - _WDC_FRAME_WIDTH_REDUCTION
    if encoded_width <= 0:
        return frame_width, frame_height, ""

    height_ok = abs(frame_height - cabinet_height) <= _WDC_MATCH_TOLERANCE
    width_matches_frame = abs(frame_width - encoded_width) <= _WDC_MATCH_TOLERANCE
    width_matches_cabinet = abs(frame_width - cabinet_width) <= _WDC_MATCH_TOLERANCE

    if height_ok and width_matches_frame:
        return frame_width, frame_height, ""

    if height_ok and width_matches_cabinet:
        note = (
            f"width {encoded_width:g} derived from part number (entered width "
            f"{frame_width:g} is the WDC cabinet {cabinet_width:g}x{cabinet_height:g} "
            "size prefilled by the order template; 2in-stile frame is 6in narrower)"
        )
        return encoded_width, frame_height, note

    raise ValueError(
        f"WDC frame dims {frame_width:g}x{frame_height:g} entered don't match part "
        f"number {part_number} (encodes cabinet {cabinet_width:g}x{cabinet_height:g}, "
        f"expected frame {encoded_width:g}x{cabinet_height:g})"
    )


#: Catalogue accessory PART NUMBER prefixes that also start with "B" (so
#: the bare ``startswith("B")`` -> BASE rule in
#: ``geometry.infer_frame_type`` would otherwise catch them) but are NOT
#: actual base-drawer-over-door faceframes: base skirt (BSK), base
#: filler/false-front (BFD), base pull-out/pantry panel (BPP), base end or
#: corner (BES), base filler strip (BF). 2026-08-04 review investigation:
#: checked BOTH reference order files on hand -- every catalogue row for
#: every one of these families is qty 0 or ``"*"`` (never a positive
#: number) in both, so none is actually ordered today. This is therefore
#: a defensive fix for a FUTURE order, not a reproduced bug: a real order
#: line for one of these would otherwise silently become a fabricated
#: base-drawer-over-door frame (a 5" drawer opening that does not exist on
#: a skirt board).
_ACCESSORY_PREFIXES = ("BSK", "BFD", "BPP", "BES", "BF")


def _is_catalog_accessory(part_number: str) -> bool:
    """True for a known "B"-prefixed catalogue accessory, not a real base frame."""
    normalized = part_number.strip().upper()
    return normalized.startswith(_ACCESSORY_PREFIXES)


def parse_order(path: str) -> ParseResult:
    """Parse a legacy ``.xls`` order spreadsheet into a :class:`ParseResult`.

    Rules (spec section 2, as amended 2026-08-03 "SD1212 / no-faceframe
    lines" and 2026-08-04 review fixes 1/3/4):
    - Only rows with QTY > 0 are considered at all. A QTY cell that
      parses to SOME number but not a whole one (``2.9``, ``inf``) is
      NOT treated as "no qty" -- fix 3 routes it to ``needs_attention``
      instead of flooring it (``2.9`` -> ``2``) or silently dropping it
      (``0.9`` -> floor ``0`` -> skipped). A blank/junk QTY cell (``NaN``,
      ``"?"``, ``"*"``) is still silently skipped, unchanged.
    - The "Quantity Total" summary row is always excluded, never surfaced
      in ``needs_attention`` or ``no_frame`` even though its QTY is > 0.
    - A row whose part number is a drawer-base family this app cannot lay
      out, or a "B"-prefixed catalogue accessory (fix 4 and the
      accompanying investigation; see ``_ACCESSORY_PREFIXES`` and
      ``geometry.FrameType.UNSUPPORTED_DRAWER_BASE``), always goes to
      ``needs_attention`` -- even when both dimensions are present -- so
      it never becomes a fabricated line with the wrong geometry.
    - A WDC row missing exactly ONE dimension whose OTHER dimension matches
      the part number is auto-resolved from the name (2026-08-03 amendment,
      :func:`derive_wdc_dimensions`) and emitted as a good line with a
      ``note`` recording the derivation — no prompt.  A contradiction is
      never guessed over.
    - A WDC row with BOTH dimensions present is now also checked (fix 1,
      :func:`check_wdc_dimensions`): correct as entered stays READY with no
      note; the order template's cabinet-width-prefill mistake is
      auto-corrected with a provenance note; any other mismatch goes to
      ``needs_attention`` stating both the entered and the encoded values.
    - Every other row with QTY > 0 missing exactly ONE of FRAME W / FRAME H
      goes to ``needs_attention`` instead of being dropped or guessed at.
    - Rows with QTY > 0 missing BOTH FRAME W and FRAME H go to
      ``no_frame`` — the order form is saying no faceframe is cut for
      this line (e.g. ``SD1212``, a sample door) — not ``needs_attention``.
      These are never prompted for.
    - Every other row (blank rows, section headers, repeated column-header
      rows, rows with QTY <= 0 or non-numeric QTY) is silently skipped and
      counted in ``skipped_rows``.
    """
    df = pd.read_excel(path, header=None, engine="xlrd")
    ncols = df.shape[1]

    def cell(row: "pd.Series", idx: int) -> object:
        return row[idx] if idx < ncols else None

    lines: list[OrderLine] = []
    needs_attention: list[NeedsAttentionLine] = []
    no_frame: list[NeedsAttentionLine] = []
    skipped_rows = 0

    for row_index, row in df.iterrows():
        part = _safe_str(cell(row, COL_PART))
        if part is None or part.strip().lower() == _QUANTITY_TOTAL:
            skipped_rows += 1
            continue

        frame_w = safe_float(cell(row, COL_FRAME_W))
        frame_h = safe_float(cell(row, COL_FRAME_H))
        drawer_faces = DrawerFaces(
            top_width=safe_float(cell(row, COL_TOP_W)),
            top_height=safe_float(cell(row, COL_TOP_H)),
            middle_width=safe_float(cell(row, COL_MID_W)),
            middle_height=safe_float(cell(row, COL_MID_H)),
            bottom_width=safe_float(cell(row, COL_BOT_W)),
            bottom_height=safe_float(cell(row, COL_BOT_H)),
        )
        frame_type = infer_frame_type(part)

        qty_cell = cell(row, COL_QTY)
        qty = safe_int(qty_cell)
        if qty is None:
            # 2026-08-04 review fix 3: tell "not a number at all" (blank
            # cell, junk like "?"/"*" -- silently skip, unchanged) apart
            # from "a number, but not a whole quantity" (a fraction, or
            # inf/-inf/overflow) -- that must surface to a human, never be
            # floored or dropped.  _raw_number keeps NaN (pandas' blank
            # cell) in the "not a number" bucket on purpose.
            raw_qty = _raw_number(qty_cell)
            if raw_qty is None:
                skipped_rows += 1
                continue
            needs_attention.append(
                NeedsAttentionLine(
                    row_index=int(row_index),
                    part_number=part,
                    qty=raw_qty,
                    frame_width=frame_w,
                    frame_height=frame_h,
                    frame_type=frame_type,
                    drawer_faces=drawer_faces,
                    missing=(),
                    reason=f"quantity {raw_qty:g} is not a whole number",
                )
            )
            continue
        if qty <= 0:
            skipped_rows += 1
            continue

        if frame_type is FrameType.UNSUPPORTED_DRAWER_BASE or _is_catalog_accessory(part):
            # 2026-08-04 review fix 4 (+ accompanying investigation): never
            # let a drawer-base family this app can't lay out, or a
            # catalogue accessory the bare startswith("B") rule would
            # otherwise treat as a base frame, become a fabricated line —
            # regardless of whether its dimensions happen to be present.
            missing = tuple(
                name for name, val in (("width", frame_w), ("height", frame_h)) if val is None
            )
            if frame_type is FrameType.UNSUPPORTED_DRAWER_BASE:
                reason = (
                    f"{part} is an unsupported drawer-base family: this app does not "
                    "know its drawer cross-bar layout; the row needs manual "
                    "dimensions/confirmation"
                )
            else:
                reason = f"{part}: not a standard faceframe part - confirm"
            needs_attention.append(
                NeedsAttentionLine(
                    row_index=int(row_index),
                    part_number=part,
                    qty=qty,
                    frame_width=frame_w,
                    frame_height=frame_h,
                    frame_type=frame_type,
                    drawer_faces=drawer_faces,
                    missing=missing,
                    reason=reason,
                )
            )
            continue

        if frame_w is None or frame_h is None:
            missing = tuple(
                name for name, val in (("width", frame_w), ("height", frame_h)) if val is None
            )
            if len(missing) == 1:
                # 2026-08-03 amendment: a WDC row missing exactly one
                # dimension whose other dimension matches the part number
                # is derived, not prompted for.  ``None`` here means "not a
                # derivable case" (not WDC, or a contradiction) and the row
                # falls through to needs_attention exactly as before.
                derived = derive_wdc_dimensions(part, frame_w, frame_h)
                if derived is not None:
                    width, height, note = derived
                    lines.append(
                        OrderLine(
                            row_index=int(row_index),
                            part_number=part,
                            qty=qty,
                            frame_width=width,
                            frame_height=height,
                            frame_type=frame_type,
                            drawer_faces=drawer_faces,
                            note=note,
                        )
                    )
                    continue
            reason = "missing frame " + " and ".join(missing)
            attention_line = NeedsAttentionLine(
                row_index=int(row_index),
                part_number=part,
                qty=qty,
                frame_width=frame_w,
                frame_height=frame_h,
                frame_type=frame_type,
                drawer_faces=drawer_faces,
                missing=missing,
                reason=reason,
            )
            if len(missing) == 2:
                # 2026-08-03 amendment: both dims missing means the order
                # form is saying no faceframe is cut for this line (e.g.
                # SD1212, a sample door) -- not a dimension to chase down.
                no_frame.append(attention_line)
            else:
                needs_attention.append(attention_line)
            continue

        note = ""
        if frame_type is FrameType.WDC:
            # 2026-08-04 review fix 1: both dims present is no longer a
            # free pass for a WDC row -- check it against what the part
            # number encodes, auto-correcting the known template-prefill
            # mistake (visible proof via `note`) and refusing to guess
            # over anything else.
            try:
                frame_w, frame_h, note = check_wdc_dimensions(part, frame_w, frame_h)
            except ValueError as exc:
                needs_attention.append(
                    NeedsAttentionLine(
                        row_index=int(row_index),
                        part_number=part,
                        qty=qty,
                        frame_width=frame_w,
                        frame_height=frame_h,
                        frame_type=frame_type,
                        drawer_faces=drawer_faces,
                        missing=(),
                        reason=str(exc),
                    )
                )
                continue

        lines.append(
            OrderLine(
                row_index=int(row_index),
                part_number=part,
                qty=qty,
                frame_width=frame_w,
                frame_height=frame_h,
                frame_type=frame_type,
                drawer_faces=drawer_faces,
                note=note,
            )
        )

    return ParseResult(
        lines=lines,
        needs_attention=needs_attention,
        no_frame=no_frame,
        skipped_rows=skipped_rows,
    )


def _whole_qty(value: object) -> Optional[int]:
    """``value`` as a whole quantity, or ``None`` when it is not one."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    return int(number)


def resolve(
    line: NeedsAttentionLine,
    *,
    width: Optional[float] = None,
    height: Optional[float] = None,
    qty: Optional["int | float"] = None,
) -> OrderLine:
    """Complete a ``needs_attention`` or ``no_frame`` line with user-supplied values.

    Works on a line from either :attr:`ParseResult.needs_attention` or
    :attr:`ParseResult.no_frame` -- both are :class:`NeedsAttentionLine`,
    and the 2026-08-03 amendment keeps ``no_frame`` lines "manually
    resolvable" (a user can still type dims and include the line if the
    order form was actually wrong).

    What the caller supplies WINS over what the sheet said (2026-08-04
    review): a dimension that is present but known-dubious -- a WDC width
    that contradicts the part number, which fix 1 above routes here
    precisely because it should not be trusted -- has to be correctable, and
    silently discarding the caller's number made that impossible. Omit a
    value (``None``) to keep the parsed one.

    ``qty`` is optional in general but REQUIRED when the line's own quantity
    is not a whole number (fix 3 above keeps a QTY cell of ``2.9`` exactly as
    the sheet wrote it rather than flooring it): a fractional quantity must
    never ride into an :class:`OrderLine`, where it would become a
    fractional demand nobody can cut.

    Raises ``ValueError`` if a required dimension is still missing, or if the
    resulting quantity is not a whole number of at least 1.
    """
    resolved_w = width if width is not None else line.frame_width
    resolved_h = height if height is not None else line.frame_height

    still_missing = [
        name for name, val in (("width", resolved_w), ("height", resolved_h)) if val is None
    ]
    if still_missing:
        raise ValueError(
            f"cannot resolve {line.part_number!r} (row {line.row_index}): "
            f"still missing {' and '.join(still_missing)}"
        )

    resolved_qty = qty if qty is not None else line.qty
    whole = _whole_qty(resolved_qty)
    if whole is None:
        raise ValueError(
            f"cannot resolve {line.part_number!r} (row {line.row_index}): "
            f"quantity {resolved_qty!r} is not a whole number - supply the "
            f"quantity to cut"
        )
    if whole <= 0:
        raise ValueError(
            f"cannot resolve {line.part_number!r} (row {line.row_index}): "
            f"quantity must be at least 1, got {resolved_qty!r}"
        )

    return OrderLine(
        row_index=line.row_index,
        part_number=line.part_number,
        qty=whole,
        frame_width=float(resolved_w),
        frame_height=float(resolved_h),
        frame_type=line.frame_type,
        drawer_faces=line.drawer_faces,
    )
