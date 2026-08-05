"""A very small, very deliberate PDF 1.4 writer.

This exists so the cut-sheet report can be produced on a shop PC that has
nothing installed but Python: no ReportLab, no Qt, no fonts to embed, no
network.  What it can do is exactly what the report needs — pages,
filled and stroked rectangles, straight lines, and text in Helvetica or
Helvetica-Bold, left/centre/right aligned and optionally turned 90
degrees.  What it cannot do is everything else, and that is on purpose:
every feature here is one more thing that can produce a file the shop's
PDF viewer will not open.

Why the base-14 fonts
---------------------
Helvetica and Helvetica-Bold are two of the fourteen faces every PDF
consumer is required to have, so nothing is embedded.  The price is that
this module has to know their metrics itself in order to centre or
right-align a string; :data:`_HELVETICA` and :data:`_HELVETICA_BOLD` are
the Adobe AFM advance widths (thousandths of the point size) for ASCII 32
to 126, which is the whole repertoire the report uses.  Anything outside
that range is transliterated (``—`` to ``-``) or replaced with ``?`` by
:func:`sanitize` before it is measured or written, so a stray character
pasted out of a spreadsheet cannot silently shift a column or produce a
string the WinAnsi encoding would render as mojibake.

Determinism
-----------
The bytes are a pure function of the calls made into this module.  There
is no ``/CreationDate``, no ``/ID``, no compression and no dictionary
iteration whose order could wobble; every number in a content stream goes
through :func:`_num`, a fixed two-decimal formatter that also flattens
``-0.00`` to ``0.00``.  Two runs of the same report are byte-identical,
which is what makes the report diffable and the tests exact.

Structure of the file
---------------------
``%PDF-1.4``, then object 1 the catalog, object 2 the page tree, objects 3
and 4 the two fonts, object 5 the document information dictionary, then
one page object and one (uncompressed) content stream object per page.
The cross-reference table is written last from the byte offsets recorded
while the objects were emitted, followed by the trailer, ``startxref``
and ``%%EOF``.  Every offset in that table points at the first byte of an
``N 0 obj`` line; a test re-reads the finished bytes and checks that.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional, Sequence

__all__ = [
    "Color",
    "Document",
    "Page",
    "HELVETICA",
    "HELVETICA_BOLD",
    "BLACK",
    "WHITE",
    "LETTER_PORTRAIT",
    "gray",
    "rgb",
    "hex_color",
    "sanitize",
    "text_width",
    "line_height",
    "fit_size",
    "truncate",
    "wrap_text",
]


# --------------------------------------------------------------------------
# Colours and geometry
# --------------------------------------------------------------------------


class Color(NamedTuple):
    """A device-RGB colour, each channel in ``[0, 1]``."""

    red: float
    green: float
    blue: float


def rgb(red: float, green: float, blue: float) -> Color:
    """Clamped device RGB."""

    def clamp(value: float) -> float:
        return 0.0 if value < 0.0 else (1.0 if value > 1.0 else float(value))

    return Color(clamp(red), clamp(green), clamp(blue))


def gray(level: float) -> Color:
    """Neutral grey; ``0`` is black and ``1`` is white."""
    return rgb(level, level, level)


def hex_color(value: str) -> Color:
    """``"#cfe0ee"`` (or ``"cfe0ee"``) as a :class:`Color`.

    The GUI canvas states its palette in hex, and the printed page is meant
    to look like the screen the user just approved, so the two can share
    the same literals.
    """
    text = str(value).strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"{value!r} is not a 6-digit hex colour")
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except ValueError as exc:  # pragma: no cover - guarded by the length check
        raise ValueError(f"{value!r} is not a 6-digit hex colour") from exc
    return rgb(red / 255.0, green / 255.0, blue / 255.0)


BLACK = Color(0.0, 0.0, 0.0)
WHITE = Color(1.0, 1.0, 1.0)

#: US Letter, portrait, in PostScript points (1/72 inch).
LETTER_PORTRAIT = (612.0, 792.0)

HELVETICA = "Helvetica"
HELVETICA_BOLD = "Helvetica-Bold"

#: Resource name each font is published under in every page's /Resources.
_FONT_RESOURCE = {HELVETICA: "F1", HELVETICA_BOLD: "F2"}


# --------------------------------------------------------------------------
# Metrics (Adobe AFM advance widths, 1/1000 em, ASCII 32-126)
# --------------------------------------------------------------------------

_HELVETICA = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333,   # 32-41
    389, 584, 278, 333, 278, 278, 556, 556, 556, 556,   # 42-51
    556, 556, 556, 556, 556, 556, 278, 278, 584, 584,   # 52-61
    584, 556, 1015, 667, 667, 722, 722, 667, 611, 778,  # 62-71
    722, 278, 500, 667, 556, 833, 722, 778, 667, 778,   # 72-81
    722, 667, 611, 722, 667, 944, 667, 667, 611, 278,   # 82-91
    278, 278, 469, 556, 333, 556, 556, 500, 556, 556,   # 92-101
    278, 556, 556, 222, 222, 500, 222, 833, 556, 556,   # 102-111
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500,   # 112-121
    500, 334, 260, 334, 584,                            # 122-126
)

_HELVETICA_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333,   # 32-41
    389, 584, 278, 333, 278, 278, 556, 556, 556, 556,   # 42-51
    556, 556, 556, 556, 556, 556, 333, 333, 584, 584,   # 52-61
    584, 611, 975, 722, 722, 722, 722, 667, 611, 778,   # 62-71
    722, 278, 556, 722, 611, 833, 722, 778, 667, 778,   # 72-81
    722, 667, 611, 722, 667, 944, 667, 667, 611, 333,   # 82-91
    278, 333, 584, 556, 333, 556, 611, 556, 611, 556,   # 92-101
    333, 611, 611, 278, 278, 556, 278, 889, 611, 611,   # 102-111
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556,   # 112-121
    500, 389, 280, 389, 584,                            # 122-126
)

_WIDTHS = {HELVETICA: _HELVETICA, HELVETICA_BOLD: _HELVETICA_BOLD}

#: Helvetica's own ascender and descender, used to place a baseline when
#: the caller asked for text centred in a box rather than sitting on a line.
ASCENT = 0.718
DESCENT = 0.207

#: Characters a spreadsheet or a docstring may contain that WinAnsi could
#: encode but this module's width table cannot measure.  Folded rather than
#: replaced, because "R720101N - R720103N" reads better than "R720101N ?".
_TRANSLITERATE = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "‘": "'", "’": "'", "‚": ",", "“": '"',
    "”": '"', "…": "...", " ": " ", "×": "x",
    "°": " deg", "½": "1/2", "¼": "1/4", "¾": "3/4",
    "•": "-", "→": "->",
}


def sanitize(text: object) -> str:
    """Text reduced to the printable ASCII this module can measure and write.

    Tabs and newlines become spaces (a content-stream string is one line);
    anything else outside 32-126 that has no transliteration becomes ``?``,
    which is visible on the page rather than silently dropped.
    """
    out: list[str] = []
    for character in str(text):
        folded = _TRANSLITERATE.get(character, character)
        for piece in folded:
            code = ord(piece)
            if 32 <= code <= 126:
                out.append(piece)
            elif piece in "\t\r\n":
                out.append(" ")
            else:
                out.append("?")
    return "".join(out)


def text_width(text: object, font: str = HELVETICA, size: float = 10.0) -> float:
    """Advance width of ``text`` in points, after :func:`sanitize`."""
    widths = _WIDTHS.get(font)
    if widths is None:
        raise ValueError(f"unknown font {font!r}")
    total = 0
    for character in sanitize(text):
        total += widths[ord(character) - 32]
    return total * float(size) / 1000.0


def line_height(size: float, leading: float = 1.2) -> float:
    """Baseline-to-baseline distance for stacked lines at ``size``."""
    return float(size) * float(leading)


def fit_size(
    text: object,
    font: str,
    max_width: float,
    *,
    start: float = 10.0,
    minimum: float = 4.0,
    step: float = 0.5,
    max_height: Optional[float] = None,
) -> float:
    """Largest size from ``start`` down that fits ``text`` in the box.

    Mirrors what the on-screen canvas does when a part is too small for its
    label: shrink rather than clip, and stop at ``minimum`` rather than
    shrink into invisibility.
    """
    size = float(start)
    floor = float(minimum)
    while size > floor:
        wide = text_width(text, font, size) > float(max_width)
        tall = max_height is not None and line_height(size) > float(max_height)
        if not (wide or tall):
            break
        size = round(size - float(step), 4)
    return max(size, floor)


def truncate(text: object, font: str, size: float, max_width: float) -> str:
    """``text``, shortened with an ellipsis until it fits ``max_width``."""
    body = sanitize(text)
    if text_width(body, font, size) <= max_width:
        return body
    while body and text_width(body + "...", font, size) > max_width:
        body = body[:-1]
    return (body + "...") if body else ""


def wrap_text(
    text: object, font: str, size: float, max_width: float, max_lines: int = 0
) -> list[str]:
    """Word-wrap ``text`` to ``max_width``, at most ``max_lines`` (0 = all).

    A word longer than the column is broken rather than allowed to run off
    the page, and when ``max_lines`` cuts the text off the last line is
    truncated with an ellipsis so the reader can see that it did.
    """
    words = sanitize(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}" if current else word
        if current and text_width(trial, font, size) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
        while text_width(current, font, size) > max_width and len(current) > 1:
            cut = len(current)
            while cut > 1 and text_width(current[:cut], font, size) > max_width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current:
        lines.append(current)
    if not lines:
        return [""]
    if max_lines and len(lines) > max_lines:
        kept = lines[: max_lines - 1] if max_lines > 1 else []
        remainder = " ".join(lines[max_lines - 1 :])
        kept.append(truncate(remainder, font, size, max_width))
        return kept
    return lines


# --------------------------------------------------------------------------
# Content-stream formatting
# --------------------------------------------------------------------------


def _num(value: float) -> str:
    """A coordinate, fixed at two decimals and never ``-0.00``.

    Fixed precision is what makes the output byte-stable: ``repr`` of a
    float carries whatever the arithmetic happened to produce, and the same
    page composed twice must not differ in its seventeenth digit.
    """
    rounded = round(float(value), 2) + 0.0
    return f"{rounded:.2f}"


def _matrix_num(value: float) -> str:
    """A text-matrix entry; five decimals, for the rotation cosines."""
    rounded = round(float(value), 5) + 0.0
    return f"{rounded:.5f}"


def _color(color: Color, stroke: bool) -> str:
    operator = "RG" if stroke else "rg"
    red, green, blue = color
    return f"{_matrix_num(red)} {_matrix_num(green)} {_matrix_num(blue)} {operator}"


def _escape(text: str) -> str:
    """Escape a PDF literal string: backslash first, then the parentheses."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _dash(pattern: Optional[Sequence[float]]) -> str:
    if not pattern:
        return "[] 0 d"
    return "[" + " ".join(_num(value) for value in pattern) + "] 0 d"


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


class Page:
    """One page's content stream, in PDF user space.

    The origin is the lower-left corner and y increases upwards — the same
    convention the nesting module uses for a sheet, which is why no drawing
    code in :mod:`~faceframe_cnc.report.cutsheet` has to flip anything.

    Every operation is wrapped in its own ``q``/``Q`` pair, so no call can
    leak a colour, a line width or a dash pattern into the next one.
    """

    def __init__(self, width: float, height: float):
        self.width = float(width)
        self.height = float(height)
        self._ops: list[str] = []

    # -- drawing ---------------------------------------------------------

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        fill: Optional[Color] = None,
        stroke: Optional[Color] = None,
        line_width: float = 0.6,
        dash: Optional[Sequence[float]] = None,
    ) -> None:
        """A rectangle, filled and/or stroked.  Neither means nothing drawn."""
        if fill is None and stroke is None:
            return
        parts = ["q"]
        if fill is not None:
            parts.append(_color(fill, stroke=False))
        if stroke is not None:
            parts.append(_color(stroke, stroke=True))
            parts.append(f"{_num(line_width)} w")
            parts.append(_dash(dash))
        parts.append(
            f"{_num(x)} {_num(y)} {_num(width)} {_num(height)} re"
        )
        if fill is not None and stroke is not None:
            parts.append("B")
        elif fill is not None:
            parts.append("f")
        else:
            parts.append("S")
        parts.append("Q")
        self._ops.append(" ".join(parts))

    def line(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        *,
        stroke: Color = BLACK,
        line_width: float = 0.6,
        dash: Optional[Sequence[float]] = None,
    ) -> None:
        """A straight segment."""
        self._ops.append(
            " ".join(
                [
                    "q",
                    _color(stroke, stroke=True),
                    f"{_num(line_width)} w",
                    _dash(dash),
                    f"{_num(x0)} {_num(y0)} m",
                    f"{_num(x1)} {_num(y1)} l",
                    "S",
                    "Q",
                ]
            )
        )

    def text(
        self,
        x: float,
        y: float,
        text: object,
        *,
        font: str = HELVETICA,
        size: float = 10.0,
        color: Color = BLACK,
        align: str = "left",
        rotate: float = 0.0,
    ) -> float:
        """Draw one line of text with its baseline origin at ``(x, y)``.

        ``align`` shifts the origin along the text's own direction, so a
        centred label stays centred when ``rotate`` is 90.  Returns the
        advance width actually drawn, which callers use to size the chip
        behind a label.
        """
        body = sanitize(text)
        if not body.strip():
            return 0.0
        resource = _FONT_RESOURCE.get(font)
        if resource is None:
            raise ValueError(f"unknown font {font!r}")
        advance = text_width(body, font, size)
        shift = 0.0
        if align == "center":
            shift = -advance / 2.0
        elif align == "right":
            shift = -advance
        elif align != "left":
            raise ValueError(f"unknown alignment {align!r}")

        radians = math.radians(float(rotate))
        cosine, sine = math.cos(radians), math.sin(radians)
        origin_x = float(x) + shift * cosine
        origin_y = float(y) + shift * sine
        self._ops.append(
            " ".join(
                [
                    "BT",
                    _color(color, stroke=False),
                    f"/{resource} {_num(size)} Tf",
                    f"{_matrix_num(cosine)} {_matrix_num(sine)} "
                    f"{_matrix_num(-sine)} {_matrix_num(cosine)} "
                    f"{_num(origin_x)} {_num(origin_y)} Tm",
                    f"({_escape(body)}) Tj",
                    "ET",
                ]
            )
        )
        return advance

    def text_centered_in(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        text: object,
        *,
        font: str = HELVETICA,
        size: float = 10.0,
        color: Color = BLACK,
    ) -> None:
        """Text centred both ways in a box, using Helvetica's own metrics."""
        # The glyph band a baseline anchors is ASCENT above it and DESCENT
        # below it, so its total height is (ASCENT + DESCENT) * size -- not
        # (ASCENT - DESCENT) * size, which is what this used to subtract and
        # which rode every centred label about DESCENT * size too high.
        baseline = y + (height - (ASCENT + DESCENT) * size) / 2.0 + DESCENT * size
        self.text(
            x + width / 2.0,
            baseline,
            text,
            font=font,
            size=size,
            color=color,
            align="center",
        )

    # -- output ----------------------------------------------------------

    def content(self) -> bytes:
        return ("\n".join(self._ops) + "\n").encode("latin-1")


# --------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------


class Document:
    """A collection of pages, rendered to PDF bytes by :meth:`to_bytes`."""

    def __init__(
        self,
        *,
        title: str = "",
        creator: str = "",
        producer: str = "faceframe-cnc report",
        subject: str = "",
    ):
        self.title = str(title)
        self.creator = str(creator)
        self.producer = str(producer)
        self.subject = str(subject)
        self.pages: list[Page] = []

    def add_page(
        self, width: float = LETTER_PORTRAIT[0], height: float = LETTER_PORTRAIT[1]
    ) -> Page:
        page = Page(width, height)
        self.pages.append(page)
        return page

    def to_bytes(self) -> bytes:
        """Render the whole document.  Pure function of the pages' contents."""
        if not self.pages:
            raise ValueError("a PDF must have at least one page")

        count = len(self.pages)
        first_page_obj = 6
        first_content_obj = first_page_obj + count
        kids = " ".join(f"{first_page_obj + i} 0 R" for i in range(count))

        objects: list[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("latin-1"),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>",
            self._info(),
        ]
        for index, page in enumerate(self.pages):
            objects.append(
                (
                    "<< /Type /Page /Parent 2 0 R "
                    f"/MediaBox [0 0 {_num(page.width)} {_num(page.height)}] "
                    "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                    f"/Contents {first_content_obj + index} 0 R >>"
                ).encode("latin-1")
            )
        for page in self.pages:
            body = page.content()
            objects.append(
                f"<< /Length {len(body)} >>\nstream\n".encode("latin-1")
                + body
                + b"endstream"
            )
        return _assemble(objects, root=1, info=5)

    def _info(self) -> bytes:
        """The document information dictionary — no dates, on purpose.

        A ``/CreationDate`` would make the file differ from run to run, and
        the stamp the operator actually needs is printed on every page.
        """
        fields = [
            ("Title", self.title),
            ("Subject", self.subject),
            ("Author", self.creator),
            ("Creator", self.creator),
            ("Producer", self.producer),
        ]
        parts = [
            f"/{name} ({_escape(sanitize(value))})"
            for name, value in fields
            if str(value)
        ]
        return ("<< " + " ".join(parts) + " >>").encode("latin-1")


def _assemble(objects: list[bytes], *, root: int, info: int) -> bytes:
    """Body, cross-reference table and trailer for numbered objects 1..N.

    The offsets in the table are recorded as the objects are written, so
    they cannot drift from the bytes they describe; each entry is the
    mandatory twenty bytes wide.
    """
    out = bytearray()
    out += b"%PDF-1.4\n"
    # A binary comment, so anything transferring this file treats it as
    # binary rather than helpfully rewriting its line endings.
    out += b"%\xe2\xe3\xcf\xd3\n"

    offsets = [0] * (len(objects) + 1)
    for number, body in enumerate(objects, start=1):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("latin-1")
        out += body
        out += b"\nendobj\n"

    xref_at = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {size} /Root {root} 0 R /Info {info} 0 R >>\n"
        f"startxref\n{xref_at}\n"
        "%%EOF\n"
    ).encode("latin-1")
    return bytes(out)
