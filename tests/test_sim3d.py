"""Milestone 3 of the 3D cut simulation: the view.

The package under test is ``faceframe_cnc/gui/sim3d/``, split so that
everything decidable is decided without a GL context:

  (a) the VIEWMODEL, pure Python: the current-tool field's text derived from
      each tool's own section header comment; the reveal model (what material
      is gone at a cursor) built from the emitter's own geometry helpers; the
      animation maths (a move's duration at a speed multiplier, and where the
      tool is part way through one); the cut-list rows;
  (b) the SCENE, a Qt3D entity tree built and driven offscreen -- one group
      per flat part, feature entities that appear and disappear with the
      cursor, a bit mesh swapped when the tool changes, a freed part lifted
      off the spoilboard, and colours that ARE the 2D preview's colours;
  (c) the WINDOW's wiring, with the 3D viewport injected as ``None``: the tool
      field across a section boundary, the cut list following the cursor,
      the scrub and speed sliders, play/pause, and the readouts;
  (d) purity: no Qt in the viewmodel and no wall clock anywhere in the
      package -- the QTimer is the only clock and it only hands the viewmodel
      a number of seconds;
  (e) determinism: driving the window's own step logic with the timer STOPPED
      lands in exactly the scene state plain cursor stepping lands in.

No test here instantiates a Qt3DWindow, a render surface or a camera
controller: those need a display, and a rule that only holds on a machine with
a monitor is not a rule this suite can keep.  The eyeball check is
``python -m faceframe_cnc.gui.sim3d --demo wdc``.

Every fixture is a sheet the planner built and every expected number is read
from the post table at assertion time.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import ast
import os
import unittest

# Must be set before the first QGuiApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from faceframe_cnc.gui.sim3d import viewmodel as vm
from faceframe_cnc.nesting import NestingConfig, PartSpec, Placement, SheetLayout
from faceframe_cnc.post import ProgramHeader, default_config, plan_sheet
from faceframe_cnc.post.generator import groove_segment, wdc_slot_segment
from faceframe_cnc.post.model import (
    SECTION_DETAIL,
    SECTION_RELEASE,
    SECTION_OPENINGS,
    SECTION_PANEL,
    SECTION_PERIMETER,
    SECTION_WDC_SLOT,
    ToolSpec,
)
from faceframe_cnc.sim import SimController, SimTimeline

try:
    from PySide6.Qt3DExtras import Qt3DExtras
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_QT = False

if HAVE_QT:
    from PySide6.Qt3DCore import Qt3DCore

    from faceframe_cnc.gui.sheet_canvas import (
        CHILD_FILL,
        CUSHION_EDGE,
        HOST_FILL,
        OPENING_EDGE,
        PART_FILL,
        SELECT_EDGE,
        SHEET_FILL,
    )
    from faceframe_cnc.gui.sim3d.scene import FREED_LIFT, SimScene
    from faceframe_cnc.gui.sim3d.window import PAUSE_TEXT, PLAY_TEXT, Sim3DWindow

TOL = 1e-9
CREATED = "01 JAN 27 - 08:00"

_APP = None


def setUpModule():  # noqa: N802 - unittest naming
    global _APP
    if HAVE_QT:
        _APP = QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------
# Fixtures (the pattern of tests/test_sim.py, rebuilt here on purpose: a test
# module that imported another test module's fixtures would make one file's
# failure the other's)
# --------------------------------------------------------------------------


def wdc_timeline() -> SimTimeline:
    """A planner-built sheet with a WDC frame on it, so a T17 section exists."""
    layout = SheetLayout(
        [
            Placement("WDC2436", 4.0, 4.0, 18.0, 36.0),
            Placement("W2436", 4.0, 44.0, 24.0, 36.0),
        ]
    )
    demand = [PartSpec("WDC2436", 18.0, 36.0, 1), PartSpec("W2436", 24.0, 36.0, 1)]
    program, plan = plan_sheet(
        layout,
        ProgramHeader(name="R990102N", created=CREATED),
        demand,
        NestingConfig(),
    )
    return SimTimeline.build(program, plan, default_config())


def nested_timeline() -> SimTimeline:
    """A W3012 turned 90 degrees inside a W2742's opening, and one beside it."""
    layout = SheetLayout(
        [
            Placement(
                "W2742",
                0.0,
                0.0,
                27.0,
                42.0,
                False,
                [Placement("W3012", 5.0, 6.0, 12.0, 30.0, True, [])],
            ),
            Placement("W3012", 30.0, 0.0, 12.0, 30.0, True, []),
        ]
    )
    demand = [PartSpec("W2742", 27.0, 42.0, 1), PartSpec("W3012", 30.0, 12.0, 2)]
    program, plan = plan_sheet(
        layout,
        ProgramHeader(name="R990103N", created=CREATED),
        demand,
        NestingConfig(),
    )
    return SimTimeline.build(program, plan, default_config())


def one_pass_timeline() -> SimTimeline:
    """The nested sheet with the post table a GENERATED sheet is cut with.

    The two fixtures above use the measured table deliberately: two perimeter
    passes, which is the dialect the reference programs are in and the only one
    with a separate onion-skin lap to draw.  This one uses what
    :func:`~faceframe_cnc.post.from_layout.post_config_for` hands the emitter —
    the through pass alone since the 2026-08-05 amendment (Scott, job R0805) —
    so the reveal model's skinned-then-freed pair can be checked where the two
    coincide.
    """
    from faceframe_cnc.post import post_config_for

    config = post_config_for(NestingConfig())
    layout = SheetLayout(
        [
            Placement(
                "W2742",
                0.0,
                0.0,
                27.0,
                42.0,
                False,
                [Placement("W3012", 5.0, 6.0, 12.0, 30.0, True, [])],
            ),
            Placement("W3012", 30.0, 0.0, 12.0, 30.0, True, []),
        ]
    )
    demand = [PartSpec("W2742", 27.0, 42.0, 1), PartSpec("W3012", 30.0, 12.0, 2)]
    program, plan = plan_sheet(
        layout,
        ProgramHeader(name="R990104N", created=CREATED),
        demand,
        NestingConfig(),
        config,
    )
    return SimTimeline.build(program, plan, config)


_TIMELINES: dict[str, SimTimeline] = {}


def timeline(label: str) -> SimTimeline:
    """Every fixture, built once: planning three sheets is not free."""
    if not _TIMELINES:
        _TIMELINES["WDC"] = wdc_timeline()
        _TIMELINES["NESTED"] = nested_timeline()
        _TIMELINES["ONE_PASS"] = one_pass_timeline()
    return _TIMELINES[label]


def hosts_and_children(item: SimTimeline) -> dict[int, list[int]]:
    """``{host flat index: [child flat indices]}`` for a nested sheet."""
    flat = item.program.flat_parts()
    index_of = {id(part): i for i, part in enumerate(flat)}
    out: dict[int, list[int]] = {}

    def walk(items, host):
        for part in items:
            if host is not None:
                out.setdefault(host, []).append(index_of[id(part)])
            walk(part.children, index_of[id(part)])

    walk(item.program.parts, None)
    return out


def at_end_of(item: SimTimeline, **match) -> SimController:
    """A cursor parked just past the first cut matching ``match``."""
    controller = SimController(item)
    cut = first_cut(item, **match)
    controller.seek(cut.end)
    return controller


def first_cut(item: SimTimeline, **match):
    for cut in item.cuts:
        if all(getattr(cut, key) == value for key, value in match.items()):
            return cut
    raise AssertionError(f"no cut matches {match}")


def reveals_at(controller: SimController, item: SimTimeline):
    """The reveal list at a cursor, plan included.

    The plan is what carries the holding tabs since the 2026-08-05 amendment
    (:attr:`~faceframe_cnc.post.model.CutPlan.tabs`), and the scene passes it for
    the same reason: without it the kerf reveals claim more than the pass cut.
    """
    return vm.reveals(controller.state, item.program, item.config, item.plan)


def of_kind(items, kind):
    return [reveal for reveal in items if reveal.kind is kind]


# --------------------------------------------------------------------------
# (a) the viewmodel -- no Qt anywhere in this section
# --------------------------------------------------------------------------


def comment_tokens(tool: ToolSpec) -> list[str]:
    """The words inside a tool's own section header comment.

    Split on the punctuation the comment happens to use so the assertion is
    about the CONTENT of :attr:`~faceframe_cnc.post.model.ToolSpec.header_comment`
    and not about any spelling this test made up.
    """
    inner = tool.header_comment.strip()
    body = inner[inner.index(":") + 1 : inner.rindex(")")]
    return [token for token in body.replace("-", " ").split() if token]


class ToolFieldTest(unittest.TestCase):
    """The owner's first ask: a field that names the bit that is cutting."""

    def test_every_tool_in_the_table_is_named_by_its_own_header_comment(self):
        config = default_config()
        seen = set()
        for section, tool in config.tools.items():
            with self.subTest(section=section, tool=tool.number):
                text = vm.tool_display(tool)
                self.assertTrue(
                    text.startswith(f"T{tool.number}"),
                    f"{text!r} must lead with the tool number",
                )
                for token in comment_tokens(tool):
                    self.assertIn(token, text, f"{token!r} missing from {text!r}")
                self.assertNotIn("ROUTE TOOL", text, "the wrapper is not the name")
                self.assertNotIn("(", text)
                seen.add(text)
        # T11 cuts two sections, so four tools give three distinct fields.
        self.assertEqual(len(seen), len({t.number for t in config.tools.values()}))

    def test_the_field_changes_the_moment_the_tool_does(self):
        item = timeline("WDC")
        controller = SimController(item)
        texts = []
        for span in item.sections:
            controller.seek(span.start)
            texts.append(vm.tool_display(controller.tool))
        expected = [
            vm.tool_display(item.config.tool(span.section)) for span in item.sections
        ]
        self.assertEqual(texts, expected)
        self.assertGreater(len(set(texts)), 1, "the fixture changes tools")

    def test_a_comment_this_post_did_not_write_falls_back_to_the_facts(self):
        odd = ToolSpec(
            number=99,
            header_comment="(SOMETHING ELSE ENTIRELY)",
            diameter_comment="(DIAMETER: 0.5)",
            diameter=0.5,
            speed=1000,
        )
        text = vm.tool_display(odd)
        self.assertIn("T99", text)
        self.assertIn("0.5", text)

    def test_a_comment_with_nothing_but_the_number_falls_back_too(self):
        bare = ToolSpec(
            number=7,
            header_comment="(ROUTE TOOL #7: T7)",
            diameter_comment="(DIAMETER: 0.25)",
            diameter=0.25,
            speed=1000,
        )
        self.assertIn("0.25", vm.tool_display(bare))

    def test_an_empty_spindle_says_so(self):
        self.assertEqual(vm.tool_display(None), vm.NO_TOOL_TEXT)


class ReadoutTest(unittest.TestCase):
    """The rest of the strip, off the same cursor."""

    def test_the_readouts_are_the_controllers_own_answers(self):
        item = timeline("WDC")
        controller = SimController(item)
        for _ in range(12):
            controller.step_forward()
        readouts = vm.Readouts.from_controller(controller, 4.0)
        self.assertEqual(readouts.tool, vm.tool_display(controller.tool))
        self.assertEqual(readouts.feed, vm.feed_text(controller.feed))
        self.assertEqual(readouts.z, vm.z_text(controller.position[2]))
        self.assertEqual(readouts.section, vm.section_display(controller.section))
        self.assertEqual(readouts.cut_label, controller.current_cut.label)
        self.assertIn(str(controller.cut_index + 1), readouts.counter)
        self.assertIn(str(controller.cut_total), readouts.counter)
        self.assertEqual(readouts.speed, "4x")

    def test_a_rapid_has_no_feed_and_an_unset_z_says_so(self):
        self.assertEqual(vm.feed_text(None), "rapid")
        self.assertIn("not set", vm.z_text(None))
        item = timeline("WDC")
        controller = SimController(item)
        # Before the first G43 the work Z is a machine position this post
        # never states, and the strip must not invent one.
        self.assertIn("not set", vm.Readouts.from_controller(controller).z)

    def test_at_the_end_of_the_program_the_counter_says_complete(self):
        item = timeline("WDC")
        controller = SimController(item)
        controller.to_end()
        readouts = vm.Readouts.from_controller(controller)
        self.assertIn("complete", readouts.counter)
        self.assertIn(str(item.cut_total), readouts.counter)
        self.assertEqual(readouts.cut_label, "program complete")


class RevealModelTest(unittest.TestCase):
    """What material is gone, taken from the emitter's own geometry."""

    def test_nothing_is_revealed_before_the_program_starts(self):
        for label in ("WDC", "NESTED"):
            with self.subTest(case=label):
                item = timeline(label)
                self.assertEqual(
                    vm.reveals(item.state_at(0), item.program, item.config), ()
                )

    def test_a_finished_groove_reveals_one_channel_the_config_describes(self):
        item = timeline("WDC")
        cut = first_cut(item, section=SECTION_PANEL)
        controller = SimController(item)

        controller.seek(cut.last_step)
        self.assertEqual(
            reveals_at(controller, item), (), "a groove being cut is not a groove"
        )

        controller.seek(cut.end)
        items = reveals_at(controller, item)
        self.assertEqual(len(items), 1)
        reveal = items[0]
        part = item.program.flat_parts()[cut.part_index]
        panel = item.config.panel
        tool = item.config.tool(SECTION_PANEL)
        self.assertIs(reveal.kind, vm.RevealKind.GROOVE)
        self.assertEqual(reveal.part_index, cut.part_index)
        self.assertEqual(
            reveal.segment,
            groove_segment(part, cut.feature.index, panel, tool.radius),
        )
        self.assertAlmostEqual(reveal.width, tool.diameter, delta=TOL)
        self.assertAlmostEqual(
            reveal.depth, item.config.stock_top_z - panel.z_cut, delta=TOL
        )
        self.assertAlmostEqual(reveal.z_cut, panel.z_cut, delta=TOL)

    def test_a_channels_swept_footprint_reaches_the_cutters_radius_past_it(self):
        item = timeline("WDC")
        cut = first_cut(item, section=SECTION_PANEL)
        controller = at_end_of(item, section=SECTION_PANEL)
        reveal = reveals_at(controller, item)[0]
        (x0, y0), (x1, y1) = reveal.segment
        swept = reveal.swept_box
        half = item.config.tool(SECTION_PANEL).radius
        self.assertAlmostEqual(swept.x0, min(x0, x1) - half, delta=TOL)
        self.assertAlmostEqual(swept.y1, max(y0, y1) + half, delta=TOL)
        self.assertAlmostEqual(
            swept.width if reveal.axis == "x" else swept.height,
            abs(x1 - x0) + abs(y1 - y0) + 2 * half,
            delta=TOL,
        )
        self.assertEqual(cut.section, SECTION_PANEL)

    def test_a_slots_two_bites_reveal_two_widths_and_deeper_means_wider(self):
        item = timeline("WDC")
        spec = item.config.wdc_slot
        section = next(s for s in item.sections if s.section == SECTION_WDC_SLOT)
        controller = SimController(item)
        controller.seek(section.end)
        slots = of_kind(reveals_at(controller, item), vm.RevealKind.SLOT)
        self.assertEqual(len(slots), 2 * len(spec.z_cuts), "two stiles, two bites")

        part_index = slots[0].part_index
        part = item.program.flat_parts()[part_index]
        one_stile = sorted(
            (r for r in slots if r.part_index == part_index and r.feature_index == 0),
            key=lambda r: r.pass_index,
        )
        self.assertEqual([r.pass_index for r in one_stile], list(range(len(spec.z_cuts))))
        widths = []
        for reveal in one_stile:
            reach = item.config.wdc_slot_reach(reveal.pass_index)
            self.assertAlmostEqual(reveal.width, 2 * reach, delta=TOL)
            self.assertAlmostEqual(
                reveal.depth,
                item.config.stock_top_z - spec.z_cuts[reveal.pass_index],
                delta=TOL,
            )
            # 45-degree flanks: the surface half-width IS the depth of cut,
            # until the cone runs out of flank at the bit's own radius.
            self.assertAlmostEqual(
                reach,
                spec.surface_radius(reveal.depth, item.config.tool(SECTION_WDC_SLOT).radius),
                delta=TOL,
            )
            self.assertEqual(
                reveal.segment, wdc_slot_segment(part, 0, spec, reach)
            )
            widths.append((reveal.depth, reveal.width))
        deepest = max(widths)
        self.assertEqual(deepest, max(widths, key=lambda pair: pair[1]))
        self.assertGreater(deepest[1], min(w for _d, w in widths))

    def test_an_opening_reveals_the_hole_each_pass_actually_leaves(self):
        item = timeline("WDC")
        for section, kind, spec in (
            # The T11 rough is drawn at the DEEPEST rung of its ladder
            # (2026-08-05 max-bite amendment), which is the depth the pocket is
            # left at once the roughing is done.
            (
                SECTION_OPENINGS,
                vm.RevealKind.OPENING,
                item.config.openings_passes[-1],
            ),
            (SECTION_DETAIL, vm.RevealKind.DETAIL, item.config.detail_pass),
        ):
            with self.subTest(section=section):
                cut = first_cut(item, section=section)
                controller = at_end_of(item, section=section)
                found = [
                    r
                    for r in of_kind(reveals_at(controller, item), kind)
                    if r.part_index == cut.part_index
                    and r.feature_index == cut.feature.index
                ]
                self.assertEqual(len(found), 1)
                opening = item.program.flat_parts()[cut.part_index].openings[
                    cut.feature.index
                ]
                tool = item.config.tool(section)
                expected = opening.grow(spec.offset + tool.radius)
                self.assertEqual(found[0].box.rounded(6), expected.rounded(6))
                self.assertAlmostEqual(
                    found[0].depth, item.config.stock_top_z - spec.z_cut, delta=TOL
                )

    def test_the_t12_pass_finishes_the_opening_to_its_own_line(self):
        """The detail pass's hole IS the opening the geometry engine asked for."""
        item = timeline("WDC")
        cut = first_cut(item, section=SECTION_DETAIL)
        controller = at_end_of(item, section=SECTION_DETAIL)
        reveal = next(
            r
            for r in of_kind(reveals_at(controller, item), vm.RevealKind.DETAIL)
            if r.part_index == cut.part_index
        )
        opening = item.program.flat_parts()[cut.part_index].openings[cut.feature.index]
        self.assertEqual(reveal.box.rounded(6), opening.rounded(6))

    def test_the_skin_appears_after_pass_zero_and_freed_after_the_last(self):
        """The measured two-pass table, where the two are separate events."""
        item = timeline("NESTED")
        last = len(item.config.perimeter_passes) - 1
        controller = SimController(item)
        for cut in item.cuts:
            if cut.section != SECTION_PERIMETER or cut.pass_index != 0:
                continue
            with self.subTest(part=cut.part_number):
                controller.seek(cut.end)
                mine = [
                    r
                    for r in reveals_at(controller, item)
                    if r.part_index == cut.part_index
                ]
                self.assertEqual(
                    [r for r in mine if r.kind is vm.RevealKind.SKIN][0].pass_index, 0
                )
                self.assertEqual(of_kind(mine, vm.RevealKind.FREED), [])

                through = first_cut(
                    item,
                    section=SECTION_PERIMETER,
                    pass_index=last,
                    part_index=cut.part_index,
                )
                controller.seek(through.end)
                mine = [
                    r
                    for r in reveals_at(controller, item)
                    if r.part_index == cut.part_index
                ]
                freed = of_kind(mine, vm.RevealKind.FREED)
                self.assertEqual(len(freed), 1)
                self.assertEqual(freed[0].pass_index, last)
                part = item.program.flat_parts()[cut.part_index]
                self.assertEqual(freed[0].box, part.box, "a freed part is its outline")
                skin = [r for r in mine if r.kind is vm.RevealKind.SKIN][0]
                self.assertEqual(
                    skin.box.rounded(6),
                    part.box.grow(item.config.perimeter_passes[0].offset).rounded(6),
                    "the scored outline is the kerf's own centre path",
                )

    def test_the_perimeter_ladder_scores_the_part_and_leaves_it_tab_held(self):
        """The kerf appears, the part does NOT come loose (2026-08-05 §3d).

        The kerf drawn is perimeter pass 0's own centre path — on a generated
        sheet the first rung of the max-bite ladder (offset 0.1895 at Z0.372) —
        and it is drawn with the standing tabs still in it through the through
        pass too, which is the honest picture: the outline is cut and the piece
        has not moved.  The loose part arrives with the release section, in the
        test below.
        """
        item = timeline("ONE_PASS")
        self.assertEqual(len(item.config.perimeter_passes), 2, "the max-bite ladder")
        spec = item.config.perimeter_passes[0]
        controller = SimController(item)
        for cut in [c for c in item.cuts if c.section == SECTION_PERIMETER]:
            with self.subTest(part=cut.part_number, pass_index=cut.pass_index):
                controller.seek(cut.end)
                mine = [
                    r
                    for r in reveals_at(controller, item)
                    if r.part_index == cut.part_index
                ]
                skin = of_kind(mine, vm.RevealKind.SKIN)
                freed = of_kind(mine, vm.RevealKind.FREED)
                self.assertEqual(len(skin), 1)
                self.assertEqual(len(freed), 0, "tab-held, not loose")
                self.assertEqual(skin[0].pass_index, 0)
                part = item.program.flat_parts()[cut.part_index]
                self.assertEqual(
                    skin[0].box.rounded(6), part.box.grow(spec.offset).rounded(6)
                )

    def test_the_release_section_is_what_shows_a_part_loose(self):
        item = timeline("ONE_PASS")
        controller = SimController(item)
        release = [
            c
            for c in item.cuts
            if c.section == SECTION_RELEASE and c.feature.kind == "perimeter"
        ]
        self.assertTrue(release, "a generated sheet has a release section")
        for cut in release:
            with self.subTest(part=cut.part_number):
                controller.seek(cut.end)
                mine = [
                    r
                    for r in reveals_at(controller, item)
                    if r.part_index == cut.part_index
                ]
                freed = of_kind(mine, vm.RevealKind.FREED)
                self.assertEqual(len(freed), 1)
                part = item.program.flat_parts()[cut.part_index]
                self.assertEqual(freed[0].box, part.box)
                self.assertEqual(
                    of_kind(mine, vm.RevealKind.BRIDGE),
                    [],
                    "and its holding tabs are gone",
                )

    def test_a_host_is_revealed_loose_only_after_its_passengers(self):
        """Inners before hosts, read off the reveal model (two-pass table)."""
        item = timeline("NESTED")
        families = hosts_and_children(item)
        self.assertTrue(families, "the fixture must nest something")
        last = len(item.config.perimeter_passes) - 1
        controller = SimController(item)
        for host, kids in families.items():
            cut = first_cut(
                item, section=SECTION_PERIMETER, pass_index=last, part_index=host
            )
            controller.seek(cut.end)
            loose = vm.freed_parts(reveals_at(controller, item))
            self.assertIn(host, loose)
            for kid in kids:
                self.assertIn(kid, loose, "a host carried a captive passenger away")

    def test_a_nested_frame_is_marked_nested_and_its_host_a_host(self):
        item = timeline("NESTED")
        families = hosts_and_children(item)
        controller = SimController(item)
        controller.to_end()
        by_part: dict[int, vm.Reveal] = {}
        for reveal in reveals_at(controller, item):
            by_part.setdefault(reveal.part_index, reveal)
        children = {kid for kids in families.values() for kid in kids}
        for index, reveal in by_part.items():
            self.assertEqual(reveal.nested, index in children)
            self.assertEqual(reveal.host, index in families)

    def test_every_reveal_at_a_cursor_has_its_own_key(self):
        for label in ("WDC", "NESTED"):
            with self.subTest(case=label):
                item = timeline(label)
                controller = SimController(item)
                controller.to_end()
                items = reveals_at(controller, item)
                keys = [reveal.key for reveal in items]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertTrue(items)

    def test_the_same_state_always_gives_the_same_reveal_list(self):
        item = timeline("WDC")
        first = vm.reveals(item.state_at(60), item.program, item.config)
        second = vm.reveals(item.state_at(60), item.program, item.config)
        self.assertEqual(first, second)


class AnimationMathsTest(unittest.TestCase):
    """Seconds and inches; the owner's third ask is the multiplier here."""

    def feed_step(self, item: SimTimeline) -> int:
        for index, motion in enumerate(item.steps):
            if motion.feed is not None and item.path_lengths[index] > 0:
                return index
        raise AssertionError("no cutting move in the fixture")

    def rapid_step(self, item: SimTimeline) -> int:
        for index, motion in enumerate(item.steps):
            if motion.feed is None and item.path_lengths[index] > 0:
                return index
        raise AssertionError("no rapid in the fixture")

    def test_at_one_times_a_cutting_move_takes_the_machines_own_time(self):
        item = timeline("WDC")
        index = self.feed_step(item)
        motion = item.steps[index]
        length = item.path_lengths[index]
        expected = length / (motion.feed / 60.0)
        self.assertAlmostEqual(
            vm.motion_duration(motion, length, 1.0), expected, delta=TOL
        )
        self.assertAlmostEqual(
            vm.step_duration(length, motion.feed, 1.0), expected, delta=TOL
        )

    def test_the_multiplier_divides_the_duration(self):
        item = timeline("WDC")
        index = self.feed_step(item)
        motion = item.steps[index]
        length = item.path_lengths[index]
        base = vm.motion_duration(motion, length, 1.0)
        for multiplier in vm.SPEED_CHOICES:
            with self.subTest(multiplier=multiplier):
                self.assertAlmostEqual(
                    vm.motion_duration(motion, length, multiplier),
                    base / multiplier,
                    delta=TOL,
                )
        self.assertGreater(vm.DEFAULT_SPEED, 1.0, "playback opens faster than real time")

    def test_a_rapid_runs_at_the_documented_display_rate(self):
        item = timeline("WDC")
        index = self.rapid_step(item)
        motion = item.steps[index]
        length = item.path_lengths[index]
        self.assertIsNone(motion.feed)
        self.assertAlmostEqual(
            vm.motion_duration(motion, length, 1.0),
            length / (vm.RAPID_DISPLAY_IPM / 60.0),
            delta=TOL,
        )

    def test_a_zero_length_move_takes_no_time(self):
        self.assertEqual(vm.step_duration(0.0, 498.2, 1.0), 0.0)

    def test_a_non_positive_multiplier_is_refused(self):
        for bad in (0.0, -1.0):
            with self.subTest(multiplier=bad):
                with self.assertRaises(ValueError):
                    vm.step_duration(1.0, 100.0, bad)

    def test_interpolation_hits_both_ends_of_the_move_exactly(self):
        item = timeline("WDC")
        rapid_z = item.config.rapid_z
        for index, motion in enumerate(item.steps):
            start = vm.point_at(motion, 0.0, rapid_z)
            end = vm.point_at(motion, 1.0, rapid_z)
            self.assertEqual(
                start,
                (
                    motion.from_x,
                    motion.from_y,
                    rapid_z if motion.from_z is None else motion.from_z,
                ),
            )
            self.assertEqual(
                end,
                (
                    motion.to_x,
                    motion.to_y,
                    rapid_z if motion.to_z is None else motion.to_z,
                ),
            )
            # Out of range clamps: the tool is never drawn past the move.
            self.assertEqual(vm.point_at(motion, -3.0, rapid_z), start)
            self.assertEqual(vm.point_at(motion, 9.0, rapid_z), end)
            self.assertEqual(index, index)

    def test_half_way_along_a_move_is_half_way(self):
        item = timeline("WDC")
        index = self.feed_step(item)
        motion = item.steps[index]
        middle = vm.point_at(motion, 0.5, item.config.rapid_z)
        self.assertAlmostEqual(middle[0], (motion.from_x + motion.to_x) / 2, delta=TOL)
        self.assertAlmostEqual(middle[1], (motion.from_y + motion.to_y) / 2, delta=TOL)

    def test_an_unknown_z_is_displayed_at_the_rapid_plane(self):
        item = timeline("WDC")
        unknown = [m for m in item.steps if m.from_z is None or m.to_z is None]
        self.assertTrue(unknown, "every section opens with one of these")
        for motion in unknown:
            for t in (0.0, 0.5, 1.0):
                point = vm.point_at(motion, t, item.config.rapid_z)
                if motion.from_z is None and motion.to_z is None:
                    self.assertAlmostEqual(point[2], item.config.rapid_z, delta=TOL)

    def test_the_tip_holds_still_once_the_program_is_finished(self):
        item = timeline("WDC")
        controller = SimController(item)
        controller.to_end()
        x, y, z = controller.position
        self.assertEqual(
            vm.tip_at(controller, 0.5, item.config),
            (x, y, item.config.rapid_z if z is None else z),
        )


class CutListTest(unittest.TestCase):
    def test_the_rows_are_the_timelines_cuts_in_order(self):
        for label in ("WDC", "NESTED"):
            with self.subTest(case=label):
                item = timeline(label)
                rows = vm.cut_rows(item)
                self.assertEqual(len(rows), item.cut_total)
                self.assertEqual(
                    [(r.index, r.label, r.section, r.part_index) for r in rows],
                    [
                        (c.index, c.label, c.section, c.part_index)
                        for c in item.cuts
                    ],
                )

    def test_the_current_row_follows_the_cursor_and_stops_at_the_last(self):
        item = timeline("WDC")
        controller = SimController(item)
        for cut in item.cuts:
            controller.seek(cut.start)
            self.assertEqual(vm.current_row(controller), cut.index)
        controller.to_end()
        self.assertEqual(
            vm.current_row(controller),
            item.cut_total - 1,
            "the finished program leaves the eye on the cut that just ran",
        )


class BitAndCameraTest(unittest.TestCase):
    def test_only_the_tables_v_bit_is_a_cone_and_its_flank_sizes_it(self):
        item = timeline("WDC")
        config = item.config
        v_tool = config.tool(SECTION_WDC_SLOT)
        for section, tool in config.tools.items():
            with self.subTest(section=section):
                profile = vm.bit_profile(tool, config)
                self.assertAlmostEqual(profile.radius, tool.radius, delta=TOL)
                if tool.number == v_tool.number:
                    self.assertEqual(profile.shape, "cone")
                    self.assertAlmostEqual(
                        profile.length,
                        tool.radius / config.wdc_slot.flank_slope,
                        delta=TOL,
                    )
                else:
                    self.assertEqual(profile.shape, "cylinder")
                    self.assertGreater(profile.length, tool.diameter)

    def test_the_three_round_bits_are_three_different_sizes(self):
        config = default_config()
        lengths = {
            vm.bit_profile(tool, config).length
            for tool in config.tools.values()
            if vm.bit_profile(tool, config).shape == "cylinder"
        }
        self.assertEqual(len(lengths), 3)

    def test_the_camera_starts_outside_the_sheet_looking_at_the_middle_of_it(self):
        config = default_config()
        eye, centre, up = vm.camera_pose(config)
        self.assertEqual(up, (0.0, 0.0, 1.0))
        self.assertAlmostEqual(centre[0], config.sheet_width / 2, delta=TOL)
        self.assertAlmostEqual(centre[1], config.sheet_length / 2, delta=TOL)
        self.assertLess(eye[0], 0.0, "front-left of the sheet")
        self.assertLess(eye[1], 0.0)
        self.assertGreater(eye[2], config.sheet_length / 4, "and well above it")

    def test_the_opening_view_holds_the_whole_sheet(self):
        """All four corners inside the field of view the lens is set to.

        The reset-view action is this pose, so a distance that cropped the
        sheet would crop it again every time the operator asked for the
        overview back.
        """
        from math import atan, radians, sqrt, tan

        config = default_config()
        eye, centre, _up = vm.camera_pose(config)

        def minus(a, b):
            return tuple(a[i] - b[i] for i in range(3))

        def dot(a, b):
            return sum(a[i] * b[i] for i in range(3))

        def cross(a, b):
            return (
                a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0],
            )

        def unit(a):
            length = sqrt(dot(a, a))
            return tuple(c / length for c in a)

        forward = unit(minus(centre, eye))
        right = unit(cross(forward, (0.0, 0.0, 1.0)))
        screen_up = cross(right, forward)
        half_fov = radians(vm.CAMERA_FOV_DEGREES) / 2.0
        # A perspective lens states the VERTICAL angle and the aspect widens
        # it, so the sideways check is against the narrowest viewport shape the
        # window allows.
        half_wide = atan(tan(half_fov) * vm.CAMERA_MIN_ASPECT)
        for corner in (
            (0.0, 0.0, 0.0),
            (config.sheet_width, 0.0, 0.0),
            (0.0, config.sheet_length, 0.0),
            (config.sheet_width, config.sheet_length, 0.0),
        ):
            with self.subTest(corner=corner):
                view = minus(corner, eye)
                depth = dot(view, forward)
                self.assertGreater(depth, 0.0, "behind the camera")
                self.assertLess(
                    atan(abs(dot(view, screen_up)) / depth),
                    half_fov,
                    "the sheet is cropped top or bottom",
                )
                self.assertLess(
                    atan(abs(dot(view, right)) / depth), half_wide, "cropped sideways"
                )


# --------------------------------------------------------------------------
# (b) the scene -- Qt, offscreen, no GL context
# --------------------------------------------------------------------------


def entities(node) -> list:
    """Every entity under ``node``, itself excluded.

    Components are parented to their entity (so nothing is collected out from
    under the tree), which is why this filters on type rather than walking
    every child.
    """
    out = []
    for child in node.children():
        if isinstance(child, Qt3DCore.QEntity):
            out.append(child)
            out.extend(entities(child))
    return out


def named(node, name: str):
    for entity in entities(node):
        if entity.objectName() == name:
            return entity
    raise AssertionError(f"no entity named {name!r}")


def diffuse_of(entity):
    for component in entity.components():
        if isinstance(component, Qt3DExtras.QPhongMaterial):
            return component.diffuse()
    raise AssertionError(f"{entity.objectName()} has no phong material")


def mesh_of(entity):
    for component in entity.components():
        if isinstance(
            component,
            (
                Qt3DExtras.QCuboidMesh,
                Qt3DExtras.QCylinderMesh,
                Qt3DExtras.QConeMesh,
            ),
        ):
            return component
    raise AssertionError(f"{entity.objectName()} has no mesh")


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class SceneTest(unittest.TestCase):
    def setUp(self):
        self.item = timeline("WDC")
        self.scene = SimScene(self.item.program, self.item.config)
        self.controller = SimController(self.item)

    def drive_to(self, position: int) -> None:
        self.controller.seek(position)
        self.scene.update_from(self.controller)

    def test_the_tree_has_one_group_per_flat_part_and_the_two_slabs(self):
        parts = self.item.program.flat_parts()
        self.assertEqual(len(self.scene.part_groups), len(parts))
        for index, _part in enumerate(parts):
            self.assertIs(named(self.scene.root, f"part-{index}"), self.scene.part_groups[index])
        self.assertIsNotNone(named(self.scene.root, "stock"))
        self.assertIsNotNone(named(self.scene.root, "spoilboard"))
        self.assertIsNotNone(named(self.scene.root, "spindle"))

    def test_the_stock_slab_is_the_configured_sheet_and_thickness(self):
        config = self.item.config
        mesh = mesh_of(named(self.scene.root, "stock"))
        self.assertAlmostEqual(mesh.xExtent(), config.sheet_width, places=4)
        self.assertAlmostEqual(mesh.yExtent(), config.sheet_length, places=4)
        self.assertAlmostEqual(mesh.zExtent(), config.material_thickness, places=4)

    def test_nothing_is_cut_before_the_program_starts(self):
        self.scene.update_from(self.controller)
        self.assertEqual(self.scene.visible_keys(), frozenset())
        self.assertEqual(self.scene.freed, frozenset())
        for index in range(len(self.scene.part_groups)):
            self.assertAlmostEqual(self.scene.lift_of(index), 0.0, places=5)

    def test_a_feature_entity_appears_when_its_cut_finishes(self):
        cut = first_cut(self.item, section=SECTION_PANEL)
        self.drive_to(cut.last_step)
        self.assertEqual(self.scene.visible_keys(), frozenset())
        self.drive_to(cut.end)
        expected = frozenset(
            r.key for r in reveals_at(self.controller, self.item)
        )
        self.assertEqual(self.scene.visible_keys(), expected)
        self.assertEqual(len(expected), 1)
        key = next(iter(expected))
        entity = named(self.scene.part_groups[cut.part_index], key)
        self.assertTrue(entity.isEnabled())

    def test_scrubbing_backwards_puts_the_material_back(self):
        cut = first_cut(self.item, section=SECTION_PANEL)
        self.drive_to(cut.end)
        self.assertTrue(self.scene.visible_keys())
        self.drive_to(0)
        self.assertEqual(self.scene.visible_keys(), frozenset())

    def test_every_revealed_feature_has_an_entity_at_the_end_of_the_program(self):
        self.drive_to(self.item.step_total)
        items = reveals_at(self.controller, self.item)
        drawn = {r.key for r in items if r.kind is not vm.RevealKind.FREED}
        self.assertEqual(self.scene.visible_keys(), frozenset(drawn))
        self.assertTrue(drawn)

    def test_a_v_slot_is_drawn_as_two_angled_flanks_not_a_box(self):
        section = next(s for s in self.item.sections if s.section == SECTION_WDC_SLOT)
        self.drive_to(section.end)
        slots = of_kind(reveals_at(self.controller, self.item), vm.RevealKind.SLOT)
        self.assertTrue(slots)
        for reveal in slots:
            group = named(self.scene.part_groups[reveal.part_index], reveal.key)
            flanks = entities(group)
            self.assertEqual(len(flanks), 2, "a V has two flanks")
            for flank in flanks:
                transform = next(
                    c
                    for c in flank.components()
                    if isinstance(c, Qt3DCore.QTransform)
                )
                # An identity rotation has scalar 1; a tilted flank does not.
                self.assertLess(
                    abs(transform.rotation().scalar()),
                    1.0 - 1e-4,
                    "a flank is angled, not a box side",
                )

    def test_the_bit_swaps_when_the_section_does(self):
        seen = []
        for span in self.item.sections:
            self.drive_to(span.start)
            tool = self.item.config.tool(span.section)
            profile = self.scene.bit_profile
            self.assertEqual(self.scene.tool, tool)
            self.assertEqual(profile, vm.bit_profile(tool, self.item.config))
            mesh = mesh_of(self.scene.bit_entity)
            if profile.shape == "cone":
                self.assertIsInstance(mesh, Qt3DExtras.QConeMesh)
                self.assertAlmostEqual(mesh.topRadius(), tool.radius, places=4)
            else:
                self.assertIsInstance(mesh, Qt3DExtras.QCylinderMesh)
                self.assertAlmostEqual(mesh.radius(), tool.radius, places=4)
            seen.append((type(mesh).__name__, round(profile.radius, 4)))
        self.assertGreater(len(set(seen)), 2, "T13, T17 and T11 are three bits")
        self.assertIn("QConeMesh", [name for name, _ in seen])

    def test_the_bit_tip_tracks_the_commanded_z(self):
        cut = first_cut(self.item, section=SECTION_PANEL)
        self.drive_to(cut.end)
        x, y, z = self.controller.position
        self.assertAlmostEqual(self.scene.tip[0], x, delta=TOL)
        self.assertAlmostEqual(self.scene.tip[1], y, delta=TOL)
        self.assertAlmostEqual(self.scene.tip[2], z, delta=TOL)

    def test_an_unknown_z_puts_the_tip_at_the_rapid_plane(self):
        self.scene.update_from(self.controller)
        self.assertAlmostEqual(self.scene.tip[2], self.item.config.rapid_z, delta=TOL)

    def test_a_freed_part_lifts_off_the_spoilboard_and_lights_its_edge(self):
        last = len(self.item.config.perimeter_passes) - 1
        cut = first_cut(self.item, section=SECTION_PERIMETER, pass_index=last)
        self.drive_to(cut.last_step)
        self.assertAlmostEqual(self.scene.lift_of(cut.part_index), 0.0, places=5)
        highlight = self.scene.part_highlights[cut.part_index]
        self.assertFalse(highlight.isEnabled())

        self.drive_to(cut.end)
        self.assertIn(cut.part_index, self.scene.freed)
        self.assertAlmostEqual(
            self.scene.lift_of(cut.part_index), FREED_LIFT, places=5
        )
        self.assertTrue(highlight.isEnabled())
        for other in range(len(self.scene.part_groups)):
            if other != cut.part_index and other not in self.scene.freed:
                self.assertAlmostEqual(self.scene.lift_of(other), 0.0, places=5)

    def test_the_colours_are_the_two_d_previews_own_colours(self):
        self.assertEqual(diffuse_of(named(self.scene.root, "stock")), SHEET_FILL)
        self.assertEqual(
            diffuse_of(named(self.scene.root, "spoilboard")), CUSHION_EDGE
        )
        parts = self.item.program.flat_parts()
        depths = hosts_and_children(self.item)
        for index, part in enumerate(parts):
            face = named(self.scene.part_groups[index], f"part-{index}-face")
            expected = HOST_FILL if part.children else PART_FILL
            self.assertEqual(diffuse_of(face), expected, part.part_number)
            self.assertEqual(index in depths, bool(part.children))

        self.drive_to(self.item.step_total)
        skin = of_kind(reveals_at(self.controller, self.item), vm.RevealKind.SKIN)[0]
        ring = named(self.scene.part_groups[skin.part_index], skin.key)
        self.assertEqual(diffuse_of(entities(ring)[0]), SELECT_EDGE)
        detail = of_kind(reveals_at(self.controller, self.item), vm.RevealKind.DETAIL)[0]
        rim = named(self.scene.part_groups[detail.part_index], detail.key)
        self.assertEqual(diffuse_of(entities(rim)[0]), OPENING_EDGE)

    def test_a_nested_part_is_tinted_as_a_passenger(self):
        item = timeline("NESTED")
        scene = SimScene(item.program, item.config)
        children = {
            kid for kids in hosts_and_children(item).values() for kid in kids
        }
        self.assertTrue(children)
        for index in children:
            face = named(scene.part_groups[index], f"part-{index}-face")
            self.assertEqual(diffuse_of(face), CHILD_FILL)

    def test_two_scenes_driven_to_one_cursor_show_the_same_thing(self):
        twin = SimScene(self.item.program, self.item.config)
        other = SimController(self.item)
        for position in (0, 20, 90, self.item.step_total, 40):
            self.drive_to(position)
            other.seek(position)
            twin.update_from(other)
            self.assertEqual(self.scene.snapshot(), twin.snapshot(), position)

    def test_updating_twice_at_one_cursor_changes_nothing(self):
        self.drive_to(90)
        before = self.scene.snapshot()
        self.scene.update_from(self.controller)
        self.assertEqual(self.scene.snapshot(), before)


# --------------------------------------------------------------------------
# (c) the window's wiring -- the viewport injected as None
# --------------------------------------------------------------------------


def no_viewport(_root):
    """The viewport hook a test uses: no Qt3DWindow, no surface, no camera."""
    return None


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class WindowTest(unittest.TestCase):
    def setUp(self):
        self.item = timeline("WDC")
        self.window = Sim3DWindow(self.item, create_viewport=no_viewport)
        self.addCleanup(self.window.close)

    def test_the_tool_field_changes_across_a_section_boundary(self):
        first = self.item.sections[0]
        second = self.item.sections[1]
        self.window.reset()
        opening = self.window.tool_field.text()
        self.assertEqual(
            opening, vm.tool_display(self.item.config.tool(first.section))
        )
        self.window.next_section()
        after = self.window.tool_field.text()
        self.assertEqual(
            after, vm.tool_display(self.item.config.tool(second.section))
        )
        self.assertNotEqual(after, opening)

    def test_the_readouts_match_the_controller_after_stepping(self):
        for _ in range(15):
            self.window.step_forward()
        controller = self.window.controller
        readouts = vm.Readouts.from_controller(controller, self.window.multiplier)
        self.assertEqual(self.window.tool_field.text(), readouts.tool)
        self.assertIn(readouts.feed, self.window.feed_label.text())
        self.assertEqual(self.window.z_label.text(), readouts.z)
        self.assertIn(readouts.section, self.window.section_label.text())
        self.assertEqual(self.window.counter_label.text(), readouts.counter)
        self.assertEqual(self.window.cut_label.text(), controller.current_cut.label)

    def test_the_cut_list_has_a_row_per_cut_and_follows_the_cursor(self):
        self.assertEqual(self.window.cut_list.count(), self.item.cut_total)
        for cut in self.item.cuts:
            self.window.controller.seek(cut.start)
            self.window.refresh()
            self.assertEqual(self.window.cut_list.currentRow(), cut.index)
        self.assertIn(self.item.cuts[3].label, self.window.cut_list.item(3).text())

    def test_clicking_a_row_seeks_that_cut_and_pauses(self):
        self.window.play()
        self.assertTrue(self.window.playing)
        target = 7
        item = self.window.cut_list.item(target)
        self.window.cut_list.itemClicked.emit(item)
        self.assertFalse(self.window.playing, "taking the wheel stops playback")
        self.assertEqual(
            self.window.controller.step_index, self.item.cuts[target].start
        )
        self.assertEqual(self.window.cut_list.currentRow(), target)
        self.assertEqual(self.window.cut_label.text(), self.item.cuts[target].label)

    def test_the_scrub_slider_spans_the_moves_and_seeks_both_ways(self):
        self.assertEqual(self.window.scrub.minimum(), 0)
        self.assertEqual(self.window.scrub.maximum(), self.item.step_total)

        self.window.scrub.setValue(42)
        self.assertEqual(self.window.controller.step_index, 42)

        self.window.next_cut()
        self.assertEqual(
            self.window.scrub.value(),
            self.window.controller.step_index,
            "the slider follows the cursor as well as driving it",
        )

    def test_scrubbing_while_playing_pauses(self):
        self.window.play()
        self.window.scrub.setValue(30)
        self.assertFalse(self.window.playing)
        self.assertEqual(self.window.controller.step_index, 30)

    def test_play_and_pause_toggle_the_timer(self):
        self.assertFalse(self.window.playing)
        self.assertEqual(self.window.play_button.text(), PLAY_TEXT)
        self.window.toggle_play()
        self.assertTrue(self.window.timer.isActive())
        self.assertEqual(self.window.play_button.text(), PAUSE_TEXT)
        self.window.toggle_play()
        self.assertFalse(self.window.timer.isActive())
        self.assertEqual(self.window.play_button.text(), PLAY_TEXT)

    def test_playback_stops_itself_at_the_end_of_the_program(self):
        self.window.play()
        self.window.advance(24.0 * 60.0)
        self.assertTrue(self.window.controller.at_end)
        self.assertFalse(self.window.playing)
        self.assertEqual(self.window.tool_field.text(), vm.tool_display(self.item.steps[-1].tool))

    def test_the_speed_slider_moves_the_multiplier_and_the_maths(self):
        self.assertEqual(self.window.multiplier, vm.DEFAULT_SPEED)
        self.assertEqual(self.window.speed_label.text(), vm.speed_text(vm.DEFAULT_SPEED))
        # Park on a CUTTING move, whose duration is the machine's own time.
        self.window.controller.seek(
            next(
                index
                for index, motion in enumerate(self.item.steps)
                if motion.feed is not None and self.item.path_lengths[index] > 0
            )
        )
        durations = {}
        for index, multiplier in enumerate(vm.SPEED_CHOICES):
            self.window.speed.setValue(index)
            self.assertEqual(self.window.multiplier, multiplier)
            self.assertEqual(
                self.window.speed_label.text(), vm.speed_text(multiplier)
            )
            durations[multiplier] = self.window.current_step_duration()
        base = durations[1.0]
        for multiplier, seconds in durations.items():
            self.assertAlmostEqual(seconds, base / multiplier, delta=TOL)

    def test_the_transport_covers_cuts_sections_and_both_ends(self):
        self.window.next_cut()
        self.assertEqual(self.window.controller.step_index, self.item.cuts[0].end)
        self.window.prev_cut()
        self.assertEqual(self.window.controller.step_index, self.item.cuts[0].start)
        self.window.next_section()
        self.assertEqual(
            self.window.controller.step_index, self.item.sections[0].end
        )
        self.window.prev_section()
        self.assertEqual(
            self.window.controller.step_index, self.item.sections[0].start
        )
        self.window.to_end()
        self.assertTrue(self.window.controller.at_end)
        self.window.reset()
        self.assertTrue(self.window.controller.at_start)

    def test_a_gesture_lands_the_animation_on_a_move_boundary(self):
        self.window.advance(0.001)
        self.assertGreater(self.window.fraction, 0.0)
        self.window.next_cut()
        self.assertEqual(self.window.fraction, 0.0)

    def test_reset_view_is_harmless_with_no_camera(self):
        # The window must not depend on a viewport existing: Milestone 5 embeds
        # it, and a machine whose driver refuses Qt3D still gets the readouts.
        self.assertIsNone(self.window.viewport)
        self.window.reset_view()
        self.assertIn("unavailable", self.window.viewport_widget.text())

    def test_the_window_paints_offscreen(self):
        self.window.resize(1000, 700)
        self.assertFalse(self.window.grab().isNull())


# --------------------------------------------------------------------------
# (d) purity
# --------------------------------------------------------------------------

#: A clock anywhere in this package would make the animation unreplayable, and
#: a Qt import in the viewmodel would make every decision it holds untestable
#: without a display.  The QTimer in ``window.py`` is the whole clock: it hands
#: the viewmodel a number of seconds and owns no other time.
FORBIDDEN_ROOTS = frozenset({"time", "datetime", "random", "secrets"})

QT_ROOTS = frozenset(
    {"PySide6", "PySide2", "PyQt5", "PyQt6", "shiboken2", "shiboken6"}
)

QT_FREE_MODULES = ("viewmodel.py", "__init__.py")


def package_sources() -> list[str]:
    import faceframe_cnc.gui.sim3d as package

    directory = os.path.dirname(package.__file__)
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(".py")
    )


def imported_modules(source: str) -> list[str]:
    """Every module name a file imports, dotted and absolute where stated."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import cannot name anything outside this package.
                continue
            if node.module:
                names.append(node.module)
    return names


class PurityTest(unittest.TestCase):
    def test_the_package_has_the_modules_this_test_thinks_it_has(self):
        """So the AST sweep below cannot pass by finding nothing."""
        found = {os.path.basename(path) for path in package_sources()}
        self.assertEqual(
            found,
            {
                "__init__.py",
                "viewmodel.py",
                "scene.py",
                "window.py",
                "__main__.py",
                # Milestone 4: the static view of a sheet the planner refused.
                # Named here so the AST sweeps below cover it too.
                "refusal.py",
            },
        )

    def test_no_module_in_the_package_reads_a_clock(self):
        for path in package_sources():
            with self.subTest(module=os.path.basename(path)):
                with open(path, "r", encoding="utf-8") as handle:
                    modules = imported_modules(handle.read())
                for module in modules:
                    self.assertNotIn(
                        module.split(".")[0],
                        FORBIDDEN_ROOTS,
                        f"{os.path.basename(path)} imports {module}",
                    )

    def test_the_viewmodel_imports_no_gui_toolkit(self):
        for path in package_sources():
            name = os.path.basename(path)
            if name not in QT_FREE_MODULES:
                continue
            with self.subTest(module=name):
                with open(path, "r", encoding="utf-8") as handle:
                    modules = imported_modules(handle.read())
                for module in modules:
                    self.assertNotIn(
                        module.split(".")[0], QT_ROOTS, f"{name} imports {module}"
                    )

    def test_the_qt_half_really_does_use_qt(self):
        """So the test above is not passing because nothing imports anything."""
        for name in ("scene.py", "window.py"):
            path = next(p for p in package_sources() if os.path.basename(p) == name)
            with open(path, "r", encoding="utf-8") as handle:
                roots = {m.split(".")[0] for m in imported_modules(handle.read())}
            self.assertTrue(roots & QT_ROOTS, f"{name} draws nothing")

    def test_the_package_adds_no_third_party_dependency(self):
        allowed = {"faceframe_cnc", "__future__", "PySide6"}
        stdlib = {
            "argparse",
            "ast",
            "dataclasses",
            "enum",
            "importlib",
            "math",
            "os",
            "re",
            "sys",
            "typing",
        }
        for path in package_sources():
            with self.subTest(module=os.path.basename(path)):
                with open(path, "r", encoding="utf-8") as handle:
                    modules = imported_modules(handle.read())
                for module in modules:
                    self.assertIn(
                        module.split(".")[0],
                        allowed | stdlib,
                        f"{os.path.basename(path)} imports {module}",
                    )


# --------------------------------------------------------------------------
# (e) determinism below the timer
# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class DeterminismTest(unittest.TestCase):
    """The animation is the cursor plus a fraction, and nothing else."""

    def test_the_windows_step_logic_agrees_with_plain_cursor_stepping(self):
        item = timeline("WDC")
        window = Sim3DWindow(item, create_viewport=no_viewport)
        self.addCleanup(window.close)
        self.assertFalse(window.timer.isActive(), "the timer never runs in a test")

        reference = SimController(item)
        scene = SimScene(item.program, item.config)
        scene.update_from(reference)
        self.assertEqual(window.scene.snapshot(), scene.snapshot())

        for step in range(item.step_total):
            window.step_forward()
            reference.step_forward()
            scene.update_from(reference)
            self.assertEqual(
                window.scene.snapshot(), scene.snapshot(), f"after step {step + 1}"
            )

    def test_playing_the_whole_program_lands_where_seeking_to_the_end_does(self):
        item = timeline("WDC")
        window = Sim3DWindow(item, create_viewport=no_viewport)
        self.addCleanup(window.close)
        # Ten minutes of animation at the default multiplier: more than the
        # program needs, and the surplus must not run off the end.
        for _ in range(200):
            window.advance(3.0)
        self.assertTrue(window.controller.at_end)

        reference = SimController(item)
        reference.to_end()
        scene = SimScene(item.program, item.config)
        scene.update_from(reference)
        self.assertEqual(window.scene.snapshot(), scene.snapshot())

    def test_two_windows_driven_the_same_way_show_the_same_sheet(self):
        item = timeline("NESTED")
        first = Sim3DWindow(item, create_viewport=no_viewport)
        second = Sim3DWindow(item, create_viewport=no_viewport)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        script = (0.4, 0.05, 2.0, 0.001, 5.0, 0.25)
        for window in (first, second):
            window.speed.setValue(2)
            for seconds in script:
                window.advance(seconds)
        self.assertEqual(first.scene.snapshot(), second.scene.snapshot())
        self.assertEqual(first.controller.step_index, second.controller.step_index)
        self.assertAlmostEqual(first.fraction, second.fraction, delta=TOL)


class BridgeRevealTest(unittest.TestCase):
    """The holding tabs, visible in the kerf (2026-08-05 amendment §3d).

    A kerf reveal draws an unbroken ring, and on a tabbed sheet that is not what
    the pass cut: it rose over every tab instead of cutting through it.  The
    BRIDGE reveal is the correction — the one kind in the model that is material
    still THERE — and it is what makes "nothing is loose until the very end"
    something the operator can see rather than something a docstring claims.
    """

    def setUp(self):
        self.item = timeline("ONE_PASS")
        self.controller = SimController(self.item)

    def bridges(self, part_index=None):
        items = reveals_at(self.controller, self.item)
        return [
            r
            for r in items
            if r.kind is vm.RevealKind.BRIDGE
            and (part_index is None or r.part_index == part_index)
        ]

    def test_nothing_stands_before_anything_is_cut(self):
        self.assertEqual(self.bridges(), [])

    def test_the_through_pass_leaves_one_bridge_per_tab(self):
        for cut in [c for c in self.item.cuts if c.section == SECTION_PERIMETER]:
            self.controller.seek(cut.end)
            zones = self.item.plan.tabs[(cut.part_index, "perimeter", 0)]
            with self.subTest(part=cut.part_number):
                mine = [
                    r
                    for r in self.bridges(cut.part_index)
                    if r.feature_index < 0  # the perimeter half of the key space
                ]
                self.assertEqual(len(mine), len(zones))

    def test_a_detailed_opening_keeps_its_dropout_hanging(self):
        cut = first_cut(self.item, section=SECTION_DETAIL)
        self.controller.seek(cut.end)
        zones = self.item.plan.tabs[(cut.part_index, "opening", cut.feature.index)]
        mine = [r for r in self.bridges(cut.part_index) if r.feature_index >= 0]
        self.assertEqual(len(mine), len(zones))

    def test_the_release_takes_them_away(self):
        self.controller.to_end()
        self.assertEqual(self.bridges(), [], "the program ends with nothing standing")

    def test_a_bridge_stands_in_its_own_kerf_and_is_a_tab_tall(self):
        cut = first_cut(self.item, section=SECTION_PERIMETER)
        self.controller.seek(cut.end)
        spec = self.item.config.perimeter_passes[-1]
        tool = self.item.config.tool(SECTION_PERIMETER)
        part = self.item.program.flat_parts()[cut.part_index]
        kerf = part.box.grow(spec.offset)
        mine = [r for r in self.bridges(cut.part_index) if r.feature_index < 0]
        self.assertTrue(mine, "the perimeter half of the key space")
        for bridge in mine:
            with self.subTest(key=bridge.key):
                self.assertAlmostEqual(bridge.z_cut, spec.z_cut, places=9)
                self.assertAlmostEqual(bridge.width, tool.diameter, places=9)
                # the block sits across the kerf: one tool wide, and the tab's
                # full-height length along the profile
                short, long = sorted((bridge.box.width, bridge.box.height))
                self.assertAlmostEqual(short, tool.diameter, places=9)
                self.assertAlmostEqual(
                    long, self.item.config.tabs.length + tool.diameter, places=9
                )
                self.assertTrue(kerf.grow(tool.radius + 1e-9).contains(bridge.box, 1e-9))

    def test_every_bridge_has_a_key_of_its_own(self):
        self.controller.seek(
            first_cut(self.item, section=SECTION_PERIMETER).end
        )
        keys = [r.key for r in self.bridges()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(key.startswith("bridge:") for key in keys))

    def test_an_untabbed_program_has_no_bridges(self):
        """Every reference file, and anything emitted before the amendment."""
        item = timeline("NESTED")
        controller = SimController(item)
        controller.to_end()
        self.assertEqual(
            [r for r in reveals_at(controller, item) if r.kind is vm.RevealKind.BRIDGE],
            [],
        )

    def test_a_reveal_list_built_without_a_plan_has_no_bridges(self):
        """The plan is where the zones live, so no plan means no claim."""
        self.controller.seek(first_cut(self.item, section=SECTION_PERIMETER).end)
        self.assertTrue(self.bridges(), "there are bridges to be missed")
        plainer = vm.reveals(
            self.controller.state, self.item.program, self.item.config
        )
        self.assertEqual(
            [r for r in plainer if r.kind is vm.RevealKind.BRIDGE], []
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
