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
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass; it is never a meaningful spreadsheet cell.
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if math.isnan(f) else f
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def safe_int(value: object) -> Optional[int]:
    """Same safety as :func:`safe_float`, coerced to ``int`` (for QTY)."""
    f = safe_float(value)
    return None if f is None else int(f)


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
    """A row with QTY > 0 but a missing/non-numeric frame dimension.

    ``missing`` lists which of ``("width", "height")`` are absent so a UI
    can prompt for exactly those; ``reason`` is a ready-to-display message.

    This dataclass is reused for :attr:`ParseResult.no_frame` rows (the
    2026-08-03 "SD1212 / no-faceframe lines" amendment): a row missing BOTH
    dimensions has ``missing == ("width", "height")`` and lands in
    ``no_frame`` instead of ``needs_attention``, but is otherwise the same
    shape — :func:`resolve` works on either.
    """

    row_index: int
    part_number: str
    qty: int
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
    - both dimensions present (nothing to derive) or both missing (a
      no-faceframe line, per the SD1212 amendment);
    - the present dimension contradicts the part number, which means the
      order form and the name disagree and a human has to look.
    """
    match = _WDC_CABINET.match(part_number.strip().upper())
    if match is None:
        return None
    cabinet_width = float(match.group(1))
    cabinet_height = float(match.group(2))
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


def parse_order(path: str) -> ParseResult:
    """Parse a legacy ``.xls`` order spreadsheet into a :class:`ParseResult`.

    Rules (spec section 2, as amended 2026-08-03 — "SD1212 / no-faceframe
    lines"):
    - Only rows with QTY > 0 are considered at all.
    - The "Quantity Total" summary row is always excluded, never surfaced
      in ``needs_attention`` or ``no_frame`` even though its QTY is > 0.
    - A WDC row missing exactly ONE dimension whose OTHER dimension matches
      the part number is auto-resolved from the name (2026-08-03 amendment,
      :func:`derive_wdc_dimensions`) and emitted as a good line with a
      ``note`` recording the derivation — no prompt.  A contradiction is
      never guessed over.
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

        qty = safe_int(cell(row, COL_QTY))
        if qty is None or qty <= 0:
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

        lines.append(
            OrderLine(
                row_index=int(row_index),
                part_number=part,
                qty=qty,
                frame_width=frame_w,
                frame_height=frame_h,
                frame_type=frame_type,
                drawer_faces=drawer_faces,
            )
        )

    return ParseResult(
        lines=lines,
        needs_attention=needs_attention,
        no_frame=no_frame,
        skipped_rows=skipped_rows,
    )


def resolve(
    line: NeedsAttentionLine,
    *,
    width: Optional[float] = None,
    height: Optional[float] = None,
) -> OrderLine:
    """Complete a ``needs_attention`` or ``no_frame`` line with a user-supplied dimension.

    Works on a line from either :attr:`ParseResult.needs_attention` or
    :attr:`ParseResult.no_frame` -- both are :class:`NeedsAttentionLine`,
    and the 2026-08-03 amendment keeps ``no_frame`` lines "manually
    resolvable" (a user can still type dims and include the line if the
    order form was actually wrong). Only dimensions the line is actually
    missing need to be supplied; already-present dimensions are kept as
    parsed. Raises ``ValueError`` if a required dimension is still missing
    after this call.
    """
    resolved_w = line.frame_width if line.frame_width is not None else width
    resolved_h = line.frame_height if line.frame_height is not None else height

    still_missing = [
        name for name, val in (("width", resolved_w), ("height", resolved_h)) if val is None
    ]
    if still_missing:
        raise ValueError(
            f"cannot resolve {line.part_number!r} (row {line.row_index}): "
            f"still missing {' and '.join(still_missing)}"
        )

    return OrderLine(
        row_index=line.row_index,
        part_number=line.part_number,
        qty=line.qty,
        frame_width=float(resolved_w),
        frame_height=float(resolved_h),
        frame_type=line.frame_type,
        drawer_faces=line.drawer_faces,
    )
