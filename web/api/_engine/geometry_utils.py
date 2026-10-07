from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import ezdxf
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, snap, unary_union

CURVE_TOLERANCE_FEET = 0.01
SNAP_TOLERANCE_FEET = 1.0
EDGE_TOLERANCE_FEET = 0.01

DEFAULT_LAYER_CONFIG = {
    "boundary": ["BOUNDARY"],
    "additional_load": [],
    "wall": ["WALL"],
    "beam": ["BEAM", "BEAMS", "TRANSFER", "TRANSFER BEAM", "GIRDER"],
    "support_point": ["POINTS", "COLUMNS", "COLUMN", "POINT"],
    "column_label": ["COLUMN NUMBER"],
    "floor_label": ["FLOOR NUMBER"],
    "datum": ["DATUM"],
}


def load_job_config(path: str | Path = "job_config.json") -> Dict:
    config_path = Path(path)
    if not config_path.exists():
        return {"layers": DEFAULT_LAYER_CONFIG, "source_units": "in"}

    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    layers = raw.get("layers", {})
    normalized_layers = {}
    for role, defaults in DEFAULT_LAYER_CONFIG.items():
        values = layers.get(role, defaults)
        if isinstance(values, str):
            normalized_layers[role] = [values]
        else:
            normalized_layers[role] = [value for value in values if value]

    source_units = str(raw.get("source_units", "in")).lower()
    if source_units not in {"in", "ft"}:
        source_units = "in"

    return {"layers": normalized_layers, "source_units": source_units}


def unit_factor(source_units: str) -> float:
    return 1.0 / 12.0 if source_units == "in" else 1.0


def sanitized_floor_token(value: str) -> str:
    token = re.sub(r"[^0-9A-Za-z]+", "_", str(value).strip().upper()).strip("_")
    return token or "FLOOR"


def extract_entity_text(entity) -> str:
    raw_text = ""
    if hasattr(entity, "plain_text"):
        try:
            raw_text = entity.plain_text()
        except TypeError:
            raw_text = entity.plain_text
        except Exception:
            raw_text = ""

    if not raw_text:
        if entity.dxftype() == "TEXT":
            raw_text = getattr(entity.dxf, "text", "")
        else:
            raw_text = getattr(entity, "text", "")

    if raw_text is None:
        return ""

    normalized_lines = [line.strip() for line in str(raw_text).replace("\\P", "\n").splitlines()]
    return " ".join(line for line in normalized_lines if line)


def _arc_points(center_x: float, center_y: float, radius: float, start_deg: float, end_deg: float) -> List[tuple]:
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    while end <= start:
        end += 2 * math.pi

    span = end - start
    if radius <= 0:
        return []

    # Convert chord-height tolerance to a conservative segment count.
    max_angle = 2.0 * math.acos(max(0.0, 1.0 - CURVE_TOLERANCE_FEET / max(radius, CURVE_TOLERANCE_FEET)))
    if not math.isfinite(max_angle) or max_angle <= 0:
        max_angle = math.radians(5)
    segments = max(16, int(math.ceil(span / max_angle)))
    return [
        (
            center_x + radius * math.cos(start + span * idx / segments),
            center_y + radius * math.sin(start + span * idx / segments),
        )
        for idx in range(segments + 1)
    ]


def _bulge_points(start: tuple, end: tuple, bulge: float) -> List[tuple]:
    """Points along the arc a polyline bulge describes, start inclusive, end exclusive."""
    from ezdxf.math import bulge_to_arc

    try:
        center, start_angle, end_angle, radius = bulge_to_arc(start, end, bulge)
    except Exception:
        return [start]
    pts = _arc_points(center.x, center.y, radius, math.degrees(start_angle), math.degrees(end_angle))
    if not pts:
        return [start]
    # bulge_to_arc returns the counter-clockwise sweep; a negative bulge runs the other way.
    if bulge < 0:
        pts = list(reversed(pts))
    return [start] + pts[1:-1]


def _polyline_vertices(entity, factor: float) -> List[tuple]:
    """Vertices in feet, with every arc bulge expanded at CURVE_TOLERANCE_FEET."""
    if entity.dxftype() == "LWPOLYLINE":
        raw = [(point[0] * factor, point[1] * factor, float(point[4] or 0.0)) for point in entity.get_points("xyseb")]
        closed = bool(entity.closed)
    else:
        raw = []
        for vertex in entity.vertices:
            location = vertex.dxf.location
            raw.append((location.x * factor, location.y * factor, float(vertex.dxf.bulge or 0.0)))
        closed = bool(entity.is_closed)

    if not raw:
        return []
    vertices: List[tuple] = []
    count = len(raw)
    for idx, (x, y, bulge) in enumerate(raw):
        has_next = idx + 1 < count or closed
        if abs(bulge) > 1e-9 and has_next:
            nx, ny, _ = raw[(idx + 1) % count]
            vertices.extend(_bulge_points((x, y), (nx, ny), bulge))
        else:
            vertices.append((x, y))
    if closed and vertices and vertices[0] != vertices[-1]:
        vertices.append(vertices[0])
    return vertices


def _coerce_polygon(vertices: Sequence[tuple], close_tolerance: float = SNAP_TOLERANCE_FEET) -> Polygon | None:
    if len(vertices) < 3:
        return None

    ring = list(vertices)
    if ring[0] != ring[-1]:
        start = Point(ring[0])
        end = Point(ring[-1])
        if start.distance(end) <= close_tolerance:
            ring.append(ring[0])
        else:
            return None

    polygon = Polygon(ring).buffer(0)
    if polygon.is_empty or polygon.area <= 1e-6:
        return None
    return polygon


def entity_to_polygon(entity, factor: float, close_tolerance: float = SNAP_TOLERANCE_FEET) -> Polygon | None:
    kind = entity.dxftype()

    if kind in {"LWPOLYLINE", "POLYLINE"}:
        vertices = _polyline_vertices(entity, factor)
        polygon = _coerce_polygon(vertices, close_tolerance=close_tolerance)
        if polygon is not None:
            return polygon

        # Some CAD exports leave boundary polylines visually closed but do not set the closed flag.
        raw_vertices = [(point[0] * factor, point[1] * factor) for point in entity.get_points()] if kind == "LWPOLYLINE" else [
            (vertex.dxf.location.x * factor, vertex.dxf.location.y * factor) for vertex in entity.vertices
        ]
        return _coerce_polygon(raw_vertices, close_tolerance=close_tolerance)

    if kind == "CIRCLE":
        center = entity.dxf.center
        radius = entity.dxf.radius * factor
        points = _arc_points(center.x * factor, center.y * factor, radius, 0.0, 360.0)
        return _coerce_polygon(points, close_tolerance=close_tolerance)

    return None


def entity_to_lines(entity, factor: float) -> List[LineString]:
    kind = entity.dxftype()
    if kind == "LINE":
        start = entity.dxf.start
        end = entity.dxf.end
        return [
            LineString(
                [
                    (start.x * factor, start.y * factor),
                    (end.x * factor, end.y * factor),
                ]
            )
        ]

    if kind in {"LWPOLYLINE", "POLYLINE"}:
        vertices = _polyline_vertices(entity, factor)
        if len(vertices) < 2:
            return []
        return [LineString([vertices[index], vertices[index + 1]]) for index in range(len(vertices) - 1)]

    if kind == "CIRCLE":
        center = entity.dxf.center
        radius = entity.dxf.radius * factor
        points = _arc_points(center.x * factor, center.y * factor, radius, 0.0, 360.0)
        return [LineString([points[index], points[index + 1]]) for index in range(len(points) - 1)]

    if kind == "ARC":
        center = entity.dxf.center
        radius = entity.dxf.radius * factor
        points = _arc_points(
            center.x * factor,
            center.y * factor,
            radius,
            entity.dxf.start_angle,
            entity.dxf.end_angle,
        )
        return [LineString([points[index], points[index + 1]]) for index in range(len(points) - 1)]

    return []


def polygons_from_entities(entities: Iterable, factor: float, snap_tolerance: float = SNAP_TOLERANCE_FEET) -> List[Polygon]:
    direct_polygons: List[Polygon] = []
    lines: List[LineString] = []
    for entity in entities:
        polygon = entity_to_polygon(entity, factor, close_tolerance=snap_tolerance)
        if polygon is not None:
            direct_polygons.append(polygon)
            continue
        lines.extend(entity_to_lines(entity, factor))

    raw_polygons = [polygon.buffer(0) for polygon in direct_polygons if not polygon.is_empty and polygon.area > 1e-6]

    if lines:
        merged = unary_union(lines)
        snapped = snap(merged, merged, snap_tolerance)
        raw_polygons.extend(polygon.buffer(0) for polygon in polygonize(snapped))

    polygons = [polygon for polygon in raw_polygons if not polygon.is_empty and polygon.area > 1e-6]
    unique_polygons: List[Polygon] = []
    for polygon in polygons:
        if any(existing.equals_exact(polygon, 1e-6) for existing in unique_polygons):
            continue
        unique_polygons.append(polygon)
    return unique_polygons


def build_floor_surfaces(loop_polygons: Sequence[Polygon]):
    """Return each boundary polygon as an individual floor surface.

    Previous behavior used even/odd nesting depth to subtract inner polygons
    as holes.  In multi-story DXFs the inner boundary is a separate floor,
    not a courtyard — subtracting it loses area.

    Now: every valid closed boundary is its own surface.  Merging of
    overlapping/adjacent floors with the same floor number happens later
    in the consolidation step.
    """
    if not loop_polygons:
        return []

    surfaces = []
    for polygon in loop_polygons:
        clean = polygon.buffer(0)
        if clean.is_empty or clean.area <= 1e-6:
            continue
        if clean.geom_type == "Polygon":
            surfaces.append(clean)
        elif clean.geom_type == "MultiPolygon":
            surfaces.extend(g for g in clean.geoms if not g.is_empty)

    return surfaces


def point_is_inside(polygon: Polygon, point: Point) -> bool:
    return polygon.buffer(EDGE_TOLERANCE_FEET).covers(point)


def line_intersects(polygon: Polygon, line: LineString) -> bool:
    return polygon.buffer(EDGE_TOLERANCE_FEET).intersects(line)


def nearest_label(polygon: Polygon, labels: Sequence[Dict], field: str) -> str | None:
    if not labels:
        return None
    centroid = polygon.centroid
    best = min(labels, key=lambda item: centroid.distance(Point(item["x"], item["y"])))
    value = str(best.get(field, "")).strip()
    return value or None


def entity_matches_layer(entity, allowed_layers: Sequence[str]) -> bool:
    if not allowed_layers:
        return False
    layer = getattr(entity.dxf, "layer", "")
    return layer in set(allowed_layers)


def iter_entities_by_layer(doc, allowed_layers: Sequence[str], allowed_types: Sequence[str] | None = None):
    allowed_set = set(allowed_layers)
    type_set = set(allowed_types or [])
    for entity in doc.modelspace():
        layer = getattr(entity.dxf, "layer", "")
        if layer not in allowed_set:
            continue
        if type_set and entity.dxftype() not in type_set:
            continue
        yield entity


def all_layers(doc) -> List[str]:
    return sorted({layer.dxf.name for layer in doc.layers} | {getattr(entity.dxf, "layer", "") for entity in doc.modelspace()})
