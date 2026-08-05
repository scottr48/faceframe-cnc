"""What the sheet looks like at a cursor, part by part.

The simulation's model of the workpiece is discrete on purpose: a part is
not "35% cut", it has a set of grooves that are finished, a set of openings
that are routed, and two booleans that decide whether it is still held to
the sheet.  That is exactly the resolution the shop cares about — the whole
point of the 2026-08-03 onion-skin order is *when a slab stops being
captive* — and it is the resolution a 3D view can draw without modelling
swept volume.

The state is therefore a fold: replay the cut occurrences
(:class:`~.timeline.CutOccurrence`) that have FINISHED and nothing else.  An
occurrence is finished only when its last motion has executed, so a cursor
parked half way through a groove leaves that groove uncut — the groove is
being cut, and a half-cut groove is not a fact this model has a slot for.

Skinned versus freed
--------------------
:attr:`PartState.skinned` is "perimeter pass 0 is done" and
:attr:`PartState.freed` is "the LAST perimeter pass is done".  With the
measured two-pass table (:attr:`~faceframe_cnc.post.model.PostConfig.perimeter_passes`)
that is Z0.06 — the 0.06 onion skin still holding the part — and then
Z-0.006, which cuts through and lets the part move.  Both are read off the
program's structure rather than off Z, because a dry-run (air cut) table
mirrors every depth above the stock and still runs the same two passes: the
program's shape is the thing the operator is stepping through.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_WDC_SLOT,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .timeline import CutOccurrence

__all__ = ["PartState", "MaterialState"]


@dataclass(frozen=True)
class PartState:
    """One part's finished features, indexed the way its plan names them.

    ``grooves_cut`` holds T13 groove indices (0..3 = stile-low, rail-low,
    stile-high, rail-high — :func:`~faceframe_cnc.post.generator.groove_segment`).
    ``slots_cut`` holds ``(stile index, depth pass)`` pairs, because one T17
    slot is two bites on one centreline and a view that draws the shallow
    bite must not draw the deep one.  ``openings_cut`` are the openings T11
    has taken to Z0.15 and ``openings_detailed`` the ones T12 has finished
    through — two separate facts, because between them the opening is a
    routed pocket with a slug still in it.
    """

    grooves_cut: frozenset[int] = frozenset()
    openings_cut: frozenset[int] = frozenset()
    openings_detailed: frozenset[int] = frozenset()
    slots_cut: frozenset[tuple[int, int]] = frozenset()
    #: Perimeter pass 0 is cut: the part is scored to size but still held.
    skinned: bool = False
    #: The last perimeter pass is cut: the part is loose on the spoilboard.
    freed: bool = False

    @property
    def touched(self) -> bool:
        """Has anything at all been cut on this part yet?"""
        return bool(
            self.grooves_cut
            or self.openings_cut
            or self.openings_detailed
            or self.slots_cut
            or self.skinned
            or self.freed
        )


@dataclass(frozen=True)
class MaterialState:
    """The sheet: one :class:`PartState` per flat part, in flat-part order.

    Indexed like :meth:`~faceframe_cnc.post.model.SheetProgram.flat_parts`,
    which is the same indexing
    :attr:`~faceframe_cnc.post.model.FeatureRef.part` uses, so a feature
    reference and a state entry never need translating.

    Immutable: :meth:`apply` returns a new state.  Two states built by two
    different routes (folded forward step by step, or recomputed from
    scratch by :meth:`~.timeline.SimTimeline.state_at`) therefore compare
    equal, which is how the controller's incremental bookkeeping is held to
    its own definition.
    """

    parts: tuple[PartState, ...]

    @classmethod
    def empty(cls, part_count: int) -> "MaterialState":
        """Uncut stock: ``part_count`` parts with nothing done to any of them."""
        return cls(tuple(PartState() for _ in range(part_count)))

    def __getitem__(self, part_index: int) -> PartState:
        return self.parts[part_index]

    def __len__(self) -> int:
        return len(self.parts)

    @property
    def freed_parts(self) -> frozenset[int]:
        return frozenset(i for i, part in enumerate(self.parts) if part.freed)

    @property
    def skinned_parts(self) -> frozenset[int]:
        return frozenset(i for i, part in enumerate(self.parts) if part.skinned)

    def apply(
        self, occurrence: "CutOccurrence", last_perimeter_pass: int
    ) -> "MaterialState":
        """This state plus one FINISHED cut occurrence.

        ``last_perimeter_pass`` is the index of the deepest configured
        perimeter pass, i.e. the one that frees the part; with a single
        configured pass it is 0 and that one occurrence both skins and frees,
        which is the truth about a one-pass table.

        Raises ``ValueError`` for a section this model has no rule for: a cut
        that changes the material silently is worse than a refusal.
        """
        index = occurrence.part_index
        part = self.parts[index]
        ref = occurrence.feature
        section = occurrence.section

        if section == SECTION_PANEL:
            part = replace(part, grooves_cut=part.grooves_cut | {ref.index})
        elif section == SECTION_WDC_SLOT:
            part = replace(
                part, slots_cut=part.slots_cut | {(ref.index, occurrence.pass_index)}
            )
        elif section == SECTION_OPENINGS:
            part = replace(part, openings_cut=part.openings_cut | {ref.index})
        elif section == SECTION_DETAIL:
            part = replace(
                part, openings_detailed=part.openings_detailed | {ref.index}
            )
        elif section == SECTION_PERIMETER:
            part = replace(
                part,
                skinned=part.skinned or occurrence.pass_index == 0,
                freed=part.freed or occurrence.pass_index == last_perimeter_pass,
            )
        else:
            raise ValueError(
                f"the simulation has no material rule for the {section!r} section, "
                f"so cut {occurrence.index} ({occurrence.label}) would change the "
                f"sheet in a way nothing downstream could draw"
            )

        parts = list(self.parts)
        parts[index] = part
        return MaterialState(tuple(parts))
