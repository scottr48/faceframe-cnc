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
frame dimension (e.g. ``WDC2436`` in the 7-21 file, missing only the
width).
"""

from __future__ import annotations

import math
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
    """One good, fully-specified order row."""

    row_index: int
    part_number: str
    qty: int
    frame_width: float
    frame_height: float
    frame_type: FrameType
    drawer_faces: DrawerFaces = field(default_factory=DrawerFaces)


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


def parse_order(path: str) -> ParseResult:
    """Parse a legacy ``.xls`` order spreadsheet into a :class:`ParseResult`.

    Rules (spec section 2, as amended 2026-08-03 — "SD1212 / no-faceframe
    lines"):
    - Only rows with QTY > 0 are considered at all.
    - The "Quantity Total" summary row is always excluded, never surfaced
      in ``needs_attention`` or ``no_frame`` even though its QTY is > 0.
    - Rows with QTY > 0 missing exactly ONE of FRAME W / FRAME H go to
      ``needs_attention`` instead of being dropped or guessed at.
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
