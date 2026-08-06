"""Independent verifier for a finished ``.anc`` program.

Nothing in this module is imported by :mod:`~faceframe_cnc.post.generator`
and this module imports no emission code — it re-reads a program the way
the MACHINE would and re-derives everything it needs from the text itself,
exactly as :func:`faceframe_cnc.nesting.validate_layouts` re-derives a
layout rather than trusting the packer.  The templates below are a
deliberate second copy: if someone edits a template in the generator, this
file disagrees and the check fails, which is the point.

What is checked
---------------
``line-endings``  every line ends CRLF; no stray CR or LF; file ends CRLF.
``wrapper``       first and last line are ``%``.
``header``        the identity block and the fixed prologue match the
                  template.  Only the ``(CREATED ON ...)`` text and the
                  O-number digits may differ, plus optional extra comment
                  lines in the banner position (spec section 6).
``footer``        the closing block matches the template byte for byte.
``section``       every tool section opens ``(ROUTE TOOL #n: ...)`` /
                  ``(DIAMETER: d)`` / ``M59`` / restated position / ``Tn``
                  and closes ``M59`` / ``G80``.
``code``          every G/M word appears in the reference files.
``feed``          every F word is a feed the post table gives the tool in
                  the spindle, for the kind of move it is on: an entry
                  (plunge or ramp) move runs at that operation's
                  ``entry_feed`` and a cutting move at the ``cut_feed`` of an
                  operation that tool performs.  Modal, as the control is
                  (see below).
``spindle-speed`` every S word is the RPM the post table gives the tool in
                  the spindle, every ``M13`` states one, and every tool
                  section states one somewhere.
``z-limit``       every commanded Z lies in [z_min, z_max] (spec section 8
                  machine protection; factory defaults are the deepest cut
                  and the rapid plane measured in the references).
``rapid``         no RAPID move travels below the top of the stock (see
                  below).
``g-mode``        ``G90``/``G91``/``G28`` appear only where the post puts
                  them — inside the fixed header/footer/section-tail lines
                  (and ``G90`` on the pattern-pinned ``G0 G54 G90 X.. Y..``
                  prepositions).  The verifier reads every coordinate as
                  ABSOLUTE, so it may not accept a program that switches to
                  incremental.
``tool-comp``     every ``G43`` states an ``H`` equal to the tool in the
                  spindle, and no other line carries an ``H`` at all (bar
                  the fixed ``G90 H0 M25`` footer line).
``spindle-start`` every tool section starts the spindle (``M13``) before its
                  first feed move.
``dry-run``       only when the config says the program is an air cut
                  (``PostConfig.dry_run``): no FEED move may reach the top
                  of the stock.  The rapid ``G28 Z0`` homing moves in the
                  fixed header and footer are exempt, which is why this is
                  a separate check and not simply a raised ``z_min``.
``bounds``        every commanded X/Y lies within the sheet plus the
                  measured 0.375 trim overhang.
``part-bounds``   every recovered part footprint lies on the sheet.
``foreign-cut``   no cutting move's SWEPT WIDTH (its tool centre grown by the
                  tool's radius) enters another part's solid (footprint minus
                  its openings, so a nested inner cut free inside its host's
                  opening is legal), and a cut that goes right THROUGH the
                  sheet may not enter ANY part's solid, its own included.
                  See "The shallow-cut waiver, and why it is gone" below —
                  this rule changed on 2026-08-05 and it is the change that
                  now refuses two of the reference files.
``v-slot``        the 45-degree T17 slot, judged on the cone it actually
                  sweeps rather than on its centreline (see below).  V-bit
                  moves are excluded from ``foreign-cut`` so that one rule
                  owns them.
``geometry``      every cut happens at a Z the post knows (panel groove,
                  WDC slot pass, a configured opening or perimeter depth
                  pass, or the detail pass) and every closed loop closes.
``max-bite``      no pass removes more material than its tool is allowed to
                  in one bite — the 2026-08-05 ratified policy for the 3/8
                  compression bit (0.4", Scott: "that will help reduce the
                  load on it").  Judged both on the configured pass ladder
                  and on the ladder the FILE cuts, re-derived from the text
                  (see "The bite limit" below).  Silent for a table that
                  declares no limit, which is every measured one.
``missing-cut``   a cut the sheet's layout calls for is not in the file.
``extra-cut``     a cut is in the file that the layout does not call for.
``cut-order``     the cuts are in the file but in an order that would drop a
                  part under the spindle (see "Chronology" below).  These
                  three are checked ONLY when the caller hands :func:`verify`
                  an :class:`ExpectedWork` manifest (see below); with
                  ``expected=None`` the file is judged entirely on its own,
                  exactly as it always was.  The RELEASE section's own three
                  ordering rules are the exception — they need no manifest,
                  because the file states everything they are about.
``hold``          the hold invariant of the 2026-08-05 amendment: on a
                  tab-held sheet nothing may be fully separated before the
                  final T12 release section, and the release must then free
                  every remaining bridge exactly once, flush with the
                  finished profile, at the ratified release feeds.  See
                  "Nothing is freed early" below.  Needs no manifest.

Rapids (2026-08-04 review, fix 1)
---------------------------------
Every material rule below is about FEED moves: a ``G0`` removes no material
on purpose, so all of them skip it.  Nothing then said where a rapid may go,
and a hand-edited ``G0 Z2.5`` retract turned into ``G0 Z0.`` verified clean —
a spinning bit rapid-traversing the whole sheet at spoilboard level, which is
the single most expensive mistake this file exists to prevent.

So ``rapid`` requires the obvious thing the references all do: a rapid
retracts, traverses high and stops ABOVE the stock, and the plunge that
follows is a feed move.  Both endpoints and therefore the whole (straight)
segment must stay at or above :attr:`~.model.PostConfig.stock_top_z`.

The one honest exemption is that after a ``G28`` the control is at its own
home position and the program's absolute Z is unknown until the next Z word —
which is exactly the state the fixed ``G0 G20 G91 G28 Z0 M15`` prologue, the
``G17 G91 G28 Z0 M95`` section tails and the ``G91 G28 Z0 M15`` / ``G90 X24.
Y96.`` footer park leave it in.  Moves made while Z is unknown carry no rapid
finding; ``g-mode`` is what stops anybody manufacturing that state mid-body,
since the post only ever writes ``G28`` on those fixed lines.

Chronology (2026-08-04 review, fix 2)
-------------------------------------
``missing-cut``/``extra-cut`` match the file against the manifest as an
unordered multiset, so a program with every required cut in a catastrophic
ORDER passed: swap the two perimeter passes and the sheet is cut through
before anything is holding it; free a host before the frame nested in its
opening and the inner is loose in a hole under a moving spindle.  Both were
verified clean before this check.

Three relations, all derived from the manifest (which is derived from the
layout), all judged on the line the matched cut appears on:

a)  per part, the onion-skin perimeter pass before the through pass — for a
    config that HAS a skin pass.  The measured two-pass table does (and the
    reference programs are judged against it); a generated sheet's table has
    carried one through pass and no skin since the 2026-08-05 amendment
    (Scott, job R0805 — :func:`~.from_layout.generated_post_passes`), and then
    there is no such pair and this rule has nothing to demand.  Rules (b) and
    (c) are unaffected either way, so the order of a single-pass program is
    still checked, just not for a pass it does not run;
b)  per host/inner pair, the inner's through pass before the host's;
c)  per part, its through perimeter pass is the LAST cut of that part —
    once a part is free, nothing may cut it again.

All three hold in R710101N, R720101N and R730101N (checked from the files,
not assumed) and in every sheet the planner generates.

Rule (c) is worth restating since the 2026-08-05 amendment, because its old
name for the through pass — "the pass that frees the part" — is no longer true
of a tab-held sheet: there the through pass cuts the outline right through and
the part is still held by its tabs, and what frees it is the release section.
The rule itself is unchanged and still worth having (nothing in the MANIFEST may
touch a part after its outline has been cut through, whether or not tabs are
still holding it), and the release cuts are deliberately not among the cuts it
judges — they come after by design, which is the next section.

Nothing is freed early (2026-08-05 amendment, Scott, job R0805, spec §3d)
------------------------------------------------------------------------
Two frames came off the machine broken on 05 AUG 26 because the sheet came apart
as it was cut: every opening dropout was fully freed before the perimeter was
touched, so the T11 finished the job on a thin, loose MDF ring.  The fix is that
a generated sheet is held everywhere by 0.25" tabs and cut free by a final slow
T12 release section, and this is the rule that proves a finished program really
does that.

``hold`` re-derives the tabs INDEPENDENTLY, which is the whole point.  It does
not ask :mod:`~.tabs` where they were placed (an AST test forbids the import),
does not count them and does not reproduce a line of the placement arithmetic.
It asks one question per side of every profile the program cuts right through:
*which parts of this side did the tool take below the tab top?*  Each move is
split at the Z where it crosses :attr:`~.model.TabSpec.top_z` — the same
:func:`_z_span` the material rules split a descending ramp with — projected onto
the side it runs along and clipped to it; the union of those intervals is the
boundary that pass severed, and every GAP in it is material still holding the
piece on.  A loop that lifted nowhere reports no gaps, which is exactly the
"freed early" case, and the traverse along a tab's crest is commanded at exactly
the tab top, which is why the comparison is strictly below it.

Then, per bridge: exactly one release cut must remove it, that cut must run on
the profile's FLUSH path (the finished edge offset into the waste by the release
tool's radius — checked numerically, which is what refuses the centreline release
spec §8 forbids), reach the through depth, and run at the release pass's own
feeds.  The feeds need their own check because :func:`_check_feeds` judges a tool
against the whole set of feeds its table gives it, deliberately, and T12 now has
two operations: a release cut at the detail pass's 293 ipm would pass that rule
and is exactly what "very slowly" rules out.

Three ordering rules come with it, all judged on line numbers and all
manifest-free: the release section is LAST, every opening's tabs are released
before any perimeter's, and a nested inner frame's before its host's.

When the rule is silent, and why that is not a hole: an untabbed post table
(:attr:`~.model.PostConfig.release` unset — the MEASURED table, which is what
the reference programs are judged against) has no release pass, no tabs and no
bridges, and judging R710101N by a rule its own CAM never had would refuse three
files this repo keeps as evidence.  An AIR cut is silent for a more literal
reason: every one of its depths is above the stock, so it removes nothing, holds
nothing and frees nothing.  A generated sheet's table always configures the
release pass, so a generated program is always judged — and the manifest adds
the one fact the file cannot know on its own, that this sheet OWES a release
section at all (:attr:`ExpectedWork.release`).

The bite limit (2026-08-05, Scott, ratified policy)
--------------------------------------------------
"When the 3/8 comp (T11) is being used, only let it take a maximum of 0.4 inch
of material per pass.  That will help reduce the load on it."  The generated
post table declares that on the tool
(:attr:`~.model.ToolSpec.max_bite`, set in one place —
:func:`~.from_layout.generated_tools`) and builds its perimeter and opening
passes as ladders of equal bites underneath it.

``max-bite`` is the independent half.  It reads the limit off the table in hand
and then re-derives, from the FILE, how much material each pass actually took:
loops are grouped by the feature they leave behind (each loop's own path taken
back by its own pass offset), the group's depths are walked down from the stock
surface, and every step has to be inside the limit.  A dropped rung, a ladder
emitted in the wrong order so the deep pass cuts alone, or a post table that
asks for a 0.756 bite in the first place are all refusals; a table that declares
no limit — :func:`~.model.default_config`, and so every reference program — is
not judged, because the rule is a decision about a bit and not a fact measured
off those files.

Foreign cuts and MISSING cuts (2026-08-04 review)
-------------------------------------------------
Every check above answers "does this file do something it must not?".  None
of them can answer "does this file do everything it must?", because a
re-parse only ever sees what is there.  Three ways a program can be
catastrophically wrong and still pass every rule above, all real:

*   drop each part's full-depth perimeter pass.  With the measured two-pass
    table the onion-skin pass still recovers every part (from the shallow
    loop), every coordinate is still legal, and the machine leaves the whole
    sheet attached at 0.06 of skin; with the one-pass table a generated sheet
    now uses, the perimeter section is simply gone and nothing else notices;
*   drop one opening's T11 and T12 loops.  The verifier then reads that
    area as solid frame — and agrees with itself about it;
*   drop a WDC's T17 slots, or a frame's T13 groove.  Nothing is out of
    bounds, so nothing complains.

The fix is that the caller who KNOWS what the sheet holds says so.
:func:`expected_work` turns an optimizer :class:`SheetLayout` into an
:class:`ExpectedWork` manifest — one entry per cut the sheet owes — and
:func:`verify` then requires that the file's recovered cuts and the
manifest match, one for one, in both directions.
:func:`faceframe_cnc.post.job.build_job` passes one for the production text
and one for the dry-run text (a rehearsal missing a cut is still wrong).

The manifest is derived from the LAYOUT, not from the emitter: placements,
:func:`faceframe_cnc.geometry.compute_geometry`, and the measured tables in
:mod:`~faceframe_cnc.post.model`.  It deliberately does NOT import
:mod:`~faceframe_cnc.post.from_layout` (the planner) or
:mod:`~faceframe_cnc.post.generator` (the emitter), and re-derives the
groove and slot centrelines, the opening transform for a rotated placement
and the per-pass offsets as a second copy — for the same reason the header
templates above are a second copy.  A manifest built from the plan would
agree with the emitter by construction and could never catch the emitter
dropping a cut, which is the entire point of this check.

The V-slot rule
---------------
A 45-degree V bit does not cut a slot as wide as the tool: it cuts a cone,
so at depth ``d`` the material it removes is ``d`` wide either side of the
centreline and the cut spreads ``d`` past each END of the commanded move.
That makes the swept region of a slot pass the centreline dilated by ``d``
— which for the deep pass reaches 0.875 past the part it belongs to, twice
the ordinary trim overhang and nearly twice the part gap.

This check re-derives that region from the text: it finds the moves made
with the V bit (by the diameter the program itself declares), works out
each one's depth of cut from the commanded Z, and rejects the file if the
swept region touches the solid of any part other than the one the cut runs
down the middle of, or leaves the sheet plus its 0.375 overhang.  The
dilation is applied to the move's bounding box, which over-states the two
rounded ends by a corner each — deliberately, since erring outward is the
safe direction for a check whose job is to catch a cut nobody approved.

Cuts that never reach the stock (a dry-run file) have no cone and are
skipped, exactly as they are by ``foreign-cut``.

Where the cone may reach is the PLANNER's rule, tightened here to match it
(2026-08-04 review, fix 11): :func:`~.from_layout.plan_sheet` refuses a sweep
the sheet does not contain, while this check used to allow the ordinary 0.375
trim overhang on top.  A cone running off the sheet is a cut into the fence,
not into trim, so the looser of the two rules was simply wrong; RFK0101N — the
file the T17 grammar was measured from — keeps its slots well inside the sheet
and is unaffected.

Ramps have a Z profile (2026-08-04 review, fix 6)
-------------------------------------------------
A lead-in ramp descends 1 unit of Z per 2 of travel, so a perimeter ramp is
about 4" long and spends most of that length 1-2 INCHES ABOVE the sheet.
Judging the whole move at its minimum Z — which is what ``foreign-cut`` used
to do — refused legal work: a 24x6 valance above a 30" frame was reported as
cutting into its neighbour by a ramp segment physically above it.

So a move is now split along its own Z profile.  Z varies linearly with
travel, so "the part of this move at or below the stock top" and "the part of
it through the stock" are each one contiguous sub-segment, and each is judged
on its own — both on the tool's swept width since 2026-08-05 (below) — while
the part above the stock is not judged at all, because up there the bit is not
touching anything.

The shallow-cut waiver, and why it is gone (2026-08-05, Scott, job R0805)
------------------------------------------------------------------------
Until 05 AUG 26 the sub-segment that was in the material but NOT through the
sheet was judged on its tool CENTRE only.  The stated reason was that the
reference files do not respect a swept-width rule for shallow cuts:
``R710101N`` lines 44-47 run a T13 groove the measured 0.375 past a part edge,
which puts 0.235 of the 0.6299 panel cutter over the neighbour's stile at 0.20
depth.  That was recorded as a deliberate design decision — "it mirrors the
reference files and current production" — and it was wrong.

Job **R0805** (sheet ``R080501N.anc``, one W3330 beside one WDC2436, the parts
0.455 apart) proved it: the W3330's stile groove overran 0.375 at the
centreline, the cut reached 0.690 past the part, and the panel cutter took two
half-round bites 0.235 deep into the WDC frame's right stile.  This verifier
returned NO findings on that sheet, which is the only reason it reached the
machine.  So the waiver is closed: every sub-segment that reaches the material
is now judged on the swept width, against every FOREIGN part's solid.

The own part stays exempt for a shallow cut — that exemption is what lets a
T13 groove cut its own part, which is the whole reason it exists — and stays
NOT exempt for a through cut (2026-08-04, fix 12).  Nothing else about the
rule moved: the narrowest change that refuses R0805 is the one made, because a
swept width entering a finished neighbour is the thing that cut the divots.

The cost, stated plainly rather than papered over: ``R710101N`` and
``R730101N`` contain the same cut with the same numbers, so this rule now
refuses BOTH of them (five findings each; ``R720101N`` is unaffected, its
parts being nested rather than shoulder to shoulder).  They are pre-amendment
files and they are documentation, not output — the shop has been taking a
0.235 x 0.63 divot 0.20 deep out of a neighbour's outer stile on every one of
those sheets.  ``tests/test_post.py`` pins exactly which cuts in which files
(``LEGACY_GROOVE_FOREIGN_CUTS``) so that nothing else on a reference file can
regress unnoticed behind this.

Feeds and spindle speeds (2026-08-04, owner-approved follow-up)
--------------------------------------------------------------
Until this change the verifier read no ``F`` and no ``S`` word at all, so a
program that cut every part in exactly the right place at 900 ipm with the
spindle at 12000 rpm verified clean: right geometry, wrong machining, burnt
bit and a scorched frame.  Recorded as a follow-up when the manifest went
in, approved by the owner on 2026-08-04, and closed here.

The grammar these rules encode is the one the reference files actually use,
read off R710101N / R720101N / R730101N (and RFK0101N for T17):

*   **one S word per tool section**, on the section's first preposition —
    the line that starts the spindle (``M13``).  Its value is the tool's
    measured RPM: T13 17500, T11 16700, T12 17000, T17 16000.  R710101N has
    four sections and four S words, the two T11 sections both stating
    16700.  A section that starts the spindle without saying how fast is the
    hazard here: the control simply keeps whatever the previous section left
    in it, so a 0.2 downshear would spin up at the panel cutter's speed.
*   **two F words per feature**: the plunge or lead-in ramp states the
    operation's ``entry_feed`` and the first cutting move states its
    ``cut_feed``.  Every later move of the feature — the remaining three
    corners of a loop, the return to the lead-in point, the overshoot and
    the lead-out ramp — states no F at all and runs on the modal one.
*   the same tool legitimately uses **more than one cutting feed in one
    program**: T11 cuts openings at F545 and perimeters at F498.2 (both
    depth passes), so the rule per tool is a SET of table feeds, not a
    single number.  T11's entry feed happens to be 150 for both.

So the check re-derives, from the SAME :class:`~.model.PostConfig` it is
handed, which tool numbers perform which operations and what each
operation's two feeds are, and then judges each feed move by its own
geometry: a move that descends is an entry move and must run at an entry
feed of that tool; anything else is a cutting move and must run at one of
its cut feeds.  Nothing is hardcoded — a dry-run table, a future feed table
or a shop that re-times a tool judges itself, and the reference files are
the ground truth the rules were read off rather than an assumption they are
held to.

Modality is tracked the way the control tracks it: an F persists until
another F replaces it, across ``G0`` moves and section boundaries alike.
That is the point of the rule — the bug worth catching is not a visibly
wrong number, it is a MISSING one, where a cutting move silently inherits
the plunge feed (or the previous feature's) and nobody can see it in the
text.  A violation is therefore attributed to the line where the wrong
value takes effect on a cutting move, not to the line that stated it, and
says which line it was inherited from.

Two deliberate exemptions:

*   **rapids carry no feed check.**  A ``G0`` ignores the feed word, so an F
    on one is not itself a fault; it does change the modal value, and the
    check picks that up at the next move that FEEDS on it, which is where it
    can do harm.  No reference file puts an F on a ``G0``.
*   **height is not an exemption.**  The rule judges every feed move
    whatever Z it is at, including moves entirely above the stock, for one
    decisive reason: the dry-run twin (:func:`~.job.dry_run_config`) lifts
    every cut ABOVE the stock and changes nothing else, so a rule that only
    bit on moves reaching the material would switch itself off for exactly
    the rehearsal the operator watches to confirm a program's feeds.  In the
    production references there is no feed move fully clear of the stock
    anyway: even the lead-out ramp starts at the cut depth.

What the feed rule does NOT catch, deliberately: T11 cutting a PERIMETER at
F545, the openings' feed.  The commanded Z would identify which operation a
move belongs to and so pin the feed to one number instead of the tool's set,
but that would rebuild the Z-to-operation map inside a feed rule — and that
map is ``geometry``'s and the manifest's job.  A file with a wrong Z would
then report a cascade of feed findings about the same lines it already
reports one clear depth finding about.  Recorded here rather than fixed:
one tool, one set of its own feeds, is the rule the owner approved.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..geometry import FrameType, compute_geometry, infer_frame_type
from .model import (
    SECTION_DETAIL,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_RELEASE,
    SECTION_WDC_SLOT,
    SIDES,
    Box,
    PostConfig,
    default_config,
)

__all__ = [
    "Violation",
    "ExpectedCut",
    "ExpectedWork",
    "expected_work",
    "verify",
    "verify_file",
]

TOL = 1e-6

#: Decimals a coordinate reaches the file with.  The post prints four and
#: strips trailing zeros, so a program can only ever state a coordinate to
#: this precision — and a manifest coordinate is therefore compared at it,
#: with :data:`TOL` on top for float noise.  This is not a looser tolerance
#: than the rest of the module: it is the same 0.0001 grid the reference
#: files are drawn on (see :data:`~faceframe_cnc.post.model.EPS`).
PLACES = 4

#: Independent copy of the fixed program lines (see the module docstring).
_HEADER_TAIL = (
    "G0 G20 G91 G28 Z0 M15",
    "G90 G40 M22",
    "M88 B0",
    "M89 B0",
    "G08 P1",
    "M25",
)
_FOOTER = (
    "M59",
    "G80",
    "M22",
    "G91 G28 Z0 M15",
    "G90 H0 M25",
    "M88 B0",
    "M89 B0",
    "G91 G28",
    "G90 X24. Y96.",
    "M59",
    "M07",
    "G08 P0",
    "M30",
    "%",
)
_SECTION_TAIL = ("M59", "G80", "G17 G91 G28 Z0 M95", "M92")

#: Every G/M code that appears in R710101N / R720101N / R730101N.  A
#: generated file may not contain any other code (spec section 9: never
#: invent G/M codes).
_ALLOWED_G = {0, 1, 8, 17, 20, 28, 40, 43, 54, 80, 90, 91}
_ALLOWED_M = {7, 13, 15, 22, 25, 30, 59, 88, 89, 92, 95}

#: The one non-literal line shape the post writes a ``G90`` on: the position
#: a tool section restates before its ``Tn`` call, and the first preposition
#: of a section (the same line plus the spindle start).  ``_check_sections``
#: pins the shape; only the coordinates and the speed vary, so ``g-mode`` can
#: recognise it exactly rather than by "starts with".
#:
#: The spindle words are optional here on purpose: whether this line starts the
#: spindle and at what speed is ``spindle-start``'s and ``spindle-speed``'s
#: business, and one wrong line should draw one finding from the rule that owns
#: it, not a second confusing one from this one.
_PREPOSITION_RE = re.compile(
    r"^G0 G54 G90 X-?(?:\d+\.?\d*|\.\d+) Y-?(?:\d+\.?\d*|\.\d+)"
    r"(?: M13)?(?: S\d+)?$"
)

_WORD_RE = re.compile(r"([A-Za-z])(-?\d*\.?\d+)")
_O_RE = re.compile(r"^O\d{4} \(.+\)$")
_CREATED_RE = re.compile(r"^\(CREATED ON .*\)$")
_TOOL_RE = re.compile(r"^\(ROUTE TOOL #(\d+): .+\)$")
_DIA_RE = re.compile(r"^\(DIAMETER: (\d*\.?\d+)\)$")
_COMMENT_RE = re.compile(r"\([^)]*\)")


@dataclass(frozen=True)
class Violation:
    """One problem found in a program.  ``line`` is 1-based, 0 if global."""

    code: str
    message: str
    line: int = 0

    def __str__(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        return f"[{self.code}] {self.message}{where}"


@dataclass
class _Move:
    """One commanded move, with the modal state that was in force for it.

    ``radius`` is the tool's, ``tool`` its number, and ``feed``/``feed_line``
    the F word the control would have used and the line it last came from —
    which is the move's own line when the move states its own F, and an
    earlier one when it inherits (see the module docstring's feed section).
    ``feed`` is ``None`` only before the program's first F word.

    ``z_known`` is False while the control's absolute Z is not something this
    re-parse can know — from a ``G28`` homing move until the next commanded Z
    word.  The rapid-safety rule is the one check that must not guess there
    (see the module docstring's "Rapids" section).
    """

    rapid: bool
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    radius: float
    line: int
    tool: int = 0
    feed: float | None = None
    feed_line: int = 0
    z_known: bool = True


def verify_file(
    path: str,
    config: PostConfig | None = None,
    expected: "ExpectedWork | None" = None,
) -> list[Violation]:
    with open(path, "r", newline="") as handle:
        return verify(handle.read(), config, expected)


def verify(
    text: str,
    config: PostConfig | None = None,
    expected: "ExpectedWork | None" = None,
) -> list[Violation]:
    """Re-parse ``text`` and return every violation found (empty = good).

    ``expected`` is optional and changes nothing about the checks above it:
    with ``None`` (the default, and the only possibility for a file the shop
    already cut, which has no layout behind it) the program is judged purely
    on its own contents.  Given an :class:`ExpectedWork` manifest — see
    :func:`expected_work` and the module docstring — the file must ALSO
    contain every cut the sheet owes and nothing else.
    """
    cfg = config or default_config()
    problems: list[Violation] = []

    problems.extend(_check_line_endings(text))
    lines = text.split("\r\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        return problems + [Violation("wrapper", "the program is empty")]

    problems.extend(_check_wrapper(lines))
    problems.extend(_check_header(lines))
    problems.extend(_check_footer(lines))
    problems.extend(_check_sections(lines))

    fixed = _fixed_line_numbers(lines)
    problems.extend(_check_g_modes(lines, fixed))
    moves, motion_problems = _simulate(lines, cfg, fixed)
    problems.extend(motion_problems)
    problems.extend(_check_limits(moves, cfg))
    problems.extend(_check_rapids(moves, cfg))
    # Feeds and speeds are judged against the SAME table, and independently
    # of the expected-work manifest: a reference file with no layout behind
    # it still has to be cutting at the right feed (2026-08-04 follow-up).
    problems.extend(_check_feeds(moves, cfg))
    problems.extend(_check_speeds(lines, cfg))
    problems.extend(_check_spindle_start(lines))
    if cfg.dry_run:
        problems.extend(_check_air_cut(moves, cfg))

    parts, found, geometry_problems = _recover_parts(moves, cfg)
    problems.extend(geometry_problems)
    # The bite limit is judged against the table in hand, like every other rule
    # here, and is silent for a table that declares none (every measured one).
    problems.extend(_check_bites(found, cfg))
    problems.extend(_check_part_bounds(parts, cfg))
    problems.extend(_check_foreign_cuts(moves, parts, cfg))
    problems.extend(_check_v_slot_cuts(moves, parts, cfg))
    # The hold invariant, when the post table in hand runs a release pass.  An
    # AIR cut is exempt: it removes nothing, so it holds nothing and frees
    # nothing, and there is no bridge up there to re-derive.
    if cfg.release is not None and not cfg.dry_run:
        problems.extend(_check_hold(moves, cfg))
    if expected is not None:
        problems.extend(_check_expected_work(found, expected))

    problems.sort(key=lambda v: (v.line, v.code))
    return problems


# --------------------------------------------------------------------------
# text-level checks
# --------------------------------------------------------------------------


def _check_line_endings(text: str) -> list[Violation]:
    problems: list[Violation] = []
    if not text.endswith("\r\n"):
        problems.append(Violation("line-endings", "the file does not end with CRLF"))
    body = text.replace("\r\n", "")
    if "\n" in body:
        problems.append(Violation("line-endings", "a line ends with a bare LF"))
    if "\r" in body:
        problems.append(Violation("line-endings", "a line ends with a bare CR"))
    return problems


def _check_wrapper(lines: list[str]) -> list[Violation]:
    problems: list[Violation] = []
    if lines[0] != "%":
        problems.append(Violation("wrapper", "the program does not open with '%'", 1))
    if lines[-1] != "%":
        problems.append(
            Violation("wrapper", "the program does not close with '%'", len(lines))
        )
    return problems


def _check_header(lines: list[str]) -> list[Violation]:
    problems: list[Violation] = []
    if len(lines) < 12:
        return [Violation("header", "the program is too short to hold a header")]
    if not _O_RE.match(lines[1]):
        problems.append(
            Violation("header", f"bad O-number line: {lines[1]!r}", 2)
        )
    if not _CREATED_RE.match(lines[2]):
        problems.append(
            Violation("header", f"bad (CREATED ON ...) line: {lines[2]!r}", 3)
        )
    if lines[3] != "(MATERIAL: MDF 3/4 )":
        problems.append(Violation("header", f"bad material line: {lines[3]!r}", 4))
    if lines[4] != "(LOAD: Material face DOWN)":
        problems.append(Violation("header", f"bad load line: {lines[4]!r}", 5))

    index = 5
    while index < len(lines) and lines[index].startswith("("):
        index += 1  # optional generated-by banner comments
    for offset, expected in enumerate(_HEADER_TAIL):
        pos = index + offset
        if pos >= len(lines) or lines[pos] != expected:
            got = lines[pos] if pos < len(lines) else "<eof>"
            problems.append(
                Violation(
                    "header",
                    f"prologue line {offset + 1} should be {expected!r}, got {got!r}",
                    pos + 1,
                )
            )
    return problems


def _check_footer(lines: list[str]) -> list[Violation]:
    tail = lines[-len(_FOOTER):]
    if list(tail) != list(_FOOTER):
        first = len(lines) - len(_FOOTER)
        for offset, expected in enumerate(_FOOTER):
            if offset >= len(tail) or tail[offset] != expected:
                got = tail[offset] if offset < len(tail) else "<eof>"
                return [
                    Violation(
                        "footer",
                        f"footer line {offset + 1} should be {expected!r}, got {got!r}",
                        first + offset + 1,
                    )
                ]
    return []


def _check_sections(lines: list[str]) -> list[Violation]:
    problems: list[Violation] = []
    heads = [i for i, line in enumerate(lines) if line.startswith("(ROUTE TOOL")]
    if not heads:
        return [Violation("section", "the program contains no tool section")]
    for pos, head in enumerate(heads):
        match = _TOOL_RE.match(lines[head])
        if not match:
            problems.append(
                Violation("section", f"bad tool header: {lines[head]!r}", head + 1)
            )
            continue
        number = int(match.group(1))
        if head == 0 or lines[head - 1] != "":
            problems.append(
                Violation(
                    "section",
                    "a tool section must be preceded by one blank line",
                    head + 1,
                )
            )
        if not _DIA_RE.match(lines[head + 1]):
            problems.append(
                Violation("section", f"bad diameter line: {lines[head + 1]!r}", head + 2)
            )
        if lines[head + 2] != "M59":
            problems.append(
                Violation("section", "a tool section must open with M59", head + 3)
            )
        if not lines[head + 3].startswith("G0 G54 G90 X"):
            problems.append(
                Violation(
                    "section",
                    "a tool section must restate the current position before the "
                    "tool call",
                    head + 4,
                )
            )
        if lines[head + 4] != f"T{number}":
            problems.append(
                Violation(
                    "section",
                    f"tool section #{number} calls {lines[head + 4]!r}",
                    head + 5,
                )
            )
        end = heads[pos + 1] - 1 if pos + 1 < len(heads) else len(lines) - len(_FOOTER) + 2
        tail = lines[end - 4 : end] if pos + 1 < len(heads) else lines[end - 2 : end]
        expected = list(_SECTION_TAIL) if pos + 1 < len(heads) else ["M59", "G80"]
        if list(tail) != expected:
            problems.append(
                Violation(
                    "section",
                    f"tool section #{number} does not close with {expected}",
                    end,
                )
            )
    return problems


# --------------------------------------------------------------------------
# motion
# --------------------------------------------------------------------------


def _fixed_line_numbers(lines: list[str]) -> set[int]:
    """1-based numbers of the lines this post writes from a FIXED template.

    The prologue, the program footer and each section's tail are byte-pinned:
    ``_check_header``/``_check_footer``/``_check_sections`` already require
    them verbatim, and they are the only places the post ever writes ``G91``,
    ``G28`` or an ``H`` word.  ``g-mode`` and ``tool-comp`` need to know WHERE
    those lines are, not merely that such text exists somewhere, or a hand
    edit could plant a copy mid-body and exempt itself.

    Only lines that actually match their template are returned, so a program
    with a broken footer gets the ``footer`` finding it deserves rather than a
    silent exemption on the mangled line.
    """
    fixed: set[int] = set()

    index = 5
    while index < len(lines) and lines[index].startswith("("):
        index += 1  # optional generated-by banner comments
    for offset, expected in enumerate(_HEADER_TAIL):
        pos = index + offset
        if 0 <= pos < len(lines) and lines[pos] == expected:
            fixed.add(pos + 1)

    first = len(lines) - len(_FOOTER)
    for offset, expected in enumerate(_FOOTER):
        pos = first + offset
        if 0 <= pos < len(lines) and lines[pos] == expected:
            fixed.add(pos + 1)

    heads = [i for i, line in enumerate(lines) if line.startswith("(ROUTE TOOL")]
    for pos, head in enumerate(heads):
        if pos + 1 >= len(heads):
            continue  # the last section runs into the footer, handled above
        end = heads[pos + 1] - 1
        for offset, expected in enumerate(_SECTION_TAIL):
            at = end - len(_SECTION_TAIL) + offset
            if 0 <= at < len(lines) and lines[at] == expected:
                fixed.add(at + 1)
    return fixed


def _check_g_modes(lines: list[str], fixed: set[int]) -> list[Violation]:
    """``G90``/``G91``/``G28`` only where the post puts them.

    This verifier reads every X/Y/Z as an ABSOLUTE coordinate, because that is
    all the post ever emits: ``G91`` appears in the fixed header, section-tail
    and footer lines and nowhere else, always paired with ``G28`` homing.  A
    ``G91`` inserted before a body loop used to be accepted (both codes are in
    the reference files, so ``code`` passed it) while every coordinate after it
    was still read as absolute — the verifier would be checking a program the
    control would not run, and the control would run an incremental runaway.

    Refusing the mode change is the honest answer rather than implementing
    incremental interpretation for a post that never writes it.  ``G28`` is on
    the same list because it is what makes the absolute Z unknown (see
    "Rapids"), so allowing it mid-body would hand the rapid rule a blind spot.
    """
    problems: list[Violation] = []
    for index, raw in enumerate(lines, start=1):
        if index in fixed:
            continue
        code = _COMMENT_RE.sub("", raw).strip()
        if not code or code == "%":
            continue
        found = sorted(
            {
                int(float(value))
                for letter, value in _WORD_RE.findall(code)
                if letter.upper() == "G" and int(float(value)) in (28, 90, 91)
            }
        )
        if not found:
            continue
        if found == [90] and _PREPOSITION_RE.match(code):
            continue  # the pattern-pinned section preposition
        problems.append(
            Violation(
                "g-mode",
                f"{', '.join('G%d' % n for n in found)} on {code!r} - this post "
                f"only sets G90/G91/G28 on its fixed header, section-tail and "
                f"footer lines, and every coordinate in this file is read as "
                f"absolute, so a mode change here means the file no longer "
                f"describes what the control would do",
                index,
            )
        )
    return problems


def _simulate(
    lines: list[str], cfg: PostConfig, fixed: set[int] | None = None
) -> tuple[list[_Move], list[Violation]]:
    """Walk the whole program as the control would and return every move."""
    problems: list[Violation] = []
    moves: list[_Move] = []
    fixed = fixed if fixed is not None else _fixed_line_numbers(lines)
    modal_rapid = True
    x = y = z = 0.0
    # A program opens by homing Z, so where the spindle IS before the first
    # commanded Z is not something this re-parse can know.
    z_known = False
    radius = 0.0
    tool_number = 0
    # Modal feed, exactly as the control holds it: set by any F word on any
    # line (a G0 ignores it for its own motion but still latches it) and
    # never cleared, not by a retract and not by a section change.
    modal_feed: float | None = None
    modal_feed_line = 0
    declared: dict[int, float] = {}
    pending_diameter: float | None = None

    for index, raw in enumerate(lines, start=1):
        dia = _DIA_RE.match(raw)
        if dia:
            pending_diameter = float(dia.group(1))
            continue
        code = _COMMENT_RE.sub("", raw).strip()
        if not code or code == "%":
            continue  # blank, comment-only, or a program wrapper line
        words = _WORD_RE.findall(code)
        if not words and code:
            problems.append(Violation("code", f"unreadable line {code!r}", index))
            continue

        new_x, new_y, new_z = x, y, z
        moved = False
        line_rapid = None
        homing = False
        commands_z = False
        comp_on = False
        h_words: list[int] = []
        for letter, value in words:
            letter = letter.upper()
            if letter == "G":
                number = int(float(value))
                if number not in _ALLOWED_G:
                    problems.append(
                        Violation("code", f"G{value} is not used by the post", index)
                    )
                if number in (0, 1):
                    line_rapid = number == 0
                if number == 28:
                    homing = True
                if number == 43:
                    comp_on = True
            elif letter == "H":
                h_words.append(int(float(value)))
            elif letter == "M":
                number = int(float(value))
                if number not in _ALLOWED_M:
                    problems.append(
                        Violation("code", f"M{value} is not used by the post", index)
                    )
            elif letter == "T":
                tool = int(float(value))
                if pending_diameter is not None:
                    declared[tool] = pending_diameter
                    pending_diameter = None
                if tool not in declared:
                    problems.append(
                        Violation(
                            "section",
                            f"T{tool} is called without a (DIAMETER: ...) comment",
                            index,
                        )
                    )
                radius = declared.get(tool, 0.0) / 2.0
                tool_number = tool
            elif letter == "F":
                modal_feed = float(value)
                modal_feed_line = index
            elif letter == "X":
                new_x = float(value)
                moved = True
            elif letter == "Y":
                new_y = float(value)
                moved = True
            elif letter == "Z":
                new_z = float(value)
                moved = True
                commands_z = True
        problems.extend(
            _tool_comp_problems(h_words, comp_on, tool_number, index, raw, fixed)
        )
        if line_rapid is not None:
            modal_rapid = line_rapid
        # A commanded Z re-establishes where the spindle is; a G28 on the same
        # line sends it home again afterwards, so homing wins.  The MOVE's Z is
        # trustworthy only when both of its ends are, which is why the state
        # before the line matters as much as the state after it.
        was_known = z_known
        if commands_z:
            z_known = True
        if homing:
            z_known = False
        if moved:
            moves.append(
                _Move(
                    modal_rapid,
                    x,
                    y,
                    z,
                    new_x,
                    new_y,
                    new_z,
                    radius,
                    index,
                    tool_number,
                    modal_feed,
                    modal_feed_line,
                    was_known and z_known,
                )
            )
            x, y, z = new_x, new_y, new_z
    return moves, problems


def _tool_comp_problems(
    h_words: list[int],
    comp_on: bool,
    tool_number: int,
    index: int,
    raw: str,
    fixed: set[int],
) -> list[Violation]:
    """``G43 Hn`` must name the tool in the spindle (2026-08-04 review, fix 3).

    ``H`` selects the tool-LENGTH offset table row.  The references pair it
    with the tool every time (``G43 H13 Z2.5`` in the T13 section), and the
    verifier read no ``H`` word at all, so ``G43 H12`` in a T11 section passed:
    the control would then hold the 3/8 compression bit at the 0.2 downshear's
    length, and every Z in the program would be wrong by the difference — a
    spoilboard strike or a sheet of onion skin, depending which way.

    So: a ``G43`` owes an ``H`` equal to the active tool, and an ``H`` anywhere
    else is refused outright.  The one ``H`` the post writes without a ``G43``
    is the fixed footer's ``G90 H0 M25``, which CANCELS the offset on the way
    out; it is exempt by position, not by value.
    """
    problems: list[Violation] = []
    if comp_on:
        if not h_words:
            problems.append(
                Violation(
                    "tool-comp",
                    f"G43 turns tool-length compensation on without an H word, so "
                    f"the control would use whatever offset row was last selected - "
                    f"T{tool_number} needs H{tool_number}",
                    index,
                )
            )
        for number in h_words:
            if number != tool_number:
                problems.append(
                    Violation(
                        "tool-comp",
                        f"G43 H{number} with T{tool_number} in the spindle - the "
                        f"tool-length offset must be the tool's own row "
                        f"(H{tool_number}), or every Z in this section is out by "
                        f"the difference between the two tools",
                        index,
                    )
                )
        return problems
    for number in h_words:
        if index in fixed:
            continue  # the fixed footer's ``G90 H0 M25`` cancels the offset
        problems.append(
            Violation(
                "tool-comp",
                f"H{number} on {raw.strip()!r} - this post states an H only on a "
                f"G43 line (where it must equal the tool number) and on the fixed "
                f"G90 H0 M25 footer line that cancels the offset",
                index,
            )
        )
    return problems


def _check_limits(moves: list[_Move], cfg: PostConfig) -> list[Violation]:
    problems: list[Violation] = []
    for move in moves:
        for z in (move.z0, move.z1):
            if z < cfg.z_min - TOL:
                problems.append(
                    Violation(
                        "z-limit",
                        f"Z{z} is below the {cfg.z_min} floor - spoilboard strike",
                        move.line,
                    )
                )
                break
            if z > cfg.z_max + TOL:
                problems.append(
                    Violation(
                        "z-limit",
                        f"Z{z} is above the {cfg.z_max} ceiling - overtravel",
                        move.line,
                    )
                )
                break
        low_x = -cfg.overhang
        low_y = -cfg.overhang
        high_x = cfg.sheet_width + cfg.overhang
        high_y = cfg.sheet_length + cfg.overhang
        for px, py in ((move.x0, move.y0), (move.x1, move.y1)):
            if not (low_x - TOL <= px <= high_x + TOL) or not (
                low_y - TOL <= py <= high_y + TOL
            ):
                problems.append(
                    Violation(
                        "bounds",
                        f"({px}, {py}) is outside the {cfg.sheet_width}x"
                        f"{cfg.sheet_length} sheet plus its {cfg.overhang} overhang",
                        move.line,
                    )
                )
                break
    return problems


def _check_rapids(moves: list[_Move], cfg: PostConfig) -> list[Violation]:
    """No rapid may travel below the top of the stock (module docstring).

    Two shapes, which is the whole rule:

    *   a rapid may not move in X or Y while it is below the stock top — that
        is the bit dragged sideways through the sheet at cutting depth;
    *   a rapid may not DESCEND to below the stock top — that is the bit driven
        into the material at rapid speed instead of fed in at the operation's
        entry feed.

    Straight moves, so Z is linear and checking the two ends checks the whole
    segment.  What is deliberately NOT a finding is the ``G0 Z2.5`` retract that
    every feature ends with: it starts at the bottom of the cut, where the bit
    already is, and goes straight up without moving in XY.  A rule that only
    looked at the lowest Z the move touches would refuse every reference file.

    Moves made while the absolute Z is unknown — after a ``G28`` and before the
    next Z word, i.e. exactly the fixed homing lines and the section
    prepositions that follow them — are exempt, because there is nothing honest
    to compare (see the module docstring's "Rapids").
    """
    problems: list[Violation] = []
    for move in moves:
        if not move.rapid or not move.z_known:
            continue
        low = min(move.z0, move.z1)
        if low >= cfg.stock_top_z - TOL:
            continue
        travels = abs(move.x1 - move.x0) > TOL or abs(move.y1 - move.y0) > TOL
        descends = move.z1 < cfg.stock_top_z - TOL and move.z1 < move.z0 - TOL
        if travels:
            problems.append(
                Violation(
                    "rapid",
                    f"a rapid (G0) traverses x[{min(move.x0, move.x1):.4f}, "
                    f"{max(move.x0, move.x1):.4f}] y[{min(move.y0, move.y1):.4f}, "
                    f"{max(move.y0, move.y1):.4f}] while as low as Z{low}, below the "
                    f"Z{cfg.stock_top_z} top of the stock - a spinning bit dragged "
                    f"sideways through the sheet at rapid speed. A rapid must "
                    f"retract first, traverse high, and only then come down",
                    move.line,
                )
            )
        elif descends:
            problems.append(
                Violation(
                    "rapid",
                    f"a rapid (G0) plunges from Z{move.z0} to Z{move.z1}, below the "
                    f"Z{cfg.stock_top_z} top of the stock - the descent into a cut "
                    f"is a G1 at the operation's entry feed, never a G0",
                    move.line,
                )
            )
    return problems


def _check_spindle_start(lines: list[str]) -> list[Violation]:
    """Every tool section starts the spindle before it feeds (fix 4).

    :func:`_check_speeds` already catches a wrong ``S``, an ``M13`` with no
    ``S`` and a section that states no speed at all — but not the plainest
    failure of the lot: DELETE the ``M13`` and keep the ``S`` word, and the
    section verified clean while the control plunged a stationary bit into
    three-quarter MDF at 150 ipm.  The S word only loads the speed register;
    ``M13`` is what turns the spindle on.

    Judged per section on the first FEED move, tracking G0/G1 modally the way
    the control does, and reported once per section: "this section never starts
    the spindle" is one fact about the section.
    """
    problems: list[Violation] = []
    head = 0
    tool_number = 0
    started = False
    reported = False
    modal_rapid = True

    for index, raw in enumerate(lines, start=1):
        if _TOOL_RE.match(raw):
            head, tool_number, started, reported = index, 0, False, False
            modal_rapid = True
            continue
        code = _COMMENT_RE.sub("", raw).strip()
        if not code or code == "%":
            continue
        moved = False
        line_rapid = None
        for letter, value in _WORD_RE.findall(code):
            letter = letter.upper()
            if letter == "T":
                tool_number = int(float(value))
            elif letter == "M" and int(float(value)) == 13:
                started = True
            elif letter == "G":
                number = int(float(value))
                if number in (0, 1):
                    line_rapid = number == 0
            elif letter in ("X", "Y", "Z"):
                moved = True
        if line_rapid is not None:
            modal_rapid = line_rapid
        if head and moved and not modal_rapid and not started and not reported:
            reported = True
            problems.append(
                Violation(
                    "spindle-start",
                    f"the T{tool_number} section feeds into the material before any "
                    f"M13 starts the spindle - an S word only loads the speed, so "
                    f"this would drive a stationary bit into the stock",
                    index,
                )
            )
    return problems


def _check_air_cut(moves: list[_Move], cfg: PostConfig) -> list[Violation]:
    """Dry run: no feed move may reach the stock (see the module docstring)."""
    problems: list[Violation] = []
    for move in moves:
        if move.rapid:
            continue
        low = min(move.z0, move.z1)
        if low < cfg.stock_top_z - TOL:
            problems.append(
                Violation(
                    "dry-run",
                    f"a feed move reaches Z{low}, at or below the Z{cfg.stock_top_z} "
                    f"top of the stock - this file is marked as an air cut",
                    move.line,
                )
            )
    return problems


# --------------------------------------------------------------------------
# feeds and spindle speeds (2026-08-04, owner-approved follow-up)
# --------------------------------------------------------------------------
#
# See the module docstring's "Feeds and spindle speeds" section for the
# grammar these two rules encode and for the two deliberate exemptions
# (rapids, and NOT exempting moves above the stock).


#: What each post-table section's operation is called in a refusal, without
#: its tool number — that is prefixed from the table, so a shop that moves an
#: operation to another tool gets a message that still reads true.
_OPERATION_NAMES = {
    SECTION_PANEL: "panel groove",
    SECTION_WDC_SLOT: "45-degree stile slot",
    SECTION_OPENINGS: "opening through-cut",
    SECTION_DETAIL: "opening finish pass",
    SECTION_PERIMETER: "perimeter pass",
    SECTION_RELEASE: "tab release cut",
}


def _operations(cfg: PostConfig) -> list[tuple[int, str, object]]:
    """``[(tool number, what it is, the spec carrying its two feeds)]``.

    Every operation the post can emit, taken from the config it was handed
    and nowhere else.  :class:`~.model.PanelSpec`,
    :class:`~.model.WdcSlotSpec` and :class:`~.model.PassSpec` all carry
    ``entry_feed`` and ``cut_feed``, which is what lets one walk cover the
    lot; a section with no tool in the table contributes nothing, so a
    cut-down table (one with no T17, say) simply has no T17 rule rather than
    an empty one that would accept anything.
    """
    out: list[tuple[int, str, object]] = []

    def add(section: str, spec, suffix: str = "") -> None:
        tool = cfg.tools.get(section)
        if tool is None:
            return
        out.append(
            (tool.number, f"T{tool.number} {_OPERATION_NAMES[section]}{suffix}", spec)
        )

    add(SECTION_PANEL, cfg.panel)
    add(SECTION_WDC_SLOT, cfg.wdc_slot)
    # Both T11 operations can be a ladder of depth passes (the 2026-08-05
    # max-bite amendment), and every rung is an operation with its own two
    # feeds — which in practice are the same two on every rung, since a ladder
    # reuses a measured pass's feeds rather than inventing one.
    openings_total = len(cfg.openings_passes)
    for position, spec in enumerate(cfg.openings_passes):
        add(
            SECTION_OPENINGS,
            spec,
            "" if openings_total == 1 else f" {position + 1} of {openings_total}",
        )
    add(SECTION_DETAIL, cfg.detail_pass)
    total = len(cfg.perimeter_passes)
    for position, spec in enumerate(cfg.perimeter_passes):
        add(SECTION_PERIMETER, spec, f" {position + 1} of {total}")
    # The 2026-08-05 release pass, and only when the table configures one: the
    # measured table names T12 for the section (one tool, named once) but runs no
    # release, and a rule that read the tool alone would quietly allow the
    # release feeds in a reference program that has no release section.
    if cfg.release is not None:
        add(SECTION_RELEASE, cfg.release)
    return out


#: ``{feed -> the operations that run at it}``.  The operations ride along so
#: a refusal can say what the table MEANS and not just what number it holds.
_FeedUses = dict[float, list[str]]


def _tool_feeds(cfg: PostConfig) -> dict[int, tuple[_FeedUses, _FeedUses]]:
    """Per tool number, ``(entry feeds, cutting feeds)`` -> which operations.

    A SET of feeds per tool per kind of move, because one tool honestly runs
    at more than one: T11 cuts openings at F545 and perimeters at F498.2 in
    the same program.  The operations behind each number are kept so a
    refusal can say what the table means, not just what it says.
    """
    table: dict[int, tuple[_FeedUses, _FeedUses]] = {}
    for number, what, spec in _operations(cfg):
        entry, cut = table.setdefault(number, ({}, {}))
        entry.setdefault(round(float(spec.entry_feed), PLACES), []).append(what)
        cut.setdefault(round(float(spec.cut_feed), PLACES), []).append(what)
    return table


def _fword(value: float) -> str:
    """A feed the way the program states it (four decimals, zeros stripped).

    Second copy of :func:`~.generator.fmt`, for the same reason every other
    template in this module is one — and so a refusal quotes the number in
    the form the operator will find in the file: ``F490.``, not ``F490.0``.
    """
    return f"{round(value, PLACES):.4f}".rstrip("0")


def _feed_choices(feeds: _FeedUses) -> str:
    """``F490. (T13 panel groove)`` / ``F498.2 (...) or F545. (...)``."""
    if not feeds:
        return "nothing - the post table gives it no feed at all"
    return " or ".join(
        f"F{_fword(value)} ({', '.join(feeds[value])})" for value in sorted(feeds)
    )


def _check_feeds(moves: list[_Move], cfg: PostConfig) -> list[Violation]:
    """Every feed move runs at a feed ``cfg`` gives its tool for that move.

    The move's own geometry decides which of the two feeds applies: a move
    that DESCENDS is the plunge or lead-in ramp of a feature and owes an
    ``entry_feed``; anything else — a lateral cut, the overshoot, the
    climbing lead-out that is still in the material when it starts — runs on
    the ``cut_feed`` the first cutting move of the feature set, and owes one
    of the tool's.  Rapids are skipped: a ``G0`` ignores F.

    A tool the table does not describe is skipped here and reported once by
    :func:`_check_speeds` instead, because "I cannot judge this tool" is one
    finding about the tool, not one per move it makes.
    """
    problems: list[Violation] = []
    table = _tool_feeds(cfg)
    for move in moves:
        if move.rapid:
            continue
        feeds = table.get(move.tool)
        if feeds is None:
            continue
        descending = move.z1 < move.z0 - TOL
        allowed = feeds[0] if descending else feeds[1]
        what = "a plunge/ramp into the cut" if descending else "a cutting move"
        kind = "entry" if descending else "cutting"
        if move.feed is None:
            problems.append(
                Violation(
                    "feed",
                    f"{what} with T{move.tool} in the spindle runs before the "
                    f"program states any F word, so it would run at whatever feed "
                    f"the last program left in the control - the post table's "
                    f"{kind} feed for T{move.tool} is {_feed_choices(allowed)}",
                    move.line,
                )
            )
            continue
        found = round(move.feed, PLACES)
        if any(abs(found - value) <= TOL for value in allowed):
            continue
        origin = (
            ""
            if move.feed_line == move.line
            else f", inherited from the F word on line {move.feed_line}"
        )
        problems.append(
            Violation(
                "feed",
                f"{what} with T{move.tool} in the spindle runs at "
                f"F{_fword(move.feed)}{origin} - the post table's {kind} feed for "
                f"T{move.tool} is {_feed_choices(allowed)}",
                move.line,
            )
        )
    return problems


def _check_speeds(lines: list[str], cfg: PostConfig) -> list[Violation]:
    """Every S word is the RPM the post table gives the tool in the spindle.

    Its own small walk of the text rather than a hitch-hike on
    :func:`_simulate`: the rule is purely about words (which tool is called,
    what speed is commanded, where the spindle is started) and owes nothing
    to where the machine is, so keeping it separate keeps both readable.

    Three findings, all of them things every reference file satisfies: an S
    that is not the table's speed for the tool; an ``M13`` that starts the
    spindle without saying how fast, which leaves the previous section's RPM
    in it; and a tool section that states no speed anywhere.  Plus the honest
    admission that a tool the table does not describe cannot be judged at all
    — which is also what turns off :func:`_check_feeds` for it.
    """
    problems: list[Violation] = []
    speeds: dict[int, set[int]] = {}
    for tool in cfg.tools.values():
        speeds.setdefault(tool.number, set()).add(int(tool.speed))

    head = 0  # line of the section head currently open, 0 for none
    tool_number = 0
    stated = 0  # S words seen since the section head

    def close_section() -> None:
        if head and tool_number and not stated:
            wanted = speeds.get(tool_number)
            problems.append(
                Violation(
                    "spindle-speed",
                    f"the T{tool_number} section states no spindle speed, so the "
                    f"spindle would keep whatever the previous section left it at - "
                    f"the post table runs T{tool_number} at "
                    f"{_speed_choices(wanted)}",
                    head,
                )
            )

    for index, raw in enumerate(lines, start=1):
        if _TOOL_RE.match(raw):
            close_section()
            head, tool_number, stated = index, 0, 0
            continue
        code = _COMMENT_RE.sub("", raw).strip()
        if not code or code == "%":
            continue
        words = _WORD_RE.findall(code)
        commanded: int | None = None
        starts_spindle = False
        for letter, value in words:
            letter = letter.upper()
            if letter == "T":
                tool_number = int(float(value))
                if tool_number not in speeds:
                    problems.append(
                        Violation(
                            "spindle-speed",
                            f"T{tool_number} is not a tool in the post table, so "
                            f"nothing here can say what speed or feed it should "
                            f"run at",
                            index,
                        )
                    )
            elif letter == "M" and int(float(value)) == 13:
                starts_spindle = True
            elif letter == "S":
                commanded = int(float(value))
        if (commanded is not None or starts_spindle) and not tool_number:
            # Structurally impossible in a file that passes ``section`` (which
            # requires the Tn call before a section's first feature), so this
            # is a guard rather than a rule: without a tool there is no table
            # row to judge against, and saying "T0" would be nonsense.
            stated += 1
            problems.append(
                Violation(
                    "spindle-speed",
                    "the spindle is commanded before any tool is called, so nothing "
                    "here says whose speed it should be",
                    index,
                )
            )
        elif commanded is not None:
            stated += 1
            wanted = speeds.get(tool_number)
            if wanted is None:
                pass  # already reported against the T word above
            elif commanded not in wanted:
                problems.append(
                    Violation(
                        "spindle-speed",
                        f"S{commanded} with T{tool_number} in the spindle - the "
                        f"post table runs T{tool_number} at {_speed_choices(wanted)}",
                        index,
                    )
                )
        elif starts_spindle:
            wanted = _speed_choices(speeds.get(tool_number))
            problems.append(
                Violation(
                    "spindle-speed",
                    f"the spindle is started (M13) with no S word, so it would run "
                    f"at whatever speed the previous section left it at - the post "
                    f"table runs T{tool_number} at {wanted}",
                    index,
                )
            )
    close_section()
    return problems


def _speed_choices(wanted: set[int] | None) -> str:
    if not wanted:
        return "no speed at all (it is not in the tool table)"
    return " or ".join(f"S{value}" for value in sorted(wanted))


# --------------------------------------------------------------------------
# geometry recovered from the cuts themselves
# --------------------------------------------------------------------------


@dataclass
class _RecoveredPart:
    box: Box
    openings: list[Box]

    def solids(self) -> list[Box]:
        bands = [self.box]
        for opening in self.openings:
            nxt: list[Box] = []
            for band in bands:
                nxt.extend(_subtract(band, opening))
            bands = nxt
        return bands


def _subtract(box: Box, hole: Box) -> list[Box]:
    if not box.overlaps(hole):
        return [box]
    out: list[Box] = []
    if hole.y0 > box.y0 + TOL:
        out.append(Box(box.x0, box.y0, box.x1, hole.y0))
    if hole.y1 < box.y1 - TOL:
        out.append(Box(box.x0, hole.y1, box.x1, box.y1))
    y0 = max(box.y0, hole.y0)
    y1 = min(box.y1, hole.y1)
    if y1 > y0 + TOL:
        if hole.x0 > box.x0 + TOL:
            out.append(Box(box.x0, y0, hole.x0, y1))
        if hole.x1 < box.x1 - TOL:
            out.append(Box(hole.x1, y0, box.x1, y1))
    return out


@dataclass(frozen=True)
class _FoundCut:
    """One cutting run the file actually makes, kept WITH its depth.

    ``kind`` is the section the Z word puts it in (``"groove"``, ``"slot"``,
    ``"opening"``, ``"detail"``, ``"perimeter"``) and ``path`` is the extent
    of the TOOL CENTRE — the closed rectangle for a profile loop, the
    degenerate box of the segment for a straight cut.  Tool-centre space,
    not feature space, because that is what the file literally states: the
    offsets are then only applied on the manifest side, once.
    """

    kind: str
    z: float
    path: Box
    line: int


def _cut_runs(moves: list[_Move]) -> list[list[_Move]]:
    runs: list[list[_Move]] = []
    current: list[_Move] = []
    for move in moves:
        if move.rapid:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(move)
    if current:
        runs.append(current)
    return runs


def _known_depths(cfg: PostConfig) -> dict[float, tuple[str, float]]:
    """``{machine Z -> (what a cut at it is, that pass's tool-centre offset)}``.

    The only thing a re-parse has to go on when it meets a cutting run is the Z
    word, so this is the table that turns a Z into a kind — and, for the profile
    kinds, into the offset that takes the cut path back to the feature it was
    cutting.  Straight cuts (the T13 groove, the T17 slot passes) carry no
    footprint to recover and so no offset; where they are allowed to REACH is
    :func:`_check_v_slot_cuts` / :func:`_check_foreign_cuts`.

    Both T11 operations can be a LADDER of depth passes since the 2026-08-05
    max-bite amendment, so both are walked; the rungs of one ladder never share a
    Z with the rungs of the other (openings 0.45/0.15, perimeters 0.372/-0.006),
    which is what lets one flat mapping name them all.
    """
    known = {round(cfg.panel.z_cut, 9): ("groove", 0.0)}
    for z_cut in cfg.wdc_slot.z_cuts:
        known[round(z_cut, 9)] = ("slot", 0.0)
    for spec in cfg.openings_passes:
        known[round(spec.z_cut, 9)] = ("opening", spec.offset)
    known[round(cfg.detail_pass.z_cut, 9)] = ("detail", cfg.detail_pass.offset)
    for spec in cfg.perimeter_passes:
        known[round(spec.z_cut, 9)] = ("perimeter", spec.offset)
    return known


#: Which post-table section a recovered profile cut belongs to, and so whose
#: tool (and whose :attr:`~.model.ToolSpec.max_bite`) judges it.
_SECTION_OF_KIND = {
    "opening": SECTION_OPENINGS,
    "detail": SECTION_DETAIL,
    "perimeter": SECTION_PERIMETER,
}


def _check_bites(
    found: list[_FoundCut], cfg: PostConfig
) -> list[Violation]:
    """No cutting pass may remove more material than its tool is allowed to.

    RATIFIED POLICY, Scott 2026-08-05: the 3/8 compression bit takes at most
    :attr:`~.model.ToolSpec.max_bite` (0.4) of material per pass, "to reduce the
    load on it" — see :data:`~.from_layout.T11_MAX_BITE`.  A tool whose table
    entry declares no limit is not judged here at all, which is every tool in
    the MEASURED table and so every reference program.

    Two halves, and the second is the one that matters:

    *   the CONFIGURED ladder.  Each of a tool's configured depth passes must be
        within the limit of the floor the pass before it left, starting from the
        top of the stock.  A post table that asks for a 0.756 bite is refused
        before its geometry is even looked at, once, globally;
    *   the ladder the FILE actually cuts, re-derived from the text: every
        closed loop is grouped with the other loops that cut the SAME feature
        (its own path taken back by its own pass offset — the same recovery
        :func:`_recover_parts` does), and the group is then walked IN THE ORDER
        THE FILE CUTS IT, each rung's bite measured from the floor the ones
        before it left.  That is what catches an emitter that dropped a rung, or
        emitted the ladder deep-rung-first so that one pass takes the whole cut
        and the other takes nothing.

    A pass ABOVE the floor it starts from has a negative bite and so can never
    exceed the limit, which is what makes an air cut
    (:func:`~.job.dry_run_config`, every depth mirrored above the surface) silent
    here without this rule needing to know it is one: up there the bit removes
    nothing, and there is no load to reduce.
    """
    problems: list[Violation] = []
    top = cfg.stock_top_z

    ladders = [
        (SECTION_OPENINGS, "opening", cfg.openings_passes),
        (SECTION_PERIMETER, "perimeter", cfg.perimeter_passes),
        (SECTION_DETAIL, "opening finish", (cfg.detail_pass,)),
        (SECTION_PANEL, "panel groove", (cfg.panel,)),
    ]
    for section, what, passes in ladders:
        tool = cfg.tools.get(section)
        if tool is None or tool.max_bite is None:
            continue
        floor = top
        for position, spec in enumerate(passes):
            bite = floor - spec.z_cut
            if bite > tool.max_bite + TOL:
                problems.append(
                    Violation(
                        "max-bite",
                        f"the post table's {what} pass {position + 1} of "
                        f"{len(passes)} cuts from Z{_zword(floor)} to "
                        f"Z{_zword(spec.z_cut)}, a bite of {bite:g} - T{tool.number} "
                        f"is allowed at most {tool.max_bite:g} of material per pass, "
                        f"so this table would have to be cut in more passes",
                    )
                )
            floor = min(floor, spec.z_cut)

    known = _known_depths(cfg)
    #: ``(kind, recovered feature) -> [(z, line)]``.  Two loops belong to one
    #: feature when the rectangles they leave behind are the same, which is the
    #: only sense in which the FILE says two passes are the same cut.
    groups: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for item in found:
        # A straight cut (groove, slot) is one bite by construction, and a
        # RELEASE cut mills a tab that earlier passes already cut round — neither
        # is a rung of a depth ladder, so only the profile kinds are grouped.
        if item.kind not in _SECTION_OF_KIND:
            continue
        tool = cfg.tools.get(_SECTION_OF_KIND[item.kind])
        if tool is None or tool.max_bite is None:
            continue
        entry = known.get(round(item.z, 9))
        if entry is None:
            continue  # ``geometry`` has already refused this depth
        feature = item.path.grow(-entry[1]).rounded()
        groups.setdefault((item.kind, repr(feature)), []).append((item.z, item.line))

    for (kind, feature), rungs in sorted(groups.items()):
        tool = cfg.tools[_SECTION_OF_KIND[kind]]
        floor = top
        # In the order the FILE cuts them, not in depth order: how much material
        # a pass removes is decided by what was already gone when it ran, so a
        # ladder emitted deep-rung-first has one pass taking the whole cut and
        # one taking nothing.  Line number is the only sense in which a re-parse
        # sees time, and it is the same sense ``cut-order`` uses.
        for z_cut, line in sorted(rungs, key=lambda pair: pair[1]):
            bite = floor - z_cut
            if bite > tool.max_bite + TOL:
                problems.append(
                    Violation(
                        "max-bite",
                        f"a {_KIND_NAMES.get(kind, kind)} takes {bite:g} of material "
                        f"in one pass (from Z{_zword(floor)} down to "
                        f"Z{_zword(z_cut)} on {feature}) - T{tool.number} is allowed "
                        f"at most {tool.max_bite:g} per pass, so this cut owes "
                        f"{math.ceil(bite / tool.max_bite)} passes and the program "
                        f"makes fewer",
                        line,
                    )
                )
            floor = min(floor, z_cut)
    return problems


def _recover_parts(
    moves: list[_Move], cfg: PostConfig
) -> tuple[list[_RecoveredPart], list[_FoundCut], list[Violation]]:
    """Rebuild part footprints and openings from the cut coordinates alone.

    Two things come back, and the difference matters (2026-08-04 review).
    The FOOTPRINTS are deduplicated across depths — a part's two perimeter
    passes recover the same rectangle, and the foreign-cut and v-slot rules
    want one part, not two.  The INVENTORY (:class:`_FoundCut`, second
    return value) is not: it is every cutting run the file makes, kept with
    the Z it was made at, because "the second pass is missing" is invisible
    to anything that has already collapsed the two passes into one
    rectangle.  That collapse is exactly what hid a dropped through pass
    before this was split in two.
    """
    problems: list[Violation] = []
    known = _known_depths(cfg)

    found: list[_FoundCut] = []
    boxes: dict[str, Box] = {}
    opening_boxes: dict[str, Box] = {}
    for run in _cut_runs(moves):
        depth = min(move.z1 for move in run)
        key = round(depth, 9)
        if key not in known:
            problems.append(
                Violation(
                    "geometry",
                    f"a cut runs at Z{depth}, which is not a depth this post uses "
                    f"{sorted(known)}",
                    run[0].line,
                )
            )
            continue
        kind, offset = known[key]
        if kind == "detail" and cfg.release is not None and _is_release_run(run):
            # A RELEASE cut (2026-08-05 amendment §3c) runs at the T12 detail
            # pass's own through depth — they are one number
            # (:attr:`~.model.PostConfig.release_z`) — so the Z word cannot tell
            # the two apart and the SHAPE does: a detail pass is a closed
            # eight-move loop and a release cut is a plunge plus one straight
            # move.  Nothing else in this post writes that shape at that depth.
            found.append(_FoundCut("release", depth, _straight_extent(run), run[0].line))
            continue
        if kind in ("groove", "slot"):
            found.append(_FoundCut(kind, depth, _straight_extent(run), run[0].line))
            continue
        box = _closed_loop_box(run)
        if box is None:
            problems.append(
                Violation("geometry", "a profile loop does not close", run[0].line)
            )
            continue
        found.append(_FoundCut(kind, depth, box, run[0].line))
        recovered = box.grow(-offset).rounded()
        target = boxes if kind == "perimeter" else opening_boxes
        target[repr(recovered)] = recovered

    parts = [_RecoveredPart(box=box, openings=[]) for box in boxes.values()]
    for opening in opening_boxes.values():
        owner = None
        for part in parts:
            if part.box.contains(opening, TOL):
                if owner is None or (part.box.width * part.box.height) < (
                    owner.box.width * owner.box.height
                ):
                    owner = part
        if owner is not None:
            owner.openings.append(opening)
    return parts, found, problems


def _closed_loop_box(run: list[_Move]) -> Box | None:
    """The rectangle a closed profile loop cut.

    The loop's motion is: ramp in to a point on an edge, four corners, back
    to that point, then an overshoot and a ramp out that both leave the
    rectangle.  So the rectangle is the extent of the path up to the move
    that returns to the lead-in point.
    """
    start = (round(run[0].x1, 6), round(run[0].y1, 6))
    close = None
    for index, move in enumerate(run[1:], start=1):
        if (round(move.x1, 6), round(move.y1, 6)) == start:
            close = index
            break
    if close is None or close < 4:
        return None
    xs = [run[0].x1] + [run[i].x1 for i in range(1, close + 1)]
    ys = [run[0].y1] + [run[i].y1 for i in range(1, close + 1)]
    return Box(min(xs), min(ys), max(xs), max(ys))


def _straight_extent(run: list[_Move]) -> Box:
    """The extent a straight cut (T13 groove, T17 slot pass) swept.

    The measured grammar for both is "plunge, then move one axis" — a plunge
    move that only changes Z and a cut move that only changes X or Y — so
    the extent of the run's endpoints IS the segment, and a degenerate box
    holds it without needing a second geometry type.  A run with more moves
    in it than that is not a straight cut this post writes, and comes back
    as a box that matches no manifest entry, which is the right answer.
    """
    xs = [move.x1 for move in run]
    ys = [move.y1 for move in run]
    return Box(min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------
# the hold invariant (2026-08-05 amendment, Scott, job R0805, spec §3d/§6)
# --------------------------------------------------------------------------
#
# See the module docstring's "Nothing is freed early" section for what this
# proves and why it re-derives the tabs instead of asking where they are.

#: Z slack for "did this move cut BELOW the tab top?".  The traverse along a
#: tab's crest is commanded at exactly :attr:`~.model.TabSpec.top_z`, so the
#: comparison has to exclude it while still counting the ramps either side; a
#: hundred-thousandth is a tenth of the 0.0001 grid the post prints on, which
#: makes the answer the same for every coordinate a program can state.
_HOLD_Z_SLACK = 1e-5

#: How far off a profile's own edge line a move may be and still count as
#: running ALONG that edge.  The one thing that legitimately stands off the
#: line is a perimeter lead-in/lead-out ramp, which is displaced by
#: :attr:`~.model.PassSpec.lateral_lead` (0.05 measured); 0.2 leaves room for
#: that and is far under the distance between two different edges of a real
#: frame.
_HOLD_SIDE_SLACK = 0.2

#: The shortest gap in a profile's cut boundary that counts as material.  A
#: tab is 0.75 long; anything under this is float noise or a corner artefact,
#: not something that holds a frame.  Erring this way is the safe direction:
#: too small a bridge reads as NO bridge, which is a refusal.
_HOLD_MIN_BRIDGE = 0.05

#: Tolerance for comparing a release cut's span and offset against a bridge.
#: Coordinates reach this module printed to four decimals, and every quantity
#: being compared is a tenth of an inch or more.
_HOLD_TOL = 1e-3


def _is_release_run(run: list[_Move]) -> bool:
    """Is this cutting run the release grammar — plunge, then one straight move?

    :meth:`~.generator._Emitter._straight`'s shape, re-read off the text: the
    first move changes Z only and the second moves along exactly one axis at a
    constant Z.  Written here rather than shared with the emitter for the reason
    every template in this module is a second copy.
    """
    if len(run) != 2:
        return False
    plunge, cut = run
    if abs(plunge.x1 - plunge.x0) > TOL or abs(plunge.y1 - plunge.y0) > TOL:
        return False
    if abs(cut.z1 - cut.z0) > TOL:
        return False
    return (abs(cut.x1 - cut.x0) > TOL) != (abs(cut.y1 - cut.y0) > TOL)


@dataclass(frozen=True)
class _Span:
    """One stretch of a profile's boundary: which side, and where along it.

    ``lo``/``hi`` are measured from the side's MIDPOINT along the side's own
    axis (X for the bottom and top sides, Y for the left and right ones).
    Midpoint-relative because a profile's several paths — the T11 kerf, the T12
    kerf, the flush release path — are concentric rectangles of different sizes,
    so an absolute coordinate means a different place on each of them while a
    midpoint offset means the same place on all of them.
    """

    side: str
    lo: float
    hi: float

    @property
    def length(self) -> float:
        return self.hi - self.lo


@dataclass
class _ThroughProfile:
    """One profile this program cuts RIGHT THROUGH, and how it is held.

    ``path`` is the tool-centre rectangle the file states, ``flush`` the
    rectangle a release cut has to run on (see :meth:`describe`'s docstring on
    :func:`~.generator.release_path` for the rule this re-derives), and
    ``bridges`` the stretches of the boundary the through pass left standing.
    """

    kind: str
    #: Machine Z the pass that cut this loop reached.
    z_cut: float
    path: Box
    flush: Box
    bridges: tuple[_Span, ...]
    line: int
    #: Signed offset of ``path`` from the FINISHED edge, so the finished profile
    #: is ``path.grow(-offset)`` — which is what a refusal has to name, because
    #: it is the rectangle the operator sees on the sheet.
    offset: float
    #: Which of :attr:`bridges` a release cut has been matched to, by index.
    released: list[int]

    def edge(self) -> Box:
        return self.path.grow(-self.offset)

    def describe(self) -> str:
        edge = self.edge()
        what = "opening" if self.kind == "detail" else "part footprint"
        return (
            f"the {edge.width:g}x{edge.height:g} {what} at "
            f"({edge.x0:.4f}, {edge.y0:.4f})"
        )


def _side_lines(box: Box) -> dict[str, float]:
    """The fixed coordinate of each side of ``box``."""
    return {"bottom": box.y0, "top": box.y1, "left": box.x0, "right": box.x1}


def _side_axis(side: str) -> str:
    return "x" if side in ("bottom", "top") else "y"


def _side_midpoint(box: Box, side: str) -> float:
    return box.mid_x if _side_axis(side) == "x" else box.mid_y


def _side_half(box: Box, side: str) -> float:
    return (box.width if _side_axis(side) == "x" else box.height) / 2.0


def _classify_segment(box: Box, x0: float, y0: float, x1: float, y1: float):
    """``(side, lo, hi)`` for a segment running along one side of ``box``.

    ``None`` when the segment is not on the boundary at all (it stands further
    off the line than :data:`_HOLD_SIDE_SLACK`) or has no length to speak of.
    The interval comes back clipped to the side's own extent, so an overshoot
    that runs past a corner contributes only the part of it that is on the side.
    """
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    if max(dx, dy) <= TOL:
        return None
    if dx >= dy:
        candidates = ("bottom", "top")
        across = (y0 + y1) / 2.0
        lo, hi = min(x0, x1), max(x0, x1)
    else:
        candidates = ("left", "right")
        across = (x0 + x1) / 2.0
        lo, hi = min(y0, y1), max(y0, y1)
    lines = _side_lines(box)
    side = min(candidates, key=lambda name: abs(across - lines[name]))
    if abs(across - lines[side]) > _HOLD_SIDE_SLACK:
        return None
    half = _side_half(box, side)
    mid = _side_midpoint(box, side)
    lo, hi = max(lo - mid, -half), min(hi - mid, half)
    if hi - lo <= TOL:
        return None
    return side, lo, hi


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The union of closed intervals, in order."""
    out: list[tuple[float, float]] = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1] + TOL:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _loop_bridges(run: list[_Move], box: Box, cfg: PostConfig) -> tuple[_Span, ...]:
    """Which stretches of ``box``'s boundary this loop left STANDING.

    Independent re-derivation of the tabs (spec §3d): nothing here knows where
    :mod:`~.tabs` put them, how long they are or how many there should be.  It
    reads the commanded motion and asks one question per side — which parts of
    this side did the tool take below :attr:`~.model.TabSpec.top_z`? — and calls
    everything else a bridge.

    Each move is split at the Z where it crosses the tab top (the same
    :func:`_z_span` the material rules split a ramp with, so a ramp counts as
    cutting only where it is actually below the line), projected onto the side it
    runs along and clipped to that side.  The union of those intervals is the
    boundary this pass severed; the gaps in it are the material still holding the
    piece on.  A loop with no lift anywhere therefore reports NO bridges, which
    is exactly the "freed early" case.
    """
    covered: dict[str, list[tuple[float, float]]] = {side: [] for side in SIDES}
    for move in run:
        below = _z_span(move, cfg.tabs.top_z - _HOLD_Z_SLACK)
        if below is None:
            continue
        piece = _span_box(move, *below)
        # _span_box loses the direction, which does not matter: the interval is
        # unsigned and the side is decided by the piece's own long axis.
        found = _classify_segment(box, piece.x0, piece.y0, piece.x1, piece.y1)
        if found is None:
            continue
        side, lo, hi = found
        covered[side].append((lo, hi))

    bridges: list[_Span] = []
    for side in SIDES:
        half = _side_half(box, side)
        at = -half
        for lo, hi in _merge(covered[side]):
            if lo - at >= _HOLD_MIN_BRIDGE:
                bridges.append(_Span(side, at, lo))
            at = max(at, hi)
        if half - at >= _HOLD_MIN_BRIDGE:
            bridges.append(_Span(side, at, half))
    return tuple(bridges)


def _through_profiles(
    runs: list[list[_Move]], cfg: PostConfig
) -> list[_ThroughProfile]:
    """Every closed loop this program cuts right through the sheet, with its
    bridges and the flush path a release cut of it has to run on."""
    bottom = cfg.stock_top_z - cfg.material_thickness
    radius = cfg.tool(SECTION_RELEASE).radius if SECTION_RELEASE in cfg.tools else 0.0
    # (kind, offset, which way the WASTE lies).  An opening's waste is the
    # dropout inside it and a perimeter's is the skeleton outside it, which is
    # the whole difference between the two flush offsets (spec §3c).
    shapes = [("detail", cfg.detail_pass.z_cut, cfg.detail_pass.offset, -1.0)]
    for spec in cfg.perimeter_passes:
        shapes.append(("perimeter", spec.z_cut, spec.offset, 1.0))

    out: list[_ThroughProfile] = []
    for run in runs:
        depth = min(move.z1 for move in run)
        if depth > bottom + TOL:
            continue  # not a through cut: it leaves material under it
        for kind, z_cut, offset, waste in shapes:
            if abs(depth - z_cut) > TOL:
                continue
            box = _closed_loop_box(run)
            if box is None:
                break  # ``geometry`` reports this; nothing to hold here
            out.append(
                _ThroughProfile(
                    kind=kind,
                    z_cut=depth,
                    path=box,
                    flush=box.grow(-offset + waste * radius),
                    bridges=_loop_bridges(run, box, cfg),
                    line=run[0].line,
                    offset=offset,
                    released=[],
                )
            )
            break
    return out


def _shallow_profiles(
    runs: list[list[_Move]], cfg: PostConfig
) -> list[_ThroughProfile]:
    """Loops that cut BELOW the tab top without cutting through the sheet.

    The T11 opening pass at Z0.15 is one, and so is a two-pass perimeter's Z0.06
    skin: neither frees anything, so neither is a through profile, but both cut
    0.10-0.19 below the tab top and would take a tab down with them if they did
    not lift (spec §3b, "every pass that cuts below Z 0.25 lifts over the tab
    zones").  They are collected with the same machinery and the same
    :class:`_ThroughProfile` shape — what makes one of these different is only
    what a missing bridge on it MEANS, which is :func:`_check_shallow_lifts`.

    A pass at or ABOVE the tab top is not one of these and is not required to
    lift: it cannot damage a tab, because it never reaches one.  The 2026-08-05
    max-bite ladder's upper rungs are exactly that — Z0.45 on an opening, Z0.372
    on a perimeter, both well clear of the 0.25 tab top — so they are filtered
    out below by the same comparison that has always excluded the T13 groove, and
    a program in which they do not lift is correct rather than refused.
    """
    bottom = cfg.stock_top_z - cfg.material_thickness
    shapes = [
        ("opening", spec.z_cut, spec.offset, -1.0) for spec in cfg.openings_passes
    ]
    for spec in cfg.perimeter_passes:
        shapes.append(("perimeter", spec.z_cut, spec.offset, 1.0))

    out: list[_ThroughProfile] = []
    for run in runs:
        depth = min(move.z1 for move in run)
        if depth <= bottom + TOL or depth >= cfg.tabs.top_z - _HOLD_Z_SLACK:
            continue  # through (handled above), or never reaches a tab
        for kind, z_cut, offset, waste in shapes:
            if abs(depth - z_cut) > TOL:
                continue
            box = _closed_loop_box(run)
            if box is None:
                break
            out.append(
                _ThroughProfile(
                    kind=kind,
                    z_cut=depth,
                    path=box,
                    flush=box,  # never released: a release cut is a through cut
                    bridges=_loop_bridges(run, box, cfg),
                    line=run[0].line,
                    offset=offset,
                    released=[],
                )
            )
            break
    return out


def _check_shallow_lifts(
    through: list[_ThroughProfile], shallow: list[_ThroughProfile], cfg: PostConfig
) -> list[Violation]:
    """A shallower deep pass may not cut away a tab the through pass stands up.

    Spec §3b makes this explicit — "T11 perimeter pass 1 lifts; it must, or the
    skin pass destroys the tab before pass 2 can preserve it" — and the same is
    true of the T11 opening pass, which cuts to Z0.15 and would leave 0.15 of tab
    where 0.25 was ratified.

    Paired by the FINISHED profile the two passes share, which is each one's own
    path taken back by its own offset: an opening's T11 and T12 loops are
    different rectangles around the same finished opening, and a part's skin and
    through loops are different rectangles around the same footprint.  Nothing is
    paired by position or by order.
    """
    problems: list[Violation] = []
    wanted: dict[tuple[str, str], _ThroughProfile] = {}
    for profile in through:
        family = "opening" if profile.kind == "detail" else "perimeter"
        wanted[(family, repr(profile.edge().rounded(PLACES)))] = profile
    for profile in shallow:
        family = "opening" if profile.kind == "opening" else "perimeter"
        deep = wanted.get((family, repr(profile.edge().rounded(PLACES))))
        if deep is None:
            continue  # no through pass on this profile: ``missing-cut``'s to say
        for bridge in deep.bridges:
            if any(
                other.side == bridge.side
                and abs(other.lo - bridge.lo) <= _HOLD_TOL
                and abs(other.hi - bridge.hi) <= _HOLD_TOL
                for other in profile.bridges
            ):
                continue
            problems.append(
                Violation(
                    "hold",
                    f"{deep.describe()} keeps a holding tab on its {bridge.side} "
                    f"side, but the pass at Z{_zword(profile.z_cut)} — the one "
                    f"that cut this loop on line {profile.line} — ran straight "
                    f"through where that tab stands instead of rising over it. A "
                    f"pass that cuts below the Z{cfg.tabs.top_z} tab top has "
                    f"to lift over every tab or there is less tab left than the "
                    f"program believes",
                    profile.line,
                )
            )
    return problems


def _release_cuts(runs: list[list[_Move]], cfg: PostConfig):
    """Every release cut in the program, as its own two-move run.

    Identified the same way :func:`_recover_parts` identifies one: the release
    depth (which IS the detail pass's, :attr:`~.model.PostConfig.release_z`) plus
    the straight plunge-and-move shape that tells it from a detail loop.
    """
    out = []
    for run in runs:
        depth = min(move.z1 for move in run)
        if abs(depth - cfg.release_z) > TOL or not _is_release_run(run):
            continue
        out.append(run)
    return out


def _check_hold(moves: list[_Move], cfg: PostConfig) -> list[Violation]:
    """The hold invariant: nothing is separated before the release section.

    The refusals, all of them things spec §3b/§3d/§6 asks for by name:

    ``freed early``
        a profile is cut right through with no bridge anywhere on it.  The part
        or the dropout is loose from that moment, which is the failure job R0805
        came off the machine with;
    ``never released``
        a bridge no release cut removes.  The frame leaves the machine still
        attached to the sheet;
    ``released twice``
        two release cuts on one bridge.  Harmless to the part and a sign the
        program is not what anybody thinks it is, so it is reported;
    ``not flush`` / ``wrong feed``
        a release cut that is not on the flush path (spec §8's forbidden
        centreline release is the case that matters — it would leave a rib of tab
        on the finished edge) or does not run at the ratified release feeds.  Its
        DEPTH needs no rule of its own: a cut at any other Z is not recognised as
        a release cut at all, and then it is an ``extra-cut`` and its bridge is a
        ``never released``;
    ``a shallower pass that did not lift``
        :func:`_check_shallow_lifts` — spec §3b applies to every pass below the
        tab top, not only the one that cuts through.

    Plus three ordering rules (:func:`_check_release_order`), reported as
    ``cut-order`` because that is what they are about.
    """
    problems: list[Violation] = []
    runs = _cut_runs(moves)
    profiles = _through_profiles(runs, cfg)
    releases = _release_cuts(runs, cfg)

    for run in releases:
        cut = run[1]
        problems.extend(_check_release_feeds(run, cfg))
        target = _match_bridge(profiles, cut)
        if target is None:
            problems.append(
                Violation(
                    "hold",
                    _no_bridge_message(profiles, cut, cfg),
                    cut.line,
                )
            )
            continue
        profile, bridge_index = target
        profile.released.append(bridge_index)

    for profile in profiles:
        if not profile.bridges:
            problems.append(
                Violation(
                    "hold",
                    f"{profile.describe()} is cut right through with no holding "
                    f"tab anywhere on it: every part of its boundary is taken "
                    f"below the Z{cfg.tabs.top_z} tab top by this one pass, so the "
                    f"piece is loose from here on and the rest of the sheet is cut "
                    f"with it lying under the spindle. That is the break job R0805 "
                    f"came off the machine with; a through profile has to keep at "
                    f"least one bridge until the T12 release section",
                    profile.line,
                )
            )
            continue
        for index, bridge in enumerate(profile.bridges):
            times = profile.released.count(index)
            if times == 1:
                continue
            if times == 0:
                problems.append(
                    Violation(
                        "hold",
                        f"{profile.describe()} keeps a {bridge.length:.4f} holding "
                        f"tab on its {bridge.side} side that no release cut ever "
                        f"removes - the piece would come off the machine still "
                        f"attached to the sheet"
                        + (
                            ""
                            if releases
                            else ", and this program has no T12 release section at "
                            "all"
                        ),
                        profile.line,
                    )
                )
            else:
                problems.append(
                    Violation(
                        "hold",
                        f"{profile.describe()}: the holding tab on its "
                        f"{bridge.side} side is released {times} times - a tab is "
                        f"milled away once, at the end, and a second cut over the "
                        f"same air is not a program anybody wrote on purpose",
                        profile.line,
                    )
                )
    problems.extend(
        _check_shallow_lifts(profiles, _shallow_profiles(runs, cfg), cfg)
    )
    problems.extend(_check_release_order(profiles, releases, runs, cfg))
    return problems


def _match_bridge(profiles: list[_ThroughProfile], cut: _Move):
    """Which profile's which bridge this release cut removes, or ``None``.

    A release cut qualifies only if it runs ON the profile's flush path (the
    finished edge offset into the waste by the release tool's radius — checked
    numerically, which is what refuses the centreline release spec §8 forbids)
    and COVERS the whole bridge.  It is allowed to run past the bridge at either
    end, because it is meant to: the tab's ramps and the release overlap all lie
    outside the full-height span.
    """
    for profile in profiles:
        found = _classify_segment(profile.flush, cut.x0, cut.y0, cut.x1, cut.y1)
        if found is None:
            continue
        side, lo, hi = found
        if abs(_across(cut) - _side_lines(profile.flush)[side]) > _HOLD_TOL:
            continue
        for index, bridge in enumerate(profile.bridges):
            if bridge.side != side:
                continue
            if lo <= bridge.lo + _HOLD_TOL and hi >= bridge.hi - _HOLD_TOL:
                return profile, index
    return None


def _across(cut: _Move) -> float:
    """The coordinate a release cut does NOT move in — its offset from the edge."""
    if abs(cut.x1 - cut.x0) >= abs(cut.y1 - cut.y0):
        return (cut.y0 + cut.y1) / 2.0
    return (cut.x0 + cut.x1) / 2.0


def _no_bridge_message(
    profiles: list[_ThroughProfile], cut: _Move, cfg: PostConfig
) -> str:
    """Why this release cut matches nothing — the near miss, if there is one.

    A release cut on the T11 CENTRELINE instead of the flush path is the
    mistake spec §8 names, and it misses by exactly the difference between the
    two radii, so the message says so rather than leaving the reader to work out
    which of the two failures this is.
    """
    # A cut running along X has a constant Y, so the sides worth comparing it
    # against are the ones whose own axis is X — the bottom and the top.
    along = "x" if abs(cut.x1 - cut.x0) >= abs(cut.y1 - cut.y0) else "y"
    best: tuple[float, _ThroughProfile, str] | None = None
    for profile in profiles:
        for side, line in _side_lines(profile.flush).items():
            if _side_axis(side) != along:
                continue
            miss = abs(_across(cut) - line)
            if best is None or miss < best[0]:
                best = (miss, profile, side)
    detail = ""
    if best is not None and best[0] > _HOLD_TOL:
        miss, profile, side = best
        detail = (
            f" The nearest flush path is the {side} side of {profile.describe()}, "
            f"{miss:.4f} away: a release cut runs one release-tool radius from the "
            f"FINISHED edge, into the waste, so its own edge lands on the finished "
            f"line. Running it down the T11 kerf's centreline instead leaves a rib "
            f"of tab standing on the finished edge, which is what the flush offset "
            f"exists to prevent."
        )
    return (
        f"a T12 release cut at Z{min(cut.z0, cut.z1)} runs "
        f"x[{min(cut.x0, cut.x1):.4f}, {max(cut.x0, cut.x1):.4f}] "
        f"y[{min(cut.y0, cut.y1):.4f}, {max(cut.y0, cut.y1):.4f}], which is not "
        f"flush with any profile's finished edge over a holding tab that pass left "
        f"standing." + detail
    )


def _check_release_feeds(run: list[_Move], cfg: PostConfig) -> list[Violation]:
    """The release plunge and cut run at the RELEASE feeds, not T12's others.

    :func:`_check_feeds` judges a tool against the whole set of feeds the table
    gives it, deliberately (module docstring), and T12 legitimately has two
    operations now — so a release cut made at the detail pass's 293/100 passes
    that rule.  It must not pass this one: "very slowly" is the entire point of
    the release pass (spec §3c), and 293 ipm through a tab at the end of a
    program is how a finished frame gets torn off its last bridge.
    """
    spec = cfg.release
    if spec is None:  # pragma: no cover - callers gate on this
        return []
    problems: list[Violation] = []
    for move, wanted, what in (
        (run[0], spec.entry_feed, "plunge"),
        (run[1], spec.cut_feed, "cut"),
    ):
        if move.feed is None or abs(move.feed - wanted) > TOL:
            got = "no F word at all" if move.feed is None else f"F{_fword(move.feed)}"
            problems.append(
                Violation(
                    "hold",
                    f"the {what} of a T12 release cut runs at {got} - the release "
                    f"pass runs at F{_fword(wanted)}, which is the feed Scott "
                    f"ratified for it on 2026-08-05 precisely because this cut "
                    f"parts a finished frame from the sheet",
                    move.line,
                )
            )
    return problems


def _check_release_order(
    profiles: list[_ThroughProfile],
    releases: list[list[_Move]],
    runs: list[list[_Move]],
    cfg: PostConfig,
) -> list[Violation]:
    """Where the release cuts sit in the program (spec §3c "Order").

    Three relations, all judged on line numbers, all independent of any plan:

    a)  the release section is LAST — no cut of any other kind comes after one;
    b)  every OPENING's release comes before every perimeter's.  A dropout is
        the lighter, more fragile piece and it is released while its frame is
        still held;
    c)  a nested inner frame's perimeter release comes before its host's, the
        same inners-before-hosts rule the freeing pass itself follows: a host
        released first would leave the inner sitting in a hole in a loose slab.
    """
    problems: list[Violation] = []
    if not releases:
        return problems
    release_lines = [run[0].line for run in releases]
    first_release = min(release_lines)
    release_set = {id(run) for run in releases}

    late = [run for run in runs if id(run) not in release_set and run[0].line > first_release]
    if late:
        problems.append(
            Violation(
                "cut-order",
                f"a cut runs AFTER the T12 release section starts on line "
                f"{first_release} - the release is the last machining in the "
                f"program (spec §3c): everything on the sheet is loose once it has "
                f"run, so anything cut afterwards is cut on a piece nothing is "
                f"holding",
                late[0][0].line,
            )
        )

    kinds: dict[str, list[int]] = {"detail": [], "perimeter": []}
    for profile in profiles:
        for run in releases:
            found = _match_bridge([profile], run[1])
            if found is not None:
                kinds[profile.kind].append(run[0].line)
    if kinds["detail"] and kinds["perimeter"]:
        if max(kinds["detail"]) > min(kinds["perimeter"]):
            problems.append(
                Violation(
                    "cut-order",
                    f"an opening's tab is released on line {max(kinds['detail'])}, "
                    f"AFTER a part perimeter's on line {min(kinds['perimeter'])} - "
                    f"spec §3c releases every opening dropout first, while its "
                    f"frame is still held to the sheet",
                    max(kinds["detail"]),
                )
            )

    #: Nesting, re-derived: a footprint that lies inside another profile's
    #: opening is that one's passenger.  Read off the recovered rectangles, like
    #: everything else here.
    openings = [p for p in profiles if p.kind == "detail"]
    footprints = [p for p in profiles if p.kind == "perimeter"]
    for inner in footprints:
        inner_edge = inner.edge()
        for opening in openings:
            void = opening.edge()
            if not void.contains(inner_edge, TOL):
                continue
            host = _footprint_containing(footprints, void)
            if host is None or host is inner:
                continue
            inner_at = _release_lines(inner, releases)
            host_at = _release_lines(host, releases)
            if not inner_at or not host_at or max(inner_at) < min(host_at):
                continue
            problems.append(
                Violation(
                    "cut-order",
                    f"{inner.describe()} is nested in {host.describe()}, but the "
                    f"host's tabs are released on line {min(host_at)}, before the "
                    f"inner's on line {max(inner_at)} - the inner would then be "
                    f"sitting in a hole in a slab that is already loose",
                    min(host_at),
                )
            )
    return problems


def _release_lines(profile: _ThroughProfile, releases) -> list[int]:
    return [
        run[0].line for run in releases if _match_bridge([profile], run[1]) is not None
    ]


def _footprint_containing(footprints: list[_ThroughProfile], void: Box):
    """The smallest recovered footprint whose own opening ``void`` is."""
    best = None
    for candidate in footprints:
        edge = candidate.edge()
        if edge.contains(void, TOL):
            if best is None or (edge.width * edge.height) < (
                best.edge().width * best.edge().height
            ):
                best = candidate
    return best


def _check_part_bounds(parts: list[_RecoveredPart], cfg: PostConfig) -> list[Violation]:
    problems: list[Violation] = []
    for part in parts:
        box = part.box
        if (
            box.x0 < -TOL
            or box.y0 < -TOL
            or box.x1 > cfg.sheet_width + TOL
            or box.y1 > cfg.sheet_length + TOL
        ):
            problems.append(
                Violation(
                    "part-bounds",
                    f"part footprint {box} does not fit the "
                    f"{cfg.sheet_width}x{cfg.sheet_length} sheet",
                )
            )
    return problems


def _check_foreign_cuts(
    moves: list[_Move], parts: list[_RecoveredPart], cfg: PostConfig
) -> list[Violation]:
    """No cut may run through a part it is not cutting.

    Each move is split along its own Z profile first (see the module
    docstring's "Ramps have a Z profile"), and each piece judged on what the
    bit is doing there:

    *   above the stock: nothing, because it is cutting nothing;
    *   in the material but not through: the tool's full SWEPT WIDTH against
        every part's solid except the one the move is attributed to — the part
        whose footprint, grown by the trim overhang, contains the whole move.
        That one exemption is what lets a T13 groove cut its own part, which is
        the whole reason it exists.  Swept rather than centre since 2026-08-05:
        judging a shallow cut on its centreline is what passed job R0805, whose
        groove sweep bit 0.235 into the neighbouring frame (module docstring,
        "The shallow-cut waiver, and why it is gone");
    *   right through the sheet: the same swept width against EVERY part's
        solid, the attributed one included.  A through cut inside a frame
        member is that member sawn in half whoever it belongs to, and
        exempting the containing footprint is how an inner frame's runaway cut
        through its host used to go unreported (2026-08-04, fix 12).
    """
    problems: list[Violation] = []
    solids = [(part, part.solids()) for part in parts]
    through_z = cfg.stock_top_z - cfg.material_thickness

    for move in moves:
        if move.rapid:
            continue
        if _v_bit_radius(move, cfg) is not None:
            continue  # the cone rule owns these (see _check_v_slot_cuts)
        in_stock = _z_span(move, cfg.stock_top_z)
        if in_stock is None:
            continue  # never enters the stock
        own = _owner_of(move, parts, cfg)
        through = _z_span(move, through_z)
        # One region per depth regime the move passes through (see the module
        # docstring's "Ramps have a Z profile").  BOTH regimes are judged on
        # the tool's full swept width; the only difference between them is who
        # is exempt.  Through the sheet: nobody -- a through cut inside a part
        # is the part sawn in half whoever it belongs to, and attributing it to
        # a containing footprint is how a runaway cut used to hide (fix 12).
        # In the material but not through: the part the move is attributed to,
        # and it alone, because that is a T13 groove cutting its own part.
        # Judging that regime on the tool CENTRE instead -- the shallow-cut
        # waiver -- is what passed job R0805 on 05 AUG 26 (module docstring).
        regions: list[tuple[Box, float, bool]] = []
        if through is not None:
            regions.append((_span_box(move, *through), move.radius, False))
            for lo, hi in _span_minus(in_stock, through):
                regions.append((_span_box(move, lo, hi), move.radius, True))
        else:
            regions.append((_span_box(move, *in_stock), move.radius, True))

        for centre, radius, owner_cuts_here in regions:
            swept = centre.grow(radius)
            depth = cfg.stock_top_z - min(move.z0, move.z1)
            for part, bands in solids:
                if owner_cuts_here and part is own:
                    continue
                whose = (
                    "the solid of the part it is itself attributed to, "
                    if part is own
                    else "the solid of the part "
                )
                why = (
                    " - a cut right through the sheet may only run in the trim "
                    "margin or inside an opening, never through a frame member"
                    if part is own
                    else ""
                )
                for solid in bands:
                    if solid.overlaps(swept, TOL):
                        problems.append(
                            Violation(
                                "foreign-cut",
                                f"a cutting move up to {depth:g} deep sweeps "
                                f"x[{swept.x0:.4f}, {swept.x1:.4f}] "
                                f"y[{swept.y0:.4f}, {swept.y1:.4f}] and enters "
                                f"{whose}at {part.box}{why}",
                                move.line,
                            )
                        )
                        break
                else:
                    continue
                break
            else:
                continue
            break
    return problems


def _z_span(move: _Move, z_limit: float) -> tuple[float, float] | None:
    """The parameter interval of ``move`` where Z is at or below ``z_limit``.

    ``0`` is the move's start and ``1`` its end.  Z varies linearly with
    travel, so the answer is always one contiguous interval (or ``None`` when
    the move stays above the limit throughout) — which is what lets the
    material checks judge a descending ramp where it is actually cutting
    instead of pretending the whole 4 inches happen at its deepest Z.
    """
    z0, z1 = move.z0, move.z1
    if abs(z1 - z0) <= 1e-12:
        return (0.0, 1.0) if z0 <= z_limit + TOL else None
    crossing = (z_limit - z0) / (z1 - z0)
    low, high = 0.0, 1.0
    if z1 < z0:
        low = max(0.0, crossing)
    else:
        high = min(1.0, crossing)
    if high < low:
        return None
    return low, high


def _span_minus(
    outer: tuple[float, float], inner: tuple[float, float]
) -> list[tuple[float, float]]:
    """``outer`` less ``inner``, both parameter intervals of one move.

    ``inner`` is always at one end of ``outer`` here (both come from
    :func:`_z_span` on the same monotonic Z), so at most one interval comes
    back; the general shape is written out anyway so a caller cannot be
    surprised by that assumption.
    """
    (low, high), (cut_low, cut_high) = outer, inner
    out: list[tuple[float, float]] = []
    if cut_low > low + 1e-12:
        out.append((low, min(cut_low, high)))
    if cut_high < high - 1e-12:
        out.append((max(cut_high, low), high))
    return out


def _span_box(move: _Move, low: float, high: float) -> Box:
    """The XY extent of the sub-segment of ``move`` between the two fractions."""
    xs = (
        move.x0 + (move.x1 - move.x0) * low,
        move.x0 + (move.x1 - move.x0) * high,
    )
    ys = (
        move.y0 + (move.y1 - move.y0) * low,
        move.y0 + (move.y1 - move.y0) * high,
    )
    return Box(min(xs), min(ys), max(xs), max(ys))


def _v_bit_radius(move: _Move, cfg: PostConfig) -> float | None:
    """The V bit's own radius when ``move`` is being made with it, else None.

    Identified by the diameter the PROGRAM declares for the tool it called,
    which is the only thing in the text that says which cutter is in the
    spindle.  No other tool in the post table shares 0.96.
    """
    tool = cfg.tools.get(SECTION_WDC_SLOT)
    if tool is None:
        return None
    return tool.radius if abs(2.0 * move.radius - tool.diameter) <= TOL else None


def _check_v_slot_cuts(
    moves: list[_Move], parts: list[_RecoveredPart], cfg: PostConfig
) -> list[Violation]:
    """The 45-degree slot, judged on the cone it sweeps (module docstring)."""
    problems: list[Violation] = []
    solids = [(part, part.solids()) for part in parts]
    # The SHEET, with no trim overhang added (2026-08-04 review, fix 11): the
    # planner's rule is that the sheet must contain the sweep, and a cone that
    # runs off the edge cuts the fence rather than trim.
    low_x, low_y = 0.0, 0.0
    high_x = cfg.sheet_width
    high_y = cfg.sheet_length

    for move in moves:
        if move.rapid:
            continue
        tool_radius = _v_bit_radius(move, cfg)
        if tool_radius is None:
            continue
        depth = cfg.stock_top_z - min(move.z0, move.z1)
        if depth <= TOL:
            continue  # above the stock: an air cut removes nothing
        # 45 degrees per side, so the cut is as wide as it is deep - until
        # the bit is buried to its shoulder, where it can get no wider.
        reach = min(depth * cfg.wdc_slot.flank_slope, tool_radius)
        centre = Box(
            min(move.x0, move.x1),
            min(move.y0, move.y1),
            max(move.x0, move.x1),
            max(move.y0, move.y1),
        )
        swept = centre.grow(reach)

        if (
            swept.x0 < low_x - TOL
            or swept.y0 < low_y - TOL
            or swept.x1 > high_x + TOL
            or swept.y1 > high_y + TOL
        ):
            problems.append(
                Violation(
                    "v-slot",
                    f"the 45-degree slot at Z{min(move.z0, move.z1)} cuts "
                    f"{reach} wide either side of its path, sweeping "
                    f"x[{swept.x0:.4f}, {swept.x1:.4f}] "
                    f"y[{swept.y0:.4f}, {swept.y1:.4f}] - off the "
                    f"{cfg.sheet_width}x{cfg.sheet_length} sheet, which is the "
                    f"fence and the spoilboard, not trim",
                    move.line,
                )
            )
            continue

        own = _smallest_containing(parts, centre.mid_x, centre.mid_y)
        for part, bands in solids:
            if part is own:
                continue
            for solid in bands:
                if solid.overlaps(swept, TOL):
                    problems.append(
                        Violation(
                            "v-slot",
                            f"the 45-degree slot at Z{min(move.z0, move.z1)} "
                            f"sweeps x[{swept.x0:.4f}, {swept.x1:.4f}] "
                            f"y[{swept.y0:.4f}, {swept.y1:.4f}] and cuts up to "
                            f"{depth} deep into the part at {part.box}",
                            move.line,
                        )
                    )
                    break
            else:
                continue
            break
    return problems


def _smallest_containing(parts: list[_RecoveredPart], x: float, y: float):
    """The smallest recovered footprint holding ``(x, y)``, or None.

    A slot runs down the middle of a stile, so the part it belongs to is
    simply the part its midpoint is inside — no tolerance games with how far
    the ends over-run.  Part footprints never overlap except when one is
    nested in another's opening, which is what "smallest" settles.
    """
    best = None
    for part in parts:
        box = part.box
        if box.x0 - TOL <= x <= box.x1 + TOL and box.y0 - TOL <= y <= box.y1 + TOL:
            if best is None or (box.width * box.height) < (
                best.box.width * best.box.height
            ):
                best = part
    return best


def _owner_of(move: _Move, parts: list[_RecoveredPart], cfg: PostConfig):
    """The part this move is cutting: the smallest footprint (grown by the
    trim overhang) that contains the whole move."""
    span = Box(
        min(move.x0, move.x1),
        min(move.y0, move.y1),
        max(move.x0, move.x1),
        max(move.y0, move.y1),
    )
    best = None
    for part in parts:
        if part.box.grow(cfg.overhang).contains(span, TOL):
            if best is None or (part.box.width * part.box.height) < (
                best.box.width * best.box.height
            ):
                best = part
    return best


# --------------------------------------------------------------------------
# the expected-work manifest (2026-08-04 review)
# --------------------------------------------------------------------------
#
# Everything below answers the question the rest of the file cannot: is any
# cut MISSING?  See the module docstring for why that needs a second input,
# and why that input is the layout rather than the plan the emitter used.


#: What each recovered/expected kind is called in a message.  The kind
#: itself is the SECTION the cut's Z word puts it in, which is all the file
#: says about it.
_KIND_NAMES = {
    "groove": "T13 panel groove",
    "slot": "T17 45-degree stile slot pass",
    "opening": "T11 opening through-cut",
    "detail": "T12 opening finish pass",
    "perimeter": "perimeter loop",
}

_GROOVE_NAMES = {
    0: "stile groove (low side)",
    1: "rail groove (low side)",
    2: "stile groove (high side)",
    3: "rail groove (high side)",
}


@dataclass(frozen=True)
class ExpectedCut:
    """One cut a sheet's program owes, described so a refusal reads plainly.

    ``path`` is the TOOL CENTRE geometry — the closed rectangle for a
    profile loop, a degenerate box for a straight cut — because that is what
    the program states and so what a re-parse can compare it against.
    ``what`` names the feature and ``consequence`` says what the operator
    gets if it is not there; both end up verbatim in the refusal the GUI and
    the PDF cut sheet show.

    ``part``, ``host`` and ``pass_position`` are the manifest's statement of
    the sheet's STRUCTURE, which is what the chronology rules need (see the
    module docstring): ``part`` is the placement's position in the layout walk
    (hosts before their passengers), ``host`` the index of the placement whose
    opening it sits in (``None`` at the top level) and ``pass_position`` which
    perimeter depth pass a ``"perimeter"`` entry is (``None`` for anything
    else).  All three come off the layout, like every other field here.
    """

    kind: str
    z: float
    path: Box
    part_number: str
    part_box: Box
    what: str
    consequence: str
    part: int = 0
    host: int | None = None
    pass_position: int | None = None

    def describe(self) -> str:
        return (
            f"{self.part_number} @({self.part_box.x0:.4f},{self.part_box.y0:.4f}): "
            f"{self.what} is not in the program - nothing cuts "
            f"x[{self.path.x0:.4f}, {self.path.x1:.4f}] "
            f"y[{self.path.y0:.4f}, {self.path.y1:.4f}] at Z{_zword(self.z)}. "
            f"{self.consequence}"
        )


@dataclass(frozen=True)
class ExpectedWork:
    """Every cut one sheet owes — and, by being complete, nothing else.

    Both directions are load bearing: an entry with no cut in the file is a
    ``missing-cut``, and a cut in the file with no entry here is an
    ``extra-cut``.  That is only sound because the manifest is exhaustive,
    which is why :func:`expected_work` builds it from the whole layout and
    never from a subset of it.
    """

    cuts: tuple[ExpectedCut, ...] = ()
    #: Does this sheet owe a T12 tab-release section (2026-08-05 amendment)?
    #: Read off the post table the manifest was built with
    #: (:attr:`~.model.PostConfig.release`), which is the only place that
    #: decision is made (:func:`~.from_layout.post_config_for`).  It is a
    #: structural fact, not a list of cuts: WHERE the tabs are is
    #: :func:`_check_hold`'s to re-derive.
    release: bool = False

    def __len__(self) -> int:
        return len(self.cuts)

    def counts(self) -> dict[str, int]:
        """How many cuts of each kind, for a caller that wants to say so."""
        out: dict[str, int] = {}
        for cut in self.cuts:
            out[cut.kind] = out.get(cut.kind, 0) + 1
        return out

    def describe(self) -> list[str]:
        """One line per owed cut, in part order (for tests and diagnostics)."""
        return [f"{cut.part_number}: {cut.what}" for cut in self.cuts]


def expected_work(layout, config: PostConfig | None = None) -> ExpectedWork:
    """The manifest of cuts an optimizer sheet owes.

    ``layout`` is a :class:`faceframe_cnc.nesting.SheetLayout` (or any object
    with a ``placements`` list, or the list itself) and ``config`` the post
    table the program was emitted with — the Z levels and offsets have to be
    the SAME table, so a dry-run program is judged against
    :func:`~.job.dry_run_config`'s lifted twin, not against the production
    one.  The sheet size is not used: where a cut may reach is
    ``bounds``/``part-bounds``/``v-slot``'s business, and this is only about
    WHICH cuts exist.

    Every part on the sheet — including frames nested inside another frame's
    opening, which are cut while their slab is still host waste — owes:

    *   its T13 panel grooves, all four, or the RAIL pair only for a WDC
        frame whose stiles take the T17 slot instead (2026-08-03 amendment);
    *   both T17 slot passes down each of a WDC frame's two stiles;
    *   for every opening the frame engine computes: one T11 roughing loop per
        configured opening depth pass (tool centre 0.1975 inside the finished
        opening edge — one loop on the measured table, two on a generated sheet
        since the 2026-08-05 max-bite amendment) and the T12 finish pass;
    *   one perimeter loop per configured depth pass, judged against the table
        the caller handed over and no other: the measured two-pass table owes
        the onion skin AND the full-depth pass that frees the part (2026-08-03
        amendment), which is what the reference programs run, while a generated
        sheet owes the rungs of its max-bite ladder ending on the through pass
        (2026-08-05, Scott — :func:`~.from_layout.generated_post_passes`).

    Raises ``ValueError`` when the sheet is empty or the frame engine cannot
    compute a part's geometry: there is then no honest statement of what the
    program owes, and refusing to make one is better than making a short one
    that a file could satisfy.
    """
    cfg = config or default_config()
    placements = getattr(layout, "placements", layout)
    cuts: list[ExpectedCut] = []
    for index, placement, host in _walk_placements(placements):
        cuts.extend(_expected_for_placement(placement, cfg, index, host))
    if not cuts:
        raise ValueError(
            "this sheet holds no placement, so there is no work to expect of its "
            "program"
        )
    return ExpectedWork(tuple(cuts), release=cfg.release is not None)


def _walk_placements(placements, host=None, counter=None):
    """``(index, placement, host index)``, hosts before their passengers.

    The index is the placement's position in this walk and is the manifest's
    identity for a part — stable, derived from the layout, and enough for the
    chronology rules to say "this frame is nested in that one" without the
    verifier ever seeing a :class:`~.model.PartProgram`.
    """
    if counter is None:
        counter = [0]
    for placement in placements:
        index = counter[0]
        counter[0] += 1
        yield index, placement, host
        yield from _walk_placements(
            getattr(placement, "children", ()) or (), index, counter
        )


def _expected_for_placement(
    placement, cfg: PostConfig, part: int = 0, host: int | None = None
) -> list[ExpectedCut]:
    box = Box.from_size(
        float(placement.x),
        float(placement.y),
        float(placement.width),
        float(placement.height),
    )
    rotated = bool(getattr(placement, "rotated", False))
    name = placement.part_number
    cuts: list[ExpectedCut] = []

    def add(
        kind: str,
        z: float,
        path: Box,
        what: str,
        consequence: str,
        pass_position: int | None = None,
    ) -> None:
        cuts.append(
            ExpectedCut(
                kind=kind,
                z=z,
                path=path,
                part_number=name,
                part_box=box,
                what=what,
                consequence=consequence,
                part=part,
                host=host,
                pass_position=pass_position,
            )
        )

    # -- T13 panel grooves -------------------------------------------------
    for index in _groove_indices(name):
        add(
            "groove",
            cfg.panel.z_cut,
            _groove_extent(
                box, rotated, index, cfg.panel, cfg.tool(SECTION_PANEL).radius
            ),
            f"the T13 {_GROOVE_NAMES[index]}",
            "The frame would come off the machine without the groove its "
            "cabinet seats in.",
        )

    # -- T17 WDC stile slots ----------------------------------------------
    if infer_frame_type(name) is FrameType.WDC:
        passes = len(cfg.wdc_slot.z_cuts)
        for index in (0, 1):
            side = "low" if index == 0 else "high"
            for position, z_cut in enumerate(cfg.wdc_slot.z_cuts):
                add(
                    "slot",
                    z_cut,
                    _slot_extent(
                        box,
                        rotated,
                        index,
                        cfg.wdc_slot,
                        cfg.wdc_slot_reach(position),
                    ),
                    f"the T17 slot down the {side}-side stile, pass "
                    f"{position + 1} of {passes} (centreline "
                    f"{cfg.wdc_slot.inset_from_inside_edge:g} from the stile's "
                    f"inside edge)",
                    "A WDC frame that has not had both passes of both slots "
                    "cannot meet its diagonal-corner cabinet.",
                )

    # -- T11 / T12 openings ------------------------------------------------
    # One T11 loop per configured depth pass (the 2026-08-05 max-bite ladder,
    # from_layout.generated_opening_passes: two 0.3 bites on a generated sheet,
    # the one measured 0.60 bite on the references), then the one T12 pass.
    openings = _sheet_openings(placement, box, rotated)
    rough_total = len(cfg.openings_passes)
    for label, opening in openings:
        for position, spec in enumerate(cfg.openings_passes):
            rung = (
                ""
                if rough_total == 1
                else f", pass {position + 1} of {rough_total} (Z{_zword(spec.z_cut)})"
            )
            add(
                "opening",
                spec.z_cut,
                opening.grow(spec.offset),
                f"the T11 through-cut of the {label!r} opening{rung}",
                "The frame would be cut free with solid material where that "
                "opening belongs."
                if rough_total == 1
                else "The T11 roughing ladder would take more than the "
                "ratified bite in one pass, or leave the opening short of "
                "the depth the T12 pass finishes from.",
            )
        add(
            "detail",
            cfg.detail_pass.z_cut,
            opening.grow(cfg.detail_pass.offset),
            f"the T12 finish pass of the {label!r} opening",
            "The opening's slug would never be cut free and the opening "
            "would stay undersize by the finish stock T12 exists to take.",
        )

    # -- T11 perimeter passes ---------------------------------------------
    # One loop per pass the config in hand carries, and the WORDS follow the same
    # table, because two different tables run a non-final perimeter pass for two
    # different reasons and an operator sent looking for the wrong one has been
    # misled:
    #
    #   * the MEASURED table's first pass is the Z0.06 onion skin the reference
    #     programs were cut with — a holding rib, and named as one;
    #   * a GENERATED sheet's non-final passes are rungs of the 2026-08-05
    #     max-bite ladder (Scott: 0.4 per pass on the 3/8 comp), which hold
    #     nothing — the tabs do that — and are named for what they are.
    #
    # Which it is comes off the table itself: a ladder exists because the TOOL
    # declares a bite limit, so that is the thing to ask (from_layout.T11_MAX_BITE
    # lands on ToolSpec.max_bite, and default_config declares none).
    total = len(cfg.perimeter_passes)
    tool = cfg.tools.get(SECTION_PERIMETER)
    laddered = tool is not None and tool.max_bite is not None
    for position, spec in enumerate(cfg.perimeter_passes):
        if position < total - 1 and laddered:
            role = f"perimeter roughing pass {position + 1}"
            consequence = (
                f"The remaining passes would then have to take "
                f"{cfg.stock_top_z - cfg.perimeter_passes[-1].z_cut:g} of "
                f"material between them in fewer bites than T{tool.number}'s "
                f"{tool.max_bite:g} limit allows."
            )
        elif total > 1 and position == 0:
            role = "the onion-skin perimeter pass"
            consequence = (
                "The onion skin is what holds every part while the through "
                "pass runs; without it the first part cut free is loose under "
                "the spindle."
            )
        elif position == total - 1:
            role = "the full-depth perimeter pass"
            consequence = (
                "The part would come off the machine still attached to the "
                "sheet by the material the earlier pass(es) left - nothing "
                "would have cut it free."
                if total > 1
                else "The part would come off the machine still attached to "
                "the sheet: this is the one pass that cuts it free."
            )
        else:
            role = f"perimeter pass {position + 1}"
            consequence = "The part would not be taken to its next depth."
        add(
            "perimeter",
            spec.z_cut,
            box.grow(spec.offset),
            f"{role} ({position + 1} of {total}, tool centre {spec.offset:g} "
            f"outside the part edge)",
            consequence,
            pass_position=position,
        )

    return cuts


def _sheet_openings(placement, box: Box, rotated: bool) -> list[tuple[str, Box]]:
    """This placement's routed openings, labelled, in SHEET coordinates.

    A deliberate second copy of the packer's rotation convention (a rotated
    placement is the frame turned 90 degrees COUNTER-clockwise, so a
    frame-local opening at ``(x, y, w, h)`` lands at
    ``(X + (W - y - h), Y + x, h, w)``): written out here rather than taken
    from :func:`~.model.program_from_placements`, because a manifest that
    borrowed the emitter's transform could not catch the emitter using the
    wrong one.  The OPENINGS themselves come from
    :func:`faceframe_cnc.geometry.compute_geometry`, which is the one
    authority on frame geometry in this program and is upstream of both.
    """
    ordered_w = box.height if rotated else box.width
    ordered_h = box.width if rotated else box.height
    geom = compute_geometry(placement.part_number, ordered_w, ordered_h)
    if geom.errors:
        raise ValueError(
            f"cannot say what {placement.part_number} owes its program: "
            f"{geom.errors[0]}"
        )
    out: list[tuple[str, Box]] = []
    for opening in geom.openings:
        if rotated:
            out.append(
                (
                    opening.label,
                    Box.from_size(
                        box.x0 + (box.width - opening.y - opening.height),
                        box.y0 + opening.x,
                        opening.height,
                        opening.width,
                    ),
                )
            )
        else:
            out.append(
                (
                    opening.label,
                    Box.from_size(
                        box.x0 + opening.x,
                        box.y0 + opening.y,
                        opening.width,
                        opening.height,
                    ),
                )
            )
    return out


def _groove_indices(part_number: str) -> tuple[int, ...]:
    """Which T13 grooves a part owes: all four, or the rail pair only.

    A WDC frame's 2" stiles take the T17 slot INSTEAD of a stile groove
    (2026-08-03 amendment), so it owes two grooves, not four.  Read off the
    amendment here rather than off
    :func:`~.from_layout.panel_groove_indices`: if that rule is ever changed
    on one side only, this check is what says so.
    """
    if infer_frame_type(part_number) is FrameType.WDC:
        return (1, 3)
    return (0, 2, 1, 3)


def _groove_extent(
    box: Box, rotated: bool, index: int, panel, tool_radius: float
) -> Box:
    """Where one T13 groove runs, as a degenerate box (measured insets).

    ``index`` 0..3 = stile-low, rail-low, stile-high, rail-high in the part's
    own orientation: the stile grooves sit ``stile_inset`` in from the two
    stile edges and run the length of the part; the rail grooves sit
    ``rail_inset`` in from the rail edges and run between the two stile centre
    lines.  A rotated part's stiles are its bottom and top edges.

    Second copy of :func:`~.generator.groove_segment`, deliberately — the
    manifest re-derives every cut from the layout and the measured tables and
    never imports the emitter (module docstring), so this arithmetic is
    written twice on purpose and the two copies disagreeing is a finding
    rather than a silence.

    The stile ends carry the 2026-08-05 amendment (Scott, job R0805): the
    endpoint is the part edge plus ``tool_radius + end_inset``, clamped
    against the measured ``overrun``, so the swept cut stops flush with the
    part instead of 0.690 past it.  ``tool_radius`` is read by the CALLER off
    its own copy of the tool table (:data:`~.model.SECTION_PANEL`), not
    handed over from anything the emitter touched.
    """
    stile, rail, over = panel.stile_inset, panel.rail_inset, panel.overrun
    reach = tool_radius + panel.end_inset
    if not rotated:
        stile_lines = (box.x0 + stile, box.x1 - stile)
        rail_lines = (box.y0 + rail, box.y1 - rail)
        if index in (0, 2):
            x = stile_lines[0 if index == 0 else 1]
            return Box(
                x,
                max(box.y0 - over, box.y0 + reach),
                x,
                min(box.y1 + over, box.y1 - reach),
            )
        y = rail_lines[0 if index == 1 else 1]
        return Box(stile_lines[0], y, stile_lines[1], y)
    stile_lines = (box.y0 + stile, box.y1 - stile)
    rail_lines = (box.x0 + rail, box.x1 - rail)
    if index in (0, 2):
        y = stile_lines[0 if index == 0 else 1]
        return Box(
            max(box.x0 - over, box.x0 + reach),
            y,
            min(box.x1 + over, box.x1 - reach),
            y,
        )
    x = rail_lines[0 if index == 1 else 1]
    return Box(x, stile_lines[0], x, stile_lines[1])


def _slot_extent(box: Box, rotated: bool, index: int, spec, overrun: float) -> Box:
    """Where one T17 slot pass runs its tool centre, as a degenerate box.

    ``index`` 0 is the low-side stile and 1 the high-side one in sheet
    coordinates.  The centreline is measured from the stile's OUTSIDE edge
    (``stile_width - inset_from_inside_edge``, 0.6614 for the measured
    2"/34 mm pair) because that is the edge the placement gives us, and each
    pass runs ``overrun`` past both ends of the part — its own effective
    radius at its own depth, from
    :meth:`~.model.PostConfig.wdc_slot_reach`, which is why the two passes
    of one slot are NOT the same segment.  Second copy of
    :func:`~.generator.wdc_slot_segment`.
    """
    inset = spec.inset_from_outside_edge
    if not rotated:
        x = box.x0 + inset if index == 0 else box.x1 - inset
        return Box(x, box.y0 - overrun, x, box.y1 + overrun)
    y = box.y0 + inset if index == 0 else box.y1 - inset
    return Box(box.x0 - overrun, y, box.x1 + overrun, y)


def _zword(z: float) -> str:
    """A Z the way the program states it (four decimals, zeros stripped)."""
    return f"{round(z, PLACES):g}"


def _same_path(left: Box, right: Box) -> bool:
    """Do two tool-centre paths describe the same cut?

    Compared on the 0.0001 grid the post prints (:data:`PLACES`) with
    :data:`TOL` on top, so that the manifest's exact arithmetic and the
    file's four printed decimals agree without either being rounded
    generously: both sides go through the same ``round``.
    """
    a, b = left.rounded(PLACES), right.rounded(PLACES)
    return (
        abs(a.x0 - b.x0) <= TOL
        and abs(a.y0 - b.y0) <= TOL
        and abs(a.x1 - b.x1) <= TOL
        and abs(a.y1 - b.y1) <= TOL
    )


def _check_expected_work(
    found: list[_FoundCut], expected: ExpectedWork
) -> list[Violation]:
    """Match the file's cuts against the manifest, both ways round.

    One-for-one: each manifest entry consumes at most one recovered cut, so
    a program that emits one pass twice instead of two passes once fails as
    both a missing cut and an extra one rather than passing on a count.

    Release cuts (2026-08-05 amendment) are the one kind the manifest does NOT
    hold positionally, and they are held out of the match here.  The manifest is
    built from the layout and the post table, which say a sheet is tab-held but
    cannot say WHERE the tabs are without becoming a second copy of
    :mod:`~.tabs` — and a manifest that copied the placement engine could never
    catch it being wrong.  So the positional accounting is
    :func:`_check_hold`'s, span by span against the bridges it re-derives from
    the motion, and what the manifest adds is the structural fact the file alone
    cannot know: that this sheet OWES a release section (``released`` is how many
    release cuts the file made).
    """
    problems: list[Violation] = []
    released = sum(1 for item in found if item.kind == "release")
    if expected.release and not released:
        problems.append(
            Violation(
                "missing-cut",
                "this sheet is cut tab-held (the post table configures a T12 "
                "release pass), but the program contains no release cut at all - "
                "every part and every opening dropout would come off the machine "
                "still attached to the sheet by its tabs",
            )
        )
    elif not expected.release and any(item.kind == "release" for item in found):
        problems.append(
            Violation(
                "extra-cut",
                "the program contains a T12 tab-release cut, but this sheet's post "
                "table runs no release pass and its parts are not tab-held - "
                "nothing on it is standing where that cut goes",
            )
        )
    found = [item for item in found if item.kind != "release"]
    used = [False] * len(found)
    at: dict[int, _FoundCut] = {}
    for index, cut in enumerate(expected.cuts):
        position = _find_cut(found, used, cut)
        if position is None:
            problems.append(Violation("missing-cut", cut.describe()))
        else:
            used[position] = True
            at[index] = found[position]
    for position, item in enumerate(found):
        if used[position]:
            continue
        problems.append(
            Violation(
                "extra-cut",
                f"a {_KIND_NAMES.get(item.kind, item.kind)} at Z{_zword(item.z)} "
                f"runs x[{item.path.x0:.4f}, {item.path.x1:.4f}] "
                f"y[{item.path.y0:.4f}, {item.path.y1:.4f}], which is not a cut "
                f"this sheet's layout calls for",
                item.line,
            )
        )
    problems.extend(_check_chronology(expected, at))
    return problems


def _check_chronology(
    expected: ExpectedWork, at: dict[int, _FoundCut]
) -> list[Violation]:
    """The three ordering rules of the module docstring's "Chronology".

    Judged on the line each matched cut appears on, which is the only sense in
    which a re-parse can see time.  A relation whose two ends are not both
    matched is skipped in silence: the missing half is already a
    ``missing-cut``, and repeating it as an ordering complaint would bury the
    one finding that says what to do about it.
    """
    problems: list[Violation] = []
    cuts = expected.cuts
    total_passes = max(
        (c.pass_position for c in cuts if c.pass_position is not None), default=None
    )
    if total_passes is None:
        return problems  # a manifest with no perimeter pass has no chronology

    #: ``part -> (first-pass index, through index, [every other cut's index])``
    #: The first perimeter pass, whether it is the measured onion skin or the
    #: first rung of the 2026-08-05 max-bite ladder: either way it must precede
    #: the pass that cuts the outline through, and for the same reason.
    onion: dict[int, int] = {}
    through: dict[int, int] = {}
    others: dict[int, list[int]] = {}
    host_of: dict[int, int | None] = {}
    for index, cut in enumerate(cuts):
        host_of[cut.part] = cut.host
        if cut.kind == "perimeter" and cut.pass_position == total_passes:
            through[cut.part] = index
        elif cut.kind == "perimeter" and cut.pass_position == 0:
            # Rule (a) owns the first pass, so it is deliberately not also in
            # ``others``: one wrong order, one finding.  A manifest built from a
            # ONE-pass table never gets here — its only perimeter cut is
            # ``pass_position == total_passes == 0``, so it is the through pass
            # and rule (a) simply has no pair to relate.
            onion[cut.part] = index
        else:
            others.setdefault(cut.part, []).append(index)

    def line(index: int) -> int | None:
        item = at.get(index)
        return None if item is None else item.line

    def name(index: int) -> str:
        cut = cuts[index]
        return f"{cut.part_number} @({cut.part_box.x0:.4f},{cut.part_box.y0:.4f})"

    # (a) the first perimeter pass before the pass that cuts through
    for part, skin_index in onion.items():
        cut_index = through.get(part)
        if cut_index is None:
            continue
        skin, freed = line(skin_index), line(cut_index)
        if skin is None or freed is None or skin < freed:
            continue
        problems.append(
            Violation(
                "cut-order",
                f"{name(cut_index)}: the full-depth perimeter pass (line {freed}) "
                f"runs BEFORE {cuts[skin_index].what} (line {skin}). The earlier "
                f"pass leaves material holding the part while the rest of the sheet "
                f"is cut, and takes the outline's first bite so the deep pass does "
                f"not have to take it all; cutting through first gives up both",
                freed,
            )
        )

    # (b) an inner frame is freed before the host it sits in
    for part, cut_index in through.items():
        host = host_of.get(part)
        if host is None:
            continue
        host_index = through.get(host)
        if host_index is None:
            continue
        inner_line, host_line = line(cut_index), line(host_index)
        if inner_line is None or host_line is None or inner_line < host_line:
            continue
        problems.append(
            Violation(
                "cut-order",
                f"{name(cut_index)} is nested in {name(host_index)}, but the host "
                f"is cut free on line {host_line}, before the inner on line "
                f"{inner_line}. The inner would then be sitting in a hole in a "
                f"slab that is no longer attached to the sheet",
                host_line,
            )
        )

    # (c) nothing touches a part after the pass that frees it
    for part, cut_index in through.items():
        freed = line(cut_index)
        if freed is None:
            continue
        for other in others.get(part, ()):
            when = line(other)
            if when is None or when < freed:
                continue
            problems.append(
                Violation(
                    "cut-order",
                    f"{name(cut_index)}: {cuts[other].what} runs on line {when}, "
                    f"AFTER the full-depth perimeter pass on line {freed} that cuts "
                    f"the part free - the part is loose by then and nothing holds "
                    f"it in place for that cut",
                    when,
                )
            )
    return problems


def _find_cut(found: list[_FoundCut], used: list[bool], cut: ExpectedCut):
    for position, item in enumerate(found):
        if used[position] or item.kind != cut.kind:
            continue
        if abs(round(item.z, PLACES) - round(cut.z, PLACES)) > TOL:
            continue
        if _same_path(item.path, cut.path):
            return position
    return None
