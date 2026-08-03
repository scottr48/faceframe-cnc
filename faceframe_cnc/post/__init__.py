"""NC post-processor for the shop's Fanuc-style ``.anc`` programs.

Everything in this package was measured from the production files in
``reference/nc_files`` (rule zero: where the build spec and those files
disagree, the FILES win).  Nothing here invents a G/M code, a feed, a
speed, a Z level or a motion pattern that is not present in
``R710101N.anc`` / ``R720101N.anc`` / ``R730101N.anc``.

Modules
-------
``model``
    The data model: sheet contents (:class:`~.model.SheetProgram`), the
    tool/pass tables measured from the references
    (:class:`~.model.PostConfig`) and the sequencing plan
    (:class:`~.model.CutPlan`) that says which feature is cut when.
``generator``
    Emits ``.anc`` text from a ``SheetProgram`` + ``CutPlan``.  Purely
    table-driven: every line comes from the templates in ``model``.
``reconstruct``
    Reads an existing ``.anc`` back into a ``SheetProgram`` + ``CutPlan``
    (part footprints, rotations, openings, nesting, cut order).  This is
    what makes the round-trip proof possible.
``verifier``
    An INDEPENDENT re-parse of a finished program (shares no code with
    ``generator``) that gates bounds, Z limits, foreign-footprint
    intrusion and header/footer integrity.
``from_layout``
    Turns an OPTIMIZER sheet (:class:`faceframe_cnc.nesting.SheetLayout`)
    into a ``SheetProgram`` + ``CutPlan``, implementing the 2026-08-03
    onion-skin cutting order — and refusing sheets that hold a WDC frame,
    whose 45-degree T17 stile slot has no tool table entry in any reference
    file.
``job``
    Writes a whole optimizer run out as one ``.anc`` per sheet, with the
    verifier gating every single write, plus the dry-run (air cut) mode.
"""

from .model import (
    Box,
    CutPlan,
    FeatureRef,
    PartProgram,
    PostConfig,
    ProgramHeader,
    SheetProgram,
    default_config,
    program_from_placements,
)
from .generator import generate
from .reconstruct import ReconstructionError, reconstruct
from .verifier import Violation, verify, verify_file
from .from_layout import (
    SheetPlanError,
    WdcNotSupportedError,
    cut_plan_for,
    plan_sheet,
    post_config_for,
    sheet_program_from_layout,
)
from .job import (
    JobError,
    JobOptions,
    JobResult,
    SheetOutcome,
    build_job,
    dry_run_config,
    sheet_filename,
    write_job,
)

__all__ = [
    "Box",
    "CutPlan",
    "FeatureRef",
    "PartProgram",
    "PostConfig",
    "ProgramHeader",
    "SheetProgram",
    "default_config",
    "program_from_placements",
    "generate",
    "reconstruct",
    "ReconstructionError",
    "verify",
    "verify_file",
    "Violation",
    "SheetPlanError",
    "WdcNotSupportedError",
    "cut_plan_for",
    "plan_sheet",
    "post_config_for",
    "sheet_program_from_layout",
    "JobError",
    "JobOptions",
    "JobResult",
    "SheetOutcome",
    "build_job",
    "dry_run_config",
    "sheet_filename",
    "write_job",
]
