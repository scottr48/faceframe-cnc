"""Milestone 6: the printable cut-sheet report.

There is no PDF library here to lean on and none is wanted, so the tests
read the app's own output back with a small parser written from the PDF 1.4
specification (:class:`Pdf` below) and check the things a broken PDF gets
wrong: the header, cross-reference offsets that really do point at their
objects, a page count that matches the page tree, ``startxref`` and
``%%EOF``.

Above that, the checks are about the paperwork rather than the bytes:

  (a) the file is structurally a PDF and the same inputs give byte-identical
      bytes -- a report that churns is a report nobody can diff;
  (b) every part number on a sheet is on that sheet's page, together with
      the file name and the run quantity;
  (c) the drawing is to scale, with ONE scale for both axes, and the routed
      openings land inside their parts;
  (d) a refused sheet still gets a page, and it says REFUSED;
  (e) a dry-run job says so on every page it can;
  (f) the cover's run quantities add up to the optimizer's sheet count, and
      the report prints that sum;
  (g) the text metrics are real: centring depends on them.

Stdlib only.  Run with: python -m unittest discover tests
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from faceframe_cnc.nesting import (
    NestingConfig,
    NestingResult,
    PartSpec,
    Placement,
    SheetLayout,
)
from faceframe_cnc.report import cutsheet, pdf
from faceframe_cnc.report.cutsheet import ReportError
from tests.test_nc_job import (
    CREATED,
    crowded_sheet,
    job_for,
    nested_order,
    nested_sample,
)


# --------------------------------------------------------------------------
# A deliberately small PDF reader, so nothing but the app writes PDFs here
# --------------------------------------------------------------------------

_TJ = re.compile(rb"\((?:[^()\\]|\\.)*\)\s*Tj")
_RECT = re.compile(rb"(-?\d+\.\d+) (-?\d+\.\d+) (-?\d+\.\d+) (-?\d+\.\d+) re")


class Pdf:
    """Just enough of a PDF reader to hold the writer to its own claims."""

    def __init__(self, data: bytes):
        self.data = data
        self.problems: list[str] = []
        self.offsets: list[int] = []
        self.trailer = b""
        self.page_count = 0
        self.streams: list[bytes] = []
        self._parse()

    # -- parsing ---------------------------------------------------------

    def _parse(self) -> None:
        data = self.data
        if not data.startswith(b"%PDF-1.4\n"):
            self.problems.append("no %PDF-1.4 header")
        if not data.endswith(b"%%EOF\n"):
            self.problems.append("no trailing %%EOF")

        tail = re.search(rb"startxref\n(\d+)\n%%EOF\n$", data)
        if tail is None:
            self.problems.append("no startxref")
            return
        start = int(tail.group(1))
        if not data[start:].startswith(b"xref\n"):
            self.problems.append(f"startxref {start} does not point at an xref table")
            return

        head = re.match(rb"xref\n0 (\d+)\n", data[start:])
        if head is None:
            self.problems.append("malformed xref subsection header")
            return
        size = int(head.group(1))
        body = data[start + head.end() :]
        for number in range(size):
            entry = body[number * 20 : (number + 1) * 20]
            if len(entry) != 20 or entry[18:20] != b" \n":
                self.problems.append(f"xref entry {number} is not 20 bytes")
                return
            self.offsets.append(int(entry[:10]))
        if self.offsets[0] != 0 or body[17:18] != b"f":
            self.problems.append("object 0 is not the free-list head")
        for number in range(1, size):
            offset = self.offsets[number]
            expected = f"{number} 0 obj".encode()
            if not data[offset : offset + len(expected)] == expected:
                self.problems.append(
                    f"xref offset {offset} for object {number} points at "
                    f"{data[offset:offset + 16]!r}"
                )

        trailer = re.search(rb"trailer\n(<<.*?>>)\n", data, re.S)
        if trailer is None:
            self.problems.append("no trailer")
        else:
            self.trailer = trailer.group(1)
            declared = re.search(rb"/Size (\d+)", self.trailer)
            if declared is None or int(declared.group(1)) != size:
                self.problems.append("the trailer /Size disagrees with the xref table")

        count = re.search(rb"/Type /Pages /Kids \[(.*?)\] /Count (\d+)", data)
        if count is None:
            self.problems.append("no page tree")
        else:
            self.page_count = int(count.group(2))
            kids = len(re.findall(rb"\d+ 0 R", count.group(1)))
            if kids != self.page_count:
                self.problems.append(f"/Count {self.page_count} but {kids} kids")

        for match in re.finditer(rb"<< /Length (\d+) >>\nstream\n", data):
            length = int(match.group(1))
            body = data[match.end() : match.end() + length]
            if data[match.end() + length : match.end() + length + 9] != b"endstream":
                self.problems.append("a stream /Length does not reach its endstream")
            self.streams.append(body)

    # -- content ---------------------------------------------------------

    def page(self, index: int) -> bytes:
        return self.streams[index]

    def texts(self, index: int | None = None) -> list[str]:
        """Every string drawn with ``Tj``, unescaped, in order."""
        source = self.streams if index is None else [self.streams[index]]
        out: list[str] = []
        for stream in source:
            for match in _TJ.findall(stream):
                body = match[1 : match.rindex(b")")].decode("latin-1")
                out.append(
                    body.replace(r"\(", "(").replace(r"\)", ")").replace("\\\\", "\\")
                )
        return out

    def text(self, index: int | None = None) -> str:
        return "\n".join(self.texts(index))

    def rects(self, index: int) -> list[tuple[float, float, float, float]]:
        return [
            tuple(float(value) for value in match)
            for match in _RECT.findall(self.streams[index])
        ]


def report_for(result, job, created: str = CREATED) -> Pdf:
    return Pdf(cutsheet.build_report(result, job, created=created))


# --------------------------------------------------------------------------
# (a) the bytes are a PDF, and always the same PDF
# --------------------------------------------------------------------------


class PdfWriterTest(unittest.TestCase):
    """The generic writer, exercised without any sheets in sight."""

    def document(self):
        document = pdf.Document(title="Structure (test)", creator="unit test")
        first = document.add_page()
        first.rect(10, 10, 100, 50, fill=pdf.gray(0.9), stroke=pdf.BLACK)
        first.line(10, 70, 110, 70, stroke=pdf.gray(0.5), dash=(2.0, 2.0))
        first.text(20, 100, "hello (world) \\ backslash", size=12)
        second = document.add_page()
        second.text(306, 400, "centred", align="center", font=pdf.HELVETICA_BOLD)
        second.text(306, 300, "turned", rotate=90.0)
        return document

    def test_the_file_is_structurally_valid(self):
        parsed = Pdf(self.document().to_bytes())
        self.assertEqual(parsed.problems, [])
        self.assertEqual(parsed.page_count, 2)
        self.assertEqual(len(parsed.streams), 2)

    def test_every_xref_offset_points_at_its_object(self):
        """Checked inside :class:`Pdf`; asserted here so the intent is
        visible, and re-checked by hand for object 1."""
        data = self.document().to_bytes()
        parsed = Pdf(data)
        self.assertEqual(parsed.problems, [])
        self.assertGreater(len(parsed.offsets), 5)
        self.assertTrue(data[parsed.offsets[1] :].startswith(b"1 0 obj"))
        self.assertIn(b"/Root 1 0 R", parsed.trailer)

    def test_startxref_is_the_byte_offset_of_the_table(self):
        data = self.document().to_bytes()
        offset = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", data).group(1))
        self.assertTrue(data[offset:].startswith(b"xref\n0 "))

    def test_parenthesis_and_backslash_are_escaped(self):
        data = self.document().to_bytes()
        self.assertIn(rb"(hello \(world\) \\ backslash) Tj", data)
        self.assertEqual(
            Pdf(data).texts(0), ["hello (world) \\ backslash"]
        )

    def test_an_empty_document_is_refused(self):
        with self.assertRaises(ValueError):
            pdf.Document().to_bytes()

    def test_coordinates_are_written_at_a_fixed_precision(self):
        page = pdf.Page(100, 100)
        page.rect(1 / 3, 2 / 3, 10, 10, fill=pdf.BLACK)
        page.rect(-0.001, 0, 1, 1, fill=pdf.BLACK)
        content = page.content().decode("latin-1")
        self.assertIn("0.33 0.67 10.00 10.00 re", content)
        self.assertIn("0.00 0.00 1.00 1.00 re", content)
        self.assertNotIn("-0.00", content)

    def test_unwritable_characters_are_folded_or_flagged(self):
        self.assertEqual(pdf.sanitize("a — b"), "a - b")
        self.assertEqual(pdf.sanitize("24 × 36"), "24 x 36")
        self.assertEqual(pdf.sanitize("line\nbreak"), "line break")
        self.assertEqual(pdf.sanitize("中"), "?")

    def test_the_metrics_are_the_real_helvetica_ones(self):
        """Centring is only as good as these numbers."""
        self.assertAlmostEqual(pdf.text_width("i", pdf.HELVETICA, 1000.0), 222.0)
        self.assertAlmostEqual(pdf.text_width("W", pdf.HELVETICA, 1000.0), 944.0)
        self.assertAlmostEqual(pdf.text_width(" ", pdf.HELVETICA, 1000.0), 278.0)
        self.assertAlmostEqual(pdf.text_width("0", pdf.HELVETICA_BOLD, 1000.0), 556.0)
        self.assertAlmostEqual(pdf.text_width("W", pdf.HELVETICA_BOLD, 1000.0), 944.0)
        # ... and they scale linearly with the point size
        self.assertAlmostEqual(
            pdf.text_width("R720101N.anc", pdf.HELVETICA, 20.0),
            2 * pdf.text_width("R720101N.anc", pdf.HELVETICA, 10.0),
        )

    def test_centred_text_is_moved_left_by_half_its_width(self):
        page = pdf.Page(200, 200)
        page.text(100, 50, "abc", size=10, align="center")
        half = pdf.text_width("abc", pdf.HELVETICA, 10.0) / 2.0
        self.assertIn(f"{100 - half:.2f} 50.00 Tm", page.content().decode("latin-1"))

    def test_a_label_shrinks_rather_than_overflows(self):
        big = pdf.fit_size("3DB24", pdf.HELVETICA_BOLD, 200.0, start=9.5)
        small = pdf.fit_size("3DB24", pdf.HELVETICA_BOLD, 12.0, start=9.5, minimum=2.0)
        self.assertEqual(big, 9.5)
        self.assertLess(small, 9.5)
        self.assertLessEqual(pdf.text_width("3DB24", pdf.HELVETICA_BOLD, small), 12.0)

    def test_shrinking_stops_at_the_floor_rather_than_vanishing(self):
        """A part too small to hold its own number still gets a number: an
        unlabelled rectangle on the paperwork is worse than a tight one."""
        size = pdf.fit_size("3DB24", pdf.HELVETICA_BOLD, 0.5, start=9.5, minimum=4.0)
        self.assertEqual(size, 4.0)

    def test_the_height_of_a_box_can_cap_the_size_too(self):
        self.assertLess(
            pdf.fit_size("W", pdf.HELVETICA, 500.0, start=10.0, max_height=6.0),
            10.0,
        )

    def test_wrapping_respects_the_column_and_the_line_budget(self):
        text = "2x3DB24, 3xW3012, 1xW2742, 4xB18, 2xWDC2436"
        lines = pdf.wrap_text(text, pdf.HELVETICA, 8.0, 90.0)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(pdf.text_width(line, pdf.HELVETICA, 8.0), 90.0)
        capped = pdf.wrap_text(text, pdf.HELVETICA, 8.0, 90.0, max_lines=2)
        self.assertEqual(len(capped), 2)
        self.assertTrue(capped[-1].endswith("..."))

    def test_text_centered_in_uses_the_correct_glyph_band(self):
        """2026-08-04 review, fix 6: the glyph band a baseline anchors is
        ``(ASCENT + DESCENT) * size`` tall (ASCENT above the baseline,
        DESCENT below it) -- not ``(ASCENT - DESCENT) * size``, which is
        what the code used to subtract, riding every centred label about
        ``DESCENT * size`` too high."""
        page = pdf.Page(200, 100)
        page.text_centered_in(0.0, 0.0, 200.0, 100.0, "x", size=20.0)
        content = page.content().decode("latin-1")
        match = re.search(r"(-?\d+\.\d{2}(?!\d)) (-?\d+\.\d{2}(?!\d)) Tm", content)
        self.assertIsNotNone(match, content)
        baseline = float(match.group(2))
        correct = (100.0 - (pdf.ASCENT + pdf.DESCENT) * 20.0) / 2.0 + pdf.DESCENT * 20.0
        buggy = (100.0 - (pdf.ASCENT - pdf.DESCENT) * 20.0) / 2.0 + pdf.DESCENT * 20.0
        self.assertAlmostEqual(baseline, correct, places=2)
        self.assertGreater(abs(baseline - buggy), 1.0)


class DeterminismTest(unittest.TestCase):
    def test_two_builds_with_the_same_stamp_are_byte_identical(self):
        result, _config = nested_sample()
        job = job_for(result)
        first = cutsheet.build_report(result, job, created=CREATED)
        second = cutsheet.build_report(result, job, created=CREATED)
        self.assertEqual(first, second)

    def test_only_the_stamp_moves_when_the_clock_does(self):
        result, _config = nested_sample()
        job = job_for(result)
        morning = cutsheet.build_report(result, job, created="01 JAN 27 - 08:00")
        evening = cutsheet.build_report(result, job, created="02 FEB 28 - 19:45")
        self.assertNotEqual(morning, evening)
        self.assertIn("created 02 FEB 28 - 19:45", Pdf(evening).text())
        self.assertNotIn("01 JAN 27", Pdf(evening).text())

    def test_the_real_order_composes_one_page_per_unique_sheet(self):
        result, _config = nested_order(0.455)
        job = job_for(result)
        parsed = report_for(result, job)
        self.assertEqual(parsed.problems, [])
        self.assertEqual(
            parsed.page_count, result.unique_sheet_count + 1, "cover plus sheets"
        )


# --------------------------------------------------------------------------
# (b) what the pages say
# --------------------------------------------------------------------------


def known_sheet():
    """A 3DB24 plus a W3012 nested inside a W2742, on a run of three.

    Coordinates a test can do arithmetic on, a nested frame, a three-opening
    frame and a run quantity that is not 1 — everything the sheet page has
    to get right, on one picture.
    """
    config = NestingConfig(inside_nesting=True, part_gap=0.455)
    layout = SheetLayout(
        [
            Placement(
                "W2742",
                0.5,
                1.0,
                27.0,
                42.0,
                children=[Placement("W3012", 8.0, 7.0, 12.0, 30.0, rotated=True)],
            ),
            Placement("3DB24", 0.5, 44.0, 24.0, 30.0),
        ]
    )
    demand = [
        PartSpec("W2742", 27.0, 42.0, 3),
        PartSpec("W3012", 30.0, 12.0, 3),
        PartSpec("3DB24", 24.0, 30.0, 3),
    ]
    result = NestingResult(
        unique_sheets=[(layout, 3)], total_sheets=3, demand=demand, config=config
    )
    return result, config


class SheetPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result, cls.config = known_sheet()
        cls.job = job_for(cls.result, prefix="7201")
        assert cls.job.outcomes[0].ok, cls.job.outcomes[0].describe()
        cls.pdf = report_for(cls.result, cls.job)
        cls.page = 1  # 0 is the cover

    def test_the_document_is_valid(self):
        self.assertEqual(self.pdf.problems, [])
        self.assertEqual(self.pdf.page_count, 2)

    def test_every_part_number_is_on_the_page(self):
        texts = self.pdf.texts(self.page)
        for part in ("W2742", "W3012", "3DB24"):
            with self.subTest(part=part):
                self.assertIn(part, texts, "the drawing must label every part")

    def test_the_run_quantity_is_prominent(self):
        texts = self.pdf.texts(self.page)
        self.assertIn("RUN QTY", texts)
        self.assertIn("3", texts)
        content = self.pdf.page(self.page).decode("latin-1")
        big = re.findall(r"/F2 (\d+\.\d+) Tf [^\n]*?\(3\) Tj", content)
        self.assertTrue(big, "the run quantity must be set large and bold")
        self.assertGreaterEqual(
            max(float(size) for size in big),
            20.0,
            "the operator must not be able to miss how many to cut",
        )

    def test_the_file_name_and_sheet_position_are_in_the_header(self):
        texts = self.pdf.texts(self.page)
        self.assertIn("R720101N.anc", texts)
        self.assertIn("SHEET 1 OF 1", texts)

    def test_the_contents_summary_is_the_banner_wording(self):
        self.assertIn("1x3DB24, 1xW2742, 1xW3012", self.pdf.text(self.page))

    def test_the_cut_list_carries_sizes_types_and_openings(self):
        text = self.pdf.text(self.page)
        self.assertIn("CUT LIST", text)
        self.assertIn("27 x 42", text)
        self.assertIn("24 x 30", text)
        self.assertIn("three_drawer", text)
        # 3DB24's three drawer openings (spec section 3)
        self.assertIn("21 x 5, 21 x 9.875, 21 x 9.125", text)

    def test_a_nested_frame_says_which_host_it_comes_out_of(self):
        self.assertIn("nested in W2742", self.pdf.text(self.page))

    def test_the_page_says_the_parts_are_tab_held(self):
        """2026-08-05 amendment (Scott, job R0805, spec §3d).

        The operator has to know that this sheet does NOT come apart as it is
        cut: two frames broke because one did.  A program that looks finished but
        has not run its last section yet has every part still attached, and the
        numbers in the note are the post table's own.
        """
        from faceframe_cnc.post.model import default_config

        text = self.pdf.text(self.page)
        self.assertIn("TAB-HELD", text)
        self.assertIn(f'{default_config().tabs.top_z:g}" tabs', text)
        self.assertIn("T12 release pass", text)
        self.assertIn("Nothing is loose until that last section has run", text)

    def test_the_scale_is_stated(self):
        scale, _ox, _oy = cutsheet.sheet_transform(self.config)
        self.assertIn(f"scale 1:{72.0 / scale:.1f}", self.pdf.text(self.page))

    def test_the_sheet_dimensions_label_the_axes(self):
        texts = self.pdf.texts(self.page)
        self.assertIn('49"', texts)
        self.assertIn('97"', texts)

    # -- (c) the drawing ------------------------------------------------

    def test_the_drawing_is_to_scale_and_the_scale_is_uniform(self):
        scale, origin_x, origin_y = cutsheet.sheet_transform(self.config)
        rects = self.pdf.rects(self.page)

        sheet = (
            origin_x,
            origin_y,
            self.config.sheet_width * scale,
            self.config.sheet_height * scale,
        )
        self.assertTrue(
            any(_close(rect, sheet) for rect in rects),
            f"no sheet outline at {sheet}, got {rects[:4]}",
        )
        # x and y really are the same scale
        drawn = next(rect for rect in rects if _close(rect, sheet))
        self.assertAlmostEqual(
            drawn[2] / self.config.sheet_width,
            drawn[3] / self.config.sheet_height,
            places=3,
        )

        for placement in (
            self.result.unique_sheets[0][0].placements[0],
            self.result.unique_sheets[0][0].placements[1],
        ):
            want = (
                origin_x + placement.x * scale,
                origin_y + placement.y * scale,
                placement.width * scale,
                placement.height * scale,
            )
            with self.subTest(part=placement.part_number):
                self.assertTrue(
                    any(_close(rect, want) for rect in rects),
                    f"{placement.part_number} should be drawn at {want}",
                )

    def test_a_nested_child_is_drawn_inside_its_host(self):
        scale, origin_x, origin_y = cutsheet.sheet_transform(self.config)
        host = self.result.unique_sheets[0][0].placements[0]
        child = host.children[0]
        want = (
            origin_x + child.x * scale,
            origin_y + child.y * scale,
            child.width * scale,
            child.height * scale,
        )
        rects = self.pdf.rects(self.page)
        self.assertTrue(any(_close(rect, want) for rect in rects))
        host_rect = (
            origin_x + host.x * scale,
            origin_y + host.y * scale,
            host.width * scale,
            host.height * scale,
        )
        self.assertTrue(_inside(want, host_rect))

    def test_a_frames_openings_are_drawn_inside_it(self):
        from faceframe_cnc.gui.session import sheet_openings

        scale, origin_x, origin_y = cutsheet.sheet_transform(self.config)
        rects = self.pdf.rects(self.page)
        part = self.result.unique_sheets[0][0].placements[1]  # 3DB24
        ordered = {spec.part_number: spec for spec in self.result.demand}
        openings = sheet_openings(part, ordered)
        self.assertEqual(len(openings), 3, "3DB24 has three drawer openings")
        part_rect = (
            origin_x + part.x * scale,
            origin_y + part.y * scale,
            part.width * scale,
            part.height * scale,
        )
        for opening in openings:
            want = (
                origin_x + opening.x * scale,
                origin_y + opening.y * scale,
                opening.width * scale,
                opening.height * scale,
            )
            with self.subTest(opening=opening.label):
                self.assertTrue(
                    any(_close(rect, want) for rect in rects),
                    f"no opening rect at {want}",
                )
                self.assertTrue(_inside(want, part_rect))

    def test_the_drawing_stays_inside_its_region(self):
        x0, y0, width, height = cutsheet.DRAWING_REGION
        scale, origin_x, origin_y = cutsheet.sheet_transform(self.config)
        self.assertGreaterEqual(origin_x, x0 - 1e-9)
        self.assertGreaterEqual(origin_y, y0 - 1e-9)
        self.assertLessEqual(
            origin_x + self.config.sheet_width * scale, x0 + width + 1e-9
        )
        self.assertLessEqual(
            origin_y + self.config.sheet_height * scale, y0 + height + 1e-9
        )


def _close(rect, want, tolerance: float = 0.01) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(rect, want))


def _inside(inner, outer, tolerance: float = 0.05) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[0] + inner[2] <= outer[0] + outer[2] + tolerance
        and inner[1] + inner[3] <= outer[1] + outer[3] + tolerance
    )


# --------------------------------------------------------------------------
# The T17 WDC slot centrelines on paper (2026-08-03 owner request)
# --------------------------------------------------------------------------

#: A line stroked with the WDC slot pen: its dash pattern is unique on the
#: page (cushion is [3.00 2.50], front margin [1.00 2.00]), so it is the
#: signature by which slot centrelines are pulled back out of the stream.
_SLOT_LINE = re.compile(
    rb"\[4\.00 2\.00\] 0 d (-?\d+\.\d+) (-?\d+\.\d+) m (-?\d+\.\d+) (-?\d+\.\d+) l S"
)


def _slot_lines(parsed: Pdf, index: int) -> list[tuple[float, float, float, float]]:
    """``(x0, y0, x1, y1)`` of every slot centreline drawn on a page."""
    return [
        tuple(float(value) for value in match)
        for match in _SLOT_LINE.findall(parsed.streams[index])
    ]


def wdc_sheet():
    """One upright and one rotated WDC2436, far enough from everything.

    Both stile-end reaches (0.875 beyond the height axis before rotation)
    clear the sheet edges and each other, so the NC job accepts the sheet
    and the report page under test is a WRITTEN one, not a refusal.
    """
    config = NestingConfig(part_gap=0.455)
    layout = SheetLayout(
        [
            Placement("WDC2436", 5.0, 3.0, 18.0, 36.0),
            Placement("WDC2436", 5.0, 60.0, 36.0, 18.0, rotated=True),
        ]
    )
    demand = [PartSpec("WDC2436", 18.0, 36.0, 2)]
    result = NestingResult(
        unique_sheets=[(layout, 1)], total_sheets=1, demand=demand, config=config
    )
    return result, config


class WdcSlotDrawingTest(unittest.TestCase):
    """The owner must be able to SEE the T17 routing on the paperwork.

    Every expected coordinate below is recomputed from the geometry
    engine's constants — the same numbers the optimizer reserves room with
    — never from the drawing code's own value, so the test still means
    something if someone edits the drawing.
    """

    @classmethod
    def setUpClass(cls):
        from faceframe_cnc.geometry import (
            WDC_SLOT_INSET_FROM_INSIDE_EDGE,
            WDC_STILE_INSET,
        )

        cls.result, cls.config = wdc_sheet()
        cls.job = job_for(cls.result, prefix="88")
        assert cls.job.outcomes[0].ok, cls.job.outcomes[0].describe()
        cls.pdf = report_for(cls.result, cls.job)
        cls.page = 1  # 0 is the cover
        cls.inset = WDC_STILE_INSET - WDC_SLOT_INSET_FROM_INSIDE_EDGE

    def test_the_drawn_centreline_is_the_posts_centreline(self):
        # The drawing derives its offset from geometry.py; the post derives
        # its cut from its own measured table.  This is the pin that stops
        # the paper and the machine drifting apart.
        from faceframe_cnc.post.model import default_config

        self.assertAlmostEqual(
            cutsheet.WDC_SLOT_CENTRELINE_FROM_OUTER_EDGE,
            default_config().wdc_slot.inset_from_outside_edge,
        )
        self.assertAlmostEqual(cutsheet.WDC_SLOT_CENTRELINE_FROM_OUTER_EDGE, self.inset)

    def test_each_wdc_gets_exactly_two_slot_centrelines(self):
        lines = _slot_lines(self.pdf, self.page)
        self.assertEqual(len(lines), 4, "two WDC parts, two stiles each")

    def test_the_upright_wdc_slots_are_vertical_full_length_at_the_stiles(self):
        scale, ox, oy = cutsheet.sheet_transform(self.config)
        part = self.result.unique_sheets[0][0].placements[0]
        lines = _slot_lines(self.pdf, self.page)
        for local_x in (self.inset, part.width - self.inset):
            want = (
                ox + (part.x + local_x) * scale,
                oy + part.y * scale,
                ox + (part.x + local_x) * scale,
                oy + (part.y + part.height) * scale,
            )
            with self.subTest(local_x=local_x):
                self.assertTrue(
                    any(_close(line, want) for line in lines),
                    f"no vertical slot centreline at {want}; got {lines}",
                )

    def test_the_rotated_wdc_slots_turn_with_the_part(self):
        # 90 degrees CCW puts the stile axis along sheet X, so the two
        # centrelines become horizontal, offset from the placement's bottom
        # and top edges by the same stile inset.
        scale, ox, oy = cutsheet.sheet_transform(self.config)
        part = self.result.unique_sheets[0][0].placements[1]
        lines = _slot_lines(self.pdf, self.page)
        for local_y in (self.inset, part.height - self.inset):
            want = (
                ox + part.x * scale,
                oy + (part.y + local_y) * scale,
                ox + (part.x + part.width) * scale,
                oy + (part.y + local_y) * scale,
            )
            with self.subTest(local_y=local_y):
                self.assertTrue(
                    any(_close(line, want) for line in lines),
                    f"no horizontal slot centreline at {want}; got {lines}",
                )

    def test_the_cut_list_says_what_the_lines_mean(self):
        text = self.pdf.text(self.page)
        self.assertIn("T17 45 deg V-slots both stiles", text)
        self.assertIn('2" stiles', text)
        self.assertIn("no T13 stile grooves", text)

    def test_non_wdc_parts_get_no_slot_lines_and_no_note(self):
        result, _config = known_sheet()
        parsed = report_for(result, job_for(result, prefix="7201"))
        self.assertEqual(_slot_lines(parsed, 1), [])
        self.assertNotIn("T17", parsed.text(1))


# --------------------------------------------------------------------------
# (d) + (e) refusals and dry runs
# --------------------------------------------------------------------------


class RefusalTest(unittest.TestCase):
    def test_a_refused_sheet_still_gets_a_page_and_says_so(self):
        result, _config = crowded_sheet()
        job = job_for(result, prefix="99")
        self.assertTrue(job.refused, "this fixture exists to be refused")
        parsed = report_for(result, job)
        self.assertEqual(parsed.problems, [])
        self.assertEqual(parsed.page_count, 2, "the gap is drawn, not hidden")

        page = parsed.text(1)
        self.assertIn("REFUSED", page)
        self.assertIn("NO NC PROGRAM WAS WRITTEN", page)
        self.assertIn("foreign-cut", page)
        for part in ("W2036", "W2436"):
            self.assertIn(part, parsed.texts(1))

        cover = parsed.text(0)
        self.assertIn("REFUSED", cover)
        self.assertIn("R9901N.anc", cover)

    def test_a_clean_job_says_nothing_about_refusals(self):
        result, _config = nested_sample()
        parsed = report_for(result, job_for(result))
        self.assertNotIn("REFUSED", parsed.text())


class PartialFailureTest(unittest.TestCase):
    """2026-08-04 review, fix 5: in one-file-per-physical-sheet mode, one
    bad file among a picture's run used to mark the WHOLE picture "REFUSED
    - NO NC PROGRAM WAS WRITTEN FOR THIS SHEET", which can be false (7 of
    8 written).  A write failure is a fact about ONE file at a time --
    real verification judges every (identical) copy of a repeated
    picture the same way, so build_job alone can never produce this
    scenario; it is the LATER, per-file disk write that can fail for just
    one of several copies (exactly how :func:`~faceframe_cnc.post.job.write_job`
    behaves).  A duck-typed job stands in for that, so this test is a
    fact about the REPORT's wording, not a re-test of the post pipeline's
    own verifier.
    """

    def test_a_partial_run_failure_says_how_many_failed_not_that_none_wrote(self):
        result, _config = known_sheet()  # one picture, run = 3
        job = _fake_job(result, per_physical_sheet=True, prefix="41")
        self.assertEqual(len(job.outcomes), 3)

        job.outcomes[1].problems = ["disk write failed: simulated for the test"]
        job.outcomes[1].ok = False

        reports = cutsheet.sheet_reports(result, job)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertTrue(report.refused)
        self.assertEqual(report.failed_files, 1)
        self.assertEqual(len(report.files), 3)

        page = report_for(result, job).text(1)
        self.assertIn("1 OF 3 FILES IN THIS RUN FAILED", page)
        self.assertIn("DO NOT MACHINE THIS SHEET UNTIL RESOLVED", page)
        self.assertNotIn("NO NC PROGRAM WAS WRITTEN", page)

    def test_a_total_failure_still_says_no_program_was_written(self):
        """The old wording is still correct, and still used, when it is
        actually true -- every file in the run failed."""
        result, _config = known_sheet()
        job = _fake_job(result, per_physical_sheet=True, prefix="41", refused=True)
        report = cutsheet.sheet_reports(result, job)[0]
        self.assertEqual(report.failed_files, len(report.files))
        page = report_for(result, job).text(1)
        self.assertIn("REFUSED - NO NC PROGRAM WAS WRITTEN FOR THIS SHEET", page)
        self.assertNotIn("FILES IN THIS RUN FAILED", page)


class ContentsCrossCheckTest(unittest.TestCase):
    """2026-08-04 review, fix 8: pictures and outcomes are paired by count
    and POSITION only.  A stale job re-paired against a freshly
    re-optimized layout of the same length would mispair silently,
    putting one sheet's filenames over another sheet's drawing.
    :attr:`~faceframe_cnc.post.job.SheetOutcome.contents` carries an
    independent part-count manifest, which is now cross-checked."""

    def test_a_mismatched_outcome_is_refused_rather_than_silently_paired(self):
        result, _config = known_sheet()
        job = _fake_job(result, prefix="7201")
        job.outcomes[0].contents = {"SOMETHING_ELSE": 9}
        with self.assertRaises(ReportError) as caught:
            cutsheet.sheet_reports(result, job)
        self.assertIn("drifted apart", str(caught.exception))

    def test_a_matching_outcome_is_unaffected(self):
        result, _config = known_sheet()
        job = _fake_job(result, prefix="7201")
        reports = cutsheet.sheet_reports(result, job)
        self.assertEqual(len(reports), 1)
        self.assertFalse(reports[0].refused)


class RefusedSummaryWrapTest(unittest.TestCase):
    """2026-08-04 review, fix 3: the cover's refused-sheet summary was one
    unwrapped ``", ".join`` of filenames that ran off the page for ~5+
    refusals.  It is now capped at a sane number of names, with an
    "and N more" tail, and the whole thing is wrapped."""

    def test_a_handful_of_refusals_are_all_named(self):
        refused = [SimpleNamespace(filename=f"R990{n}N.anc") for n in range(1, 4)]
        text = cutsheet._refused_summary_text(refused)
        for report in refused:
            self.assertIn(report.filename, text)
        self.assertNotIn("more", text)

    def test_five_or_more_refusals_are_capped_with_an_and_n_more_tail(self):
        refused = [SimpleNamespace(filename=f"R99{n:02d}N.anc") for n in range(1, 9)]
        text = cutsheet._refused_summary_text(refused)
        shown, hidden = refused[: cutsheet._REFUSED_NAMES_SHOWN], refused[cutsheet._REFUSED_NAMES_SHOWN :]
        self.assertTrue(hidden, "the fixture must have more names than the cap")
        for report in shown:
            self.assertIn(report.filename, text)
        for report in hidden:
            self.assertNotIn(report.filename, text)
        self.assertIn(f"and {len(hidden)} more", text)

    def test_the_summary_is_wrapped_rather_than_running_off_the_page(self):
        for line in pdf.wrap_text(
            cutsheet._refused_summary_text(
                [SimpleNamespace(filename=f"R99{n:02d}N.anc") for n in range(1, 9)]
            ),
            cutsheet.BOLD,
            9,
            cutsheet.CONTENT_WIDTH,
            max_lines=cutsheet._REFUSED_SUMMARY_MAX_LINES,
        ):
            self.assertLessEqual(pdf.text_width(line, cutsheet.BOLD, 9), cutsheet.CONTENT_WIDTH)

    def test_a_real_cover_with_many_refusals_wraps_onto_more_than_one_line(self):
        result, _config = many_refused_pictures(8)
        job = _fake_job(result, prefix="99", refused=True)
        parsed = report_for(result, job)
        cover = parsed.text(0)
        self.assertIn("and 2 more", cover)
        # Every sheet's filename legitimately appears once already, in the
        # per-sheet TABLE above the summary line -- what must be capped is
        # the SUMMARY sentence specifically, so the check is scoped to the
        # text that follows it, not the whole cover.
        marker = "have no NC program:"
        self.assertIn(marker, cover)
        summary = cover[cover.index(marker) + len(marker) :]
        for name in [o.filename for o in job.outcomes[:6]]:
            self.assertIn(name, summary)
        for name in [o.filename for o in job.outcomes[6:]]:
            self.assertNotIn(name, summary)


def many_refused_pictures(count: int) -> tuple[NestingResult, NestingConfig]:
    """``count`` distinct, entirely refused unique pictures -- a fixture
    for the cover's refused-list wrapping, cheap enough not to need a
    real NC generation pass (every sheet here is refused by fiat)."""
    config = NestingConfig(part_gap=0.455)
    unique_sheets = []
    demand = []
    for index in range(count):
        name = f"W20{index:02d}"
        unique_sheets.append((SheetLayout([Placement(name, 1.0, 1.0, 20.0, 36.0)]), 1))
        demand.append(PartSpec(name, 20.0, 36.0, 1))
    return (
        NestingResult(
            unique_sheets=unique_sheets, total_sheets=count, demand=demand, config=config
        ),
        config,
    )


def _fake_job(
    result,
    *,
    prefix: str = "1",
    dry_run: bool = False,
    refused: bool = False,
    per_physical_sheet: bool = False,
):
    """A minimal duck-typed job, decoupled from the real NC post pipeline.

    Used to exercise the REPORT layer's own pagination and text logic in
    isolation -- for fixtures (many distinct part numbers, deliberately
    unrealistic dimensions, a picture repeated many times) that have
    nothing to do with whether a real program would verify, and so a real
    :func:`~faceframe_cnc.post.job.build_job` pass would only be slow and
    fragile noise.  Every field the report code actually reads is
    present; nothing else is.  ``contents`` always matches its picture,
    so :func:`cutsheet.sheet_reports`'s own cross-check (fix 8) never
    rejects this fixture by accident.  Outcomes are :class:`SimpleNamespace`,
    so a test can mutate one in place to simulate a specific failure.
    """
    outcomes = []
    index = 1
    for layout, run in result.unique_sheets:
        copies = int(run) if per_physical_sheet else 1
        for _ in range(copies):
            outcomes.append(
                SimpleNamespace(
                    filename=f"R{prefix}{index:02d}N.anc",
                    problems=["simulated refusal for the test"] if refused else [],
                    contents=dict(layout.part_counts()),
                    ok=not refused,
                )
            )
            index += 1
    options = SimpleNamespace(
        per_physical_sheet=per_physical_sheet, app_name="Faceframe Optimizer", prefix=prefix
    )
    return SimpleNamespace(
        outcomes=outcomes, options=options, dry_run=dry_run, output_dir="unused"
    )


class DryRunTest(unittest.TestCase):
    def test_every_page_of_a_dry_run_is_marked(self):
        result, _config = nested_sample()
        job = job_for(result, dry_run=True)
        parsed = report_for(result, job)
        self.assertIn("DRY RUN", parsed.text(0))
        self.assertIn("DRY RUN", parsed.text(1))
        self.assertIn("NOT A PRODUCTION PROGRAM", parsed.text(1))

    def test_a_production_report_is_not_marked(self):
        result, _config = nested_sample()
        parsed = report_for(result, job_for(result))
        self.assertNotIn("DRY RUN", parsed.text())


def many_sheet_pictures(count: int) -> tuple[NestingResult, NestingConfig]:
    """``count`` copies of :func:`known_sheet`'s layout, as distinct unique
    pictures -- enough (comfortably past the review's ">42" figure) to
    push the cover's unique-sheets table onto a continuation page, so the
    "dry runs marked on every page" invariant actually exercises the
    overflow path rather than just page 1.
    """
    config = NestingConfig(inside_nesting=True, part_gap=0.455)

    def one_layout() -> SheetLayout:
        return SheetLayout(
            [
                Placement(
                    "W2742",
                    0.5,
                    1.0,
                    27.0,
                    42.0,
                    children=[Placement("W3012", 8.0, 7.0, 12.0, 30.0, rotated=True)],
                ),
                Placement("3DB24", 0.5, 44.0, 24.0, 30.0),
            ]
        )

    unique_sheets = [(one_layout(), 1) for _ in range(count)]
    demand = [
        PartSpec("W2742", 27.0, 42.0, count),
        PartSpec("W3012", 30.0, 12.0, count),
        PartSpec("3DB24", 24.0, 30.0, count),
    ]
    result = NestingResult(
        unique_sheets=unique_sheets, total_sheets=count, demand=demand, config=config
    )
    return result, config


class DryRunCoverContinuationTest(unittest.TestCase):
    """2026-08-04 review, fix 1: the cover's continuation page was written
    but never exercised (RESUME's own follow-up note) -- a dry-run job
    with enough unique pictures to spill the table onto a second cover
    page used to leave that whole page unmarked, with rows saying
    "written" and nothing on the page to say otherwise."""

    def test_the_cover_continuation_page_of_a_dry_run_is_marked(self):
        result, _config = many_sheet_pictures(45)
        job = _fake_job(result, dry_run=True)
        parsed = report_for(result, job)

        cover_pages = parsed.page_count - result.unique_sheet_count
        self.assertGreaterEqual(
            cover_pages,
            2,
            "the fixture must actually overflow the cover onto a "
            "continuation page for this test to mean anything",
        )
        for index in range(cover_pages):
            with self.subTest(cover_page=index):
                self.assertIn("DRY RUN", parsed.text(index))
        self.assertIn(
            "UNIQUE SHEETS (continued)", parsed.text(cover_pages - 1)
        )

    def test_a_production_jobs_continuation_page_says_nothing_about_dry_runs(self):
        result, _config = many_sheet_pictures(45)
        job = _fake_job(result)
        parsed = report_for(result, job)
        cover_pages = parsed.page_count - result.unique_sheet_count
        self.assertGreaterEqual(cover_pages, 2)
        self.assertNotIn("DRY RUN", parsed.text())


# --------------------------------------------------------------------------
# (f) the cover page
# --------------------------------------------------------------------------


class CoverPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result, cls.config = nested_order(0.455)
        cls.job = job_for(cls.result, prefix="7201")
        cls.pdf = report_for(cls.result, cls.job)

    def test_the_run_quantities_sum_to_the_sheet_count(self):
        reports = cutsheet.sheet_reports(self.result, self.job)
        total = sum(report.run for report in reports)
        self.assertEqual(total, self.result.total_sheets)
        self.assertIn(
            f"Run quantities sum to {total} physical sheets.", self.pdf.text(0)
        )
        self.assertNotIn("WARNING", self.pdf.text(0))

    def test_the_headline_numbers_are_there(self):
        cover = self.pdf.text(0)
        self.assertIn("PHYSICAL SHEETS", cover)
        self.assertIn("UNIQUE PICTURES", cover)
        self.assertIn("SHEETS SAVED BY NESTING", cover)
        self.assertIn(str(self.result.total_sheets), self.pdf.texts(0))
        self.assertIn(str(self.result.unique_sheet_count), self.pdf.texts(0))
        self.assertIn(str(self.result.sheets_saved), self.pdf.texts(0))

    def test_every_unique_sheet_is_listed_by_name(self):
        cover = self.pdf.text(0)
        for outcome in self.job.outcomes:
            with self.subTest(sheet=outcome.filename):
                self.assertIn(outcome.filename, cover)

    def test_the_job_folder_and_stamp_are_on_the_cover(self):
        cover = self.pdf.text(0)
        self.assertIn(f"created {CREATED}", cover)
        self.assertIn("Cut-sheet report - job R7201", cover)

    def test_a_sheets_saved_of_none_is_printed_as_a_dash_not_a_crash(self):
        result, _config = nested_sample()
        self.assertIsNone(result.sheets_saved)
        parsed = report_for(result, job_for(result))
        self.assertIn("NO BASELINE RUN", parsed.text(0))


# --------------------------------------------------------------------------
# Pairing pictures with files
# --------------------------------------------------------------------------


class PairingTest(unittest.TestCase):
    def test_one_file_per_unique_sheet_pairs_one_to_one(self):
        result, _config = nested_order(0.455)
        job = job_for(result)
        reports = cutsheet.sheet_reports(result, job)
        self.assertEqual(len(reports), result.unique_sheet_count)
        for report, outcome in zip(reports, job.outcomes):
            self.assertEqual(report.filename, outcome.filename)
            self.assertEqual(report.run, outcome.run_quantity)

    def test_one_file_per_physical_sheet_folds_back_into_a_range(self):
        result, _config = nested_order(0.455)
        job = job_for(result, per_physical_sheet=True)
        reports = cutsheet.sheet_reports(result, job)
        self.assertEqual(len(reports), result.unique_sheet_count)
        self.assertEqual(sum(len(r.files) for r in reports), result.total_sheets)

        position = 0
        for report, (_layout, run) in zip(reports, result.unique_sheets):
            group = job.outcomes[position : position + run]
            position += run
            self.assertEqual(report.files, tuple(o.filename for o in group))
            if run > 1:
                self.assertEqual(
                    report.filename, f"{group[0].filename} - {group[-1].filename}"
                )
            else:
                self.assertEqual(report.filename, group[0].filename)

        parsed = report_for(result, job)
        self.assertEqual(
            parsed.page_count,
            result.unique_sheet_count + 1,
            "one page per PICTURE even when one file per physical sheet",
        )
        multi = next(r for r in reports if len(r.files) > 1)
        self.assertIn(multi.filename, parsed.text())

    def test_a_job_that_does_not_match_the_layout_is_refused(self):
        result, _config = nested_order(0.455)
        job = job_for(result)
        job.outcomes = job.outcomes[:-1]
        with self.assertRaises(ReportError) as caught:
            cutsheet.sheet_reports(result, job)
        self.assertIn("cannot pair", str(caught.exception))

    def test_an_empty_layout_is_refused(self):
        empty = NestingResult(
            unique_sheets=[], total_sheets=0, demand=[], config=NestingConfig()
        )
        job = job_for(nested_sample()[0])
        with self.assertRaises(ReportError) as caught:
            cutsheet.sheet_reports(empty, job)
        self.assertIn("run the optimizer first", str(caught.exception))
        with self.assertRaises(ReportError):
            cutsheet.build_report(None, job)


# --------------------------------------------------------------------------
# Cut-list assembly
# --------------------------------------------------------------------------


class CutListTest(unittest.TestCase):
    def test_it_counts_every_part_including_nested_ones(self):
        result, _config = known_sheet()
        layout = result.unique_sheets[0][0]
        ordered = {spec.part_number: spec for spec in result.demand}
        rows = cutsheet.cut_list(layout, ordered)
        self.assertEqual([row.part_number for row in rows], ["3DB24", "W2742", "W3012"])
        self.assertEqual([row.count for row in rows], [1, 1, 1])

    def test_the_ordered_size_is_used_not_the_placed_one(self):
        """W3012 is placed rotated (12 x 30); the cut list must state the
        frame as ORDERED, which is what the shop cuts to."""
        result, _config = known_sheet()
        ordered = {spec.part_number: spec for spec in result.demand}
        rows = cutsheet.cut_list(result.unique_sheets[0][0], ordered)
        row = next(r for r in rows if r.part_number == "W3012")
        self.assertEqual((row.width, row.height), (30.0, 12.0))
        self.assertEqual(row.size_text, "30 x 12")
        self.assertEqual(row.hosts, ("W2742",))
        self.assertEqual(row.nested_text, "nested in W2742")

    def test_frame_types_come_from_the_geometry_engine(self):
        result, _config = known_sheet()
        ordered = {spec.part_number: spec for spec in result.demand}
        rows = {r.part_number: r for r in cutsheet.cut_list(result.unique_sheets[0][0], ordered)}
        self.assertEqual(rows["3DB24"].frame_type, "three_drawer")
        self.assertEqual(rows["W2742"].frame_type, "wall")
        self.assertEqual(rows["3DB24"].openings, "21 x 5, 21 x 9.875, 21 x 9.125")
        self.assertEqual(rows["W2742"].openings, "24 x 39")

    def test_a_repeated_part_is_one_row_with_a_count(self):
        config = NestingConfig()
        layout = SheetLayout(
            [
                Placement("W2436", 1.0, 1.0, 24.0, 36.0),
                Placement("W2436", 1.0, 40.0, 24.0, 36.0),
            ]
        )
        rows = cutsheet.cut_list(layout, {"W2436": PartSpec("W2436", 24.0, 36.0, 2)})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].count, 2)
        self.assertEqual(config.sheet_width, 49.0)


class DimFormattingTest(unittest.TestCase):
    """2026-08-04 review, fix 4: ``%g`` keeps only 6 SIGNIFICANT digits, so
    an exact 32nd -- the machine's own grid -- at typical sheet-size
    magnitude printed as "47.0312", no longer exact.  Rounding to 5
    DECIMAL places instead keeps every 32nd exact regardless of how many
    digits are in front of the point, while still swallowing float noise
    left over from earlier arithmetic."""

    def test_an_exact_32nd_prints_exactly_at_typical_magnitude(self):
        self.assertEqual(cutsheet._dim(47.03125), "47.03125")
        self.assertEqual(cutsheet._dim(33.46875), "33.46875")

    def test_float_noise_is_still_swallowed(self):
        self.assertEqual(cutsheet._dim(19.099999999999998), "19.1")

    def test_integers_print_without_a_trailing_point(self):
        self.assertEqual(cutsheet._dim(21.0), "21")
        self.assertEqual(cutsheet._dim(0.0), "0")

    def test_the_old_percent_g_rounding_is_gone(self):
        self.assertNotEqual(cutsheet._dim(47.03125), f"{47.03125:g}")


class LongPartNumberTest(unittest.TestCase):
    """2026-08-04 review, fix 2: the cut list's fixed "xN" count column
    sits at ``x0 + 78``; an untruncated part number of ~15+ characters
    prints UNDER it -- an unreadable glyph pile."""

    LONG_NAME = "W2036CUSTOMLONGNAME"

    def long_name_sheet(self):
        config = NestingConfig(part_gap=0.455)
        layout = SheetLayout([Placement(self.LONG_NAME, 1.0, 1.0, 20.0, 36.0)])
        demand = [PartSpec(self.LONG_NAME, 20.0, 36.0, 1)]
        result = NestingResult(
            unique_sheets=[(layout, 1)], total_sheets=1, demand=demand, config=config
        )
        return result

    def test_a_long_part_number_is_truncated_clear_of_the_count_column(self):
        result = self.long_name_sheet()
        job = _fake_job(result, prefix="88")
        parsed = report_for(result, job)
        texts = parsed.texts(1)
        occurrences = [t for t in texts if t.startswith(self.LONG_NAME[:10])]
        self.assertTrue(occurrences, "the part number must appear on the page")
        # The drawing's own label may print the full name (shrunk, not
        # truncated -- that machinery is unaffected by this fix); the cut
        # list draws its copy LAST, so it is the final occurrence.
        cutlist_text = occurrences[-1]
        self.assertNotEqual(cutlist_text, self.LONG_NAME)
        self.assertTrue(cutlist_text.endswith("..."), cutlist_text)
        self.assertLessEqual(
            pdf.text_width(cutlist_text, pdf.HELVETICA_BOLD, 9.5),
            cutsheet._CUTLIST_NUMBER_MAX_WIDTH,
        )
        self.assertIn("x1", texts, "the count column must still be there, untouched")


class CutListBudgetTest(unittest.TestCase):
    """2026-08-04 review, fix 7: the per-row page-budget check assumed one
    wrapped detail line where wrap_text can produce two -- a worst-case
    row could spill past the region floor onto the drawing beneath it --
    and the overflow pointer sent the operator to the cover's contents
    list, which is capped at 3 lines and never carries a size."""

    def test_the_budget_accounts_for_every_wrapped_detail_line(self):
        row = cutsheet.CutListRow(
            part_number="W2436",
            count=1,
            width=24.0,
            height=36.0,
            frame_type="wall",
            openings=(
                "a deliberately long opening description engineered to wrap "
                "onto two full lines at the cut list's own font and column width"
            ),
            hosts=("SOMEHOST",),
        )
        width = cutsheet.CUTLIST_REGION[2]
        needed, is_wdc, detail_lines = cutsheet._cut_list_row_plan(row, width)
        self.assertGreaterEqual(len(detail_lines), 2, "the fixture must actually wrap")
        self.assertFalse(is_wdc)
        self.assertEqual(needed, 10.0 + len(detail_lines) * 8.5 + 8.5 + 6.0)

    def test_a_sheet_with_more_rows_than_one_column_holds_continues_the_list(self):
        count = 30
        config = NestingConfig(part_gap=0.455)
        placements = [
            Placement(
                f"W20{index:02d}", 1.0 + (index % 9) * 5.0, 1.0 + (index // 9) * 5.0, 4.0, 4.0
            )
            for index in range(count)
        ]
        layout = SheetLayout(placements)
        demand = [PartSpec(f"W20{index:02d}", 4.0, 4.0, 1) for index in range(count)]
        result = NestingResult(
            unique_sheets=[(layout, 1)], total_sheets=1, demand=demand, config=config
        )
        job = _fake_job(result, prefix="55")
        parsed = report_for(result, job)

        self.assertGreaterEqual(
            parsed.page_count, 3, "the cut list must have spilled onto its own page"
        )
        self.assertEqual(parsed.problems, [])
        sheet_text = "\n".join(parsed.text(i) for i in range(1, parsed.page_count))
        for index in range(count):
            with self.subTest(part=index):
                self.assertIn(f"W20{index:02d}", sheet_text)
        self.assertIn("4 x 4", sheet_text)
        self.assertIn(job.outcomes[0].filename, sheet_text)
        self.assertIn("CUT LIST (continued)", sheet_text)
        self.assertNotIn("cover page contents list", parsed.text())


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


class WriteReportTest(unittest.TestCase):
    def test_it_writes_the_bytes_it_composed(self):
        result, _config = nested_sample()
        job = job_for(result)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, cutsheet.report_filename("7201"))
            written = cutsheet.write_report(result, job, path, created=CREATED)
            self.assertEqual(os.listdir(folder), ["R7201_report.pdf"])
            self.assertEqual(written, os.path.abspath(path))
            with open(written, "rb") as handle:
                data = handle.read()
            self.assertEqual(data, cutsheet.build_report(result, job, created=CREATED))
            self.assertEqual(Pdf(data).problems, [])

    def test_nothing_is_written_when_the_report_cannot_be_composed(self):
        result, _config = nested_sample()
        job = job_for(result)
        job.outcomes = []
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "R1_report.pdf")
            with self.assertRaises(ReportError):
                cutsheet.write_report(result, job, path, created=CREATED)
            self.assertEqual(os.listdir(folder), [])

    def test_the_file_name_follows_the_job_prefix(self):
        self.assertEqual(cutsheet.report_filename("7201"), "R7201_report.pdf")
        self.assertEqual(cutsheet.report_filename("62"), "R62_report.pdf")

    def test_a_successful_write_leaves_no_partial_file_behind(self):
        """2026-08-04 review, fix 9: the write now goes through
        ``<path>.partial`` and :func:`os.replace` -- the exact discipline
        :func:`faceframe_cnc.post.job.write_job` was fixed to use in the
        previous review, for a bare ``open(path, "wb")`` in this same
        shape.  A successful write must not leave the intermediate file
        sitting next to the report."""
        result, _config = nested_sample()
        job = job_for(result)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, cutsheet.report_filename("7201"))
            cutsheet.write_report(result, job, path, created=CREATED)
            self.assertEqual(os.listdir(folder), ["R7201_report.pdf"])
            self.assertFalse(os.path.exists(path + cutsheet.PARTIAL_SUFFIX))

    def test_a_failed_write_does_not_touch_an_existing_report(self):
        """The whole point: a crash mid-write must never leave a half
        report at the name an operator would actually open."""
        result, _config = nested_sample()
        job = job_for(result)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, cutsheet.report_filename("7201"))
            original = b"an old report that must survive a failed rewrite"
            with open(path, "wb") as handle:
                handle.write(original)

            with mock.patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(ReportError):
                    cutsheet.write_report(result, job, path, created=CREATED)

            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), original, "the old report must survive intact")
            self.assertEqual(
                os.listdir(folder),
                [os.path.basename(path)],
                "the failed .partial must not be left behind either",
            )


if __name__ == "__main__":
    unittest.main()
