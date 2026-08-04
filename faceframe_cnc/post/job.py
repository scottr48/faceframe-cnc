"""Write a whole optimizer run out as ``.anc`` programs, one per sheet.

This is the only module in the package that touches the filesystem, and it
is deliberately paranoid about it: **nothing is written until the finished
program text has been handed to the independent verifier and come back
clean.**  A sheet that fails is reported, not written; the rest of the job
still goes out.

The gates, in order
-------------------
1.  :func:`faceframe_cnc.nesting.validate_layouts` re-checks the whole
    layout from scratch.  Any finding aborts the job before a single sheet
    is planned — a bad layout is not a per-sheet problem.
2.  :func:`~.from_layout.plan_sheet` refuses sheets this post cannot
    honestly cut (today: anything holding a WDC frame, whose T17 slot has
    no tool table entry).
3.  :func:`~.generator.generate` refuses a post table that would drive the
    machine outside its Z window (spec section 8).
4.  :func:`~.verifier.verify` re-parses the PRODUCTION text — bounds, Z
    limits, foreign-footprint intrusion, header/footer integrity.
5.  In dry-run mode the lifted text is generated and verified as well, and
    the production text from step 4 must ALSO have been clean: an air cut
    is a rehearsal of a program that is itself safe to run, so a sheet
    whose real program would be refused never gets a dry run either.

Naming (spec section 6, confirmed)
----------------------------------
``R`` + a configurable digit prefix + a two-digit sheet index + ``N.anc``,
e.g. prefix ``7201`` gives ``R720101N.anc``, ``R720102N.anc``, ...  The
``O`` line numbers sequentially (``O0001``, ``O0002``, ...).  Refused sheets
keep their index — the numbering has a gap rather than shifting every later
sheet, because the sheet numbers appear on the operator's paperwork.

Dry run
-------
Every CUT depth is mirrored about the top of the stock: a cut at Z is
emitted at ``2 * stock_top_z - Z``, so the deepest, most dangerous cut
(Z-0.006, through) flies highest (Z1.506) and the shallowest (the Z0.55
panel groove) sits at Z0.95 — all of them above the 0.75 stock top and all
of them below the unchanged Z2. ramp plane.  Nothing else moves: the same
tools, feeds, speeds, XY profile and rapid/ramp planes.  The lead-in ramps
are shorter, because their length is ``(ramp plane - cut Z) * 2`` in the
measured grammar and the cut Z is now close to the ramp plane.

The mirrored numbers are not a new table — they are derived from the
measured one by :func:`dry_run_config`, which also sets
:attr:`~.model.PostConfig.dry_run` so the verifier applies its extra
"no cutting move may reach the stock" check.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, replace

from .from_layout import (
    SheetPlanError,
    WdcNotSupportedError,
    plan_sheet,
    post_config_for,
)
from .generator import generate
from .model import PostConfig, ProgramHeader, default_config
from .verifier import verify

__all__ = [
    "JobError",
    "JobOptions",
    "SheetOutcome",
    "JobResult",
    "APP_BANNER_NAME",
    "DRY_RUN_BANNER",
    "dry_run_config",
    "now_created",
    "sheet_filename",
    "build_job",
    "write_job",
]

APP_BANNER_NAME = "FACEFRAME NESTING OPTIMIZER"
DRY_RUN_BANNER = "*** DRY RUN - AIR CUT ABOVE THE STOCK - NOT A PRODUCTION PROGRAM ***"

#: ``31 JUL 26 - 15:20`` (R720101N line 3).  The month is spelled out from
#: this table rather than by ``%b`` so a shop PC running under a non-English
#: locale cannot put an accented or non-ASCII month into an NC comment.
_MONTHS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

_PREFIX_RE = re.compile(r"^\d{1,8}$")
#: A comment may not contain a parenthesis: the control ends the comment at
#: the first ``)``, and the verifier's comment stripper agrees.
_BANNER_SAFE_RE = re.compile(r"[()\r\n]")

#: Longest banner comment line emitted, including its parentheses.  The
#: reference files' longest comment is 45 characters; 72 keeps a wrapped
#: contents list readable on a shop terminal without wrapping in the editor.
_BANNER_WIDTH = 72


class JobError(RuntimeError):
    """The job as a whole cannot be run (bad options, or an invalid layout)."""


def now_created(when: time.struct_time | None = None) -> str:
    """``(CREATED ON ...)`` text for right now, in the references' format."""
    stamp = when or time.localtime()
    return (
        f"{stamp.tm_mday:02d} {_MONTHS[stamp.tm_mon - 1]} "
        f"{stamp.tm_year % 100:02d} - {stamp.tm_hour:02d}:{stamp.tm_min:02d}"
    )


# --------------------------------------------------------------------------
# Options and results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JobOptions:
    """Everything the job writer needs besides the layout itself."""

    output_dir: str
    #: Digits between the leading ``R`` and the two-digit sheet index.
    prefix: str = "0001"
    #: Emit the air-cut rehearsal instead of the production program.
    dry_run: bool = False
    #: One file per PHYSICAL sheet instead of one per unique picture with a
    #: run quantity in the banner (spec section 5's toggle).
    per_physical_sheet: bool = False
    #: Injectable ``(CREATED ON ...)`` text; ``None`` means "now".  Tests set
    #: it so that two runs of the same job are byte-identical.
    created: str | None = None
    app_name: str = APP_BANNER_NAME
    first_sheet_index: int = 1
    first_o_number: int = 1
    #: Base post table; the sheet size is taken from the nesting config.
    post_config: PostConfig | None = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not _PREFIX_RE.match(self.prefix or ""):
            problems.append(
                f"job prefix {self.prefix!r} must be 1-8 digits (the file name is "
                f"R<prefix><sheet index>N.anc)"
            )
        if not str(self.output_dir or "").strip():
            problems.append("no output folder was given")
        if self.first_sheet_index < 1 or self.first_sheet_index > 98:
            problems.append("the first sheet index must be between 1 and 98")
        if self.first_o_number < 1 or self.first_o_number > 9999:
            problems.append("the first O-number must be between 1 and 9999")
        if _BANNER_SAFE_RE.search(self.app_name or ""):
            problems.append("the banner name may not contain parentheses or newlines")
        return problems


@dataclass
class SheetOutcome:
    """What happened to one sheet."""

    sheet_index: int
    o_number: int
    filename: str
    run_quantity: int
    contents: dict[str, int]
    nested: int = 0
    written: bool = False
    path: str | None = None
    text: str | None = None
    #: Why the sheet was refused, empty when it was not.
    problems: list[str] = field(default_factory=list)
    #: ``"wdc"``, ``"plan"``, ``"verifier"`` or ``None``.
    refusal_kind: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    def contents_text(self) -> str:
        return ", ".join(f"{count}x{name}" for name, count in sorted(self.contents.items()))

    def describe(self) -> str:
        if self.ok:
            where = self.path or "(not written)"
            return f"{self.filename}: run {self.run_quantity} - {where}"
        return f"{self.filename}: REFUSED - " + "; ".join(self.problems)


@dataclass
class JobResult:
    """Every sheet's outcome, plus enough context to explain the job."""

    outcomes: list[SheetOutcome]
    options: JobOptions
    output_dir: str
    dry_run: bool
    total_sheets: int = 0

    @property
    def written(self) -> list[SheetOutcome]:
        return [o for o in self.outcomes if o.written]

    @property
    def refused(self) -> list[SheetOutcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def files(self) -> list[str]:
        return [o.path for o in self.written if o.path]

    def summary(self) -> str:
        head = (
            f"{len(self.written)} of {len(self.outcomes)} sheet programs written to "
            f"{self.output_dir}"
        )
        if self.dry_run:
            head += "  [DRY RUN - air cut]"
        lines = [head]
        for outcome in self.outcomes:
            lines.append("  " + outcome.describe())
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


def _mirror(z: float, stock_top: float) -> float:
    """A cut depth reflected to the same distance ABOVE the stock top."""
    return round(2.0 * stock_top - z, 9)


def dry_run_config(config: PostConfig | None = None) -> PostConfig:
    """The air-cut twin of a post table (see the module docstring).

    Only the cut depths move.  Feeds, speeds, tools, offsets, the ramp and
    rapid planes and the Z window are the measured ones, so a dry-run file
    differs from its production twin in exactly the Z words of its cutting
    moves (and the ramp lengths those Z words imply).
    """
    cfg = config or default_config()
    top = cfg.stock_top_z
    lifted = replace(
        cfg,
        openings_pass=replace(
            cfg.openings_pass, z_cut=_mirror(cfg.openings_pass.z_cut, top)
        ),
        detail_pass=replace(cfg.detail_pass, z_cut=_mirror(cfg.detail_pass.z_cut, top)),
        perimeter_passes=tuple(
            replace(spec, z_cut=_mirror(spec.z_cut, top))
            for spec in cfg.perimeter_passes
        ),
        panel=replace(cfg.panel, z_cut=_mirror(cfg.panel.z_cut, top)),
        # The V slot's end overrun is derived from its DEPTH of cut, which
        # a lifted pass no longer has, so the production geometry is pinned
        # here: an air cut must trace the real program's XY path exactly.
        wdc_slot=replace(
            cfg.wdc_slot,
            z_cuts=tuple(_mirror(z, top) for z in cfg.wdc_slot.z_cuts),
            overruns=tuple(
                cfg.wdc_slot_reach(position)
                for position in range(len(cfg.wdc_slot.z_cuts))
            ),
        ),
        dry_run=True,
    )
    depths = [
        lifted.openings_pass.z_cut,
        lifted.detail_pass.z_cut,
        lifted.panel.z_cut,
        *lifted.wdc_slot.z_cuts,
        *(spec.z_cut for spec in lifted.perimeter_passes),
    ]
    deepest = min(depths)
    if deepest < top - 1e-9:
        raise JobError(
            f"the dry-run lift leaves a cut at Z{deepest}, which is still in the "
            f"{top} stock — refusing to call that an air cut"
        )
    highest = max(depths)
    if highest > lifted.approach_z - 1e-9:
        raise JobError(
            f"the dry-run lift puts a cut at Z{highest}, at or above the "
            f"Z{lifted.approach_z} ramp plane — the lead-in would climb, not descend"
        )
    return lifted


# --------------------------------------------------------------------------
# Naming and banner
# --------------------------------------------------------------------------


def sheet_filename(prefix: str, index: int) -> str:
    """``R`` + prefix + two-digit index + ``N.anc`` (spec section 6)."""
    return f"R{prefix}{index:02d}N.anc"


def _sanitise(text: str) -> str:
    return _BANNER_SAFE_RE.sub(" ", str(text)).strip()


def _wrap_comment(label: str, body: str) -> list[str]:
    """``(LABEL: a, b, c)``, continued on ``(LABEL CONT: ...)`` lines."""
    words = [w for w in body.split(", ") if w]
    if not words:
        return [f"({label}: none)"]
    lines: list[str] = []
    current: list[str] = []
    head = label
    for word in words:
        trial = ", ".join(current + [word])
        if current and len(trial) + len(head) + 4 > _BANNER_WIDTH:
            lines.append(f"({head}: {', '.join(current)})")
            head = f"{label} CONT"
            current = [word]
        else:
            current.append(word)
    lines.append(f"({head}: {', '.join(current)})")
    return lines


def _banner(
    options: JobOptions,
    outcome: SheetOutcome,
    sheet_count: int,
    physical_total: int,
) -> tuple[str, ...]:
    lines = [
        f"({_sanitise(options.app_name)} - GENERATED PROGRAM, DO NOT HAND EDIT)",
        f"(SHEET {outcome.sheet_index:02d} OF {sheet_count:02d} - "
        f"RUN QTY {outcome.run_quantity} OF {physical_total} SHEETS)",
    ]
    lines.extend(_wrap_comment("CONTENTS", _sanitise(outcome.contents_text())))
    if outcome.nested:
        lines.append(
            f"(NESTED: {outcome.nested} FRAME{'S' if outcome.nested != 1 else ''} "
            f"CUT FROM HOST WASTE)"
        )
    if options.dry_run:
        lines.append(f"({_sanitise(DRY_RUN_BANNER)})")
    return tuple(lines)


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------


def _expand(result, per_physical: bool):
    """``[(layout, run_quantity)]`` for the files that will be produced."""
    if not per_physical:
        return list(result.unique_sheets)
    out = []
    for layout, run in result.unique_sheets:
        out.extend([(layout, 1)] * int(run))
    return out


def build_job(result, options: JobOptions) -> JobResult:
    """Plan, generate and verify every sheet WITHOUT touching the disk.

    :class:`SheetOutcome.text` holds the finished program for every sheet
    that passed; :func:`write_job` is this function plus the writes.
    """
    from ..nesting import validate_layouts

    problems = options.validate()
    if problems:
        raise JobError("; ".join(problems))
    if result is None or not result.unique_sheets:
        raise JobError("there is no layout to generate — run the optimizer first")

    layout_problems = validate_layouts(result, result.config)
    if layout_problems:
        raise JobError(
            "the layout does not pass its own validator, so no NC was generated:\n  "
            + "\n  ".join(layout_problems)
        )

    production = post_config_for(result.config, options.post_config)
    air = dry_run_config(production) if options.dry_run else None

    sheets = _expand(result, options.per_physical_sheet)
    last_index = options.first_sheet_index + len(sheets) - 1
    if last_index > 99:
        raise JobError(
            f"this job needs {len(sheets)} files, which would run the sheet index "
            f"up to {last_index} — the confirmed file name format "
            f"R<prefix><NN>N.anc only has two digits for it. Split the job, or "
            f"generate one file per unique sheet instead of one per physical sheet."
        )
    created = options.created if options.created is not None else now_created()

    outcomes: list[SheetOutcome] = []
    for position, (layout, run) in enumerate(sheets):
        index = options.first_sheet_index + position
        outcome = SheetOutcome(
            sheet_index=index,
            o_number=options.first_o_number + position,
            filename=sheet_filename(options.prefix, index),
            run_quantity=int(run),
            contents=dict(layout.part_counts()),
            nested=layout.child_count(),
        )
        _render(outcome, layout, result, options, production, air, created, len(sheets))
        outcomes.append(outcome)

    return JobResult(
        outcomes=outcomes,
        options=options,
        output_dir=os.path.abspath(options.output_dir),
        dry_run=options.dry_run,
        total_sheets=result.total_sheets,
    )


def _render(
    outcome: SheetOutcome,
    layout,
    result,
    options: JobOptions,
    production: PostConfig,
    air: PostConfig | None,
    created: str,
    sheet_count: int,
) -> None:
    """Fill in ``outcome.text`` or ``outcome.problems`` for one sheet."""
    name = outcome.filename[: -len(".anc")]
    header = ProgramHeader(name=name, o_number=outcome.o_number, created=created)
    banner = _banner(options, outcome, sheet_count, result.total_sheets)

    try:
        program, plan = plan_sheet(
            layout, header, result.demand, result.config, production
        )
    except WdcNotSupportedError as exc:
        outcome.problems = [str(exc)]
        outcome.refusal_kind = "wdc"
        return
    except SheetPlanError as exc:
        outcome.problems = [str(exc)]
        outcome.refusal_kind = "plan"
        return

    # -- the production program is generated and verified even for a dry
    #    run: an air cut is a rehearsal of a program that must itself be
    #    safe, and the verifier's foreign-cut rule only bites on cuts that
    #    reach the stock, which a lifted program by definition never does.
    real_config = replace(production, banner_lines=banner)
    try:
        real_text = generate(program, plan, real_config)
    except ValueError as exc:
        outcome.problems = [str(exc)]
        outcome.refusal_kind = "plan"
        return
    violations = verify(real_text, real_config)
    if violations:
        outcome.problems = [str(v) for v in violations]
        outcome.refusal_kind = "verifier"
        return

    if air is None:
        outcome.text = real_text
        return

    air_config = replace(air, banner_lines=banner)
    try:
        air_text = generate(program, plan, air_config)
    except ValueError as exc:
        outcome.problems = [str(exc)]
        outcome.refusal_kind = "plan"
        return
    air_violations = verify(air_text, air_config)
    if air_violations:
        outcome.problems = [str(v) for v in air_violations]
        outcome.refusal_kind = "verifier"
        return
    outcome.text = air_text


def write_job(result, options: JobOptions) -> JobResult:
    """Generate, verify and WRITE one ``.anc`` per sheet.

    Returns a :class:`JobResult`; sheets that failed a gate are in
    :attr:`JobResult.refused` and have no file on disk.  Raises
    :class:`JobError` only for whole-job failures (bad options, or a layout
    that does not pass :func:`~faceframe_cnc.nesting.validate_layouts`).
    """
    job = build_job(result, options)
    try:
        os.makedirs(job.output_dir, exist_ok=True)
    except OSError as exc:
        raise JobError(f"cannot create the output folder {job.output_dir}: {exc}") from exc

    for outcome in job.outcomes:
        if outcome.text is None:
            continue
        path = os.path.join(job.output_dir, outcome.filename)
        try:
            with open(path, "w", newline="") as handle:
                handle.write(outcome.text)
        except OSError as exc:
            outcome.problems = [f"could not write {path}: {exc}"]
            outcome.refusal_kind = "write"
            continue
        outcome.path = path
        outcome.written = True
    return job
