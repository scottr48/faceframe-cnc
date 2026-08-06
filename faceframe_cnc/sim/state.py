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

Skinned versus freed — and, since 2026-08-05, freed at RELEASE
--------------------------------------------------------------
:attr:`PartState.skinned` is "the part is cut to size but something is still
holding it" and :attr:`PartState.freed` is "the part is loose on the
spoilboard".  What does the holding, and therefore which occurrence flips which
boolean, depends on the program's own structure:

*   an UNTABBED program (the measured two-pass table, which is what every
    reference file was cut with): pass 0 at Z0.06 leaves the 0.06 onion skin —
    skinned — and pass 1 at Z-0.006 cuts through — freed.  An opening's dropout
    is likewise loose the moment the T12 detail pass finishes it;
*   a TAB-HELD program (a generated sheet since the 2026-08-05 amendment, Scott,
    job R0805, spec §3c/§3d): the perimeter's one through pass leaves the part
    cut to size and held by its tabs — skinned, NOT freed — and the piece comes
    loose only when the final T12 release section has milled its last tab away.
    Same for an opening dropout: :attr:`PartState.openings_detailed` means the
    kerf is cut right through and the slug is still hanging on its tabs, and
    :attr:`PartState.openings_released` is when it drops.

That distinction is the whole point of the amendment — the shop broke two frames
because a dropout was loose while the perimeter was being cut — so the
simulation states it rather than approximating it.  Which mode a program is in is
read off the program (does its plan carry a release section?), never off a
switch written down here, and never off Z: a dry-run table mirrors every depth
above the stock and still runs the same passes, and the program's shape is the
thing the operator is stepping through.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..post.model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_RELEASE,
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
    has ROUGHED and ``openings_detailed`` the ones T12 has finished
    through — two separate facts, because between them the opening is a
    routed pocket with a slug still in it.

    "Roughed" rather than "taken to Z0.15" since the 2026-08-05 max-bite
    amendment: a generated sheet's T11 roughing is a ladder of depth passes
    (Z0.45 then Z0.15), and this model holds one fact per opening, set by its
    first rung.  Unlike ``slots_cut``, whose two bites really do look different
    on the sheet (a shallow V and a deep one), the rungs of an opening ladder
    leave the same rectangle at two depths, and how deep to draw it is the view's
    business (:func:`~faceframe_cnc.gui.sim3d.viewmodel.reveals`).
    """

    grooves_cut: frozenset[int] = frozenset()
    openings_cut: frozenset[int] = frozenset()
    openings_detailed: frozenset[int] = frozenset()
    slots_cut: frozenset[tuple[int, int]] = frozenset()
    #: Perimeter pass 0 is cut: the part is scored to size but still held.
    skinned: bool = False
    #: The part is loose on the spoilboard.  In a TAB-HELD program that is when
    #: the T12 release section has taken its last tab away, not when the
    #: perimeter pass cut through (module docstring, 2026-08-05 amendment).
    freed: bool = False
    #: Which opening dropouts have actually dropped.  The same distinction one
    #: level down: an opening in :attr:`openings_detailed` is cut right through
    #: and, on a tab-held sheet, still hanging on its tabs.
    openings_released: frozenset[int] = frozenset()

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
            or self.openings_released
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
        self,
        occurrence: "CutOccurrence",
        last_perimeter_pass: int,
        tab_held: bool = False,
    ) -> "MaterialState":
        """This state plus one FINISHED cut occurrence.

        ``last_perimeter_pass`` is the index of the deepest configured
        perimeter pass — the one that cuts a part's outline right through.

        ``tab_held`` says whether this program holds its pieces with tabs and
        cuts them free in a final T12 release section (2026-08-05 amendment;
        the timeline reads it off the plan it emitted, see
        :attr:`~.timeline.SimTimeline.tab_held`).  It changes one thing, and it
        is the thing the amendment exists for: on a tab-held sheet the through
        pass no longer FREES anything, the release does.  ``False`` is every
        program written before the amendment and every reference file, where the
        through pass is the last thing holding the piece and cutting it is what
        lets go.

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
                part,
                openings_detailed=part.openings_detailed | {ref.index},
                # Untabbed, the T12 pass IS what drops the slug.
                openings_released=part.openings_released
                if tab_held
                else part.openings_released | {ref.index},
            )
        elif section == SECTION_PERIMETER:
            through = occurrence.pass_index == last_perimeter_pass
            part = replace(
                part,
                skinned=part.skinned or occurrence.pass_index == 0 or through,
                freed=part.freed or (through and not tab_held),
            )
        elif section == SECTION_RELEASE:
            # The release section frees exactly one profile per occurrence: the
            # part itself, or one of its opening dropouts (spec §3c).
            if ref.kind == "perimeter":
                part = replace(part, freed=True)
            else:
                part = replace(
                    part, openings_released=part.openings_released | {ref.index}
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
