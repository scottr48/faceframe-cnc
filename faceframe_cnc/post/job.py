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
    limits, foreign-footprint intrusion, header/footer integrity — and, since
    the 2026-08-04 review, is handed the sheet's expected-work manifest as
    well (:func:`~.verifier.expected_work`, derived from the LAYOUT, not from
    the plan the emitter used), so that a program which does nothing wrong
    but leaves a cut OUT is refused too: a dropped through pass, an unrouted
    opening, a missing T13 groove or T17 slot pass.  That gap was the one
    thing an independent re-parse could not see on its own, since it only
    ever knew what the file said, never what the sheet owed.
5.  In dry-run mode the lifted text is generated and verified as well —
    against its own manifest, built from the lifted table, because a
    rehearsal missing a cut is still a wrong rehearsal — and the production
    text from step 4 must ALSO have been clean: an air cut is a rehearsal of
    a program that is itself safe to run, so a sheet whose real program would
    be refused never gets a dry run either.

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

How the files reach the disk (2026-08-04 review)
------------------------------------------------
The verifier gate above is only half of "safe on the shop floor".  The
other half is that the FOLDER the operator carries to the machine may
never contain a program that is not part of the job he just generated.
Three ways the first version of :func:`write_job` broke that, all found in
the 2026-08-04 review, all fixed here:

(a) *Regenerating a prefix that used to produce more sheets.*  Run one
    writes ``R720101N.anc`` .. ``R720112N.anc``; the order shrinks and run
    two writes 01..09.  10, 11 and 12 stayed on disk looking exactly as
    current as the nine live ones.
(b) *A sheet refused this run.*  It was simply skipped — so a file of that
    name left by an earlier run stayed, and the one sheet the post
    REFUSED to stand behind was the one the operator could still cut.
(c) *A crash or a full disk part way through a write.*  ``open(path, "w")``
    truncates first, so the failure left a half program under a
    production name.

So: every file is written to a per-run ``<name>.partial-<pid>-<clock>`` in
the same folder and moved by :func:`os.replace` onto its final name only
once the whole text is written, flushed, fsynced and read back (fix for (c)
— a failed write cannot even reach the final name, let alone truncate what
is there), and after the writes :func:`write_job` sweeps the output folder
for files that match THIS job's naming pattern but were not written by THIS
run and moves them into ``superseded/<stamp>/`` (fixes (a) and (b)).

Nothing is ever deleted: a stale program is somebody's previous job and
may be the only copy of it.  Nothing outside the job's own naming pattern
is touched at all — the PDF report, other prefixes' programs and anything
the operator keeps in that folder are none of this module's business.  And
none of it is silent: every move is recorded on
:attr:`JobResult.superseded` (plus :attr:`SheetOutcome.superseded_path`
for the sheet whose name it was) and every move that FAILS is a loud
:attr:`JobResult.quarantine_problems` entry naming the file that is still
lying beside the job.  The shop rule is that anything the app does on its
own it also shows.

Three more holes in that, all closed here (2026-08-04 review, fix 7)
--------------------------------------------------------------------
(d) *Two orders, one folder and prefix.*  The sweep above only preserved
    files this run did NOT write, so generating order B into order A's
    folder at A's prefix silently :func:`os.replace`'d every same-named
    program of A's out of existence.  Quarantining what a run overwrites
    was the one case not covered, and it is the likeliest one: the prefix
    is a setting the shop leaves alone.  Now the file about to be replaced
    is moved into the SAME ``superseded/<stamp>/`` first, so
    ``superseded`` lists the previous version of every sheet this run
    republishes.  If that move cannot be made the sheet is NOT published:
    a file that cannot be preserved is not one this module will destroy,
    and the refusal says so on the outcome and in
    :attr:`JobResult.quarantine_problems`.
(e) *Publication was atomic per FILE, not per job.*  Seventeen sheets were
    written and renamed one at a time, so an interruption in the middle
    left a folder holding new programs for sheets 1-9 and yesterday's for
    10-17 — every one of them plausible, and no way to tell from the
    folder.  Now every program is written and verified as a partial FIRST
    and the renames happen afterwards in one tight loop that does nothing
    else.  The residual window is honest and small rather than gone: the
    loop still performs N pairs of renames, so a power failure inside it
    can leave the last few sheets unpublished.  What it cannot leave is a
    half-written program, a destroyed previous version (both copies of the
    boundary sheet are on disk — the old one in ``superseded/``) or a
    silent mix, because a run that did not finish publishing did not
    return a :class:`JobResult` at all.
(f) *One shared ``.partial`` name.*  Two runs writing the same sheet at the
    same time took turns inside one temp file and published whatever
    interleaving of the two texts won, under a production name, past every
    gate.  The temp name now carries this process's id and a clock reading,
    so concurrent runs cannot touch each other's bytes.  Concurrent runs
    into one folder are still not a supported thing to do — the loser's
    in-flight temp file may be quarantined by the winner's sweep — but the
    failure is then a loud write refusal in one run instead of a corrupt
    program in both.
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
from .verifier import expected_work, verify

__all__ = [
    "JobError",
    "JobOptions",
    "SheetOutcome",
    "JobResult",
    "SupersededFile",
    "APP_BANNER_NAME",
    "DRY_RUN_BANNER",
    "PARTIAL_SUFFIX",
    "SUPERSEDED_DIR_NAME",
    "partial_suffix",
    "dry_run_config",
    "now_created",
    "now_stamp",
    "sheet_filename",
    "job_file_pattern",
    "build_job",
    "write_job",
]

APP_BANNER_NAME = "FACEFRAME NESTING OPTIMIZER"
DRY_RUN_BANNER = "*** DRY RUN - AIR CUT ABOVE THE STOCK - NOT A PRODUCTION PROGRAM ***"

#: What a program is called while it is still being written.  It is in the
#: SAME folder as its final name (``os.replace`` is only atomic within one
#: filesystem) and it deliberately does not end in ``.anc``, so a file the
#: shop's machine or file browser will offer up as a program never exists
#: until it is complete.
#:
#: This is the STEM of the temp name; :func:`partial_suffix` adds a per-run
#: tail to it (2026-08-04 review, fix 7f).  The stem is what
#: :data:`_PARTIAL_RE` and :mod:`faceframe_cnc.report.cutsheet` recognise, so a
#: leftover temp file is still identified as one whatever run made it.
PARTIAL_SUFFIX = ".partial"

#: Matches :data:`PARTIAL_SUFFIX` with or without a run tail, at the end of a
#: name.  The tail is made of digits, letters and dashes only, so it cannot
#: swallow a dot and mistake ``R720101N.anc.partial-1.bak`` for a temp file.
_PARTIAL_RE = re.compile(re.escape(PARTIAL_SUFFIX) + r"(?:-[A-Za-z0-9-]*)?$")

#: Subfolder of the output folder that stale programs are moved into.  A
#: fresh ``superseded/<stamp>/`` per run, so two regenerations of the same
#: prefix cannot bury each other's evidence.
SUPERSEDED_DIR_NAME = "superseded"

#: ``31 JUL 26 - 15:20`` (R720101N line 3).  The month is spelled out from
#: this table rather than by ``%b`` so a shop PC running under a non-English
#: locale cannot put an accented or non-ASCII month into an NC comment.
_MONTHS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

_PREFIX_RE = re.compile(r"^\d{1,8}$")
#: A quarantine stamp names a folder, so it may not carry a path separator,
#: a drive letter or a ``..``.
_STAMP_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
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


def now_stamp(when: time.struct_time | None = None) -> str:
    """``20260804-151204`` — a sortable folder name for one run's quarantine.

    Deliberately NOT the :func:`now_created` format: this one goes in a path,
    so it carries no spaces, colons or locale-dependent month names, and it
    sorts chronologically in the file browser the operator will use to find
    last week's programs.

    A clock reading in a PATH is fine; a clock reading in the CONTENT of an
    ``.anc`` would break the byte-for-byte determinism the post is proved
    with, which is why :attr:`JobOptions.created` exists.  For the same
    reason (tests need a fixed answer) this one is injectable too, via
    :attr:`JobOptions.quarantine_stamp`.
    """
    stamp = when or time.localtime()
    return (
        f"{stamp.tm_year:04d}{stamp.tm_mon:02d}{stamp.tm_mday:02d}"
        f"-{stamp.tm_hour:02d}{stamp.tm_min:02d}{stamp.tm_sec:02d}"
    )


def partial_suffix() -> str:
    """A temp-file suffix no other run can be using (fix 7f).

    ``.partial-<pid>-<monotonic ns in hex>``.  The process id keeps two
    concurrent programs apart and the clock reading keeps two jobs inside one
    process apart, which is what the GUI's Generate button and a future batch
    loop between them could otherwise produce: with ONE shared ``.partial``
    name two runs writing the same sheet took turns in the same file and
    published whichever interleaving of the two texts finished last, under a
    production name, having passed every gate before the collision existed.

    Only characters :data:`_PARTIAL_RE` accepts, so a leftover is still
    recognised as a temp file by the stale sweep.
    """
    return f"{PARTIAL_SUFFIX}-{os.getpid()}-{time.monotonic_ns():x}"


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
    #: Name of this run's ``superseded/<stamp>/`` folder; ``None`` means
    #: "now" (:func:`now_stamp`).  Injectable for the same reason
    #: :attr:`created` is: a test needs to know where the stale files went.
    quarantine_stamp: str | None = None
    app_name: str = APP_BANNER_NAME
    first_sheet_index: int = 1
    first_o_number: int = 1
    #: Base post table; the sheet size is taken from the nesting config.
    post_config: PostConfig | None = None

    def validate(self, sheet_count: int | None = None) -> list[str]:
        """Everything wrong with these options, in words for the UI.

        ``sheet_count`` is how many files the job will produce, which the
        caller only knows once the layout is in hand — hence optional, and
        hence :func:`build_job` calling this twice: once on the way in and once
        with the count.  It is what makes the O-number range checkable: the
        header format is ``O0001``, four digits, and ``first_o_number`` plus the
        sheet count is where the numbering ENDS (2026-08-04 review, fix 8).
        Without it, a job starting at 9995 with 12 sheets used to be refused
        per sheet, at the eleventh one, by the verifier's ``header`` rule
        complaining about ``O10005`` — a baffling place to learn that a number
        in the options dialog was too big.
        """
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
        elif sheet_count is not None and self.first_o_number + sheet_count - 1 > 9999:
            problems.append(
                f"this job needs {sheet_count} programs, so numbering them from "
                f"O{self.first_o_number:04d} would run up to "
                f"O{self.first_o_number + sheet_count - 1} - the confirmed header "
                f"format O0001 has four digits for it. Start the O-numbers at "
                f"{9999 - sheet_count + 1} or lower, or split the job"
            )
        if _BANNER_SAFE_RE.search(self.app_name or ""):
            problems.append("the banner name may not contain parentheses or newlines")
        stamp = self.quarantine_stamp
        if stamp is not None and (
            not _STAMP_SAFE_RE.match(stamp) or not stamp.strip(".")
        ):
            # It becomes a folder name inside the output folder; a separator
            # or a ".." in it would write outside the job's own folder.
            problems.append(
                f"the quarantine stamp {self.quarantine_stamp!r} must be letters, "
                f"digits, dots, dashes or underscores (it names a subfolder)"
            )
        return problems


@dataclass(frozen=True)
class SupersededFile:
    """One stale program this run moved out of the way (2026-08-04 review).

    A file gets one of these when it matches the job's own naming pattern
    (see :func:`job_file_pattern`) and is not part of this run's output:
    either this run REPLACED it with its own program for the same sheet (fix
    7d — the cross-order overwrite, and by far the commonest case: every
    regeneration of a prefix produces one of these per sheet), or the job is
    shorter than the one that filled the folder before, or the sheet of that
    name was refused this time, or it is a temp file an earlier run died in the
    middle of.  Nothing is deleted — ``old_path`` is where it was, ``new_path``
    is where it is now, and ``reason`` is what to tell the operator.
    """

    filename: str
    old_path: str
    new_path: str
    reason: str

    def describe(self) -> str:
        return f"{self.filename}: {self.reason} - moved to {self.new_path}"


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
    #: ``"wdc"``, ``"plan"``, ``"verifier"``, ``"write"`` or ``None``.
    refusal_kind: str | None = None
    #: Where an OLDER file of this sheet's name was moved to, if there was
    #: one this run did not write (2026-08-04 review).  On a refused sheet
    #: this is the whole point: the name is now empty, so nothing stale can
    #: pass for this run's program.  ``None`` means there was nothing there.
    superseded_path: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    def contents_text(self) -> str:
        return ", ".join(f"{count}x{name}" for name, count in sorted(self.contents.items()))

    def describe(self) -> str:
        if self.ok:
            where = self.path or "(not written)"
            text = f"{self.filename}: run {self.run_quantity} - {where}"
        else:
            text = f"{self.filename}: REFUSED - " + "; ".join(self.problems)
        if self.superseded_path:
            text += f"  [older file of this name moved to {self.superseded_path}]"
        return text


@dataclass
class JobResult:
    """Every sheet's outcome, plus enough context to explain the job."""

    outcomes: list[SheetOutcome]
    options: JobOptions
    output_dir: str
    dry_run: bool
    total_sheets: int = 0
    #: Stale programs moved into :attr:`quarantine_dir` after this run's
    #: writes (2026-08-04 review).  Empty on a first run into a clean
    #: folder, which is the normal case.
    superseded: list[SupersededFile] = field(default_factory=list)
    #: Loud failures of the quarantine itself: a file that matches this
    #: job's naming pattern, does not belong to this run, and is STILL in
    #: the output folder because it could not be moved (locked by the
    #: machine's file browser, read-only, permissions).  Non-empty means the
    #: folder is not safe to hand to the operator as-is, so the UI must show
    #: this — it is never swallowed.
    quarantine_problems: list[str] = field(default_factory=list)
    #: ``<output_dir>/superseded/<stamp>``, or ``None`` when there was
    #: nothing to move (or the folder could not be made).
    quarantine_dir: str | None = None

    @property
    def written(self) -> list[SheetOutcome]:
        return [o for o in self.outcomes if o.written]

    @property
    def refused(self) -> list[SheetOutcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def files(self) -> list[str]:
        return [o.path for o in self.written if o.path]

    @property
    def quarantine_ok(self) -> bool:
        """True when nothing stale was left behind (whether or not any was
        found).  False is a "do not take this folder to the machine yet"."""
        return not self.quarantine_problems

    def superseded_lines(self) -> list[str]:
        """One display line per moved file, for the UI to list verbatim."""
        return [item.describe() for item in self.superseded]

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
        if self.superseded:
            lines.append(
                f"  {len(self.superseded)} older file(s) of prefix "
                f"{self.options.prefix} moved to {self.quarantine_dir} (nothing deleted):"
            )
            lines.extend("    " + item.describe() for item in self.superseded)
        for problem in self.quarantine_problems:
            lines.append("  *** STALE FILE STILL IN THE OUTPUT FOLDER: " + problem)
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


def job_file_pattern(prefix: str) -> re.Pattern[str]:
    """Exactly the file names :func:`sheet_filename` can produce for ``prefix``.

    Used by the stale-file sweep, so it has to be exact rather than a loose
    ``R*.anc`` glob — a wrong match moves somebody else's program.  It is the
    inverse of :func:`sheet_filename` and nothing else:

    * the prefix is matched literally, and the index is exactly two digits,
      so the total digit count is ``len(prefix) + 2``.  That is what keeps
      prefixes of different lengths apart: prefix ``720`` writes
      ``R72017N.anc`` (5 digits) and prefix ``7201`` writes ``R720117N.anc``
      (6), and neither pattern can match the other's files.
    * case-insensitively, because the shop PC's filesystem is: on Windows
      ``r720101n.anc`` IS ``R720101N.anc``, so pretending otherwise would
      leave a stale file behind under an alias of its own name.

    A dry run writes the same names as production (a rehearsal of sheet 3 is
    called sheet 3), so one pattern covers both — and that is deliberate: an
    air-cut file left at a name this run refused is exactly the kind of
    thing that must not stay in the folder looking current.

    Index ``00`` is excluded by the caller: :attr:`JobOptions.first_sheet_index`
    starts at 1, so no job can ever have written one.
    """
    return re.compile(rf"^R{re.escape(str(prefix))}(\d{{2}})N\.anc$", re.IGNORECASE)


def _job_file_index(name: str, pattern: re.Pattern[str]) -> int | None:
    """The sheet index ``name`` carries, or ``None`` if it is not ours."""
    match = pattern.match(name)
    if match is None:
        return None
    index = int(match.group(1))
    return index if 1 <= index <= 99 else None


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
    # Now that the file count is known, the O-number range can be judged up
    # front instead of surfacing as a per-sheet header refusal (fix 8).
    counted = options.validate(len(sheets))
    if counted:
        raise JobError("; ".join(counted))
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

    # What this sheet OWES its program, stated from the layout rather than
    # from the plan just handed to the emitter (2026-08-04 review): the
    # verifier can only catch a missing cut if something independent tells it
    # what was supposed to be there.  One manifest per post table, because
    # the Z levels are part of "the right cut" and the dry-run twin's are
    # lifted.
    try:
        expected = expected_work(layout, real_config)
    except ValueError as exc:  # no honest statement of the work is possible
        outcome.problems = [str(exc)]
        outcome.refusal_kind = "plan"
        return
    violations = verify(real_text, real_config, expected)
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
    air_violations = verify(air_text, air_config, expected_work(layout, air_config))
    if air_violations:
        outcome.problems = [str(v) for v in air_violations]
        outcome.refusal_kind = "verifier"
        return
    outcome.text = air_text


def write_job(result, options: JobOptions) -> JobResult:
    """Generate, verify and WRITE one ``.anc`` per sheet.

    Returns a :class:`JobResult`; sheets that failed a gate are in
    :attr:`JobResult.refused` and have no CURRENT file on disk.  Raises
    :class:`JobError` only for whole-job failures (bad options, or a layout
    that does not pass :func:`~faceframe_cnc.nesting.validate_layouts`).

    Three things happen here that the module docstring's "how the files reach
    the disk" section explains in full, and that the caller has to know
    about because it has to SHOW them:

    * every program is written to a per-run temp name and read back BEFORE any
      of them is published, so a failed write cannot truncate the file that is
      there and cannot leave the job half published either (the sheet is
      reported with ``refusal_kind == "write"`` instead);
    * a file about to be REPLACED is moved into ``superseded/<stamp>/`` first,
      so the previous version of every sheet this run republishes survives —
      and a sheet whose previous version cannot be preserved is not published
      at all, because destroying it is not this module's call;
    * once the writes are done, files in the output folder that match this
      job's naming pattern but were not written by this run are moved into the
      same ``superseded/<stamp>/`` and listed on
      :attr:`JobResult.superseded`; a move that fails is a loud
      :attr:`JobResult.quarantine_problems`.

    The PDF cut-sheet report is NOT written here (that is
    :mod:`faceframe_cnc.report`, called by the GUI after this returns), so
    it is never a quarantine candidate: it does not match the pattern.
    """
    job = build_job(result, options)
    try:
        os.makedirs(job.output_dir, exist_ok=True)
    except OSError as exc:
        raise JobError(f"cannot create the output folder {job.output_dir}: {exc}") from exc

    quarantine = _Quarantine(job)
    prepared = _write_partials(job)
    _publish(job, prepared, quarantine)
    _quarantine_stale(job, quarantine)
    return job


class _Quarantine:
    """This run's ``superseded/<stamp>/``, created the first time it is needed.

    Both users of it — the publish loop preserving a file it is about to
    replace, and the sweep moving aside a file this run did not write — want
    ONE folder per run (so the evidence of a single Generate is in a single
    place) and neither wants to create it when there is nothing to put in it.
    Hence a tiny object rather than two callers passing a path around and each
    remembering whether it exists yet.
    """

    def __init__(self, job: "JobResult"):
        self._job = job
        self._folder: str | None = None
        self._failed = ""

    def preserve(self, path: str, filename: str, reason: str) -> tuple[str | None, str]:
        """Move ``path`` into the quarantine; ``(new path, problem)``."""
        if self._failed:
            return None, self._failed
        if self._folder is None:
            stamp = self._job.options.quarantine_stamp or now_stamp()
            folder, problem = _quarantine_dir(self._job.output_dir, stamp)
            if folder is None:
                self._failed = problem
                return None, problem
            self._folder = folder
            self._job.quarantine_dir = folder
        target = os.path.join(self._folder, filename)
        try:
            os.replace(path, target)
        except OSError as exc:
            return None, (
                f"could not move {path} into {self._folder} ({reason}): {exc}"
            )
        self._job.superseded.append(
            SupersededFile(
                filename=filename, old_path=path, new_path=target, reason=reason
            )
        )
        return target, ""


def _write_partials(job: JobResult) -> list[tuple[SheetOutcome, str, str]]:
    """Write every program to its own temp name and read it back.

    ``[(outcome, partial path, final path)]`` for the sheets that made it;
    a sheet that did not is refused here with ``refusal_kind == "write"`` and
    leaves nothing behind.

    Nothing is published in this phase, which is the point (fix 7e): a full
    disk on sheet 12 of 17 is found while the folder still holds exactly the
    previous job, instead of half way through replacing it.
    """
    suffix = partial_suffix()
    prepared: list[tuple[SheetOutcome, str, str]] = []
    for outcome in job.outcomes:
        if outcome.text is None:
            continue
        path = os.path.join(job.output_dir, outcome.filename)
        partial = path + suffix
        try:
            _write_partial(partial, outcome.text)
        except OSError as exc:
            # Plain ASCII: a refusal reason is printed on the PDF cut sheet.
            outcome.problems = [
                f"could not write {path}: {exc} (nothing reached that name, so any "
                f"file already there is intact - it is quarantined, not overwritten)"
            ]
            outcome.refusal_kind = "write"
            continue
        prepared.append((outcome, partial, path))
    return prepared


def _publish(
    job: JobResult,
    prepared: list[tuple[SheetOutcome, str, str]],
    quarantine: _Quarantine,
) -> None:
    """Rename every prepared partial onto its final name, in one tight loop.

    Two renames per sheet: the file already at that name (if any) goes into
    ``superseded/<stamp>/`` first, then the partial takes its place.  Nothing
    else happens in here — no generating, no verifying, no directory listing —
    because the length of this loop IS the window in which the folder is a mix
    of this job and the last one (see the module docstring's (e)).

    A previous version that cannot be preserved stops that sheet being
    published: overwriting it would be the very thing (d) is about, and a
    refusal the operator can read beats a file nobody can get back.
    """
    for outcome, partial, path in prepared:
        if os.path.exists(path):
            reason = (
                f"replaced by this run's program for sheet {outcome.sheet_index:02d}"
            )
            moved, problem = quarantine.preserve(path, outcome.filename, reason)
            if moved is None:
                job.quarantine_problems.append(
                    f"{problem} - sheet {outcome.sheet_index:02d} was NOT published, "
                    f"because replacing that file would have destroyed the only copy "
                    f"of it"
                )
                outcome.problems = [
                    f"could not write {path}: the program already there could not be "
                    f"moved into the quarantine folder first, and this run will not "
                    f"overwrite a file it cannot preserve"
                ]
                outcome.refusal_kind = "write"
                _discard(partial)
                continue
            outcome.superseded_path = moved
        try:
            os.replace(partial, path)
        except OSError as exc:
            outcome.problems = [
                f"could not write {path}: {exc} (nothing reached that name, so any "
                f"file already there is intact - it is quarantined, not overwritten)"
            ]
            outcome.refusal_kind = "write"
            _discard(partial)
            continue
        outcome.path = path
        outcome.written = True


def _discard(path: str) -> None:
    """Remove a temp file, ignoring a failure to do so.

    A leftover is not silently lost either way: the sweep at the end of the job
    recognises it and quarantines it.
    """
    try:
        os.remove(path)
    except OSError:
        pass


def _write_partial(partial: str, text: str) -> None:
    """Write ``text`` to ``partial`` and prove the bytes are on the disk.

    The 2026-08-04 review's failure mode (c): ``open(path, "w")`` truncates
    before the first byte is written, so a crash, a full disk or a machine
    switched off mid-write used to leave a production file name holding half
    a program — which starts with a valid header and ends nowhere, and the
    verifier is upstream of that, so nothing would catch it.

    Instead the text goes to a temp name in the SAME folder (only a
    same-filesystem rename is atomic) and :func:`_publish` moves it onto the
    final name in one step once the bytes are on the platter.  ``flush`` +
    ``fsync`` because the rename being atomic is no help if the CONTENT is
    still in a buffer the crash eats — and then the file is read back and
    compared, because "the write returned without an error" and "the file on
    the disk is the program the verifier passed" are not the same claim.

    The content is byte-for-byte what the first version wrote: same text,
    same ``newline=""`` (the generator emits its own CRLFs and the round-trip
    proofs depend on them surviving untranslated).
    """
    try:
        with open(partial, "w", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with open(partial, "r", newline="") as handle:
            written = handle.read()
        if written != text:
            raise OSError(
                f"the {len(text)} characters written back read as {len(written)} - "
                f"the file on the disk is not the program the verifier passed"
            )
    except OSError:
        # Leave nothing behind that a later run would have to reason about.
        # If even this fails the sweep at the end quarantines the temp file.
        _discard(partial)
        raise


def _stale_files(job: JobResult) -> list[tuple[str, str, int | None]]:
    """``[(filename, reason, sheet index)]`` for this job's leftovers.

    Everything in the output folder is compared against this job's own
    naming pattern; anything that does not match it is not this module's
    property and is never returned (see :func:`job_file_pattern`).
    """
    pattern = job_file_pattern(job.options.prefix)
    written = {o.filename.casefold() for o in job.outcomes if o.written}
    planned = {o.filename.casefold(): o for o in job.outcomes}

    stale: list[tuple[str, str, int | None]] = []
    for name in sorted(os.listdir(job.output_dir)):
        if not os.path.isfile(os.path.join(job.output_dir, name)):
            continue  # the superseded/ folder itself, among other things
        match = _PARTIAL_RE.search(name)
        base = name[: match.start()] if match else name
        index = _job_file_index(base, pattern)
        if index is None:
            continue
        if match:
            # Nothing a completed write leaves behind is a temp file: this run
            # cleans up its own, published ones are renamed away, and since fix
            # 7f the temp name carries this run's pid and clock so it cannot be
            # sharing one with anybody.  So whatever is still here belongs to a
            # run that died, and half a program sitting in the job's folder is
            # exactly what nobody should have to think about at the machine.
            stale.append((name, "half written by an interrupted earlier run", None))
            continue
        if base.casefold() in written:
            continue
        outcome = planned.get(base.casefold())
        if outcome is None:
            reason = (
                f"this job has no sheet {index:02d} - left over from an earlier, "
                f"longer run of prefix {job.options.prefix}"
            )
        else:
            reason = (
                f"sheet {index:02d} was refused this run, so this file is an "
                f"earlier run's and is not part of this job"
            )
        stale.append((name, reason, index))
    return stale


def _quarantine_dir(output_dir: str, stamp: str) -> tuple[str | None, str]:
    """Make a fresh ``superseded/<stamp>`` folder; ``(path, problem)``.

    Created only when there is something to put in it, so a normal job never
    litters the operator's folder with an empty subfolder.  Exclusive
    creation with a ``-2``, ``-3`` ... bump because two regenerations inside
    one second are perfectly possible (the GUI's Generate button is right
    there) and the second one must not be able to overwrite the first one's
    evidence.
    """
    base = os.path.join(output_dir, SUPERSEDED_DIR_NAME)
    for attempt in range(1, 100):
        candidate = os.path.join(base, stamp if attempt == 1 else f"{stamp}-{attempt}")
        try:
            os.makedirs(candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            return None, f"could not create the folder {candidate}: {exc}"
        return candidate, ""
    return None, (
        f"could not find an unused folder name under {base} for this run "
        f"(99 tried)"
    )


def _quarantine_stale(job: JobResult, quarantine: "_Quarantine") -> None:
    """Move this job's leftovers aside, and say so — never delete one.

    The 2026-08-04 review's failure modes (a) and (b): a regenerated prefix
    that used to run to a higher sheet number, and a sheet refused this run
    whose name an earlier run had already filled.  Both leave a file that
    looks exactly as current as the ones just written, and an operator
    working from the folder cannot tell.  Both are covered by one rule —
    *a file matching this job's names that this run did not write is not part
    of this job* — and the answer is quarantine, not deletion: it is somebody
    else's job and may be its only copy.

    Failures are reported, not swallowed: if the file cannot be moved the
    folder is still unsafe, and :attr:`JobResult.quarantine_problems` says
    which file and why so the UI can put it in front of the operator.
    """
    try:
        stale = _stale_files(job)
    except OSError as exc:
        job.quarantine_problems.append(
            f"could not read {job.output_dir} to look for programs left by an "
            f"earlier run, so this folder may still hold stale ones: {exc}"
        )
        return
    if not stale:
        return

    by_name = {o.filename.casefold(): o for o in job.outcomes}
    for name, reason, _index in stale:
        source = os.path.join(job.output_dir, name)
        target, problem = quarantine.preserve(source, name, reason)
        if target is None:
            job.quarantine_problems.append(
                f"{problem} - it is STILL beside this job's programs and does not "
                f"belong to it; move or delete it by hand before taking this folder "
                f"to the machine"
            )
            continue
        outcome = by_name.get(name.casefold())
        if outcome is not None:
            outcome.superseded_path = target
