"""The emitter's output as a typed event stream, not just text.

:mod:`~faceframe_cnc.post.generator` writes one ``.anc`` line at a time.  A
3D cut simulation needs the same program as *motion*: where the tool centre
was, where it went, at what feed, cutting or not, and which planned feature
each move belongs to.  Both come out of one walk of the plan — an
:class:`Event` is the pair (the rendered line, the motion it commands, if
any) — so the text can never describe a move the stream does not, and the
line numbers the verifier cites map straight onto motions through
:attr:`Motion.line_index`.

Why the text is captured beside the motion rather than re-derived from it
------------------------------------------------------------------------
The ``.anc`` grammar is modal in three separate ways: an unchanged X or Y is
omitted (but the ``X.. Y.. Z2.5`` preposition states both regardless), an
unchanged feed is omitted, and G0/G1 carry over from the line before.  Two
renderers of that grammar are two chances to drift, and the byte-for-byte
round trip of ``R710101N.anc`` / ``R720101N.anc`` / ``R730101N.anc`` is the
whole proof that this post writes what the shop's CAM wrote.  So the line and
the :class:`Motion` are built from the same numbers at the same moment, and
:func:`render` is a join — there is exactly one path from a plan to text.
``tests/test_motion.py`` cross-checks every motion against the words on its
own line, which is what holds the pair honest.

Motion classification
---------------------
:func:`classify` is a pure function of (is this a G0?, dZ) and nothing else:

===========  =========  ==================================================
G word       dZ         kind
===========  =========  ==================================================
G0           > 0        :attr:`MotionKind.RETRACT`
G0           <= 0       :attr:`MotionKind.RAPID` (XY traverses and the
                        ``G0 Z2.`` drop to the ramp plane)
G1           > 0        :attr:`MotionKind.RETRACT` (the lead-out ramp,
                        which climbs at the cutting feed)
G1           < 0        :attr:`MotionKind.PLUNGE` (straight plunges and
                        lead-in ramps alike)
G1           == 0       :attr:`MotionKind.FEED` — the at-depth cut moves
===========  =========  ==================================================

The one move that is not classified by that rule is the ``G0 Z2.5`` of the
``M59`` marker after the first perimeter loop (R710101N 230-232): the machine
is already at the rapid plane, so its dZ is zero.  It is emitted as a
:attr:`MotionKind.RETRACT` of zero displacement — the command is a retract,
and a simulation sees the no-op either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .model import FeatureRef, ToolSpec

__all__ = [
    "MotionKind",
    "Motion",
    "Event",
    "EmittedProgram",
    "classify",
    "render",
    "Z_EPS",
]

#: Z comparison tolerance, the same 1e-9 the emitter uses to decide whether
#: an axis word has changed.  Coordinates in the reference files are exact
#: multiples of 0.0001, so nothing real is ever this close to nothing.
Z_EPS = 1e-9


class MotionKind(StrEnum):
    """What a commanded move does to the material."""

    RAPID = "rapid"
    PLUNGE = "plunge"
    FEED = "feed"
    RETRACT = "retract"


def classify(
    rapid: bool, from_z: float | None, to_z: float | None
) -> MotionKind:
    """Which :class:`MotionKind` a move is, from its G word and its dZ only.

    See the module docstring for the table.  An unknown Z (``None``, the
    state before a section's ``G43``) cannot have risen, so it falls to
    :attr:`MotionKind.RAPID` for a G0; a G1 never reaches this module with an
    unknown Z, because every G1 in the grammar follows the ``G43`` that
    established one.
    """
    rises = from_z is not None and to_z is not None and to_z > from_z + Z_EPS
    if rapid:
        return MotionKind.RETRACT if rises else MotionKind.RAPID
    if rises:
        return MotionKind.RETRACT
    if from_z is not None and to_z is not None and to_z < from_z - Z_EPS:
        return MotionKind.PLUNGE
    return MotionKind.FEED


@dataclass(frozen=True)
class Motion:
    """One commanded move, in TOOL CENTRE coordinates as emitted.

    The coordinates are the numbers on the line, not part edges: the tool
    centre path already carries every pass's offset
    (:attr:`~.model.PassSpec.offset`), so a consumer that wants the finished
    edge back has to know which pass it is looking at.

    ``from_z``/``to_z`` are ``None`` only for the first spindle-on rapid of a
    section, which is commanded before the ``G43 H.. Z2.5`` that establishes
    work Z (every section is preceded by a ``G91 G28 Z0`` homing move, so the
    Z the tool starts a section at is a machine position this post never
    states).  ``feed`` is ``None`` for a G0 and otherwise the feed in force,
    which is NOT always a word on the line — F is modal, so the corner moves
    of a loop inherit the first one's cutting feed.

    ``feature`` is the planned cut this move serves; a preposition belongs to
    the feature it is positioning FOR.  ``pass_index`` is the depth pass
    within that feature's section — 0/1 for a perimeter
    (:attr:`~.model.PostConfig.perimeter_passes`) or a WDC slot
    (:attr:`~.model.WdcSlotSpec.z_cuts`) — and ``None`` for a section that
    has only one.

    ``line_index`` is the 0-based index of this move's line in the rendered
    text, which is how a verifier finding (it cites 1-based line numbers)
    names a motion.
    """

    kind: MotionKind
    from_x: float
    from_y: float
    from_z: float | None
    to_x: float
    to_y: float
    to_z: float | None
    tool: ToolSpec
    feed: float | None
    section: str
    feature: FeatureRef | None
    pass_index: int | None
    line_index: int

    @property
    def is_cut(self) -> bool:
        """Is this move removing material?

        A plunge and an at-depth feed are; a rapid and a retract are not.
        The lead-out ramp of a profile loop is a RETRACT and does cut on its
        way up, so this is the conservative half of the answer and a swept-
        volume consumer should use :attr:`kind` directly.
        """
        return self.kind in (MotionKind.PLUNGE, MotionKind.FEED)


@dataclass(frozen=True)
class Event:
    """One rendered line, and the motion it commands if it commands one.

    Header comments, the prologue/epilogue, section heads and tails, blank
    lines and the bare ``M59`` marker carry no motion.  Nor does a section
    head's ``G0 G54 G90 X.. Y..``: it restates the position the previous
    section left the machine in and moves nothing.
    """

    text: str
    line_index: int
    section: str | None = None
    motion: Motion | None = None


@dataclass(frozen=True)
class EmittedProgram:
    """The result of one emitter walk: the text and the stream it came from."""

    text: str
    events: tuple[Event, ...]

    @property
    def motions(self) -> tuple[Motion, ...]:
        return tuple(e.motion for e in self.events if e.motion is not None)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(e.text for e in self.events)


def render(events, newline: str) -> str:
    """Join ``events`` into ``.anc`` text.

    The whole renderer: every event is exactly one line, which is why
    :attr:`Event.line_index` is its position in the stream.
    """
    return newline.join(event.text for event in events) + newline
