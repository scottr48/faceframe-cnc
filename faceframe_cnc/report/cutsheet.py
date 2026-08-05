"""The printable cut-sheet report: the paperwork that goes to the machine.

One PDF for a whole job.  A cover page carries the headline numbers and
the full unique-sheet list; after it comes **one page per unique sheet
picture**, with the sheet drawn to scale, every part labelled with its part
number, the run quantity boxed where nobody can miss it, and a cut list
beside the drawing.

What the page is FOR
--------------------
An operator standing at the machine with a stack of MDF needs three
answers and needs them from paper, because the app is not in the shop:

1.  *how many of this one do I cut?* — the boxed **RUN QTY**;
2.  *which file do I load?* — the file name in the header, as a range when
    the job was written one file per physical sheet;
3.  *what comes off it, and is anything hiding inside something else?* —
    the labelled drawing plus the cut list, whose "nested in ..." notes
    say which small frame is cut out of which big frame's waste.

A sheet the NC job REFUSED still gets a page.  Its header carries a
refusal banner and the first problem line, because paperwork with a hole
in it is how the gap gets noticed; paperwork that quietly omits the sheet
is how a cabinet ends up missing a frame.

Where the numbers come from
---------------------------
Pages are paired with :attr:`~faceframe_cnc.nesting.NestingResult.unique_sheets`,
never with the job's outcome list, so the report is one page per PICTURE
even when the job was written one file per physical sheet — in that mode
the expanded outcomes are folded back onto their picture and the header
shows the file range.  Everything else is read from the same places the
GUI reads it: :func:`~faceframe_cnc.gui.session.sheet_openings` for the
routed openings (the module-level, Qt-free one),
:func:`~faceframe_cnc.geometry.compute_geometry` for the cut list's
opening sizes, and the layouts themselves for the footprints.

Determinism
-----------
:func:`build_report` is a pure function of the layout, the job and the
injected ``created`` stamp — exactly like
:attr:`faceframe_cnc.post.job.JobOptions.created`, ``None`` means "now".
Two builds with the same stamp are byte-identical.

Writing
-------
:func:`write_report` composes the entire document in memory first and only
then opens the file, so a report that cannot be composed leaves nothing
behind — the same discipline
:func:`faceframe_cnc.post.job.write_job` applies to a program that cannot
be verified.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Sequence

from ..geometry import (
    FrameType,
    WDC_SLOT_INSET_FROM_INSIDE_EDGE,
    WDC_STILE_INSET,
    compute_geometry,
    infer_frame_type,
    wdc_slot_axis_is_height,
)
from ..gui.session import sheet_openings
from ..post.job import APP_BANNER_NAME, DRY_RUN_BANNER, PARTIAL_SUFFIX, now_created
from . import pdf

__all__ = [
    "ReportError",
    "SheetReport",
    "CutListRow",
    "DRAWING_REGION",
    "CUTLIST_REGION",
    "build_report",
    "write_report",
    "report_filename",
    "sheet_reports",
    "sheet_transform",
    "cut_list",
]


class ReportError(RuntimeError):
    """The report cannot be composed (and so nothing is written)."""


# --------------------------------------------------------------------------
# Page geometry (points; Letter portrait)
# --------------------------------------------------------------------------

PAGE_WIDTH, PAGE_HEIGHT = pdf.LETTER_PORTRAIT
MARGIN = 36.0
CONTENT_LEFT = MARGIN
CONTENT_RIGHT = PAGE_WIDTH - MARGIN
CONTENT_WIDTH = CONTENT_RIGHT - CONTENT_LEFT

FOOTER_RULE_Y = 40.0
FOOTER_TEXT_Y = 30.0

#: Where the scale drawing lives on a sheet page: ``(x, y, width, height)``.
#: Fixed rather than computed from the header's height, so the scale and
#: the origin are the same on every page of the report — an operator
#: comparing two sheets is comparing two identical projections.
DRAWING_REGION = (36.0, 56.0, 264.0, 574.0)
#: The cut list occupies the mirror-image column to its right.
CUTLIST_REGION = (312.0, 56.0, 264.0, 574.0)

#: How far the cut list's boxed "xN" count sits from the row's left edge.
#: The part number column must stay clear of it: a name of ~15+ characters
#: at the row's bold 9.5pt would otherwise print UNDER the count and the
#: two would overlap into an unreadable glyph pile.
_CUTLIST_COUNT_OFFSET = 78.0
_CUTLIST_NUMBER_MAX_WIDTH = _CUTLIST_COUNT_OFFSET - 4.0

REGULAR = pdf.HELVETICA
BOLD = pdf.HELVETICA_BOLD


# --------------------------------------------------------------------------
# Palette
#
# The muted colours the on-screen canvas uses (gui/sheet_canvas.py), so the
# page looks like the layout the user just approved.  All of them are light
# enough to photocopy or print on a mono laser without the part numbers
# disappearing into their fills.
# --------------------------------------------------------------------------

SHEET_FILL = pdf.hex_color("#fbfbf8")
SHEET_EDGE = pdf.hex_color("#8a8a80")
CUSHION_EDGE = pdf.hex_color("#c4c4b4")
FRONT_MARGIN_EDGE = pdf.hex_color("#c9b8a0")
PART_FILL = pdf.hex_color("#cfe0ee")
PART_EDGE = pdf.hex_color("#2f4a63")
HOST_FILL = pdf.hex_color("#bcd6ea")
CHILD_FILL = pdf.hex_color("#f6dfae")
CHILD_EDGE = pdf.hex_color("#8a6410")
OPENING_FILL = pdf.WHITE
OPENING_EDGE = pdf.hex_color("#9aa7b1")
LABEL_COLOR = pdf.hex_color("#12212e")
CHIP_FILL = pdf.hex_color("#ffffff")
REFUSED_COLOR = pdf.hex_color("#b00020")
DRY_RUN_COLOR = pdf.hex_color("#8a4b00")
MUTED = pdf.gray(0.38)
RULE = pdf.gray(0.72)
TILE_FILL = pdf.gray(0.955)
TILE_EDGE = pdf.gray(0.78)

#: Dash patterns matching the canvas's guides.
CUSHION_DASH = (3.0, 2.5)
FRONT_MARGIN_DASH = (1.0, 2.0)

# -- The T17 WDC stile slot on paper (2026-08-03 owner request) ------------
#
# The owner wants to SEE where the special routing runs before trusting a
# WDC line, so every WDC placement is drawn with its two slot centrelines.
# Everything is DERIVED from the geometry engine — the same constants the
# optimizer reserves room with and the post cross-checks — so the paper can
# never show a slot the machine does not cut.

#: Its own colour and its own dash: the slot is a CUT, not a guide, so it
#: must not read as the cushion (grey 3/2.5) or the front margin (tan 1/2).
WDC_SLOT_EDGE = pdf.hex_color("#b3541e")
WDC_SLOT_DASH = (4.0, 2.0)

#: Frame-local distance from a stile's OUTER edge to its slot centreline:
#: the amendment gives the centreline 34 mm off the stile's INSIDE
#: (opening-side) edge, and the stile is 2" wide, so the line sits at
#: 0.6614 from the outside.  The post derives the same number as
#: ``WdcSlotSpec.inset_from_outside_edge``; a test pins the two.
WDC_SLOT_CENTRELINE_FROM_OUTER_EDGE = (
    WDC_STILE_INSET - WDC_SLOT_INSET_FROM_INSIDE_EDGE
)


# --------------------------------------------------------------------------
# What the report says about one unique sheet
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CutListRow:
    """One distinct part number on one sheet, as the cut list prints it."""

    part_number: str
    count: int
    width: float
    height: float
    frame_type: str
    openings: str
    #: Host part numbers this part is nested inside on this sheet, if any.
    hosts: tuple[str, ...] = ()

    @property
    def size_text(self) -> str:
        return f"{_dim(self.width)} x {_dim(self.height)}"

    @property
    def nested_text(self) -> str:
        if not self.hosts:
            return ""
        return "nested in " + ", ".join(self.hosts)


@dataclass(frozen=True)
class SheetReport:
    """One unique sheet picture, joined to whatever the NC job did with it."""

    index: int
    layout: object
    run: int
    #: The file name, or ``"first - last"`` when one file per physical sheet.
    filename: str
    files: tuple[str, ...]
    contents: str
    nested: int
    refused: bool
    problems: tuple[str, ...]
    #: How many of :attr:`files` failed — always equal to ``len(files)``
    #: outside one-file-per-physical-sheet mode, but in that mode a single
    #: bad file among a picture's run must not read as "nothing was
    #: written" when the rest of the run wrote clean (2026-08-04 review).
    failed_files: int = 0

    @property
    def status_text(self) -> str:
        if not self.files:
            return "NO FILE"
        if not self.refused:
            return "written"
        if 0 < self.failed_files < len(self.files):
            return f"{self.failed_files} of {len(self.files)} FAILED"
        return "REFUSED"


def sheet_reports(result, job) -> list[SheetReport]:
    """Pair every unique sheet picture with its NC outcome(s).

    In the default one-file-per-picture mode this is a one-to-one join.  In
    one-file-per-physical-sheet mode the job's outcomes were produced by
    :func:`faceframe_cnc.post.job._expand`, which repeats each picture
    ``run`` times in order, so they fold back onto their picture by simply
    walking the runs — and the header then prints the file RANGE.
    """
    if result is None or not getattr(result, "unique_sheets", None):
        raise ReportError(
            "there is no layout to report on — run the optimizer first"
        )
    if job is None:
        raise ReportError("there is no NC job to report on")

    outcomes = list(job.outcomes)
    per_physical = bool(getattr(job.options, "per_physical_sheet", False))
    pictures = list(result.unique_sheets)

    groups: list[list] = []
    if per_physical:
        expected = sum(int(run) for _layout, run in pictures)
        if len(outcomes) != expected:
            raise ReportError(
                f"the NC job has {len(outcomes)} files but the layout needs "
                f"{expected} physical sheets — the report cannot say which "
                f"file cuts which sheet"
            )
        position = 0
        for _layout, run in pictures:
            groups.append(outcomes[position : position + int(run)])
            position += int(run)
    else:
        if len(outcomes) != len(pictures):
            raise ReportError(
                f"the NC job has {len(outcomes)} files but the layout has "
                f"{len(pictures)} unique sheets — the report cannot pair them"
            )
        groups = [[outcome] for outcome in outcomes]

    reports: list[SheetReport] = []
    for index, ((layout, run), group) in enumerate(zip(pictures, groups)):
        # Pictures and outcomes are paired by count and position alone above
        # — a stale job re-paired against a freshly re-optimized layout of
        # the same LENGTH would mispair silently, putting one sheet's
        # filenames over another sheet's drawing.  Every SheetOutcome
        # carries its own part-count manifest (contents), independent of
        # position, so it is cross-checked against this picture's actual
        # part counts before the pairing is trusted (2026-08-04 review).
        expected_counts = layout.part_counts()
        for outcome in group:
            if dict(outcome.contents) != expected_counts:
                raise ReportError(
                    f"sheet {index + 1}: NC outcome {outcome.filename!r} carries "
                    f"{dict(outcome.contents)} but the layout for this picture has "
                    f"{expected_counts} — the job and the layout have drifted apart, "
                    f"refusing to pair them"
                )
        names = tuple(outcome.filename for outcome in group)
        problems: list[str] = []
        failed_files = 0
        for outcome in group:
            if not outcome.ok:
                failed_files += 1
            for problem in outcome.problems:
                if problem not in problems:
                    problems.append(problem)
        if len(names) > 1:
            label = f"{names[0]} - {names[-1]}"
        elif names:
            label = names[0]
        else:  # pragma: no cover - guarded by the length checks above
            label = "(no file)"
        reports.append(
            SheetReport(
                index=index,
                layout=layout,
                run=int(run),
                filename=label,
                files=names,
                contents=_contents_text(layout),
                nested=layout.child_count(),
                refused=bool(problems),
                problems=tuple(problems),
                failed_files=failed_files,
            )
        )
    return reports


def cut_list(layout, ordered: dict) -> list[CutListRow]:
    """The distinct part numbers on one sheet, with their sizes and openings.

    Sorted by part number rather than by placement order: the operator is
    checking a list against a pile of parts, not walking the sheet.
    """
    counts: dict[str, int] = {}
    hosts: dict[str, list[str]] = {}

    def walk(placements: Sequence, host: Optional[str]) -> None:
        for placement in placements:
            name = placement.part_number
            counts[name] = counts.get(name, 0) + 1
            if host is not None and host not in hosts.setdefault(name, []):
                hosts[name].append(host)
            walk(placement.children, name)

    walk(layout.placements, None)

    rows: list[CutListRow] = []
    for name in sorted(counts):
        spec = ordered.get(name)
        if spec is not None:
            width, height = float(spec.width), float(spec.height)
        else:
            width, height = _placed_dims(layout, name)
        geometry = compute_geometry(name, width, height)
        if geometry.errors or not geometry.openings:
            openings = "-"
        else:
            openings = ", ".join(
                f"{_dim(opening.width)} x {_dim(opening.height)}"
                for opening in geometry.openings
            )
        rows.append(
            CutListRow(
                part_number=name,
                count=counts[name],
                width=width,
                height=height,
                frame_type=infer_frame_type(name).value,
                openings=openings,
                hosts=tuple(sorted(hosts.get(name, ()))),
            )
        )
    return rows


def sheet_transform(config, region=DRAWING_REGION) -> tuple[float, float, float]:
    """``(points per inch, origin x, origin y)`` for the sheet drawing.

    A single UNIFORM scale for both axes — a sheet drawn 5% wider than it
    is tall would be a drawing nobody can measure off — with the sheet
    centred in its region.
    """
    x0, y0, width, height = region
    sheet_width = float(config.sheet_width)
    sheet_height = float(config.sheet_height)
    if sheet_width <= 0 or sheet_height <= 0:
        raise ReportError(
            f"the sheet size {sheet_width} x {sheet_height} cannot be drawn"
        )
    scale = min(width / sheet_width, height / sheet_height)
    origin_x = x0 + (width - sheet_width * scale) / 2.0
    origin_y = y0 + (height - sheet_height * scale) / 2.0
    return scale, origin_x, origin_y


def report_filename(prefix: str) -> str:
    """``R<prefix>_report.pdf`` — the report beside the job's ``.anc`` files."""
    return f"R{prefix}_report.pdf"


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def build_report(result, job, *, created: Optional[str] = None) -> bytes:
    """Compose the whole PDF and return its bytes.  Touches no files.

    ``created`` is the stamp printed in every footer; ``None`` means now,
    exactly like :attr:`faceframe_cnc.post.job.JobOptions.created`.  Pass it
    to make two builds byte-identical.
    """
    reports = sheet_reports(result, job)
    stamp = created if created is not None else now_created()
    app_name = str(getattr(job.options, "app_name", APP_BANNER_NAME) or APP_BANNER_NAME)
    prefix = str(getattr(job.options, "prefix", "") or "")
    ordered = {spec.part_number: spec for spec in result.demand}

    document = pdf.Document(
        title=f"{app_name} cut sheets - R{prefix}" if prefix else f"{app_name} cut sheets",
        creator=app_name,
        subject=(
            f"{result.total_sheets} sheets from {len(reports)} unique pictures"
            + (" (DRY RUN)" if job.dry_run else "")
        ),
    )

    _cover(document, result, job, reports, stamp, app_name)
    for report in reports:
        _sheet_page(document, result, job, report, ordered, app_name)
    _footers(document, stamp, app_name)
    return document.to_bytes()


def write_report(result, job, path: str, *, created: Optional[str] = None) -> str:
    """Write the report to ``path`` and return the absolute path.

    The bytes are composed in full before any file is touched, so a report
    that cannot be composed never truncates a previous one.  The write
    itself is also atomic (2026-08-04 review — this had the exact
    bare-``open(path, "wb")`` shape :func:`faceframe_cnc.post.job.write_job`
    was fixed of the review before): the bytes go to ``<path>.partial`` in
    the SAME folder first, flushed and ``fsync``'d, and :func:`os.replace`
    swaps it onto ``target`` in one step, so a crash or a full disk
    mid-write can only leave the ``.partial`` behind — never a half-written
    report at the name an operator would actually open.  Every failure
    comes back as :class:`ReportError`.
    """
    data = build_report(result, job, created=created)
    target = os.path.abspath(str(path))
    folder = os.path.dirname(target)
    if folder:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            raise ReportError(f"cannot create the report folder {folder}: {exc}") from exc
    partial = target + PARTIAL_SUFFIX
    try:
        with open(partial, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, target)
    except OSError as exc:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise ReportError(f"could not write {target}: {exc}") from exc
    return target


# --------------------------------------------------------------------------
# Cover page
# --------------------------------------------------------------------------

#: Cover table column left edges and widths.
_COL_FILE = (36.0, 148.0)
_COL_RUN = (190.0, 30.0)
_COL_CONTENTS = (228.0, 236.0)
_COL_NESTED = (470.0, 30.0)
_COL_STATUS = (508.0, 68.0)

#: How many refused filenames the cover names explicitly before falling
#: back to "and N more" — a sane cap so the line stays readable regardless
#: of how many sheets were refused, and :func:`~faceframe_cnc.report.pdf.wrap_text`
#: below still wraps it, so even the capped line cannot run off the page
#: (2026-08-04 review: an unwrapped ", ".join of 5+ names used to).
_REFUSED_NAMES_SHOWN = 6
_REFUSED_SUMMARY_MAX_LINES = 4


def _refused_summary_text(refused: Sequence) -> str:
    names = [report.filename for report in refused]
    shown = ", ".join(names[:_REFUSED_NAMES_SHOWN])
    if len(names) > _REFUSED_NAMES_SHOWN:
        shown += f", and {len(names) - _REFUSED_NAMES_SHOWN} more"
    return f"{len(refused)} sheet(s) were REFUSED and have no NC program: {shown}"


def _cover(document, result, job, reports, stamp, app_name) -> None:
    page = document.add_page()
    y = PAGE_HEIGHT - MARGIN - 12.0

    page.text(CONTENT_LEFT, y, app_name, font=BOLD, size=17)
    y -= 17.0
    prefix = str(getattr(job.options, "prefix", "") or "")
    heading = "Cut-sheet report" + (f" - job R{prefix}" if prefix else "")
    page.text(CONTENT_LEFT, y, heading, font=BOLD, size=11)
    y -= 13.0
    page.text(
        CONTENT_LEFT,
        y,
        f"created {stamp}",
        size=8.5,
        color=MUTED,
    )
    y -= 11.0
    page.text(
        CONTENT_LEFT,
        y,
        pdf.truncate(f"NC folder: {job.output_dir}", REGULAR, 8.5, CONTENT_WIDTH),
        size=8.5,
        color=MUTED,
    )
    y -= 18.0

    if job.dry_run:
        y = _dry_run_banner(page, y)

    y = _headline_tiles(page, result, reports, y)
    y -= 16.0

    page.text(CONTENT_LEFT, y, "UNIQUE SHEETS", font=BOLD, size=10)
    y -= 12.0
    y = _table_header(page, y)

    for report in reports:
        height = _cover_row_height(report)
        if y - height < 96.0:
            page = document.add_page()
            top = PAGE_HEIGHT - MARGIN - 12.0
            page.text(
                CONTENT_LEFT, top, "UNIQUE SHEETS (continued)", font=BOLD, size=10
            )
            # A plain 12pt drop (enough for the heading's own line height)
            # was too tight once the banner box was added below it: the
            # box's top edge landed a couple of points ABOVE the heading's
            # baseline and printed through the bottom of its letters.  18pt
            # matches the gap the first cover page already uses after its
            # own last header line, so the banner never touches the text
            # above it.
            y = top - 18.0
            # The "dry runs marked on every page" invariant applies to the
            # cover's own overflow pages too — a continuation page carries
            # rows that say "written" with nothing else on it to say
            # otherwise, so the banner repeats here exactly as it does on
            # every sheet page.
            if job.dry_run:
                y = _dry_run_banner(page, y)
            y = _table_header(page, y)
        y = _cover_row(page, y, report)

    y -= 6.0
    page.line(CONTENT_LEFT, y, CONTENT_RIGHT, y, stroke=RULE, line_width=0.8)
    y -= 13.0
    total_run = sum(report.run for report in reports)
    page.text(
        CONTENT_LEFT,
        y,
        f"Run quantities sum to {total_run} physical sheets.",
        font=BOLD,
        size=9.5,
    )
    if total_run != result.total_sheets:
        y -= 12.0
        page.text(
            CONTENT_LEFT,
            y,
            f"WARNING: the optimizer reports {result.total_sheets} sheets, not "
            f"{total_run} - do not cut from this report.",
            font=BOLD,
            size=9,
            color=REFUSED_COLOR,
        )
    refused = [report for report in reports if report.refused]
    if refused:
        y -= 13.0
        lines = pdf.wrap_text(
            _refused_summary_text(refused), BOLD, 9, CONTENT_WIDTH,
            max_lines=_REFUSED_SUMMARY_MAX_LINES,
        )
        for offset, line in enumerate(lines):
            page.text(
                CONTENT_LEFT,
                y - offset * 11.0,
                line,
                font=BOLD,
                size=9,
                color=REFUSED_COLOR,
            )


def _dry_run_banner(page, y) -> float:
    """The cover's dry-run banner, factored out so it can be repeated on
    every continuation page of the unique-sheets table (fix for the
    2026-08-04 review: a >42-unique-sheet dry run's overflow page carried
    rows saying "written" with no mark that this whole job is a rehearsal)."""
    page.rect(
        CONTENT_LEFT,
        y - 4.0,
        CONTENT_WIDTH,
        18.0,
        fill=pdf.hex_color("#fdf1e0"),
        stroke=DRY_RUN_COLOR,
        line_width=0.9,
    )
    page.text(
        CONTENT_LEFT + 6.0, y + 1.0, DRY_RUN_BANNER, font=BOLD, size=9,
        color=DRY_RUN_COLOR,
    )
    return y - 24.0


def _headline_tiles(page, result, reports, y) -> float:
    """The answer the owner bought this app for, in four boxes."""
    saved = result.sheets_saved
    tiles = [
        (str(result.total_sheets), "PHYSICAL SHEETS"),
        (str(len(reports)), "UNIQUE PICTURES"),
        (str(result.total_parts), "FRAMES"),
        (
            ("-" if saved is None else str(saved)),
            "SHEETS SAVED BY NESTING"
            if saved is not None
            else "NO BASELINE RUN",
        ),
    ]
    gap = 8.0
    width = (CONTENT_WIDTH - gap * (len(tiles) - 1)) / len(tiles)
    height = 46.0
    top = y
    for position, (value, label) in enumerate(tiles):
        x = CONTENT_LEFT + position * (width + gap)
        page.rect(
            x, top - height, width, height,
            fill=TILE_FILL, stroke=TILE_EDGE, line_width=0.7,
        )
        page.text(x + width / 2.0, top - 25.0, value, font=BOLD, size=19, align="center")
        size = pdf.fit_size(label, REGULAR, width - 8.0, start=7.0, minimum=5.0, step=0.25)
        page.text(
            x + width / 2.0, top - height + 8.0, label,
            size=size, color=MUTED, align="center",
        )
    return top - height


def _table_header(page, y) -> float:
    for (x, width), title, align in (
        (_COL_FILE, "NC FILE", "left"),
        (_COL_RUN, "RUN", "right"),
        (_COL_CONTENTS, "CONTENTS", "left"),
        (_COL_NESTED, "NEST", "right"),
        (_COL_STATUS, "STATUS", "left"),
    ):
        anchor = x + width if align == "right" else x
        page.text(anchor, y, title, font=BOLD, size=7.5, color=MUTED, align=align)
    y -= 3.5
    page.line(CONTENT_LEFT, y, CONTENT_RIGHT, y, stroke=RULE, line_width=0.7)
    return y - 11.0


def _cover_contents_lines(report) -> list[str]:
    return pdf.wrap_text(report.contents, REGULAR, 8.0, _COL_CONTENTS[1], max_lines=3)


def _cover_row_height(report) -> float:
    lines = len(_cover_contents_lines(report))
    height = max(1, lines) * 10.0 + 2.0
    if report.refused:
        height += 10.0
    return height


def _cover_row(page, y, report) -> float:
    lines = _cover_contents_lines(report)
    page.text(
        _COL_FILE[0],
        y,
        pdf.truncate(report.filename, BOLD, 8.5, _COL_FILE[1]),
        font=BOLD,
        size=8.5,
    )
    page.text(
        _COL_RUN[0] + _COL_RUN[1], y, str(report.run), font=BOLD, size=8.5, align="right"
    )
    for offset, line in enumerate(lines):
        page.text(_COL_CONTENTS[0], y - offset * 10.0, line, size=8.0)
    page.text(
        _COL_NESTED[0] + _COL_NESTED[1],
        y,
        str(report.nested) if report.nested else "-",
        size=8.0,
        align="right",
    )
    status_font = BOLD if report.refused else REGULAR
    page.text(
        _COL_STATUS[0],
        y,
        pdf.truncate(report.status_text, status_font, 8.0, CONTENT_RIGHT - _COL_STATUS[0]),
        font=status_font,
        size=8.0,
        color=REFUSED_COLOR if report.refused else MUTED,
    )
    y -= max(1, len(lines)) * 10.0 + 2.0
    if report.refused:
        page.text(
            _COL_CONTENTS[0],
            y,
            pdf.truncate(report.problems[0], REGULAR, 7.5, CONTENT_RIGHT - _COL_CONTENTS[0]),
            size=7.5,
            color=REFUSED_COLOR,
        )
        y -= 10.0
    return y


# --------------------------------------------------------------------------
# One page per unique sheet
# --------------------------------------------------------------------------


def _sheet_page(document, result, job, report, ordered, app_name) -> None:
    page = document.add_page()
    total = len(result.unique_sheets)

    _sheet_header(page, job, report, total)
    scale, origin_x, origin_y = sheet_transform(result.config)
    page.text(
        DRAWING_REGION[0],
        DRAWING_REGION[1] + DRAWING_REGION[3] + 6.0,
        f"{_dim(result.config.sheet_width)} x {_dim(result.config.sheet_height)} "
        f"sheet, drawn to scale 1:{72.0 / scale:.1f}",
        font=BOLD,
        size=8.5,
        color=MUTED,
    )
    _draw_sheet(page, report.layout, result.config, ordered, (scale, origin_x, origin_y))
    _draw_cut_list(document, page, report, ordered)


def _sheet_header(page, job, report, total) -> None:
    top = PAGE_HEIGHT - MARGIN

    # -- RUN QTY, boxed, top right.  The operator must not be able to miss
    #    it: it is the one number on the page that decides how much MDF
    #    goes through the machine.
    box_width, box_height = 152.0, 46.0
    box_x = CONTENT_RIGHT - box_width
    box_y = top - box_height
    page.rect(
        box_x, box_y, box_width, box_height,
        fill=pdf.gray(0.94), stroke=pdf.BLACK, line_width=1.5,
    )
    page.text(
        box_x + box_width / 2.0, box_y + box_height - 13.0, "RUN QTY",
        font=BOLD, size=9, color=MUTED, align="center",
    )
    page.text(
        box_x + box_width / 2.0, box_y + 8.0, str(report.run),
        font=BOLD, size=24, align="center",
    )

    y = top - 16.0
    title_width = box_x - CONTENT_LEFT - 10.0
    size = pdf.fit_size(report.filename, BOLD, title_width, start=20.0, minimum=9.0)
    page.text(CONTENT_LEFT, y, report.filename, font=BOLD, size=size)
    y -= 15.0
    page.text(
        CONTENT_LEFT,
        y,
        f"SHEET {report.index + 1} OF {total}"
        + (f"   -   {len(report.files)} files" if len(report.files) > 1 else ""),
        font=BOLD,
        size=9.5,
        color=MUTED,
    )
    y -= 14.0

    for line in pdf.wrap_text(report.contents, REGULAR, 9.0, title_width, max_lines=2):
        page.text(CONTENT_LEFT, y, line, size=9.0)
        y -= 11.0
    if report.nested:
        page.text(
            CONTENT_LEFT,
            y,
            f"{report.nested} frame(s) nested in host waste on this sheet",
            size=8.5,
            color=MUTED,
        )
        y -= 12.0
    if job.dry_run:
        page.text(
            CONTENT_LEFT, y, DRY_RUN_BANNER, font=BOLD, size=9, color=DRY_RUN_COLOR
        )
        y -= 13.0
    if report.refused:
        # 2026-08-04 review: in one-file-per-physical-sheet mode a single
        # bad file among a picture's run used to print the same banner as a
        # total loss ("NO NC PROGRAM WAS WRITTEN") even when most of the
        # run wrote clean — 7 of 8, say.  The banner text now says which
        # case this is; either way it errs loud, because a partial failure
        # is still a reason not to run this sheet until it is resolved.
        total_files = len(report.files)
        if 0 < report.failed_files < total_files:
            banner_text = (
                f"{report.failed_files} OF {total_files} FILES IN THIS RUN FAILED - "
                "DO NOT MACHINE THIS SHEET UNTIL RESOLVED"
            )
        else:
            banner_text = "REFUSED - NO NC PROGRAM WAS WRITTEN FOR THIS SHEET"
        banner_height = 15.0
        page.rect(
            CONTENT_LEFT, y - 3.0, CONTENT_WIDTH, banner_height,
            fill=pdf.hex_color("#fdeaee"), stroke=REFUSED_COLOR, line_width=1.0,
        )
        page.text(
            CONTENT_LEFT + 5.0, y + 1.0,
            pdf.truncate(banner_text, BOLD, 9, CONTENT_WIDTH - 10.0),
            font=BOLD, size=9, color=REFUSED_COLOR,
        )
        y -= banner_height + 2.0
        # One problem line only: the header band is fixed so that the scale
        # drawing starts in the same place on every page.  The rest are in
        # the job's own report; this is the one the operator needs to see.
        first = report.problems[0]
        if len(report.problems) > 1:
            first += f"  (+{len(report.problems) - 1} more)"
        page.text(
            CONTENT_LEFT, y,
            pdf.truncate(first, REGULAR, 7.5, CONTENT_WIDTH),
            size=7.5, color=REFUSED_COLOR,
        )
        y -= 9.0


def _draw_sheet(page, layout, config, ordered, transform) -> None:
    scale, origin_x, origin_y = transform
    sheet_width = float(config.sheet_width)
    sheet_height = float(config.sheet_height)

    page.rect(
        origin_x, origin_y, sheet_width * scale, sheet_height * scale,
        fill=SHEET_FILL, stroke=SHEET_EDGE, line_width=1.2,
    )

    cushion = float(getattr(config, "edge_cushion", 0.0) or 0.0)
    if cushion > 0 and 2 * cushion < min(sheet_width, sheet_height):
        page.rect(
            origin_x + cushion * scale,
            origin_y + cushion * scale,
            (sheet_width - 2 * cushion) * scale,
            (sheet_height - 2 * cushion) * scale,
            stroke=CUSHION_EDGE,
            line_width=0.6,
            dash=CUSHION_DASH,
        )

    margin = float(getattr(config, "front_margin", 0.0) or 0.0)
    if 0.0 < margin < sheet_height:
        page.line(
            origin_x,
            origin_y + margin * scale,
            origin_x + sheet_width * scale,
            origin_y + margin * scale,
            stroke=FRONT_MARGIN_EDGE,
            line_width=0.6,
            dash=FRONT_MARGIN_DASH,
        )

    for placement in layout.placements:
        _draw_part(page, placement, transform, ordered, depth=0)

    # Axis labels, so a printed page can be measured against the machine.
    page.text(
        origin_x + sheet_width * scale / 2.0,
        origin_y - 10.0,
        f'{_dim(sheet_width)}"',
        font=BOLD, size=8, color=MUTED, align="center",
    )
    page.text(
        origin_x - 8.0,
        origin_y + sheet_height * scale / 2.0,
        f'{_dim(sheet_height)}"',
        font=BOLD, size=8, color=MUTED, align="center", rotate=90.0,
    )
    page.text(
        origin_x, origin_y - 20.0,
        "X across the width, Y up the length; origin at the lower-left corner",
        size=6.5, color=MUTED,
    )


def _draw_part(page, placement, transform, ordered, depth: int) -> None:
    scale, origin_x, origin_y = transform
    x = origin_x + placement.x * scale
    y = origin_y + placement.y * scale
    width = placement.width * scale
    height = placement.height * scale

    nested = depth > 0
    is_host = bool(placement.children)
    if nested:
        fill, edge = CHILD_FILL, CHILD_EDGE
    elif is_host:
        fill, edge = HOST_FILL, PART_EDGE
    else:
        fill, edge = PART_FILL, PART_EDGE
    page.rect(x, y, width, height, fill=fill, stroke=edge, line_width=0.8)

    for opening in sheet_openings(placement, ordered):
        page.rect(
            origin_x + opening.x * scale,
            origin_y + opening.y * scale,
            opening.width * scale,
            opening.height * scale,
            fill=OPENING_FILL,
            stroke=OPENING_EDGE,
            line_width=0.4,
        )

    if infer_frame_type(placement.part_number) is FrameType.WDC:
        _draw_wdc_slots(page, placement, transform)

    for child in placement.children:
        _draw_part(page, child, transform, ordered, depth + 1)

    _draw_label(page, x, y, width, height, placement.part_number, nested, is_host)


def _draw_wdc_slots(page, placement, transform) -> None:
    """Dash the two T17 slot centrelines across a WDC placement.

    Each 2" stile carries one straight V slot, centreline
    :data:`WDC_SLOT_CENTRELINE_FROM_OUTER_EDGE` in from that stile's outer
    edge, running the full length of the part along the stile axis.  The
    stiles run along the frame's HEIGHT axis, so
    :func:`~faceframe_cnc.geometry.wdc_slot_axis_is_height` decides whether
    the lines are vertical (upright placement) or horizontal (rotated) —
    the same call the optimizer uses to orient the slot's end clearance,
    so the drawing and the reserved room can never disagree.
    """
    scale, origin_x, origin_y = transform
    inset = WDC_SLOT_CENTRELINE_FROM_OUTER_EDGE
    if wdc_slot_axis_is_height(placement.rotated):
        # Upright: stiles are the left and right members, slots run in Y.
        for local_x in (inset, placement.width - inset):
            x = origin_x + (placement.x + local_x) * scale
            page.line(
                x,
                origin_y + placement.y * scale,
                x,
                origin_y + (placement.y + placement.height) * scale,
                stroke=WDC_SLOT_EDGE,
                line_width=0.8,
                dash=WDC_SLOT_DASH,
            )
        return
    # Rotated 90 degrees CCW: the stile axis lies along sheet X, and the
    # frame-local stile offsets land on the placement's Y extents.
    for local_y in (inset, placement.height - inset):
        y = origin_y + (placement.y + local_y) * scale
        page.line(
            origin_x + placement.x * scale,
            y,
            origin_x + (placement.x + placement.width) * scale,
            y,
            stroke=WDC_SLOT_EDGE,
            line_width=0.8,
            dash=WDC_SLOT_DASH,
        )


def _draw_label(page, x, y, width, height, text, nested: bool, is_host: bool) -> None:
    """Every part carries its part number (spec 5), shrunk to fit if need be.

    A host's label sits in its top-left corner on a white chip, over the
    frame member rather than over whatever is nested in its opening;
    everything else is centred, where the routed opening gives it a white
    background of its own.
    """
    font = REGULAR if nested else BOLD
    size = pdf.fit_size(
        text,
        font,
        max(1.0, width - 4.0),
        start=8.0 if nested else 9.5,
        minimum=4.0,
        step=0.25,
        max_height=max(1.0, height - 2.0),
    )
    if is_host:
        advance = pdf.text_width(text, font, size)
        chip_width = advance + 4.0
        chip_height = size * 1.15 + 2.0
        chip_x = x + 1.5
        chip_y = y + height - 1.5 - chip_height
        page.rect(chip_x, chip_y, chip_width, chip_height, fill=CHIP_FILL)
        page.text_centered_in(
            chip_x, chip_y, chip_width, chip_height, text,
            font=font, size=size, color=LABEL_COLOR,
        )
        return
    page.text_centered_in(
        x, y, width, height, text, font=font, size=size, color=LABEL_COLOR
    )


def _cut_list_row_plan(row, width: float) -> tuple[float, bool, list[str]]:
    """``(vertical space this row needs, is_wdc, its wrapped detail lines)``.

    The wrapped lines are computed once here and reused by the drawing
    code below, so the page-budget check and the actual draw can never
    disagree about how many lines a row takes — the 2026-08-04 review
    finding was exactly that disagreement: the old check assumed ONE
    wrapped detail line where :func:`~faceframe_cnc.report.pdf.wrap_text`
    can hand back two, so a worst-case row could spill past the region
    floor onto the drawing beneath it.
    """
    is_wdc = row.frame_type == FrameType.WDC.value
    detail = f"{row.frame_type} - openings {row.openings}"
    detail_lines = pdf.wrap_text(detail, REGULAR, 7.5, width, max_lines=2)
    needed = (
        10.0  # the title line (part number / count / size)
        + len(detail_lines) * 8.5
        + (8.5 if is_wdc else 0.0)
        + (8.5 if row.hosts else 0.0)
        + 6.0  # the separator rule's spacing, above and below
    )
    return needed, is_wdc, detail_lines


def _draw_cut_list_row(page, x0, width, y, row, is_wdc, detail_lines) -> float:
    page.text(
        x0,
        y,
        pdf.truncate(row.part_number, BOLD, 9.5, _CUTLIST_NUMBER_MAX_WIDTH),
        font=BOLD,
        size=9.5,
    )
    page.text(
        x0 + _CUTLIST_COUNT_OFFSET, y, f"x{row.count}", font=BOLD, size=9.5, color=MUTED
    )
    page.text(
        x0 + width, y, row.size_text, font=BOLD, size=9.5, align="right"
    )
    y -= 10.0
    for line in detail_lines:
        page.text(x0, y, line, size=7.5, color=MUTED)
        y -= 8.5
    if is_wdc:
        # 2026-08-03 owner request: the paperwork says out loud what is
        # special about a WDC frame, in the slot centrelines' colour so
        # the note and the dashed lines on the drawing read as one fact.
        page.text(x0, y, _wdc_cutlist_note(), size=7.5, color=WDC_SLOT_EDGE)
        y -= 8.5
    if row.hosts:
        page.text(x0, y, row.nested_text, size=7.5, color=CHILD_EDGE)
        y -= 8.5
    y -= 3.0
    page.line(x0, y + 2.0, x0 + width, y + 2.0, stroke=pdf.gray(0.88), line_width=0.4)
    y -= 3.0
    return y


def _cut_list_page_head(page, x0, width, top: float, title: str) -> float:
    page.text(x0, top, title, font=BOLD, size=10)
    y = top - 4.0
    page.line(x0, y, x0 + width, y, stroke=RULE, line_width=0.8)
    return y - 12.0


def _draw_cut_list(document, page, report, ordered) -> None:
    """The cut list beside the drawing.

    2026-08-04 review: a sheet with more distinct part numbers than one
    column holds used to stop with "see the cover page contents list" —
    but that list is capped at 3 wrapped lines and never carries a size,
    so an overflowed row's dimensions were nowhere on the paperwork at
    all.  Instead, a row that will not fit starts a real continuation
    page (the same page-flow the cover's own table overflow already
    uses), so every part number this sheet carries is on paper somewhere
    with its size next to it.
    """
    x0, _y0, width, height = CUTLIST_REGION
    top = CUTLIST_REGION[1] + height
    floor = CUTLIST_REGION[1]
    y = _cut_list_page_head(page, x0, width, top, "CUT LIST")

    for row in cut_list(report.layout, ordered):
        needed, is_wdc, detail_lines = _cut_list_row_plan(row, width)
        if y - needed < floor:
            page = document.add_page()
            y = _cut_list_page_head(
                page,
                x0,
                width,
                PAGE_HEIGHT - MARGIN - 12.0,
                f"CUT LIST (continued) - {report.filename}",
            )
        y = _draw_cut_list_row(page, x0, width, y, row, is_wdc, detail_lines)


# --------------------------------------------------------------------------
# Footers (added last, once the page count is known)
# --------------------------------------------------------------------------


def _footers(document, stamp, app_name) -> None:
    total = len(document.pages)
    for number, page in enumerate(document.pages, start=1):
        page.line(
            CONTENT_LEFT, FOOTER_RULE_Y, CONTENT_RIGHT, FOOTER_RULE_Y,
            stroke=RULE, line_width=0.5,
        )
        page.text(CONTENT_LEFT, FOOTER_TEXT_Y, app_name, size=7.5, color=MUTED)
        page.text(
            PAGE_WIDTH / 2.0, FOOTER_TEXT_Y, f"page {number} of {total}",
            size=7.5, color=MUTED, align="center",
        )
        page.text(
            CONTENT_RIGHT, FOOTER_TEXT_Y, f"created {stamp}",
            size=7.5, color=MUTED, align="right",
        )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _dim(value: float) -> str:
    """A dimension in inches, trimmed: ``21``, ``9.875``, ``0.455``.

    ``%g`` (the previous implementation) keeps only 6 SIGNIFICANT digits,
    so a value like 47.03125 — an exact 32nd, the machine's own grid —
    printed as "47.0312": no longer exact, and wrong by more than
    rounding at the last cut digit as magnitude grows.  Rounding to 5
    DECIMAL places instead keeps every 32nd (0.03125) exact regardless of
    how many digits are in front of the point, while still swallowing
    float noise from earlier arithmetic (19.099999999999998 -> "19.1").
    Trailing zeros (and a bare trailing point) are trimmed the same way
    ``%g`` would have.
    """
    text = f"{round(float(value), 5):.5f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-", "-0") else "0"


def _wdc_cutlist_note() -> str:
    """The cut list's one-liner on what makes a WDC frame special.

    Built from the geometry engine's constant so the stile width printed
    here is the one the openings were computed with.
    """
    return (
        f'{WDC_STILE_INSET:g}" stiles - T17 45 deg V-slots both stiles - '
        f"no T13 stile grooves"
    )


def _contents_text(layout) -> str:
    """``"2x3DB24, 3xW3012"`` — the same phrasing the NC banner uses."""
    return ", ".join(
        f"{count}x{name}" for name, count in sorted(layout.part_counts().items())
    )


def _placed_dims(layout, part_number: str) -> tuple[float, float]:
    """As-ordered dimensions read back off the sheet, for a part the demand
    list somehow does not carry (the job's own validator would normally have
    refused such a layout long before the report was asked for)."""

    def walk(placements):
        for placement in placements:
            if placement.part_number == part_number:
                if placement.rotated:
                    return placement.height, placement.width
                return placement.width, placement.height
            found = walk(placement.children)
            if found is not None:
                return found
        return None

    return walk(layout.placements) or (0.0, 0.0)
