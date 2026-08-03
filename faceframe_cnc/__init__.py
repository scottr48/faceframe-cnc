"""faceframe_cnc — geometry engine for Eagle Woodworking's faceframe optimizer.

Milestone 1a scope only: frame-type inference and opening geometry
(spec sections 2 and 3). The spreadsheet parser, nesting optimizer, GUI,
and NC generation are separate, not-yet-built milestones.
"""

from .geometry import FrameGeometry, FrameType, Opening, compute_geometry, infer_frame_type

__all__ = [
    "FrameType",
    "Opening",
    "FrameGeometry",
    "infer_frame_type",
    "compute_geometry",
]
