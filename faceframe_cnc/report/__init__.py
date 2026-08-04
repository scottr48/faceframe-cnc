"""Printable paperwork for a generated job (Milestone 6).

The report is the sheet of paper that goes to the machine beside the
``.anc`` files: one page per unique sheet, drawn to scale, every part
labelled with its part number, the run quantity impossible to miss, and a
cut list the operator can tick off.

The package is split the same way the rest of this app is:

``pdf``
    A minimal, dependency-free PDF 1.4 writer — pages, rectangles, lines
    and text in the base-14 Helvetica faces, with the AFM width tables
    needed to centre and right-align accurately.  It knows nothing about
    sheets, frames or nesting.
``cutsheet``
    Composes the report from an optimizer
    :class:`~faceframe_cnc.nesting.NestingResult` plus the
    :class:`~faceframe_cnc.post.job.JobResult` that says which file each
    picture became.  It knows nothing about the PDF byte format beyond the
    calls it makes into ``pdf``.

Nothing here imports Qt, and nothing here imports anything outside the
standard library.  Output is deterministic: the same inputs and the same
injected ``created`` stamp produce byte-identical PDFs, so a report can be
diffed and re-issued without churn.
"""

from .cutsheet import ReportError, build_report, write_report

__all__ = ["ReportError", "build_report", "write_report"]
