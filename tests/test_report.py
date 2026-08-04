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


if __name__ == "__main__":
    unittest.main()
