"""Verifier findings, located on the playback timeline.

:mod:`faceframe_cnc.post.verifier` is this project's machine-safety
authority: it re-reads a finished ``.anc`` the way the control would and says
what is wrong with it.  This module does ONE thing — it says WHERE on the
timeline each of the authority's answers lands, so a 3D view can point at it.

The division of labour, which is not negotiable
-----------------------------------------------
*   the verifier JUDGES.  :func:`run_verifier` is one call to
    :func:`~faceframe_cnc.post.verifier.verify` and nothing else: no filter,
    no second opinion, no extra rule of this module's own;
*   this module LOCATES.  A :class:`~faceframe_cnc.post.verifier.Violation`
    cites a 1-based line number of the rendered text; a
    :class:`~faceframe_cnc.sim.SimTimeline` knows which commanded move each
    line is (:meth:`~faceframe_cnc.sim.SimTimeline.step_for_line`), which cut
    occurrence owns that move and which part that cut belongs to.  Three
    lookups, all of them the timeline's own.

The mapping is TOTAL and FAITHFUL: every violation the authority returns
becomes exactly one :class:`Finding`, in the authority's own order, and its
:attr:`Finding.display` is ``str(violation)`` verbatim.  Nothing is dropped
for being unmappable and nothing is added for looking dangerous.  A clean
program gives an empty :class:`FindingSet`, and an empty set is the only
thing that may make a sheet look clean.

Not every line commands a move
------------------------------
A violation can cite a line that moves nothing — a fixed section tail, the
``G0 G54 G90 X.. Y..`` a section head restates before its ``Tn``, a header
comment — or no line at all (``line == 0``, which is what the verifier uses
for a whole-file finding such as ``part-bounds``).  Those resolve to no step
and no cut, and they are still findings: they land in
:attr:`FindingSet.global_findings` and a view shows them without seeking
anywhere.  Silently discarding one would be the sim disagreeing with the
authority, which is the one thing it may never do.

Which part a finding names
--------------------------
:attr:`Finding.part_index` is the part whose CUT the offending line belongs
to — the part the machine was working on — and never the part a message says
would be damaged.  A WDC cone reaching into its neighbour is a finding on the
SLOT's line, so it names the WDC frame; the neighbour is named in the
verifier's own words in :attr:`~Finding.display`.  Reading a part out of a
message would mean parsing prose the verifier is free to reword, and would be
this module inventing an attribution the authority never made.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..post.verifier import Violation, verify
from .timeline import SimTimeline

__all__ = ["Finding", "FindingSet", "run_verifier"]


def run_verifier(
    timeline: SimTimeline, expected=None
) -> list[Violation]:
    """The authority's own answer about ``timeline``'s program.

    One call, with the text the timeline was built from and the post table it
    was emitted against.  ``expected`` is passed straight through: given an
    :class:`~faceframe_cnc.post.verifier.ExpectedWork` manifest the verifier
    also checks that the file holds every cut the sheet owes and nothing
    else, and that they are in a survivable order.
    """
    return verify(timeline.emitted.text, timeline.config, expected)


@dataclass(frozen=True)
class Finding:
    """One :class:`~faceframe_cnc.post.verifier.Violation` and where it is.

    :attr:`step_index` is the commanded move the violation's line commands,
    or ``None`` for a line that commands none (see the module docstring).
    :attr:`cut_index` and :attr:`part_index` come with a resolved step and
    are ``None`` without one.

    :attr:`display` is what the operator reads, and it is the verifier's
    ``__str__`` unchanged — code, message and line — because a paraphrase of
    a machine-safety finding is a different finding.
    """

    violation: Violation
    step_index: int | None
    cut_index: int | None
    part_index: int | None
    display: str

    @classmethod
    def locate(cls, violation: Violation, timeline: SimTimeline) -> "Finding":
        """``violation`` placed on ``timeline``, as far as it can be placed.

        The line number is 1-based and
        :meth:`~faceframe_cnc.sim.SimTimeline.step_for_line` takes a 0-based
        index, which is the whole reason that method exists.
        """
        step: int | None = None
        if violation.line > 0:
            step = timeline.step_for_line(violation.line - 1)
        cut_index: int | None = None
        part_index: int | None = None
        if step is not None:
            cut = timeline.cut_at_step(step)
            cut_index = cut.index
            part_index = cut.part_index
        return cls(
            violation=violation,
            step_index=step,
            cut_index=cut_index,
            part_index=part_index,
            display=str(violation),
        )

    @property
    def code(self) -> str:
        return self.violation.code

    @property
    def message(self) -> str:
        return self.violation.message

    @property
    def line(self) -> int:
        """The 1-based line the verifier cited; 0 for a whole-file finding."""
        return self.violation.line

    @property
    def is_global(self) -> bool:
        """True when this finding is about the file rather than about a move."""
        return self.step_index is None


@dataclass(frozen=True)
class FindingSet:
    """Every finding on one program, indexed the way a view reads it.

    Two sets built from the same timeline and the same violations are equal:
    the lookups below are derived, so they take no part in the comparison.
    """

    #: The findings in the verifier's own order, one per violation.
    findings: tuple[Finding, ...]
    _by_step: dict[int, tuple[Finding, ...]] = field(compare=False, repr=False)
    _by_cut: dict[int, tuple[Finding, ...]] = field(compare=False, repr=False)

    # -- construction ------------------------------------------------------

    @classmethod
    def build(cls, timeline: SimTimeline, violations) -> "FindingSet":
        """Locate every violation in ``violations`` on ``timeline``.

        The order of ``violations`` is kept exactly (the verifier sorts its
        own list by line then code), and every one of them appears, which is
        what makes this mapping something a view can be trusted to have
        shown in full.
        """
        located = tuple(Finding.locate(v, timeline) for v in violations)
        by_step: dict[int, list[Finding]] = {}
        by_cut: dict[int, list[Finding]] = {}
        for finding in located:
            if finding.step_index is not None:
                by_step.setdefault(finding.step_index, []).append(finding)
            if finding.cut_index is not None:
                by_cut.setdefault(finding.cut_index, []).append(finding)
        return cls(
            findings=located,
            _by_step={k: tuple(v) for k, v in by_step.items()},
            _by_cut={k: tuple(v) for k, v in by_cut.items()},
        )

    @classmethod
    def verified(
        cls, timeline: SimTimeline, expected=None
    ) -> "FindingSet":
        """:func:`run_verifier` on ``timeline``, located by :meth:`build`.

        The convenience a caller that wants the authority's verdict reaches
        for; nothing in the view calls it by itself, because whether a sheet
        is verified is the caller's decision to make and to show.
        """
        return cls.build(timeline, run_verifier(timeline, expected))

    @classmethod
    def empty(cls) -> "FindingSet":
        """A clean program's set: no findings, nothing flagged."""
        return cls(findings=(), _by_step={}, _by_cut={})

    # -- what a view reads -------------------------------------------------

    @property
    def all(self) -> tuple[Finding, ...]:
        """Every finding, in the verifier's order.  The findings panel's rows."""
        return self.findings

    @property
    def count(self) -> int:
        return len(self.findings)

    @property
    def global_findings(self) -> tuple[Finding, ...]:
        """The findings that reached no step (see the module docstring)."""
        return tuple(f for f in self.findings if f.step_index is None)

    def for_step(self, step_index: int) -> tuple[Finding, ...]:
        """Findings on the move at ``step_index``; empty for a clean move."""
        return self._by_step.get(step_index, ())

    def for_cut(self, cut_index: int) -> tuple[Finding, ...]:
        """Findings anywhere inside cut occurrence ``cut_index``."""
        return self._by_cut.get(cut_index, ())

    @property
    def flagged_steps(self) -> frozenset[int]:
        return frozenset(self._by_step)

    @property
    def flagged_cuts(self) -> frozenset[int]:
        """Which cut occurrences the verifier condemned."""
        return frozenset(self._by_cut)

    @property
    def flagged_parts(self) -> frozenset[int]:
        """Which parts own a condemned cut (see the module docstring)."""
        return frozenset(
            f.part_index for f in self.findings if f.part_index is not None
        )

    def __len__(self) -> int:
        return len(self.findings)

    def __bool__(self) -> bool:
        return bool(self.findings)

    def __iter__(self):
        return iter(self.findings)
