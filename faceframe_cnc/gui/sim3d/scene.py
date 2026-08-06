"""The Qt3D entity tree for one sheet, and how a cursor updates it.

Scene units are INCHES and the scene's axes are the sheet's own
(:mod:`faceframe_cnc.post.model`): origin at the sheet's lower-left corner, X
across the 49" width, Y along the 97" length, Z up with Z0 the top of the
spoilboard and Z0.75 the top of the stock.  Nothing is scaled, so a
coordinate read off the readout strip is a coordinate in this tree.

Buildable without a GL context
------------------------------
Everything here is a plain ``QObject``: entities, transforms, meshes and
materials.  No :class:`~PySide6.Qt3DExtras.Qt3DExtras.Qt3DWindow`, no render
surface, no camera — the viewport owns those
(:mod:`~faceframe_cnc.gui.sim3d.window`).  That is what lets
``tests/test_sim3d.py`` build the whole tree offscreen, drive it, and assert
on what is showing.

The visual vocabulary
---------------------
====================  =====================================================
what                  how it reads
====================  =====================================================
stock / spoilboard    two slabs, the sheet's own colours from
                      :mod:`~faceframe_cnc.gui.sheet_canvas`
a part                a tinted overlay on the stock's top face: HOST_FILL
                      for a frame with passengers, CHILD_FILL for a nested
                      one, PART_FILL otherwise — the 2D preview's language
T13 groove            a dark channel sunk from the cut Z to the surface,
                      the cutter's own width
T17 stile slot        two angled flank plates meeting at the apex: a V, not
                      a box, and a different colour from a groove
T11 opening           a dark through-pocket
T12 detail            a rim ring around that pocket, in the 2D preview's
                      opening-edge colour
perimeter pass 0      a bright scored outline lying on the part's face: the
                      part is cut to size.  On the measured two-pass table
                      the onion skin still holds it; on a generated sheet's
                      max-bite ladder (2026-08-05) that pass is the first
                      0.378 bite and the tabs hold it; on a table with a
                      single perimeter pass the same occurrence also frees
                      it, so both rows below apply at once
last perimeter pass   the whole part group LIFTS by :data:`FREED_LIFT` and
                      an edge highlight comes on — a freed part must be
                      unmistakable
the spindle           a body above a bit sized from the ToolSpec in hand:
                      a cylinder of the tool's diameter, or a cone for the
                      45-degree V bit, swapped when the tool changes
====================  =====================================================

Every dimension above comes from the post table
(:class:`~faceframe_cnc.post.model.PostConfig`) or the reveal model
(:mod:`~faceframe_cnc.gui.sim3d.viewmodel`) at call time.  The constants in
this module are visual only and each says so.

The error vocabulary, and what may use it
-----------------------------------------
====================  =====================================================
what                  how it reads
====================  =====================================================
a flagged cut         its feature entity is re-tinted :data:`ERROR_FILL` —
                      the 2D preview's own bad red (``GHOST_BAD``).  A cut
                      that has no feature entity (the pass that FREES a
                      part) reddens the part's face instead
a flagged move        a thin red bar along the move's own XY path at its own
                      Z: the exact travel the verifier cited
the bit               turns red while the move about to run is one a finding
                      names, and goes back to its tool colour after it
a refusal             the refused part's footprint outlined in the same red
                      (:meth:`SimScene.add_error_mark`), for a sheet that
                      never got as far as a program
====================  =====================================================

Every one of those, without exception, comes from a
:class:`~faceframe_cnc.post.verifier.Violation` that
:mod:`faceframe_cnc.sim.findings` located, or from a refusal the planner
raised.  This module owns no rule about what is dangerous and has no way to
decide that something is: given no findings it draws nothing red, and it
cannot draw something red that no finding named.

The two ENVELOPE overlays (:class:`~.viewmodel.OverlayKind`) are the other
thing added here, and they are deliberately not part of that vocabulary:
translucent, neutrally coloured, off until the operator asks for them.  Where
the material of a WDC cone really ends and how far a lead-in ramp really
reaches are facts about the machine, true of a perfectly good sheet.
"""

from __future__ import annotations

from math import degrees, atan2

from PySide6.Qt3DCore import Qt3DCore
from PySide6.Qt3DExtras import Qt3DExtras
from PySide6.Qt3DRender import Qt3DRender
from PySide6.QtGui import QColor, QQuaternion, QVector3D

from ...post.from_layout import part_depths
from ...post.model import Box, PostConfig, SheetProgram, ToolSpec
from ..sheet_canvas import (
    CHILD_EDGE,
    CHILD_FILL,
    CUSHION_EDGE,
    GHOST_BAD,
    HOST_FILL,
    OPENING_EDGE,
    PART_EDGE,
    PART_FILL,
    SELECT_EDGE,
    SHEET_FILL,
)
from .viewmodel import (
    BitProfile,
    DangerModel,
    Overlay,
    OverlayKind,
    Reveal,
    RevealKind,
    bit_profile,
    reveals,
    tip_at,
)

__all__ = ["SimScene", "FREED_LIFT", "ERROR_FILL", "OVERLAY_COLORS"]

# --------------------------------------------------------------------------
# Visual-only constants.  Machine numbers all come from PostConfig.
# --------------------------------------------------------------------------

#: VISUAL ONLY.  How far a freed part is lifted off the spoilboard so the
#: operator cannot miss that it has come loose.
FREED_LIFT = 0.30

#: VISUAL ONLY.  Thickness of the tint plate that colours a part's top face,
#: and of the scored outline that lies on it.
OVERLAY_THICKNESS = 0.02

#: VISUAL ONLY.  The spoilboard slab under the stock.
SPOILBOARD_THICKNESS = 0.50

#: VISUAL ONLY.  A feature's drawn top is lifted this far above the stock
#: face so it cuts visibly through the part's tint plate instead of
#: z-fighting with it.
FEATURE_TOP_LIFT = OVERLAY_THICKNESS * 1.5

#: VISUAL ONLY.  Floor on a drawn depth.  A dry-run table
#: (:func:`~faceframe_cnc.post.job.dry_run_config`) cuts ABOVE the stock, so
#: its reveals have zero or negative depth; they are still drawn, as the
#: thinnest scratch this scene can show, because an air cut is a program the
#: operator watches on purpose.
MIN_FEATURE_DEPTH = 0.02

#: VISUAL ONLY.  Thickness of one flank plate of a V slot, and of an outline
#: ring's bars where the reveal does not give a width.
PLATE_THICKNESS = 0.015

#: VISUAL ONLY.  The spindle body above the bit.
SPINDLE_BODY_LENGTH = 4.0
SPINDLE_BODY_RADIUS = 0.85

#: VISUAL ONLY.  Colours for things the 2D preview has no colour for: a
#: machined channel, a V slot (deliberately unlike the channel), a void, and
#: the spindle.
GROOVE_COLOR = QColor("#54636e")
#: VISUAL ONLY.  A holding tab standing in a kerf (2026-08-05 amendment §3d):
#: the stock's own tone, a shade brighter so a bar across a kerf reads as
#: material rather than as a gap in the drawing.
BRIDGE_COLOR = QColor("#c8b48c")
SLOT_COLOR = QColor("#8a6a3a")
VOID_COLOR = QColor("#232a30")
SPINDLE_COLOR = QColor("#41474d")

#: The colour each kind of revealed material is drawn in, in ONE place: a
#: feature reddened for a verifier finding has to be able to go back to the
#: colour it belongs in, and two spellings of that colour would eventually be
#: two different colours.
FEATURE_COLORS = {
    RevealKind.GROOVE: GROOVE_COLOR,
    RevealKind.SLOT: SLOT_COLOR,
    RevealKind.OPENING: VOID_COLOR,
    RevealKind.DETAIL: OPENING_EDGE,
    RevealKind.SKIN: SELECT_EDGE,
    # A holding tab is the one "reveal" that is material still THERE
    # (:attr:`~.viewmodel.RevealKind.BRIDGE`), so it is drawn in the stock's own
    # colour: what the operator sees is the sheet showing through its own kerf.
    RevealKind.BRIDGE: BRIDGE_COLOR,
}

#: VISUAL ONLY.  Bit colours, keyed by the tool NUMBER the post table names —
#: a table that adds a tool gets :data:`BIT_DEFAULT_COLOR` and a mesh sized
#: from its own diameter, so nothing breaks and nothing is invented.
BIT_COLORS = {
    11: QColor("#c0c8d0"),
    12: QColor("#d9b25a"),
    13: QColor("#7fb2d9"),
    17: QColor("#c2410c"),
}
BIT_DEFAULT_COLOR = QColor("#b0b7bd")

#: The 2D preview's own "this cannot be cut" red
#: (:data:`~faceframe_cnc.gui.sheet_canvas.GHOST_BAD`), opaque: a phong
#: material carries no alpha, and the red has to mean the same thing in both
#: views or the operator has to learn it twice.
ERROR_FILL = QColor(GHOST_BAD.red(), GHOST_BAD.green(), GHOST_BAD.blue())

#: VISUAL ONLY.  Width and thickness of the bar drawn along a flagged move.
#: Thin on purpose: it marks a PATH, and a fat one would hide the feature the
#: path is ruining.
MARK_WIDTH = 0.09
MARK_THICKNESS = 0.05

#: VISUAL ONLY.  The neutral colours of the informational envelopes.  Not the
#: error red, and deliberately unlike it: an envelope is where the machine
#: legitimately reaches (see the module docstring).
OVERLAY_COLORS = {
    OverlayKind.CONE_REACH: QColor("#c08a3a"),
    OverlayKind.LEAD_IN: QColor("#6f8fa8"),
    OverlayKind.FENCE: QColor("#9aa7b1"),
}

#: VISUAL ONLY.  How see-through an envelope slab is; the sheet under it has
#: to stay readable, because the point of the overlay is the comparison.
OVERLAY_ALPHA = 0.20

#: VISUAL ONLY.  How far above the stock face an envelope slab floats, and how
#: thick it is.  It is not a cut, so it lies ON the sheet rather than in it —
#: except for the depth it reports, which sinks the slab's bottom to the cut Z
#: so a cone's reach can be seen going INTO the material.
OVERLAY_LIFT = OVERLAY_THICKNESS * 4.0
OVERLAY_MIN_THICKNESS = 0.03


def _material(color: QColor, parent: Qt3DCore.QEntity) -> Qt3DExtras.QPhongMaterial:
    """A phong material PARENTED to the entity it dresses.

    Components are parented on purpose everywhere in this module: a mesh,
    transform or material that an entity merely references is owned by nothing
    and can be collected out from under the tree.
    """
    material = Qt3DExtras.QPhongMaterial(parent)
    material.setDiffuse(color)
    material.setAmbient(color.darker(160))
    material.setSpecular(QColor("#202020"))
    material.setShininess(12.0)
    return material


def _alpha_material(
    color: QColor, alpha: float, parent: Qt3DCore.QEntity
) -> Qt3DExtras.QPhongAlphaMaterial:
    """A see-through material, parented like :func:`_material`'s."""
    material = Qt3DExtras.QPhongAlphaMaterial(parent)
    material.setDiffuse(color)
    material.setAmbient(color.darker(140))
    material.setAlpha(max(0.0, min(float(alpha), 1.0)))
    return material


def _recolor(entity: Qt3DCore.QEntity, color: QColor) -> None:
    """Re-tint ``entity`` and everything under it.

    A feature is sometimes one box and sometimes a group of four bars or two
    flank plates, so reddening it means walking the sub-tree: the alternative
    is a second entity laid over the first, and two entities for one piece of
    material is how a view starts disagreeing with itself.
    """
    for component in entity.components():
        if isinstance(component, Qt3DExtras.QPhongMaterial):
            component.setDiffuse(color)
            component.setAmbient(color.darker(160))
    for child in entity.children():
        if isinstance(child, Qt3DCore.QEntity):
            _recolor(child, color)


def _bar(
    parent: Qt3DCore.QEntity,
    start: tuple[float, float],
    end: tuple[float, float],
    z: float,
    width: float,
    thickness: float,
    color: QColor,
    name: str,
) -> Qt3DCore.QEntity:
    """A thin box lying along the segment ``start``..``end`` at height ``z``.

    Rotated about Z to the path's own bearing rather than snapped to an axis:
    a lead-in ramp with lateral lead runs diagonally, and a mark that ran
    along the wrong line would be pointing at the wrong material.  A move
    that travels nowhere (a zero-length retract) still gets a mark, at the
    minimum length a box can have, because the verifier still cited it.
    """
    (x0, y0), (x1, y1) = start, end
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    entity = Qt3DCore.QEntity(parent)
    entity.setObjectName(name)
    mesh = Qt3DExtras.QCuboidMesh(entity)
    mesh.setXExtent(max(length, width))
    mesh.setYExtent(width)
    mesh.setZExtent(thickness)
    transform = Qt3DCore.QTransform(entity)
    if length > 1e-9:
        transform.setRotation(
            QQuaternion.fromAxisAndAngle(
                QVector3D(0.0, 0.0, 1.0), degrees(atan2(y1 - y0, x1 - x0))
            )
        )
    transform.setTranslation(
        QVector3D((x0 + x1) / 2.0, (y0 + y1) / 2.0, z + thickness / 2.0)
    )
    entity.addComponent(mesh)
    entity.addComponent(transform)
    entity.addComponent(_material(color, entity))
    return entity


def _slab(
    parent: Qt3DCore.QEntity,
    box: Box,
    z_bottom: float,
    z_top: float,
    color: QColor,
    name: str,
) -> Qt3DCore.QEntity:
    """A box entity spanning ``box`` in XY and ``z_bottom``..``z_top`` in Z."""
    entity = Qt3DCore.QEntity(parent)
    entity.setObjectName(name)
    mesh = Qt3DExtras.QCuboidMesh(entity)
    mesh.setXExtent(max(abs(box.width), 1e-6))
    mesh.setYExtent(max(abs(box.height), 1e-6))
    mesh.setZExtent(max(abs(z_top - z_bottom), 1e-6))
    transform = Qt3DCore.QTransform(entity)
    transform.setTranslation(
        QVector3D(box.mid_x, box.mid_y, (z_bottom + z_top) / 2.0)
    )
    entity.addComponent(mesh)
    entity.addComponent(transform)
    entity.addComponent(_material(color, entity))
    return entity


def _outline(
    parent: Qt3DCore.QEntity,
    box: Box,
    z_bottom: float,
    z_top: float,
    bar_width: float,
    color: QColor,
    name: str,
) -> Qt3DCore.QEntity:
    """A rectangular ring of four bars centred on ``box``'s edges.

    Used for anything whose reveal is a LINE around a rectangle rather than a
    filled area: the perimeter kerf, the T12 opening rim, the highlight on a
    freed part.
    """
    group = Qt3DCore.QEntity(parent)
    group.setObjectName(name)
    half = max(bar_width, PLATE_THICKNESS) / 2.0
    _slab(
        group,
        Box(box.x0 - half, box.y0 - half, box.x1 + half, box.y0 + half),
        z_bottom,
        z_top,
        color,
        f"{name}-low",
    )
    _slab(
        group,
        Box(box.x0 - half, box.y1 - half, box.x1 + half, box.y1 + half),
        z_bottom,
        z_top,
        color,
        f"{name}-high",
    )
    _slab(
        group,
        Box(box.x0 - half, box.y0 + half, box.x0 + half, box.y1 - half),
        z_bottom,
        z_top,
        color,
        f"{name}-left",
    )
    _slab(
        group,
        Box(box.x1 - half, box.y0 + half, box.x1 + half, box.y1 - half),
        z_bottom,
        z_top,
        color,
        f"{name}-right",
    )
    return group


class SimScene:
    """The entity tree for one sheet, updatable from a reveal list.

    Construction builds everything that does not depend on the cursor (the
    slabs, one group per flat part, the spindle) and nothing that does:
    :meth:`update` creates a feature's entity the first time that feature is
    revealed and thereafter only enables or disables it, so scrubbing
    backwards puts the sheet back rather than rebuilding it.
    """

    def __init__(
        self,
        program: SheetProgram,
        config: PostConfig,
        parent: Qt3DCore.QNode | None = None,
        danger: DangerModel | None = None,
    ):
        self.program = program
        self.config = config
        self.root = Qt3DCore.QEntity(parent)
        self.root.setObjectName("sim3d-root")
        #: What is wrong with this program and where the envelopes are.  An
        #: empty model (the default) has neither, so nothing here is red.
        self.danger = DangerModel.empty() if danger is None else danger

        self._parts = program.flat_parts()
        self._features: dict[str, Qt3DCore.QEntity] = {}
        self._freed: frozenset[int] = frozenset()
        self._tool: ToolSpec | None = None
        self._profile: BitProfile | None = None
        self._bit: Qt3DCore.QEntity | None = None
        self._bit_flagged = False
        self._tip = (0.0, 0.0, config.rapid_z)
        #: Which feature keys and part faces are currently showing red, so a
        #: tint is applied once and taken off again when it stops applying.
        self._red_features: frozenset[str] = frozenset()
        self._red_faces: frozenset[int] = frozenset()
        self._overlays: dict[str, Qt3DCore.QEntity] = {}
        self._overlay_shown: set[OverlayKind] = set()
        self._marks: dict[int, Qt3DCore.QEntity] = {}
        self._error_marks: list[Qt3DCore.QEntity] = []

        self._build_lights()
        self._build_stock()
        self._build_parts()
        self._build_spindle()
        self._build_marks()
        self._apply_tip()

    # -- construction ------------------------------------------------------

    def _build_lights(self) -> None:
        """Two directional lights, so the tree is lit by itself.

        A phong material with no light in the scene renders black, and the
        viewport is optional here (a test injects none), so the lighting
        cannot live with the camera.
        """
        for direction, intensity, name in (
            ((-0.4, -0.6, -1.0), 1.0, "key-light"),
            ((0.6, 0.5, -0.4), 0.45, "fill-light"),
        ):
            entity = Qt3DCore.QEntity(self.root)
            entity.setObjectName(name)
            light = Qt3DRender.QDirectionalLight(entity)
            light.setWorldDirection(QVector3D(*direction))
            light.setIntensity(intensity)
            entity.addComponent(light)

    def _build_stock(self) -> None:
        cfg = self.config
        sheet = Box(0.0, 0.0, cfg.sheet_width, cfg.sheet_length)
        self.spoilboard = _slab(
            self.root, sheet, -SPOILBOARD_THICKNESS, 0.0, CUSHION_EDGE, "spoilboard"
        )
        self.stock = _slab(
            self.root,
            sheet,
            cfg.stock_top_z - cfg.material_thickness,
            cfg.stock_top_z,
            SHEET_FILL,
            "stock",
        )

    def _build_parts(self) -> None:
        """One group per flat part: its tint plate and its freed highlight.

        The group carries the transform that LIFTS a freed part, and every
        feature entity is parented into it, so a part that comes loose takes
        its own grooves and openings with it.
        """
        depths = part_depths(self.program)
        self.part_groups: list[Qt3DCore.QEntity] = []
        self.part_lifts: list[Qt3DCore.QTransform] = []
        self.part_highlights: list[Qt3DCore.QEntity] = []
        #: The tint plates themselves and the colour each one belongs in, so a
        #: face reddened for a finding can be put back.
        self.part_faces: list[Qt3DCore.QEntity] = []
        self.part_fills: list[QColor] = []
        top = self.config.stock_top_z

        for index, part in enumerate(self._parts):
            nested = depths[index] > 0
            group = Qt3DCore.QEntity(self.root)
            group.setObjectName(f"part-{index}")
            lift = Qt3DCore.QTransform(group)
            group.addComponent(lift)

            fill = CHILD_FILL if nested else (HOST_FILL if part.children else PART_FILL)
            face = _slab(
                group,
                part.box,
                top,
                top + OVERLAY_THICKNESS,
                fill,
                f"part-{index}-face",
            )
            self.part_faces.append(face)
            self.part_fills.append(fill)
            highlight = _outline(
                group,
                part.box,
                top,
                top + OVERLAY_THICKNESS * 3.0,
                PLATE_THICKNESS * 4.0,
                CHILD_EDGE if nested else PART_EDGE,
                f"part-{index}-loose",
            )
            highlight.setEnabled(False)

            self.part_groups.append(group)
            self.part_lifts.append(lift)
            self.part_highlights.append(highlight)

    def _build_spindle(self) -> None:
        self.spindle = Qt3DCore.QEntity(self.root)
        self.spindle.setObjectName("spindle")
        self.spindle_transform = Qt3DCore.QTransform(self.spindle)
        self.spindle.addComponent(self.spindle_transform)
        # The body hangs above whatever bit is fitted; the bit's own length
        # decides where it starts, so it is rebuilt with the bit.
        self._body: Qt3DCore.QEntity | None = None

    # -- the bit -----------------------------------------------------------

    def _build_bit(self, profile: BitProfile) -> None:
        """(Re)build the bit and the body above it for ``profile``.

        The tip sits at the spindle group's own origin and the flute runs UP
        from it, because the machine cuts downward into stock whose top is
        Z0.75: a bit drawn tip-up would be pointing away from the material.
        """
        for existing in (self._bit, self._body):
            if existing is not None:
                existing.setParent(None)
                existing.deleteLater()
        self._bit = None
        self._body = None

        bit = Qt3DCore.QEntity(self.spindle)
        bit.setObjectName(f"bit-T{profile.tool_number}")
        length = max(profile.length, PLATE_THICKNESS)
        if profile.shape == "cone":
            mesh = Qt3DExtras.QConeMesh(bit)
            # Tip at the mesh's -Y end, shoulder at +Y: a V bit cuts with its
            # point, and the cone's half angle is the flank the slot spec
            # states (radius grows one unit per unit of length at 45 degrees).
            mesh.setBottomRadius(0.0)
            mesh.setTopRadius(profile.radius)
            mesh.setLength(length)
        else:
            mesh = Qt3DExtras.QCylinderMesh(bit)
            mesh.setRadius(profile.radius)
            mesh.setLength(length)
        transform = Qt3DCore.QTransform(bit)
        # Qt3D's round meshes run along their local Y; a 90-degree turn about
        # X maps +Y onto +Z, which is up in sheet coordinates.
        transform.setRotation(QQuaternion.fromAxisAndAngle(QVector3D(1.0, 0.0, 0.0), 90.0))
        transform.setTranslation(QVector3D(0.0, 0.0, length / 2.0))
        bit.addComponent(mesh)
        bit.addComponent(transform)
        bit.addComponent(
            _material(BIT_COLORS.get(profile.tool_number, BIT_DEFAULT_COLOR), bit)
        )
        self._bit = bit

        body = Qt3DCore.QEntity(self.spindle)
        body.setObjectName("spindle-body")
        body_mesh = Qt3DExtras.QCylinderMesh(body)
        body_mesh.setRadius(SPINDLE_BODY_RADIUS)
        body_mesh.setLength(SPINDLE_BODY_LENGTH)
        body_transform = Qt3DCore.QTransform(body)
        body_transform.setRotation(
            QQuaternion.fromAxisAndAngle(QVector3D(1.0, 0.0, 0.0), 90.0)
        )
        body_transform.setTranslation(
            QVector3D(0.0, 0.0, length + SPINDLE_BODY_LENGTH / 2.0)
        )
        body.addComponent(body_mesh)
        body.addComponent(body_transform)
        body.addComponent(_material(SPINDLE_COLOR, body))
        self._body = body

    def _set_tool(self, tool: ToolSpec | None) -> None:
        if tool is None or (self._tool is not None and tool == self._tool):
            return
        self._tool = tool
        self._profile = bit_profile(tool, self.config)
        self._build_bit(self._profile)
        # A bit fitted while the current move is flagged is red from the
        # moment it appears: the tool changing does not clear a finding.
        self._apply_bit_color()

    def _apply_tip(self) -> None:
        self.spindle_transform.setTranslation(QVector3D(*self._tip))

    # -- features ----------------------------------------------------------

    def _feature_top(self) -> float:
        return self.config.stock_top_z + FEATURE_TOP_LIFT

    def _floor(self, reveal: Reveal) -> float:
        """The Z a reveal is drawn down to, never less than a visible scratch."""
        top = self.config.stock_top_z
        return min(reveal.z_cut, top - MIN_FEATURE_DEPTH)

    def _build_feature(self, reveal: Reveal) -> Qt3DCore.QEntity:
        parent = self.part_groups[reveal.part_index]
        top = self._feature_top()
        floor = self._floor(reveal)
        color = FEATURE_COLORS.get(reveal.kind)

        if reveal.kind is RevealKind.GROOVE:
            return _slab(parent, reveal.swept_box, floor, top, color, reveal.key)
        if reveal.kind is RevealKind.SLOT:
            return self._build_slot(parent, reveal, floor, top, color)
        if reveal.kind is RevealKind.OPENING:
            return _slab(parent, reveal.box, floor, top, color, reveal.key)
        if reveal.kind is RevealKind.DETAIL:
            return _outline(
                parent, reveal.box, floor, top, reveal.width, color, reveal.key
            )
        if reveal.kind is RevealKind.BRIDGE:
            # From the kerf floor UP to the tab top: the material the pass that
            # cut this kerf rose over instead of cutting (spec §3b).  It goes
            # away when the release cut takes it, which the enable/disable pass
            # in :meth:`update` does for free.
            return _slab(
                parent,
                reveal.box,
                floor,
                min(self.config.tabs.top_z, top),
                color,
                reveal.key,
            )
        if reveal.kind is RevealKind.SKIN:
            face = self.config.stock_top_z
            return _outline(
                parent,
                reveal.box,
                face,
                face + OVERLAY_THICKNESS * 2.0,
                reveal.width,
                color,
                reveal.key,
            )
        raise ValueError(
            f"the scene has no shape for a {reveal.kind.value} reveal, so cut "
            f"{reveal.key} would change the sheet without showing it"
        )

    def _build_slot(
        self,
        parent: Qt3DCore.QEntity,
        reveal: Reveal,
        floor: float,
        top: float,
        color: QColor = SLOT_COLOR,
    ) -> Qt3DCore.QEntity:
        """A V channel as its two flanks, meeting at the apex.

        The flank angle is measured from the reveal itself — half its surface
        width against its depth of cut — rather than assumed to be 45 degrees,
        because :meth:`~faceframe_cnc.post.model.WdcSlotSpec.surface_radius`
        caps the width at the bit's own radius and a capped pass is steeper
        than its flanks.
        """
        group = Qt3DCore.QEntity(parent)
        group.setObjectName(reveal.key)
        swept = reveal.swept_box
        along_x = reveal.axis == "x"
        along = swept.width if along_x else swept.height
        depth = max(top - floor, MIN_FEATURE_DEPTH)
        half = max(reveal.width / 2.0, PLATE_THICKNESS)
        face = (depth * depth + half * half) ** 0.5
        tilt = degrees(atan2(half, depth))
        align = (
            QQuaternion.fromAxisAndAngle(QVector3D(0.0, 0.0, 1.0), 90.0)
            if along_x
            else QQuaternion()
        )

        for sign, name in ((1.0, "high"), (-1.0, "low")):
            flank = Qt3DCore.QEntity(group)
            flank.setObjectName(f"{reveal.key}-{name}")
            mesh = Qt3DExtras.QCuboidMesh(flank)
            mesh.setXExtent(PLATE_THICKNESS)
            mesh.setYExtent(along)
            mesh.setZExtent(face)
            offset = align.rotatedVector(QVector3D(sign * half / 2.0, 0.0, 0.0))
            transform = Qt3DCore.QTransform(flank)
            transform.setRotation(
                align
                * QQuaternion.fromAxisAndAngle(QVector3D(0.0, 1.0, 0.0), sign * tilt)
            )
            transform.setTranslation(
                QVector3D(
                    swept.mid_x + offset.x(),
                    swept.mid_y + offset.y(),
                    floor + depth / 2.0,
                )
            )
            flank.addComponent(mesh)
            flank.addComponent(transform)
            flank.addComponent(_material(color, flank))
        return group

    # -- findings, and the envelopes they are judged against ---------------

    def _build_marks(self) -> None:
        """One red bar per move a finding names, built once and left showing.

        A finding is a fact about the PROGRAM, not about the cursor: the move
        that will cut into the neighbour is wrong before it runs and stays
        wrong after it has, so its mark does not come and go with playback.
        The bars hang off the root rather than off a part group, so a part
        lifting when it comes free does not carry away the mark on a cut that
        should never have been made.
        """
        for mark in self.danger.marks:
            start, end = mark.segment
            self._marks[mark.step_index] = _bar(
                self.root,
                start,
                end,
                mark.z,
                MARK_WIDTH,
                MARK_THICKNESS,
                ERROR_FILL,
                mark.key,
            )

    def _overlay_entity(self, item: Overlay) -> Qt3DCore.QEntity:
        """Build one envelope slab, on first use.

        Drawn from the cut floor it belongs to up to just above the stock
        face, so a cone's reach is visible going INTO the material rather than
        as a rectangle painted on top of it.  The fence reports no depth and
        becomes a thin sheet lying on the surface.
        """
        top = self.config.stock_top_z + OVERLAY_LIFT
        bottom = min(item.z_cut, top - OVERLAY_MIN_THICKNESS)
        entity = Qt3DCore.QEntity(self.root)
        entity.setObjectName(item.key)
        mesh = Qt3DExtras.QCuboidMesh(entity)
        mesh.setXExtent(max(abs(item.box.width), 1e-6))
        mesh.setYExtent(max(abs(item.box.height), 1e-6))
        mesh.setZExtent(max(top - bottom, OVERLAY_MIN_THICKNESS))
        transform = Qt3DCore.QTransform(entity)
        transform.setTranslation(
            QVector3D(item.box.mid_x, item.box.mid_y, (bottom + top) / 2.0)
        )
        entity.addComponent(mesh)
        entity.addComponent(transform)
        entity.addComponent(
            _alpha_material(
                OVERLAY_COLORS.get(item.kind, CUSHION_EDGE), OVERLAY_ALPHA, entity
            )
        )
        return entity

    def set_overlay_visible(self, kind: OverlayKind, visible: bool) -> None:
        """Show or hide one envelope family.

        Off is the default state of every family: a clean sheet has to look
        clean, and the operator turns an envelope on when investigating.
        """
        if visible:
            self._overlay_shown.add(kind)
        else:
            self._overlay_shown.discard(kind)
        for item in self.danger.of_kind(kind):
            entity = self._overlays.get(item.key)
            if entity is None:
                if not visible:
                    continue  # never built, nothing to hide
                entity = self._overlay_entity(item)
                self._overlays[item.key] = entity
            entity.setEnabled(visible)

    def add_error_mark(self, box: Box, name: str = "error-mark") -> Qt3DCore.QEntity:
        """Outline ``box`` in the error red: what a REFUSED sheet needs.

        There is no program behind a refusal and therefore no finding to
        locate, so this is the one red mark that comes from a planner refusal
        instead (:class:`~faceframe_cnc.post.from_layout.SheetPlanError` and
        the part it names).  Nothing in playback calls it.
        """
        top = self.config.stock_top_z
        mark = _outline(
            self.root,
            box,
            top,
            top + OVERLAY_THICKNESS * 4.0,
            PLATE_THICKNESS * 6.0,
            ERROR_FILL,
            name,
        )
        self._error_marks.append(mark)
        return mark

    def _apply_flags(self) -> None:
        """Redden every flagged cut, on its feature or on its part's face.

        A feature entity exists only once its cut has finished, so before then
        the finding is shown on the part: rewinding past a flagged groove must
        not hide the fact that the groove is wrong, it just moves where the
        red is.
        """
        red_features: set[str] = set()
        red_faces: set[int] = set()
        for flag in self.danger.flagged:
            key = flag.reveal_key
            entity = None if key is None else self._features.get(key)
            if entity is not None and entity.isEnabled():
                red_features.add(key)
            else:
                red_faces.add(flag.part_index)

        for key in self._red_features - red_features:
            entity = self._features.get(key)
            if entity is not None:
                _recolor(entity, self._feature_color(key))
        for key in red_features - self._red_features:
            _recolor(self._features[key], ERROR_FILL)
        self._red_features = frozenset(red_features)

        for index in self._red_faces - red_faces:
            _recolor(self.part_faces[index], self.part_fills[index])
        for index in red_faces - self._red_faces:
            _recolor(self.part_faces[index], ERROR_FILL)
        self._red_faces = frozenset(red_faces)

    def _feature_color(self, key: str) -> QColor:
        """The colour a feature key belongs in, off its own kind."""
        kind = RevealKind(key.split(":", 1)[0])
        return FEATURE_COLORS[kind]

    def _set_bit_flagged(self, flagged: bool) -> None:
        """Turn the bit red while the move about to run is a flagged one."""
        if flagged == self._bit_flagged:
            return
        self._bit_flagged = flagged
        self._apply_bit_color()

    def _apply_bit_color(self) -> None:
        if self._bit is None or self._profile is None:
            return
        base = BIT_COLORS.get(self._profile.tool_number, BIT_DEFAULT_COLOR)
        _recolor(self._bit, ERROR_FILL if self._bit_flagged else base)

    # -- the update itself -------------------------------------------------

    def update(
        self,
        items: tuple[Reveal, ...],
        tool: ToolSpec | None,
        tip: tuple[float, float, float],
        flagged_step: int | None = None,
    ) -> None:
        """Show exactly ``items``, fit ``tool``, and put the bit tip at ``tip``.

        Idempotent and order-free: the same reveal list always leaves the same
        tree, whichever cursor position it was reached from.

        ``flagged_step`` is the step index of the move about to run, or
        ``None`` where there is none; the bit reddens when that move is one
        the findings name, and never for any other reason.
        """
        present = {reveal.key: reveal for reveal in items}
        for key, entity in self._features.items():
            entity.setEnabled(key in present)
        for key, reveal in present.items():
            if reveal.kind is RevealKind.FREED:
                continue
            entity = self._features.get(key)
            if entity is None:
                entity = self._build_feature(reveal)
                self._features[key] = entity
            entity.setEnabled(True)

        freed = frozenset(
            reveal.part_index for reveal in items if reveal.kind is RevealKind.FREED
        )
        if freed != self._freed:
            self._freed = freed
            for index, transform in enumerate(self.part_lifts):
                loose = index in freed
                transform.setTranslation(
                    QVector3D(0.0, 0.0, FREED_LIFT if loose else 0.0)
                )
                self.part_highlights[index].setEnabled(loose)

        self._set_tool(tool)
        self._tip = (float(tip[0]), float(tip[1]), float(tip[2]))
        self._apply_tip()
        self._apply_flags()
        self._set_bit_flagged(self.danger.is_flagged_step(flagged_step))

    def update_from(self, controller, fraction: float = 0.0) -> None:
        """:meth:`update` from a cursor, ``fraction`` into the move in progress.

        The step whose flag matters is the one about to run — the cursor sits
        between moves, so at the end of the program there is none.
        """
        step = None if controller.current_motion is None else controller.step_index
        self.update(
            reveals(
                controller.state,
                self.program,
                self.config,
                controller.timeline.plan,
            ),
            controller.tool,
            tip_at(controller, fraction, self.config),
            step,
        )

    # -- what is showing ---------------------------------------------------

    @property
    def tool(self) -> ToolSpec | None:
        return self._tool

    @property
    def bit_profile(self) -> BitProfile | None:
        return self._profile

    @property
    def bit_entity(self) -> Qt3DCore.QEntity | None:
        return self._bit

    @property
    def freed(self) -> frozenset[int]:
        return self._freed

    @property
    def tip(self) -> tuple[float, float, float]:
        return self._tip

    def visible_keys(self) -> frozenset[str]:
        return frozenset(
            key for key, entity in self._features.items() if entity.isEnabled()
        )

    def lift_of(self, part_index: int) -> float:
        return self.part_lifts[part_index].translation().z()

    @property
    def bit_flagged(self) -> bool:
        """Is the bit showing red, i.e. is the move about to run a flagged one?"""
        return self._bit_flagged

    @property
    def flagged_features(self) -> frozenset[str]:
        """Feature keys currently tinted with the error colour."""
        return self._red_features

    @property
    def flagged_faces(self) -> frozenset[int]:
        """Parts whose tint plate is currently the error colour."""
        return self._red_faces

    def mark_keys(self) -> frozenset[str]:
        """The flagged-move bars in the tree, one per move a finding named."""
        return frozenset(
            entity.objectName()
            for entity in self._marks.values()
            if entity.isEnabled()
        )

    def visible_overlay_keys(self) -> frozenset[str]:
        return frozenset(
            key for key, entity in self._overlays.items() if entity.isEnabled()
        )

    def overlays_shown(self) -> frozenset[OverlayKind]:
        return frozenset(self._overlay_shown)

    def error_mark_names(self) -> tuple[str, ...]:
        return tuple(mark.objectName() for mark in self._error_marks)

    def snapshot(self) -> tuple:
        """Everything the scene is showing, comparable and hashable.

        Two scenes with equal snapshots are showing the same sheet, the same
        tool and the same tool position, which is how the tests hold the
        window's animation to plain cursor stepping.  The four danger fields
        are all empty for a scene with no findings and no envelope switched
        on, so a clean sheet's snapshot is the same tuple Milestone 3
        produced.
        """
        return (
            None if self._tool is None else self._tool.number,
            tuple(round(value, 6) for value in self._tip),
            tuple(sorted(self.visible_keys())),
            tuple(sorted(self._freed)),
            tuple(sorted(self._red_features)),
            tuple(sorted(self._red_faces)),
            self._bit_flagged,
            tuple(sorted(self.mark_keys() | self.visible_overlay_keys())),
        )
