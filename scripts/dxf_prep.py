#!/usr/bin/env python3
"""Calm, low-tech helper for turning messy architect CAD into a formatted
input DXF for the tributary tool.

Three sub-commands, each deterministic and reviewable:

    inspect  what is in a DXF (layers, entity types, texts, blocks, extents)
    prep     build one clean working DXF from per-floor source DXFs + a map
    check    validate a formatted DXF against the engine's input contract

The target format is documented in canonical-docs/INPUT_DXF_CONTRACT.md.
The checker imports the engine's own geometry helpers so its verdicts match
what the app will do with the file.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import ezdxf
from ezdxf import path as ezpath
from shapely.geometry import Point, Polygon

ROOT_DIR = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT_DIR / "web" / "api" / "_engine"
sys.path.insert(0, str(ENGINE_DIR))

from geometry_utils import (  # noqa: E402
    entity_to_polygon,
    extract_entity_text,
    polygons_from_entities,
    sanitized_floor_token,
)
from inspection_utils import ROLE_KEYWORDS, suggest_layers  # noqa: E402

# ---------------------------------------------------------------------------
# Contract constants (mirror the engine; see INPUT_DXF_CONTRACT.md)
# ---------------------------------------------------------------------------

CANONICAL_LAYERS = {
    "boundary": "BOUNDARY",
    "additional_load": "ADDITIONAL-LOAD",
    "wall": "WALL",
    "beam": "BEAM",
    "support_point": "COLS",
    "column_label": "COL-LABEL",
    "floor_label": "FLOOR NUMBER",
    "datum": "DATUM",
}
# ACI colours so the canonical layers read instantly in AutoCAD.
CANONICAL_COLORS = {
    "BOUNDARY": 1,          # red
    "ADDITIONAL-LOAD": 6,   # magenta
    "WALL": 4,              # cyan
    "BEAM": 30,             # orange
    "COLS": 2,              # yellow
    "COL-LABEL": 2,         # yellow
    "FLOOR NUMBER": 3,      # green
    "DATUM": 7,             # white/black
}
BACKGROUND_COLOR = 8  # dark grey for reference linework

STRUCTURAL_ROLES = ("boundary", "additional_load", "wall", "beam", "support_point")
LOOP_ROLES = ("boundary", "additional_load")
GEOMETRY_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE"}
TEXT_TYPES = {"TEXT", "MTEXT"}
COPYABLE_TYPES = GEOMETRY_TYPES | TEXT_TYPES | {"POINT"}

MIN_COLUMN_FOOTPRINT_AREA_SF = 0.25        # extract_dxf_data.py
MIN_COLUMN_FOOTPRINT_DIMENSION_FT = 0.5    # extract_dxf_data.py
LABEL_LOW_CONFIDENCE_FT = 10.0             # ENGINEERING_METHOD.md
UNIT_CONFIRM_AREA_SF = 10_000.0            # TECH_SPEC.md
IMPLAUSIBLE_AREA_SF = 150_000.0            # a single plate bigger than this means wrong units
CURVE_FLATTEN_FT = 0.01                    # geometry_utils.CURVE_TOLERANCE_FEET
DEFAULT_STACK_GAP_FT = 40.0
DEFAULT_LABEL_HEIGHT_FT = 3.0
INSUNITS = {1: "in", 2: "ft"}


def unit_factor(units: str) -> float:
    return 1.0 / 12.0 if units == "in" else 1.0


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def header_units(doc) -> str | None:
    return INSUNITS.get(doc.header.get("$INSUNITS", 0))


def iter_flat(entities: Iterable, depth: int = 0, keep_insert=None):
    """Yield entities with INSERTs exploded (recursively) into virtual entities.

    `keep_insert(insert)` may return True to yield that INSERT whole instead
    of exploding it (used for column blocks, which become one footprint).
    """
    for entity in entities:
        if entity.dxftype() == "INSERT":
            if keep_insert is not None and keep_insert(entity):
                yield entity
                continue
            if depth > 8:
                continue
            try:
                children = list(entity.virtual_entities())
            except Exception:
                continue
            yield from iter_flat(children, depth + 1, keep_insert)
        else:
            yield entity


def hatch_segments(hatch) -> List[tuple]:
    """Straight segments of a HATCH boundary (polyline paths and line edges)."""
    segments = []
    try:
        for p in hatch.paths:
            if p.type == ezdxf.entities.BoundaryPathType.POLYLINE:
                verts = [(v[0], v[1]) for v in p.vertices]
                if len(verts) >= 2:
                    ring = verts + [verts[0]] if p.is_closed or len(verts) > 2 else verts
                    segments.extend((ring[i], ring[i + 1]) for i in range(len(ring) - 1))
            else:
                for edge in p.edges:
                    if edge.type == ezdxf.entities.EdgeType.LINE:
                        segments.append(((edge.start[0], edge.start[1]), (edge.end[0], edge.end[1])))
    except Exception:
        return segments
    return segments


NAMED_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
NAMED_DIA_RE = re.compile(r"(?:DIA|ROUND)\D*(\d+(?:\.\d+)?)", re.IGNORECASE)


def named_footprint(insert) -> List[tuple] | None:
    """Footprint from the size in a Revit column block name.

    `Concrete-Rectangular-Column - 14 x 36-...` is a 14 by 36 rectangle
    centred on the insert point (verified on this set: insert == centroid),
    rotated by the insert rotation; `... ROUND - DIA 30 ...` is a circle.
    Works even when the block definition came through conversion empty.
    """
    if insert.dxftype() != "INSERT":
        return None
    name = insert.dxf.name or ""
    try:
        ix, iy = insert.dxf.insert.x, insert.dxf.insert.y
        rot = math.radians(insert.dxf.rotation)
        sx, sy = insert.dxf.xscale, insert.dxf.yscale
    except Exception:
        return None
    m = NAMED_SIZE_RE.search(name)
    if m:
        w, h = float(m.group(1)) * sx, float(m.group(2)) * sy
        local = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    else:
        m = NAMED_DIA_RE.search(name)
        if not m:
            return None
        r = float(m.group(1)) * sx / 2
        local = [(r * math.cos(2 * math.pi * i / 32), r * math.sin(2 * math.pi * i / 32)) for i in range(32)]
    c, s = math.cos(rot), math.sin(rot)
    return [(ix + x * c - y * s, iy + x * s + y * c) for x, y in local]


def segments_to_rings(segments: List[tuple], min_area: float = 1e-6) -> List[List[tuple]]:
    """Polygonize straight segments; return exterior rings, largest first."""
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union

    lines = [LineString(s) for s in segments if s[0] != s[1]]
    if not lines:
        return []
    polys = [p for p in polygonize(unary_union(lines)) if p.area > min_area]
    polys.sort(key=lambda p: -p.area)
    return [list(p.exterior.coords)[:-1] for p in polys]


def block_footprint(insert) -> List[tuple] | None:
    """Closed footprint ring (source units) for a column block.

    Order: size parsed from the block name (exact, survives empty block
    definitions); else polygonize the block's LINEs plus HATCH boundary (a
    wall may hide one line, the hatch still carries the outline); else the
    minimum rotated rectangle of whatever points exist.
    """
    from shapely.geometry import MultiPoint

    named = named_footprint(insert)
    if named:
        return named

    segments: List[tuple] = []
    try:
        children = list(insert.virtual_entities())
    except Exception:
        return None
    for v in children:
        kind = v.dxftype()
        if kind == "LINE":
            segments.append(((v.dxf.start.x, v.dxf.start.y), (v.dxf.end.x, v.dxf.end.y)))
        elif kind in {"LWPOLYLINE", "POLYLINE"}:
            pts = entity_points(v)
            if is_closed(v) and pts:
                pts = pts + [pts[0]]
            segments.extend((pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        elif kind == "HATCH":
            segments.extend(hatch_segments(v))
        elif kind == "CIRCLE":
            pts = flatten_vertices(v, "in")
            segments.extend((pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    if not segments:
        return None
    rings = segments_to_rings(segments)
    if rings:
        return rings[0]
    points = [pt for s in segments for pt in s]
    if len(points) < 3:
        return None
    rect = MultiPoint(points).minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return None
    return list(rect.exterior.coords)[:-1]


def entity_points(entity) -> List[tuple]:
    kind = entity.dxftype()
    try:
        if kind == "LINE":
            return [(entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y)]
        if kind == "POINT":
            return [(entity.dxf.location.x, entity.dxf.location.y)]
        if kind in TEXT_TYPES:
            return [(entity.dxf.insert.x, entity.dxf.insert.y)]
        if kind == "LWPOLYLINE":
            return [(p[0], p[1]) for p in entity.get_points()]
        if kind == "POLYLINE":
            return [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
        if kind in {"CIRCLE", "ARC"}:
            c, r = entity.dxf.center, entity.dxf.radius
            return [(c.x - r, c.y - r), (c.x + r, c.y + r)]
    except Exception:
        return []
    return []


def bbox_of(points: Sequence[tuple]):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def has_bulge(entity) -> bool:
    kind = entity.dxftype()
    try:
        if kind == "LWPOLYLINE":
            return any(abs(p[4]) > 1e-9 for p in entity.get_points("xyseb"))
        if kind == "POLYLINE":
            return any(abs(v.dxf.bulge) > 1e-9 for v in entity.vertices)
    except Exception:
        return False
    return False


def is_closed(entity) -> bool:
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        return bool(entity.closed)
    if kind == "POLYLINE":
        return bool(entity.is_closed)
    return kind == "CIRCLE"


def flatten_vertices(entity, units: str) -> List[tuple]:
    """Vertices of a curve-bearing entity, flattened to the engine's tolerance."""
    distance = CURVE_FLATTEN_FT / unit_factor(units)  # tolerance in source units
    try:
        p = ezpath.make_path(entity)
        return [(v.x, v.y) for v in p.flattening(distance)]
    except Exception:
        return entity_points(entity)


def fmt_ft(value_source_units: float, units: str) -> str:
    return f"{value_source_units * unit_factor(units):.1f} ft"


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def cmd_inspect(args) -> int:
    doc = ezdxf.readfile(args.dxf)
    msp = doc.modelspace()
    units = args.units or header_units(doc) or "in"

    per_layer: Dict[str, Dict] = defaultdict(
        lambda: {"counts": Counter(), "closed": 0, "open": 0, "bulge": 0, "texts": [], "pts": []}
    )
    inserts = 0
    for raw in msp:
        if raw.dxftype() == "INSERT":
            inserts += 1
        for e in iter_flat([raw]):
            layer = getattr(e.dxf, "layer", "0")
            item = per_layer[layer]
            kind = e.dxftype()
            item["counts"][kind] += 1
            if kind in {"LWPOLYLINE", "POLYLINE"}:
                item["closed" if is_closed(e) else "open"] += 1
                if has_bulge(e):
                    item["bulge"] += 1
            if kind in TEXT_TYPES and len(item["texts"]) < 5:
                text = extract_entity_text(e)
                if text:
                    item["texts"].append(text[:24])
            item["pts"].extend(entity_points(e))

    all_pts = [p for item in per_layer.values() for p in item["pts"]]
    extent = bbox_of(all_pts)

    blocks = [b.name for b in doc.blocks if not b.name.startswith("*")]
    layer_counts = {layer: dict(item["counts"]) for layer, item in per_layer.items()}
    guess = suggest_layers(layer_counts)

    if args.json:
        payload = {
            "file": str(args.dxf),
            "dxf_version": doc.dxfversion,
            "header_units": header_units(doc),
            "assumed_units": units,
            "extent_source_units": extent,
            "blocks": len(blocks),
            "inserts": inserts,
            "layers": {
                layer: {
                    "counts": dict(item["counts"]),
                    "closed_polylines": item["closed"],
                    "open_polylines": item["open"],
                    "bulged_polylines": item["bulge"],
                    "text_samples": item["texts"],
                    "extent": bbox_of(item["pts"]),
                }
                for layer, item in sorted(per_layer.items())
            },
            "heuristic_role_guess": guess,
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"FILE      {args.dxf}")
    print(f"VERSION   {doc.dxfversion}   $INSUNITS={doc.header.get('$INSUNITS', 0)} ({header_units(doc) or '?'})   assuming {units}")
    if extent:
        w = extent[2] - extent[0]
        h = extent[3] - extent[1]
        print(f"EXTENT    x {extent[0]:.1f}..{extent[2]:.1f}  y {extent[1]:.1f}..{extent[3]:.1f}  ({fmt_ft(w, units)} x {fmt_ft(h, units)})")
    print(f"BLOCKS    {len(blocks)} definitions, {inserts} inserts in modelspace (exploded for the counts below)")
    print()
    print(f"{'LAYER':36s} {'ENT':>5s} {'CLOSED':>6s} {'OPEN':>5s} {'BULGE':>5s}  TYPES / TEXT SAMPLES")
    for layer, item in sorted(per_layer.items(), key=lambda kv: -sum(kv[1]["counts"].values())):
        total = sum(item["counts"].values())
        types = " ".join(f"{k}:{v}" for k, v in item["counts"].most_common(4))
        texts = ("  " + " | ".join(repr(t) for t in item["texts"])) if item["texts"] else ""
        print(f"{layer[:36]:36s} {total:5d} {item['closed']:6d} {item['open']:5d} {item['bulge']:5d}  {types}{texts}")
    print()
    print("HEURISTIC ROLE GUESS (what the app would preselect)")
    for role in ROLE_KEYWORDS:
        print(f"  {role:16s} {', '.join(guess.get(role, [])) or '-'}")
    return 0


# ---------------------------------------------------------------------------
# prep
# ---------------------------------------------------------------------------


RASTER_RES_IN = 6.0  # half a foot per pixel for the automatic envelope


def envelope_polygons(segments: List[tuple], rings: List[List[tuple]], close_ft: float, open_ft: float, min_area_sf: float, units: str, grow_ft: float = 1.0) -> List[Polygon]:
    """Draft slab outline from architectural linework.

    Rasterise the lines and column rings on a half-foot grid, close gaps up
    to `close_ft` (dilate then erode), drop slivers thinner than `open_ft`
    (erode then dilate), and trace each filled region's outer ring. Holes
    are ignored on purpose: the engineer cuts real openings afterwards.
    Returns polygons in source units.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    import contourpy

    if not segments and not rings:
        return []
    f = unit_factor(units)
    res = RASTER_RES_IN * (1.0 / 12.0) / f  # source units per pixel (0.5 ft)
    pts = [p for s in segments for p in s] + [p for r in rings for p in r]
    pad = 2 * close_ft / f
    x0 = min(p[0] for p in pts) - pad
    y0 = min(p[1] for p in pts) - pad
    W = int((max(p[0] for p in pts) + pad - x0) / res) + 1
    H = int((max(p[1] for p in pts) + pad - y0) / res) + 1
    if W * H > 40_000_000:
        raise RuntimeError("envelope raster too large; reduce the auto_boundary layers")
    im = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(im)

    def T(p):
        return ((p[0] - x0) / res, H - 1 - (p[1] - y0) / res)

    for a, b in segments:
        draw.line([T(a), T(b)], fill=255, width=2)
    for r in rings:
        if len(r) >= 3:
            draw.polygon([T(p) for p in r], fill=255)

    def k(ft):
        return max(1, int(round(ft / f / res))) * 2 + 1

    def outer_rings(image):
        mask = (np.array(image) > 127).astype(float)
        gen = contourpy.contour_generator(z=mask, fill_type="OuterOffset")
        points, offsets = gen.filled(0.5, 1.5)
        return [arr[offs[0]:offs[1]] for arr, offs in zip(points, offsets)]

    # 1. close gaps; 2. fill every enclosed interior by redrawing outer rings
    # solid; 3. only then remove slivers, so a thin perimeter band around a
    # large empty deck is never eroded open.
    im = im.filter(ImageFilter.MaxFilter(k(close_ft))).filter(ImageFilter.MinFilter(k(close_ft)))
    solid = Image.new("L", (W, H), 0)
    sdraw = ImageDraw.Draw(solid)
    for ring in outer_rings(im):
        if len(ring) >= 3:
            sdraw.polygon([(float(px), float(py)) for px, py in ring], fill=255)
    if open_ft > 0:
        solid = solid.filter(ImageFilter.MinFilter(k(open_ft))).filter(ImageFilter.MaxFilter(k(open_ft)))
    polys = []
    for outer in outer_rings(solid):
        poly = Polygon([(x0 + px * res, y0 + (H - 1 - py) * res) for px, py in outer]).buffer(0)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.is_empty:
            continue
        poly = Polygon(poly.exterior)
        if grow_ft > 0:
            poly = Polygon(poly.buffer(grow_ft / f, join_style=2).exterior)
        poly = poly.simplify(res / 2)
        if poly.area * f * f >= min_area_sf:
            polys.append(poly)
    polys.sort(key=lambda p: -p.area)
    return polys


def load_map(path: str) -> Dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    layers = {role: list(raw.get("layers", {}).get(role, []) or []) for role in CANONICAL_LAYERS}
    reference = raw.get("reference", {}) or {}
    compiled = {name: [re.compile(p, re.IGNORECASE) for p in pats] for name, pats in reference.items()}
    auto = raw.get("auto_boundary") or None
    if auto:
        auto = {
            "layers": [re.compile(p, re.IGNORECASE) for p in auto.get("layers", [])],
            "include_columns": bool(auto.get("include_columns", True)),
            "close_ft": float(auto.get("close_ft", 5.0)),
            "open_ft": float(auto.get("open_ft", 2.5)),
            "grow_ft": float(auto.get("grow_ft", 1.0)),
            "min_area_sf": float(auto.get("min_area_sf", 500.0)),
        }
    return {
        "auto_boundary": auto,
        "auto_labels": bool(raw.get("auto_labels", False)),
        # "same_plan": columns drawn on a plan carry that plan's slab (structural
        # framing plans). "plan_below": columns drawn on a plan stand on it and
        # carry the slab above (architectural plans), so each output floor takes
        # its columns from the previous source in `floors`.
        "columns_from": str(raw.get("columns_from", "same_plan")),
        "label_prefix": str(raw.get("label_prefix", "C")),
        "col_label_height_ft": float(raw.get("col_label_height_ft", 1.5)),
        "source_units": str(raw.get("source_units", "in")).lower(),
        "layers": layers,
        "reference": compiled,
        "explode_blocks": bool(raw.get("explode_blocks", True)),
        "source_dir": raw.get("source_dir"),
        "datum_xy": raw.get("datum_xy"),
        "stack": raw.get("stack", {}) or {},
        "floors": raw.get("floors", []) or [],
        "label_height_ft": float(raw.get("label_height_ft", DEFAULT_LABEL_HEIGHT_FT)),
    }


def classify_layer(layer: str, mapping: Dict) -> str | None:
    for role, names in mapping["layers"].items():
        if layer in names:
            return CANONICAL_LAYERS[role]
    for ref_name, patterns in mapping["reference"].items():
        if any(p.search(layer) for p in patterns):
            return ref_name
    return None


def ensure_layers(doc, mapping: Dict) -> None:
    for role, name in CANONICAL_LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=CANONICAL_COLORS.get(name, 7))
    for ref_name in mapping["reference"]:
        if ref_name not in doc.layers:
            doc.layers.add(ref_name, color=BACKGROUND_COLOR)


def copy_entity(msp, entity, target_layer: str, dx: float, dy: float, units: str, structural: bool) -> bool:
    """Re-create `entity` in `msp` on `target_layer`, shifted by (dx, dy).

    Structural layers are flattened to straight segments so the file holds
    exactly the geometry the engine will see. Reference layers keep arcs.
    """
    kind = entity.dxftype()
    attribs = {"layer": target_layer}
    try:
        if kind == "INSERT":
            ring = block_footprint(entity)
            if not ring:
                return False
            msp.add_lwpolyline([(x + dx, y + dy) for x, y in ring], format="xy", close=True, dxfattribs=attribs)
        elif kind == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            msp.add_line((s.x + dx, s.y + dy), (e.x + dx, e.y + dy), dxfattribs=attribs)
        elif kind == "POINT":
            loc = entity.dxf.location
            msp.add_point((loc.x + dx, loc.y + dy), dxfattribs=attribs)
        elif kind in {"LWPOLYLINE", "POLYLINE"}:
            closed = is_closed(entity)
            if structural and has_bulge(entity):
                pts = [(x + dx, y + dy) for x, y in flatten_vertices(entity, units)]
                if closed and len(pts) > 1 and pts[0] == pts[-1]:
                    pts = pts[:-1]
                if len(pts) < 2:
                    return False
                msp.add_lwpolyline(pts, format="xy", close=closed, dxfattribs=attribs)
            elif kind == "LWPOLYLINE":
                pts = [(p[0] + dx, p[1] + dy, p[2], p[3], p[4]) for p in entity.get_points("xyseb")]
                if len(pts) < 2:
                    return False
                msp.add_lwpolyline(pts, format="xyseb", close=closed, dxfattribs=attribs)
            else:
                pts = [(v.dxf.location.x + dx, v.dxf.location.y + dy, 0.0, 0.0, v.dxf.bulge) for v in entity.vertices]
                if len(pts) < 2:
                    return False
                msp.add_lwpolyline(pts, format="xyseb", close=closed, dxfattribs=attribs)
        elif kind == "CIRCLE":
            c = entity.dxf.center
            if structural:
                pts = [(x + dx, y + dy) for x, y in flatten_vertices(entity, units)]
                if len(pts) > 1 and pts[0] == pts[-1]:
                    pts = pts[:-1]
                msp.add_lwpolyline(pts, format="xy", close=True, dxfattribs=attribs)
            else:
                msp.add_circle((c.x + dx, c.y + dy), entity.dxf.radius, dxfattribs=attribs)
        elif kind == "ARC":
            c = entity.dxf.center
            if structural:
                pts = [(x + dx, y + dy) for x, y in flatten_vertices(entity, units)]
                if len(pts) < 2:
                    return False
                msp.add_lwpolyline(pts, format="xy", close=False, dxfattribs=attribs)
            else:
                msp.add_arc(
                    (c.x + dx, c.y + dy),
                    entity.dxf.radius,
                    entity.dxf.start_angle,
                    entity.dxf.end_angle,
                    dxfattribs=attribs,
                )
        elif kind == "TEXT":
            text = extract_entity_text(entity)
            if not text:
                return False
            ins = entity.dxf.insert
            msp.add_text(
                text,
                height=entity.dxf.height,
                rotation=entity.dxf.rotation,
                dxfattribs=attribs,
            ).set_placement((ins.x + dx, ins.y + dy))
        elif kind == "MTEXT":
            text = extract_entity_text(entity)
            if not text:
                return False
            ins = entity.dxf.insert
            mt = msp.add_mtext(text, dxfattribs={**attribs, "char_height": entity.dxf.char_height})
            mt.set_location((ins.x + dx, ins.y + dy), rotation=entity.dxf.rotation)
        else:
            return False
    except Exception:
        return False
    return True


def cmd_prep(args) -> int:
    mapping = load_map(args.map)
    units = args.units or mapping["source_units"]
    floors = list(mapping["floors"])
    if args.dxf:
        labels = [s.strip() for s in (args.labels or "").split(",") if s.strip()]
        if labels and len(labels) != len(args.dxf):
            print(f"ERROR: --labels has {len(labels)} entries for {len(args.dxf)} files", file=sys.stderr)
            return 2
        floors = [
            {"file": f, "label": labels[i] if labels else str(i + 1)}
            for i, f in enumerate(args.dxf)
        ]
    if not floors:
        print("ERROR: no floors given (map 'floors' list or positional DXF files)", file=sys.stderr)
        return 2

    # Pass 1: read every source, pick the entities we keep, measure extents.
    sources = []
    common = None
    src_dir = Path(args.src) if args.src else (Path(mapping["source_dir"]) if mapping["source_dir"] else None)
    missing = []
    for floor in floors:
        src_path = Path(floor["file"])
        if not src_path.is_absolute():
            candidates = [src_path, Path(args.map).parent / src_path]
            if src_dir:
                candidates.insert(0, src_dir / src_path)
            src_path = next((c for c in candidates if c.exists()), candidates[0])
        if not src_path.exists():
            missing.append(str(src_path))
            continue
        doc = ezdxf.readfile(str(src_path))
        src_units = header_units(doc)
        if src_units and src_units != units:
            print(f"WARNING: {src_path.name} header says {src_units}, map says {units}; using {units}")
        kept = []
        dropped = Counter()
        support_layers = set(mapping["layers"]["support_point"])
        is_column_block = lambda ins: getattr(ins.dxf, "layer", "0") in support_layers  # noqa: E731
        entities = (
            iter_flat(doc.modelspace(), keep_insert=is_column_block)
            if mapping["explode_blocks"]
            else doc.modelspace()
        )
        loose_segments: List[tuple] = []  # LINE/ARC drawn directly on a column layer
        auto = mapping["auto_boundary"]
        env_segments: List[tuple] = []
        env_rings: List[List[tuple]] = []
        for e in entities:
            layer = getattr(e.dxf, "layer", "0")
            if e.dxftype() == "INSERT":
                if layer in support_layers:
                    kept.append((e, CANONICAL_LAYERS["support_point"]))
                    if auto and auto["include_columns"]:
                        ring = block_footprint(e)
                        if ring:
                            env_rings.append(ring)
                else:
                    dropped["INSERT (explode_blocks=false)"] += 1
                continue
            if auto and e.dxftype() in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC"} and any(p.search(layer) for p in auto["layers"]):
                epts = flatten_vertices(e, units) if e.dxftype() == "ARC" or has_bulge(e) else entity_points(e)
                if is_closed(e) and epts and epts[0] != epts[-1]:
                    epts = epts + [epts[0]]
                env_segments.extend((epts[i], epts[i + 1]) for i in range(len(epts) - 1))
            target = classify_layer(layer, mapping)
            if target is None:
                dropped[layer] += 1
                continue
            if e.dxftype() not in COPYABLE_TYPES:
                dropped[f"{layer}/{e.dxftype()}"] += 1
                continue
            if target == CANONICAL_LAYERS["support_point"] and e.dxftype() in {"LINE", "ARC"}:
                pts = flatten_vertices(e, units) if e.dxftype() == "ARC" else entity_points(e)
                loose_segments.extend((pts[i], pts[i + 1]) for i in range(len(pts) - 1))
                continue
            kept.append((e, target))
        # Columns drawn as loose lines (not blocks): close them into footprints.
        loose_rings = segments_to_rings(loose_segments)
        floor["_loose_rings"] = loose_rings
        floor["_loose_leftover"] = len(loose_segments) - 4 * len(loose_rings) if loose_segments else 0
        floor["_auto_boundary"] = []
        if auto and not mapping["layers"]["boundary"]:
            env_rings.extend(loose_rings)
            floor["_auto_boundary"] = envelope_polygons(
                env_segments, env_rings, auto["close_ft"], auto["open_ft"], auto["min_area_sf"], units, auto["grow_ft"]
            )
        def points_of(e):
            return (block_footprint(e) if e.dxftype() == "INSERT" else entity_points(e)) or []

        pts = [p for e, _ in kept for p in points_of(e)]
        pts += [p for ring in loose_rings for p in ring]
        # Structural extent (columns/boundary/walls) is where the floor label goes.
        structural_targets = {CANONICAL_LAYERS[r] for r in STRUCTURAL_ROLES}
        spts = [p for e, t in kept if t in structural_targets for p in points_of(e)]
        spts += [p for ring in loose_rings for p in ring]
        floor["_struct_extent"] = bbox_of(spts)
        ext = bbox_of(pts)
        if ext is None:
            print(f"WARNING: {src_path.name}: nothing kept; check the map", file=sys.stderr)
            ext = (0.0, 0.0, 0.0, 0.0)
        common = ext if common is None else (
            min(common[0], ext[0]), min(common[1], ext[1]), max(common[2], ext[2]), max(common[3], ext[3])
        )
        sources.append({"floor": floor, "path": src_path, "doc": doc, "kept": kept, "extent": ext, "dropped": dropped})

    if missing:
        print("ERROR: source DXF not found:\n  " + "\n  ".join(missing), file=sys.stderr)
        print("Set 'source_dir' in the map or pass --src <dir>.", file=sys.stderr)
        return 2
    if not sources:
        return 2

    # Pass 2: stack floors bottom-up with a uniform pitch so all floors keep
    # their shared model coordinates (same x, y offset per floor) and a single
    # source datum point lands at the same relative spot on every floor.
    gap = float(mapping["stack"].get("gap_ft", args.gap_ft)) / unit_factor(units)
    axis = str(mapping["stack"].get("axis", "y")).lower()
    width = common[2] - common[0]
    height = common[3] - common[1]
    pitch = math.ceil(((height if axis == "y" else width) + gap) * unit_factor(units)) / unit_factor(units)

    out = ezdxf.new("R2018")
    out.header["$INSUNITS"] = 1 if units == "in" else 2
    ensure_layers(out, mapping)
    msp = out.modelspace()
    label_h = mapping["label_height_ft"] / unit_factor(units)
    structural_layers = {CANONICAL_LAYERS[r] for r in STRUCTURAL_ROLES}

    # Column labels that stay the same up the building: columns are matched
    # floor to floor in the shared source coordinates (same Revit model), so a
    # column inherits the label of the column within 1 ft below it.
    label_counter = 0
    prev_labels: List[tuple] = []  # (x, y, label) in source units, previous floor
    label_h = mapping["col_label_height_ft"] / unit_factor(units)
    match_tol = CONTINUOUS_FT / unit_factor(units)

    plan_below = mapping["columns_from"] == "plan_below"
    col_layer = CANONICAL_LAYERS["support_point"]

    def column_rings_of(src) -> List[List[tuple]]:
        rings = []
        for e, target in src["kept"]:
            if target == col_layer:
                if e.dxftype() == "INSERT":
                    ring = block_footprint(e)
                elif e.dxftype() in {"LWPOLYLINE", "POLYLINE", "CIRCLE"}:
                    ring = flatten_vertices(e, units) if e.dxftype() == "CIRCLE" or has_bulge(e) else entity_points(e)
                else:
                    ring = None
                if ring and len(ring) >= 3:
                    rings.append(list(ring))
        rings.extend(src["floor"].get("_loose_rings", []))
        return rings

    print(f"{'FLOOR':10s} {'SOURCE':24s} {'KEPT':>5s} {'DROP':>6s} {'COLS':>5s} {'FROM':>6s} {'NAMED':>5s} {'LOOSE':>5s} {'BNDRY SF':>9s}  OFFSET (source units)")
    for index, src in enumerate(sources):
        dx = -common[0] + (index * pitch if axis == "x" else 0.0)
        dy = -common[1] + (index * pitch if axis == "y" else 0.0)
        copied = 0
        n_named = 0
        floor_meta = src["floor"]
        # Which source supplies this floor's columns.
        col_src = src
        col_from = "same"
        if plan_below:
            is_range = bool(re.fullmatch(r"\s*[A-Za-z]*\d+\s*[-–—]\s*[A-Za-z]*\d+\s*", str(floor_meta["label"])))
            if index > 0 and not is_range:
                col_src = sources[index - 1]
                col_from = str(col_src["floor"]["label"])
            elif index == 0:
                col_from = "same!"
                print(f"WARNING: floor '{floor_meta['label']}' is the lowest plan; columns_from=plan_below needs the plan below it, using its own columns", file=sys.stderr)
            else:
                col_from = "same"  # typical-floor range: its own columns stand for the plan below
        for e, target in src["kept"]:
            if target == col_layer:
                continue  # columns are written from col_src below
            if copy_entity(msp, e, target, dx, dy, units, structural=target in structural_layers):
                copied += 1
        col_rings = column_rings_of(col_src)
        for ring in col_rings:
            msp.add_lwpolyline([(x + dx, y + dy) for x, y in ring], format="xy", close=True, dxfattribs={"layer": col_layer})
            copied += 1
        n_cols = len(col_rings)
        n_named = sum(1 for e, t in col_src["kept"] if t == col_layer and e.dxftype() == "INSERT" and named_footprint(e))
        n_loose = len(col_src["floor"].get("_loose_rings", []))
        boundary_sf = 0.0
        boundary_polys = list(floor_meta.get("_auto_boundary", []))
        for poly in boundary_polys:
            coords = list(poly.exterior.coords)[:-1]
            msp.add_lwpolyline(
                [(x + dx, y + dy) for x, y in coords],
                format="xy",
                close=True,
                dxfattribs={"layer": CANONICAL_LAYERS["boundary"]},
            )
            boundary_sf += poly.area * unit_factor(units) ** 2
        if mapping["auto_labels"] and col_rings:
            cur_labels: List[tuple] = []
            centroids = [(sum(p[0] for p in r) / len(r), sum(p[1] for p in r) / len(r), r) for r in col_rings]
            # A column outside every draft slab loop is dropped by the engine;
            # leaving its label behind would only mislead the label matching.
            covered = [p.buffer(0.5 / unit_factor(units)) for p in boundary_polys]
            def labelable(cx, cy):
                return not covered or any(c.covers(Point(cx, cy)) for c in covered)
            # Inherit labels greedily by distance so each lower column is used once.
            inherited: Dict[int, str] = {}
            pairs = sorted(
                (math.hypot(px - cx, py - cy), i, j)
                for i, (cx, cy, _) in enumerate(centroids)
                for j, (px, py, _) in enumerate(prev_labels)
            )
            used_prev = set()
            for d, i, j in pairs:
                if d > match_tol:
                    break
                if i in inherited or j in used_prev:
                    continue
                inherited[i] = prev_labels[j][2]
                used_prev.add(j)
            # deterministic order for new labels: west to east, then south to north
            for i in sorted(range(len(centroids)), key=lambda i: (round(centroids[i][0]), round(centroids[i][1]))):
                cx, cy, ring = centroids[i]
                label = inherited.get(i)
                if label is None:
                    label_counter += 1
                    label = f"{mapping['label_prefix']}{label_counter}"
                cur_labels.append((cx, cy, label))
                if not labelable(cx, cy):
                    continue
                half_w = (max(p[0] for p in ring) - min(p[0] for p in ring)) / 2
                half_h = (max(p[1] for p in ring) - min(p[1] for p in ring)) / 2
                msp.add_text(
                    label, height=label_h, dxfattribs={"layer": CANONICAL_LAYERS["column_label"]}
                ).set_placement((cx + half_w + 0.25 * label_h + dx, cy + half_h + 0.25 * label_h + dy))
            prev_labels = cur_labels
        if floor_meta.get("_loose_leftover"):
            print(f"WARNING: {src['path'].name}: {floor_meta['_loose_leftover']} loose line(s) on the column layer did not close into a footprint", file=sys.stderr)
        label = str(floor_meta["label"])
        ext = floor_meta.get("_struct_extent") or src["extent"]
        cx = (ext[0] + ext[2]) / 2 + dx
        cy = (ext[1] + ext[3]) / 2 + dy
        msp.add_text(
            label, height=label_h, dxfattribs={"layer": CANONICAL_LAYERS["floor_label"]}
        ).set_placement((cx, cy))
        datum = src["floor"].get("datum_xy") or mapping["datum_xy"]
        if datum:
            msp.add_point((float(datum[0]) + dx, float(datum[1]) + dy), dxfattribs={"layer": CANONICAL_LAYERS["datum"]})
        dropped_total = sum(src["dropped"].values())
        print(f"{label:10s} {src['path'].name[:24]:24s} {copied:5d} {dropped_total:6d} {n_cols:5d} {col_from:>6s} {n_named:5d} {n_loose:5d} {boundary_sf:9,.0f}  ({dx:.1f}, {dy:.1f})")
    if mapping["auto_labels"]:
        print(f"COLUMN LABELS {mapping['label_prefix']}1..{mapping['label_prefix']}{label_counter}, consistent across floors where columns stack within {CONTINUOUS_FT:.0f} ft")

    if args.verbose:
        print()
        print("DROPPED LAYERS (union across floors, top 40)")
        total = Counter()
        for src in sources:
            total.update(src["dropped"])
        for layer, n in total.most_common(40):
            print(f"  {n:6d}  {layer}")

    out.saveas(args.out)
    print()
    print(f"WROTE {args.out}  units={units}  pitch={fmt_ft(pitch, units)}  floors={len(sources)}")
    print(f"NEXT  open it in AutoCAD, trace/clean on the canonical layers, then: dxf_prep.py check {args.out}")
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


class Report:
    def __init__(self):
        self.fails: List[str] = []
        self.warns: List[str] = []
        self.notes: List[str] = []

    def fail(self, msg: str):
        self.fails.append(msg)

    def warn(self, msg: str):
        self.warns.append(msg)

    def note(self, msg: str):
        self.notes.append(msg)


def role_layers(mapping: Dict | None, role: str) -> List[str]:
    if mapping and mapping["layers"].get(role):
        return mapping["layers"][role]
    return [CANONICAL_LAYERS[role]]


def column_footprint_ok(polygon) -> bool:
    if polygon is None or polygon.is_empty or polygon.area < MIN_COLUMN_FOOTPRINT_AREA_SF:
        return False
    min_x, min_y, max_x, max_y = polygon.bounds
    return min(max_x - min_x, max_y - min_y) >= MIN_COLUMN_FOOTPRINT_DIMENSION_FT


def cmd_check(args) -> int:
    mapping = load_map(args.map) if args.map else None
    doc = ezdxf.readfile(args.dxf)
    msp = doc.modelspace()
    units = args.units or (mapping["source_units"] if mapping else None) or header_units(doc) or "in"
    factor = unit_factor(units)
    rep = Report()

    hdr = header_units(doc)
    if hdr and hdr != units:
        rep.warn(f"$INSUNITS says {hdr} but checking as {units}; confirm units before upload")

    layers = {role: role_layers(mapping, role) for role in CANONICAL_LAYERS}
    known = {name for names in layers.values() for name in names}
    by_layer: Dict[str, List] = defaultdict(list)
    for e in msp:
        by_layer[getattr(e.dxf, "layer", "0")].append(e)

    def ents(role: str, kinds: set | None = None):
        for name in layers[role]:
            for e in by_layer.get(name, []):
                if kinds is None or e.dxftype() in kinds:
                    yield e

    # --- unsupported entity types on structural layers --------------------
    for role in STRUCTURAL_ROLES + ("column_label", "floor_label", "datum"):
        bad = Counter(e.dxftype() for e in ents(role) if e.dxftype() not in COPYABLE_TYPES)
        for kind, n in bad.items():
            if kind == "INSERT":
                rep.fail(f"{role}: {n} block INSERT(s) on {layers[role]} — the engine ignores blocks; explode them")
            else:
                rep.warn(f"{role}: {n} {kind} entit{'y' if n == 1 else 'ies'} on {layers[role]} will be ignored")

    # --- bulges on structural layers ---------------------------------------
    for role in STRUCTURAL_ROLES:
        n = sum(1 for e in ents(role, {"LWPOLYLINE", "POLYLINE"}) if has_bulge(e))
        if n:
            rep.warn(f"{role}: {n} polyline(s) with arc bulges — the engine reads vertices only and will chord them; flatten curves (prep does this)")

    # --- boundaries / floors ----------------------------------------------
    boundary_entities = list(ents("boundary", GEOMETRY_TYPES))
    open_loops = sum(1 for e in boundary_entities if e.dxftype() in {"LWPOLYLINE", "POLYLINE"} and not is_closed(e))
    if open_loops:
        rep.warn(f"boundary: {open_loops} polyline(s) not flagged closed; engine will try to close within 1 ft")
    primary_polys = polygons_from_entities(boundary_entities, factor)
    if not primary_polys:
        rep.fail(f"boundary: no closed slab loops found on {layers['boundary']}")

    # Additional-load loops (balconies, terraces, roof-only plates) are floor
    # surfaces to the engine as well: each gets the nearest floor label, and a
    # label with only additional-load loops becomes an additional-load-only
    # floor that is attached to its primary slab by DATUM.
    add_entities = list(ents("additional_load", GEOMETRY_TYPES))
    add_polys = polygons_from_entities(add_entities, factor) if add_entities else []
    add_open = sum(1 for e in add_entities if e.dxftype() in {"LWPOLYLINE", "POLYLINE"} and not is_closed(e))
    if add_open:
        rep.warn(f"additional_load: {add_open} polyline(s) not flagged closed")
    add_ids = {id(p) for p in add_polys}
    floor_polys = list(primary_polys) + list(add_polys)

    floor_labels = []
    for e in ents("floor_label", TEXT_TYPES):
        text = extract_entity_text(e)
        if text:
            ins = e.dxf.insert
            floor_labels.append({"floor_number": text.strip(), "x": ins.x * factor, "y": ins.y * factor})
    if not floor_labels:
        rep.fail(f"floor_label: no TEXT/MTEXT on {layers['floor_label']}; every floor needs a label")
    dup_labels = [t for t, n in Counter(l["floor_number"] for l in floor_labels).items() if n > 1]
    if dup_labels:
        rep.warn(f"floor_label: duplicate label text {dup_labels}; polygons with the same label are merged into one floor")

    # assign each polygon to nearest label by centroid (engine rule)
    floors: Dict[str, List[Polygon]] = defaultdict(list)
    for poly in floor_polys:
        if floor_labels:
            c = poly.centroid
            best = min(floor_labels, key=lambda l: c.distance(Point(l["x"], l["y"])))
            floors[best["floor_number"]].append(poly)
        else:
            floors[f"UNLABELED_{len(floors)}"].append(poly)
    used = set(floors.keys())
    orphans = [l["floor_number"] for l in floor_labels if l["floor_number"] not in used]
    if orphans:
        rep.warn(f"floor_label: labels nearest to no slab polygon: {orphans}")

    # floors must not overlap in plan (they would polygonize together)
    names = list(floors.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            for pa in floors[names[i]]:
                for pb in floors[names[j]]:
                    if pa.intersects(pb) and pa.intersection(pb).area > 1.0:
                        rep.fail(f"floors '{names[i]}' and '{names[j]}' overlap in plan; stack floors apart")
                        break

    # --- columns ----------------------------------------------------------
    columns = []  # (x, y, kind)
    tiny = 0
    open_cols = 0
    for e in ents("support_point"):
        kind = e.dxftype()
        if kind == "POINT":
            loc = e.dxf.location
            columns.append((loc.x * factor, loc.y * factor, "POINT"))
            continue
        if kind not in GEOMETRY_TYPES:
            continue
        poly = entity_to_polygon(e, factor)
        if column_footprint_ok(poly):
            c = poly.centroid
            columns.append((c.x, c.y, "FOOTPRINT"))
        elif poly is not None and not poly.is_empty:
            tiny += 1
        else:
            open_cols += 1
    if tiny:
        rep.warn(f"support_point: {tiny} closed shape(s) smaller than {MIN_COLUMN_FOOTPRINT_DIMENSION_FT} ft / {MIN_COLUMN_FOOTPRINT_AREA_SF} sf skipped as too small")
    if open_cols:
        rep.warn(f"support_point: {open_cols} open/unclosed entit{'y' if open_cols == 1 else 'ies'} on {layers['support_point']} (drafting errors; engine flags them)")
    if not columns and not list(ents("wall", GEOMETRY_TYPES)):
        rep.fail("no columns or walls found; nothing to carry the slab")

    col_labels = []
    for e in ents("column_label", TEXT_TYPES):
        text = extract_entity_text(e)
        if text:
            ins = e.dxf.insert
            col_labels.append((text.strip(), ins.x * factor, ins.y * factor))

    # datum
    datums = [(e.dxf.location.x * factor, e.dxf.location.y * factor) for e in ents("datum", {"POINT"})]

    # --- per-floor table --------------------------------------------------
    rows = []
    for name, polys in floors.items():
        primary = [p for p in polys if id(p) not in add_ids]
        secondary = [p for p in polys if id(p) in add_ids]
        area = sum(p.area for p in primary)
        buffered = [p.buffer(0.01) for p in polys]
        if not primary:
            rep.note(f"floor '{name}': additional-load loops only (no BOUNDARY); engine attaches it to the primary slab with the same label by DATUM, or keeps it as its own floor")

        def inside(x, y):
            return any(b.covers(Point(x, y)) for b in buffered)

        f_cols = [c for c in columns if inside(c[0], c[1])]
        f_labels = [l for l in col_labels if inside(l[1], l[2]) or min(
            (Point(l[1], l[2]).distance(p) for p in polys), default=1e9) <= LABEL_LOW_CONFIDENCE_FT]
        f_datum = [d for d in datums if inside(d[0], d[1]) or min(
            (Point(d).distance(p) for p in polys), default=1e9) <= 2.0]
        f_add = secondary
        f_walls = 0
        for e in ents("wall", GEOMETRY_TYPES):
            pts = entity_points(e)
            if pts and inside(pts[0][0] * factor, pts[0][1] * factor):
                f_walls += 1
        far = 0
        if f_cols and f_labels:
            for l in f_labels:
                d = min(math.hypot(l[1] - c[0], l[2] - c[1]) for c in f_cols)
                if d > LABEL_LOW_CONFIDENCE_FT:
                    far += 1
        dup_col = [t for t, n in Counter(l[0] for l in f_labels).items() if n > 1]
        rows.append((name, len(primary), area, len(f_cols), len(f_labels), far, f_walls, len(f_add), len(f_datum)))

        if area > IMPLAUSIBLE_AREA_SF:
            other = "in" if units == "ft" else "ft"
            rep.warn(f"floor '{name}': {area:,.0f} sf is implausible as {units}; the header may be wrong, re-run with --units {other}")
        elif area > UNIT_CONFIRM_AREA_SF:
            rep.note(f"floor '{name}': {area:,.0f} sf > {UNIT_CONFIRM_AREA_SF:,.0f} sf; app will ask to confirm units (expected for big plates)")
        if primary and area < 50:
            rep.warn(f"floor '{name}': only {area:.1f} sf; are the units right?")
        if f_cols and f_labels and len(f_labels) != len(f_cols):
            rep.warn(f"floor '{name}': {len(f_cols)} columns vs {len(f_labels)} column labels; unlabeled columns get generated names")
        if far:
            rep.warn(f"floor '{name}': {far} column label(s) more than {LABEL_LOW_CONFIDENCE_FT:.0f} ft from any column (low confidence)")
        if dup_col:
            rep.warn(f"floor '{name}': duplicate column labels {dup_col[:8]}")
        if len(f_datum) == 0:
            rep.warn(f"floor '{name}': no DATUM point; cross-floor column continuity will fall back to a guess")
        elif len(f_datum) > 1:
            rep.warn(f"floor '{name}': {len(f_datum)} DATUM points; keep exactly one")

    stray_cols = len(columns) - sum(r[3] for r in rows)
    if stray_cols > 0:
        rep.warn(f"support_point: {stray_cols} column(s) sit outside every slab polygon (opening, balcony-only, or off-floor); engine drops them")

    # --- stray layers -----------------------------------------------------
    stray = {}
    for layer, items in by_layer.items():
        if layer in known:
            continue
        kinds = Counter(e.dxftype() for e in items)
        stray[layer] = kinds
        lower = layer.lower()
        hits = [role for role, kws in ROLE_KEYWORDS.items() if any(k in lower for k in kws)]
        if hits:
            rep.warn(f"layer '{layer}' is not canonical but its name matches role(s) {hits}; the app may preselect it by mistake — rename it (e.g. BG-...)")

    # --- print ------------------------------------------------------------
    print(f"FILE   {args.dxf}")
    print(f"UNITS  {units} (header {hdr or '?'})   DXF {doc.dxfversion}")
    print(f"LAYERS " + "  ".join(f"{role}={'/'.join(layers[role])}" for role in CANONICAL_LAYERS))
    print()
    print(f"{'FLOOR':12s} {'LOOPS':>5s} {'AREA SF':>9s} {'COLS':>5s} {'LBLS':>5s} {'FAR':>4s} {'WALLS':>5s} {'ADDL':>5s} {'DATUM':>5s}")
    for name, loops, area, ncol, nlbl, far, walls, nadd, ndat in sorted(rows, key=lambda r: floor_sort_key(r[0])):
        print(f"{name[:12]:12s} {loops:5d} {area:9,.0f} {ncol:5d} {nlbl:5d} {far:4d} {walls:5d} {nadd:5d} {ndat:5d}")
    if stray:
        print()
        print("OTHER LAYERS (ignored by the engine)")
        for layer, kinds in sorted(stray.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"  {sum(kinds.values()):6d}  {layer:32s} " + " ".join(f"{k}:{v}" for k, v in kinds.most_common(3)))
    print()
    for msg in rep.fails:
        print(f"FAIL  {msg}")
    for msg in rep.warns:
        print(f"WARN  {msg}")
    for msg in rep.notes:
        print(f"NOTE  {msg}")
    verdict = "NOT READY" if rep.fails else ("READY (review warnings)" if rep.warns else "READY")
    print(f"\nRESULT {verdict}  fails={len(rep.fails)} warns={len(rep.warns)}")
    return 1 if rep.fails else 0


def floor_sort_key(name: str):
    text = name.upper()
    if "ROOF" in text:
        return (900, text)
    if "BULK" in text:
        return (950, text)
    if "PH" in text or "PENT" in text:
        return (800, text)
    m = re.search(r"-?\d+", text)
    if text.startswith("B") and m:
        return (-abs(int(m.group())), text)
    return (int(m.group()) if m else 0, text)


# ---------------------------------------------------------------------------
# floors of a formatted/working DXF (shared by stack and render)
# ---------------------------------------------------------------------------

CONTINUOUS_FT = 1.0
OFFSET_FT = 3.0


def read_floors(doc, units: str, mapping: Dict | None = None) -> List[Dict]:
    """Split a stacked DXF into floors by nearest FLOOR NUMBER label along the
    stacking axis. Returns floors sorted bottom-up by floor_sort_key."""
    factor = unit_factor(units)
    msp = doc.modelspace()
    layers = {role: role_layers(mapping, role) for role in CANONICAL_LAYERS}
    labels = []
    for e in msp:
        if e.dxf.layer in layers["floor_label"] and e.dxftype() in TEXT_TYPES:
            t = extract_entity_text(e)
            if t:
                labels.append({"label": t.strip(), "x": e.dxf.insert.x, "y": e.dxf.insert.y})
    if not labels:
        return []
    ys = sorted(l["y"] for l in labels)
    xs = sorted(l["x"] for l in labels)
    axis = "y" if (ys[-1] - ys[0]) >= (xs[-1] - xs[0]) else "x"
    floors = {l["label"]: {"label": l["label"], "label_xy": (l["x"], l["y"]), "entities": [], "columns": [], "datum": None} for l in labels}

    def nearest(p):
        return min(labels, key=lambda l: abs((p[1] if axis == "y" else p[0]) - (l["y"] if axis == "y" else l["x"])))["label"]

    for e in msp:
        kind = e.dxftype()
        pts = entity_points(e)
        if not pts:
            continue
        fl = floors[nearest(pts[0])]
        fl["entities"].append(e)
        layer = e.dxf.layer
        if layer in layers["support_point"]:
            if kind == "POINT":
                fl["columns"].append((pts[0][0] * factor, pts[0][1] * factor))
            elif kind in GEOMETRY_TYPES:
                poly = entity_to_polygon(e, factor)
                if column_footprint_ok(poly):
                    c = poly.centroid
                    fl["columns"].append((c.x, c.y))
        elif layer in layers["datum"] and kind == "POINT":
            fl["datum"] = (pts[0][0] * factor, pts[0][1] * factor)
    out = sorted(floors.values(), key=lambda f: floor_sort_key(f["label"]))
    for f in out:
        f["axis"] = axis
        f["layers"] = layers
    return out


def continuity(lower: Dict, upper: Dict) -> Dict:
    """Match each upper column to the nearest lower column after datum alignment."""
    if lower["datum"] and upper["datum"]:
        dx = lower["datum"][0] - upper["datum"][0]
        dy = lower["datum"][1] - upper["datum"][1]
        how = "datum"
    else:
        dx = (lower["label_xy"][0] - upper["label_xy"][0]) * 0  # no x shift without datum
        dy = 0.0
        how = "none"
    res = {"how": how, "continuous": 0, "offset": 0, "unsupported": [], "stops": []}
    if not lower["columns"] or not upper["columns"]:
        res["unsupported"] = list(upper["columns"])
        res["stops"] = list(lower["columns"])
        return res
    used = set()
    for ux, uy in upper["columns"]:
        ax, ay = ux + dx, uy + dy
        j, d = min(((j, math.hypot(ax - lx, ay - ly)) for j, (lx, ly) in enumerate(lower["columns"])), key=lambda t: t[1])
        if d <= CONTINUOUS_FT:
            res["continuous"] += 1
            used.add(j)
        elif d <= OFFSET_FT:
            res["offset"] += 1
            used.add(j)
        else:
            res["unsupported"].append((ux, uy))
    for j, (lx, ly) in enumerate(lower["columns"]):
        if j in used:
            continue
        ax, ay = lx - dx, ly - dy
        d = min(math.hypot(ax - ux, ay - uy) for ux, uy in upper["columns"])
        if d > OFFSET_FT:
            res["stops"].append((lx, ly))
    return res


def cmd_stack(args) -> int:
    mapping = load_map(args.map) if args.map else None
    doc = ezdxf.readfile(args.dxf)
    units = args.units or (mapping["source_units"] if mapping else None) or header_units(doc) or "in"
    floors = read_floors(doc, units, mapping)
    if len(floors) < 2:
        print("need at least two labelled floors")
        return 1
    rows = []
    print(f"{'LOWER':8s} {'UPPER':8s} {'ALIGN':6s} {'LO':>4s} {'UP':>4s} {'CONT':>5s} {'OFFS':>5s} {'NEW':>4s} {'STOP':>5s}  NEW = upper column with nothing within {OFFSET_FT:.0f} ft below; STOP = lower column with nothing above")
    for lo, up in zip(floors, floors[1:]):
        c = continuity(lo, up)
        rows.append({"lower": lo["label"], "upper": up["label"], **{k: (len(v) if isinstance(v, list) else v) for k, v in c.items()}, "unsupported_xy": c["unsupported"], "stops_xy": c["stops"]})
        print(f"{lo['label']:8s} {up['label']:8s} {c['how']:6s} {len(lo['columns']):4d} {len(up['columns']):4d} {c['continuous']:5d} {c['offset']:5d} {len(c['unsupported']):4d} {len(c['stops']):5d}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


def cmd_render(args) -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection, PolyCollection

    mapping = load_map(args.map) if args.map else None
    doc = ezdxf.readfile(args.dxf)
    units = args.units or (mapping["source_units"] if mapping else None) or header_units(doc) or "in"
    factor = unit_factor(units)
    floors = read_floors(doc, units, mapping)
    if not floors:
        print("no FLOOR NUMBER labels found")
        return 1
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cont_up = {}
    cont_down = {}
    for lo, up in zip(floors, floors[1:]):
        c = continuity(lo, up)
        cont_up[up["label"]] = c["unsupported"]
        cont_down[lo["label"]] = c["stops"]
    role_style = {
        "boundary": ("#c81e1e", 1.6), "additional_load": ("#b01ea6", 1.2), "wall": ("#0e8a9a", 1.4), "beam": ("#d9731a", 1.0),
    }
    written = []
    for fl in floors:
        segs: Dict[str, List] = defaultdict(list)
        cols = []
        texts = []
        layers = fl["layers"]
        role_of = {name: role for role, names in layers.items() for name in names}
        for e in fl["entities"]:
            layer = e.dxf.layer
            kind = e.dxftype()
            role = role_of.get(layer)
            key = role or ("hint" if layer.upper().endswith("HINT") else "bg")
            if role == "support_point" and kind in GEOMETRY_TYPES:
                poly = entity_to_polygon(e, factor)
                if column_footprint_ok(poly):
                    cols.append([(x / factor, y / factor) for x, y in poly.exterior.coords])
                    continue
            if role in {"column_label", "floor_label"} and kind in TEXT_TYPES:
                texts.append((extract_entity_text(e), e.dxf.insert.x, e.dxf.insert.y, role))
                continue
            if kind == "LINE":
                segs[key].append([(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)])
            elif kind in {"LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE"}:
                pts = flatten_vertices(e, units) if kind in {"ARC", "CIRCLE"} or has_bulge(e) else entity_points(e)
                if is_closed(e) and pts and pts[0] != pts[-1]:
                    pts = pts + [pts[0]]
                segs[key].extend([[pts[i], pts[i + 1]] for i in range(len(pts) - 1)])
        spts = [p for ring in cols for p in ring] + [p for k in role_style if k in segs for s in segs[k] for p in s]
        ext = bbox_of(spts) or bbox_of([p for k in segs for s in segs[k] for p in s])
        if ext is None:
            continue
        pad = 0.08 * max(ext[2] - ext[0], ext[3] - ext[1], 1)
        w_in = min(30, max(12, (ext[2] - ext[0]) / (ext[3] - ext[1] + 1e-9) * 12))
        fig, ax = plt.subplots(figsize=(w_in, 12), dpi=args.dpi)
        if segs["bg"]:
            ax.add_collection(LineCollection(segs["bg"], colors="#b8b8b8", linewidths=0.25))
        if segs["hint"]:
            ax.add_collection(LineCollection(segs["hint"], colors="#2d6fd6", linewidths=0.8))
        for role, (color, lw) in role_style.items():
            if segs[role]:
                ax.add_collection(LineCollection(segs[role], colors=color, linewidths=lw))
        if cols:
            ax.add_collection(PolyCollection(cols, facecolors="#f2b705", edgecolors="black", linewidths=0.6))
        for x, y in cont_up.get(fl["label"], []):
            ax.plot(x / factor, y / factor, "o", ms=14, mfc="none", mec="#c81e1e", mew=2)
        for x, y in cont_down.get(fl["label"], []):
            ax.plot(x / factor, y / factor, "s", ms=14, mfc="none", mec="#0e8a9a", mew=2)
        if fl["datum"]:
            ax.plot(fl["datum"][0] / factor, fl["datum"][1] / factor, "+", ms=18, mew=2.5, color="#c81e1e")
        for t, x, y, role in texts:
            ax.annotate(t, (x, y), fontsize=14 if role == "floor_label" else 6, color="#1a7f2e" if role == "floor_label" else "black", weight="bold" if role == "floor_label" else "normal")
        ax.set_xlim(ext[0] - pad, ext[2] + pad)
        ax.set_ylim(ext[1] - pad, ext[3] + pad)
        ax.set_aspect("equal")
        ax.set_axis_off()
        n_new = len(cont_up.get(fl["label"], []))
        n_stop = len(cont_down.get(fl["label"], []))
        ax.set_title(f"floor {fl['label']}: {len(cols)} columns; red ring = no column within {OFFSET_FT:.0f} ft on the floor below ({n_new}); teal square = no column above ({n_stop}); + = datum", fontsize=10)
        name = out_dir / f"floor_{sanitized_floor_token(fl['label'])}.png"
        fig.savefig(name, bbox_inches="tight", pad_inches=0.05)
        plt.close(fig)
        written.append(name)
        print(f"{fl['label']:8s} columns={len(cols):4d} new={n_new:3d} stops={n_stop:3d} -> {name}")
    return 0 if written else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="layer / entity / text audit of a DXF")
    p.add_argument("dxf")
    p.add_argument("--units", choices=["in", "ft"], help="override header units for the ft readouts")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("prep", help="build a clean stacked working DXF from per-floor sources")
    p.add_argument("--map", required=True, help="map JSON (layers, reference, floors, datum_xy, stack)")
    p.add_argument("--out", required=True)
    p.add_argument("dxf", nargs="*", help="per-floor source DXFs (bottom floor first); overrides map 'floors'")
    p.add_argument("--labels", help="comma list of floor labels matching positional files, e.g. '1,2-3,4-5,6'")
    p.add_argument("--units", choices=["in", "ft"])
    p.add_argument("--src", help="directory holding the source DXFs named in the map (overrides 'source_dir')")
    p.add_argument("--gap-ft", type=float, default=DEFAULT_STACK_GAP_FT)
    p.add_argument("-v", "--verbose", action="store_true", help="list dropped layers")
    p.set_defaults(func=cmd_prep)

    p = sub.add_parser("check", help="validate a formatted DXF against the input contract")
    p.add_argument("dxf")
    p.add_argument("--map", help="map JSON if the file does not use canonical layer names")
    p.add_argument("--units", choices=["in", "ft"])
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("stack", help="column continuity between adjacent floors (datum-aligned)")
    p.add_argument("dxf")
    p.add_argument("--map")
    p.add_argument("--units", choices=["in", "ft"])
    p.add_argument("--json", help="also write per-pair results with column coordinates")
    p.set_defaults(func=cmd_stack)

    p = sub.add_parser("render", help="one PNG per floor for visual review")
    p.add_argument("dxf")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--map")
    p.add_argument("--units", choices=["in", "ft"])
    p.add_argument("--dpi", type=int, default=110)
    p.set_defaults(func=cmd_render)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
