"""Turn the TakedownExport AppBundle JSON (Revit elements) into an app-ready takedown DXF.

Revit is the source of truth for element roles, so no linework guessing is needed:
- slab      = union of structural Floor top faces at the level (finish floors skipped), openings kept
- columns   = Structural Columns (Architectural Columns only when no structural column is nearby)
              that support the level (base below it, top at/above the slab underside)
- shear walls = Walls scored on structural flag/usage, concrete material, type name, thickness and
              proximity to elevator/stair/shaft markers or slab openings; every score keeps its evidence

Layers (inches, floors side by side, ascending elevation left to right):
  S-SLAB  S-SLAB-OPNG  S-BALCONY  S-COLS  S-COLS-IDEN  S-SHEARWALL  S-LEVEL-IDEN  S-DATUM
  S-GRID  S-GRID-TEXT  Z-REVIEW-LOWCONF (low-confidence walls, not mapped)  Z-REF-ELEV-STAIR
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

LAYERS = {
    "slab": ("S-SLAB", 5),
    "opening": ("S-SLAB-OPNG", 1),
    "balcony": ("S-BALCONY", 4),
    "column": ("S-COLS", 3),
    "column_label": ("S-COLS-IDEN", 7),
    "shear_wall": ("S-SHEARWALL", 6),
    "review_wall": ("Z-REVIEW-LOWCONF", 8),
    "level_label": ("S-LEVEL-IDEN", 2),
    "datum": ("S-DATUM", 4),
    "grid": ("S-GRID", 9),
    "grid_text": ("S-GRID-TEXT", 9),
    "core_marker": ("Z-REF-ELEV-STAIR", 30),
}

# Suggested app layer mapping for the DXF this module writes.
APP_LAYER_MAPPING = {
    "boundary": ["S-SLAB"],
    "additional_load": ["S-BALCONY"],
    "opening": ["S-SLAB-OPNG"],
    "wall": ["S-SHEARWALL"],
    "beam": [],
    "support_point": ["S-COLS"],
    "column_label": ["S-COLS-IDEN"],
    "floor_label": ["S-LEVEL-IDEN"],
    "datum": ["S-DATUM"],
}

FINISH_FLOOR = re.compile(
    r"FINISH|TOPPING|TILE|CARPET|WOOD|VCT|LVT|UNDERLAY|MEMBRANE|PAVER|ROOFING|INSUL|SCREED|RUBBER|TERRAZZO|VINYL|LAMINATE",
    re.I,
)
STRUCTURAL_FLOOR = re.compile(r"CONC|SLAB|STRUCT|C\.?I\.?P|\bPT\b|POST.?TENS|PLANK|DECK", re.I)
BALCONY = re.compile(r"BALCON|TERRACE", re.I)
CONCRETE = re.compile(r"CONC|CAST.?IN.?PLACE|C\.I\.P|\bCIP\b", re.I)
MASONRY = re.compile(r"CMU|MASONRY|BLOCK", re.I)
SHEAR_NAME = re.compile(r"SHEAR|CORE|STRUCT|BEARING", re.I)
PARTITION = re.compile(
    r"GYP|GWB|STUD|MTL|METAL|PARTITION|DRYWALL|FURR|CLAD|VENEER|BRICK|CURTAIN|GLAZ|STOREFRONT|PARAPET|SHAFT ?WALL|CH.?STUD",
    re.I,
)
SUPPORT_USAGE = {"Bearing", "Shear", "Combined", "StructuralCombined"}

LEVEL_MATCH_FT = 1.0
COLUMN_TOP_TOLERANCE_FT = 1.5
CORE_PROXIMITY_FT = 1.5
MIN_OPENING_SF = 1.0
FLOOR_GAP_FT = 60.0
TEXT_HEIGHT_IN = 18.0


@dataclass
class ColumnOut:
    label: str
    label_source: str
    xy: tuple
    footprint: Polygon
    size: str
    category: str
    type_name: str
    source: str
    element_id: int


@dataclass
class WallOut:
    line: LineString
    width_ft: float
    score: int
    confidence: str
    evidence: List[str]
    type_name: str
    source: str
    element_id: int


@dataclass
class LevelOut:
    name: str
    elevation: float
    slab: Polygon | MultiPolygon | None = None
    balcony: Polygon | MultiPolygon | None = None
    floors_used: List[dict] = field(default_factory=list)
    floors_skipped: List[dict] = field(default_factory=list)
    dropped_holes: int = 0
    columns: List[ColumnOut] = field(default_factory=list)
    columns_outside_slab: List[str] = field(default_factory=list)
    walls: List[WallOut] = field(default_factory=list)
    core_markers: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def openings(self) -> List[Polygon]:
        return [Polygon(ring) for poly in _polys(self.slab) for ring in poly.interiors]


def load(source: str | Path | dict) -> dict:
    if isinstance(source, dict):
        return source
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    if data.get("schema") != "takedown-revit/1":
        raise ValueError(f"Unexpected schema {data.get('schema')!r}; expected takedown-revit/1")
    return data


def _polys(geom) -> List[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]


def _loops_to_geometry(loops: Sequence[Sequence[Sequence[float]]]):
    rings = [Polygon(loop).buffer(0) for loop in loops if len(loop) >= 3]
    rings = [r for r in rings if not r.is_empty and r.area > 1e-4]
    if not rings:
        return None
    result = None
    for ring in sorted(rings, key=lambda r: r.area, reverse=True):
        depth = sum(1 for other in rings if other is not ring and other.area > ring.area and other.contains(ring.representative_point()))
        if result is None:
            result = ring
        elif depth % 2:
            result = result.difference(ring)
        else:
            result = result.union(ring)
    return result.buffer(0)


def _floor_kind(floor: dict) -> tuple[str, List[str]]:
    name = f"{floor.get('familyName') or ''} {floor.get('typeName') or ''}"
    materials = " ".join(f"{m.get('name') or ''} {m.get('materialClass') or ''}" for m in floor.get("materials", []))
    thickness_in = float(floor.get("thickness") or 0.0) * 12.0
    evidence = [f"type '{floor.get('typeName')}'", f'{thickness_in:.1f}" thick']
    if floor.get("isStructural"):
        evidence.append("Structural checkbox on")
    if BALCONY.search(name):
        return "balcony", evidence + ["balcony/terrace in type name"]
    if FINISH_FLOOR.search(name) or thickness_in < 3.0:
        return "finish", evidence + ["finish/thin floor"]
    if floor.get("isStructural") or STRUCTURAL_FLOOR.search(name) or CONCRETE.search(materials):
        return "structural", evidence
    return "unknown", evidence


def _column_polygon(column: dict) -> tuple[Polygon, str]:
    footprint = column.get("footprint")
    if footprint and len(footprint) >= 3:
        poly = Polygon(footprint).buffer(0)
        if not poly.is_empty and poly.area > 0.05:
            return poly, "solid"
    x, y = column["xy"]
    return box(x - 0.5, y - 0.5, x + 0.5, y + 0.5), "assumed 12x12 (no footprint)"


def _size_label(poly: Polygon) -> str:
    rect = poly.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    sides = sorted(math.dist(coords[i], coords[i + 1]) * 12 for i in range(2))
    if poly.area > 0 and abs(poly.area / max(rect.area, 1e-9) - math.pi / 4) < 0.05:
        return f"Ø{sides[1]:.0f}"
    return f"{sides[0]:.0f}x{sides[1]:.0f}"


def _clean_grid_mark(mark: str | None) -> str | None:
    if not mark:
        return None
    cleaned = re.sub(r"\([^)]*\)", "", mark).strip(" -")
    return cleaned or None


def _nearest_grid_label(xy, grids: List[dict]) -> Optional[str]:
    lines = [(g["name"], LineString(g["points"])) for g in grids if len(g.get("points", [])) >= 2]
    if len(lines) < 2:
        return None
    point = Point(xy)
    ranked = sorted(lines, key=lambda item: item[1].distance(point))
    first = ranked[0]
    for name, line in ranked[1:]:
        a = first[1].coords
        b = line.coords
        va = (a[-1][0] - a[0][0], a[-1][1] - a[0][1])
        vb = (b[-1][0] - b[0][0], b[-1][1] - b[0][1])
        cross = abs(va[0] * vb[1] - va[1] * vb[0]) / (math.hypot(*va) * math.hypot(*vb) or 1)
        if cross > 0.5 and first[1].distance(point) < 3 and line.distance(point) < 3:
            names = sorted([first[0], name], key=lambda n: (not n[:1].isalpha(), n))
            return "-".join(names)
    return None


def _spans(item: dict, elevation: float, below: float, above: float) -> bool:
    return float(item.get("baseElevation", 0)) < elevation - below and float(item.get("topElevation", 0)) >= elevation - above


def score_wall(wall: dict, core_zones: List[Polygon], openings: List[Polygon]) -> tuple[int, List[str]]:
    name = f"{wall.get('familyName') or ''} {wall.get('typeName') or ''}"
    layer_materials = [m for m in wall.get("materials", []) if m]
    structural_layers = [m for m in layer_materials if m.get("function") == "Structure"] or layer_materials
    material_text = " ".join(
        f"{m.get('name') or ''} {m.get('materialClass') or ''}" for m in structural_layers
    ) + f" {wall.get('structuralMaterial') or ''} {wall.get('structuralMaterialClass') or ''}"
    width_in = float(wall.get("width") or 0.0) * 12.0
    score, evidence = 0, []

    if wall.get("structuralFlag") or wall.get("structuralUsage") in SUPPORT_USAGE:
        score += 3
        evidence.append(f"structural (flag={bool(wall.get('structuralFlag'))}, usage={wall.get('structuralUsage')})")
    if MASONRY.search(material_text) or MASONRY.search(name):
        score += 1
        evidence.append("masonry (CMU) material/type")
    elif CONCRETE.search(material_text) or CONCRETE.search(name):
        score += 3
        evidence.append("concrete material/type")
    if SHEAR_NAME.search(name):
        score += 2
        evidence.append("shear/core/structural in type name")
    if width_in >= 8.0:
        score += 1
        evidence.append(f'{width_in:.1f}" thick')
    elif width_in < 5.0:
        score -= 2
        evidence.append(f'thin ({width_in:.1f}")')
    line = LineString(wall["curve"])
    near = [z for z in core_zones + openings if z.distance(line) <= CORE_PROXIMITY_FT]
    if near:
        score += 2
        evidence.append("adjacent to elevator/stair/shaft or slab opening")
    if PARTITION.search(name):
        score -= 3
        evidence.append("partition/cladding keyword in type name")
    if wall.get("function") == "Exterior" and "concrete material/type" not in evidence:
        score -= 1
        evidence.append("exterior, non-concrete")
    return score, evidence


def _confidence(score: int) -> Optional[str]:
    if score >= 7:
        return "high"
    if score >= 5:
        return "medium"
    if score >= 3:
        return "low"
    return None


def build_levels(data: dict, level_names: Optional[Iterable[str]] = None) -> List[LevelOut]:
    wanted = {n.strip().lower() for n in level_names} if level_names else None
    levels = sorted(data.get("levels", []), key=lambda l: l["elevation"])
    grids = data.get("grids", [])
    results = []
    for index, level in enumerate(levels):
        if wanted and level["name"].strip().lower() not in wanted:
            continue
        elevation = float(level["elevation"])
        below_elevation = float(levels[index - 1]["elevation"]) if index > 0 else elevation - 12.0
        out = LevelOut(name=level["name"], elevation=elevation)

        candidates = [
            f for f in data.get("floors", [])
            if f.get("levelName") == level["name"] or abs(float(f.get("topElevation", 1e9)) - elevation) <= LEVEL_MATCH_FT
        ]
        classified = [(f, *_floor_kind(f)) for f in candidates]
        structural = [c for c in classified if c[1] == "structural"]
        chosen = structural or [c for c in classified if c[1] == "unknown"]
        if not structural and chosen:
            out.warnings.append("No floor typed/flagged structural at this level; used non-finish floors (check types).")
        slab_parts, balcony_parts = [], []
        for floor, kind, evidence in classified:
            record = {"id": floor.get("id"), "type": floor.get("typeName"), "source": floor.get("source"), "evidence": evidence}
            geom = _loops_to_geometry(floor.get("loops", []))
            if geom is None:
                out.floors_skipped.append({**record, "reason": "no top-face loops"})
                continue
            if kind == "balcony":
                balcony_parts.append(geom)
                out.floors_used.append({**record, "role": "balcony", "area_sf": round(geom.area, 1)})
            elif any(floor is c[0] for c in chosen):
                slab_parts.append(geom)
                out.floors_used.append({**record, "role": "slab", "area_sf": round(geom.area, 1)})
            else:
                out.floors_skipped.append({**record, "reason": kind})
        if not slab_parts:
            if candidates:
                out.warnings.append("Floors found at this level but none usable as slab.")
            results.append(out)
            continue
        slab = unary_union(slab_parts).buffer(0)

        column_records = []
        for column in data.get("columns", []):
            if not _spans(column, elevation, below=LEVEL_MATCH_FT, above=COLUMN_TOP_TOLERANCE_FT):
                continue
            poly, footprint_source = _column_polygon(column)
            column_records.append((column, poly, footprint_source))
        structural_polys = [p for c, p, _ in column_records if c.get("category") == "structural"]
        kept = []
        for column, poly, footprint_source in column_records:
            if column.get("category") != "structural" and any(s.distance(poly) < 1.0 for s in structural_polys):
                continue
            kept.append((column, poly, footprint_source))
        if kept and all(c.get("category") != "structural" for c, _, _ in kept):
            out.warnings.append("No Structural Columns at this level; used Architectural Columns (verify they are structural).")

        column_polys = [p for _, p, _ in kept]
        # Column/floor joins cut notches and holes into the slab face; tributary area includes the column.
        touching = [p for p in column_polys if p.distance(slab) < 0.05]
        if touching:
            slab = unary_union([slab, *touching]).buffer(0.05, join_style=2).buffer(-0.05, join_style=2)
        shells, dropped = [], 0
        for part in _polys(slab):
            holes = []
            for ring in part.interiors:
                hole = Polygon(ring)
                if hole.area < MIN_OPENING_SF or any(
                    hole.representative_point().distance(cp) < 0.5 and hole.area <= cp.area * 1.5 for cp in column_polys
                ):
                    dropped += 1
                    continue
                holes.append(ring)
            shells.append(Polygon(part.exterior, holes))
        for shaft in data.get("openings", []):
            if shaft.get("loop") and _spans(shaft, elevation, below=-1.0, above=2.0):
                shaft_poly = Polygon(shaft["loop"]).buffer(0)
                shells = [s.difference(shaft_poly) if s.contains(shaft_poly.representative_point()) else s for s in shells]
        out.slab = unary_union(shells).buffer(0)
        out.dropped_holes = dropped
        out.balcony = unary_union(balcony_parts).buffer(0) if balcony_parts else None

        load_area = out.slab.union(out.balcony) if out.balcony is not None else out.slab
        used_labels: Dict[str, int] = {}
        for n, (column, poly, footprint_source) in enumerate(sorted(kept, key=lambda k: (-k[0]["xy"][1], k[0]["xy"][0])), 1):
            label, label_source = _clean_grid_mark(column.get("gridMark")), "column location mark"
            if not label and column.get("mark"):
                label, label_source = column["mark"].strip(), "mark"
            if not label:
                label, label_source = _nearest_grid_label(column["xy"], grids), "nearest grids"
            if not label:
                label, label_source = f"C{n}", "sequential"
            used_labels[label] = used_labels.get(label, 0) + 1
            if used_labels[label] > 1:
                label = f"{label}.{used_labels[label]}"
            record = ColumnOut(
                label=label, label_source=label_source, xy=tuple(poly.centroid.coords[0]), footprint=poly,
                size=_size_label(poly), category=column.get("category", "structural"),
                type_name=f"{column.get('familyName') or ''}: {column.get('typeName') or ''}".strip(": "),
                source=column.get("source", "host"), element_id=column.get("id", -1),
            )
            if not load_area.buffer(1.0).covers(poly.centroid):
                out.columns_outside_slab.append(label)
                continue
            if footprint_source != "solid":
                out.warnings.append(f"Column {label}: {footprint_source}.")
            out.columns.append(record)

        story_zone = (below_elevation - 1.0, elevation + 1.0)
        core_zones = []
        for marker in data.get("cores", []) + [dict(o, kind="shaft", name="shaft opening") for o in data.get("openings", [])]:
            bbox = marker.get("bbox")
            if not bbox:
                continue
            if float(marker.get("topElevation", 0)) < story_zone[0] or float(marker.get("baseElevation", 0)) > story_zone[1]:
                continue
            core_zones.append(box(*bbox))
            out.core_markers.append({"kind": marker.get("kind"), "name": marker.get("name"), "bbox": [round(v, 2) for v in bbox]})
        for wall in data.get("walls", []):
            if len(wall.get("curve", [])) < 2 or not _spans(wall, elevation, below=0.5, above=COLUMN_TOP_TOLERANCE_FT):
                continue
            line = LineString(wall["curve"])
            if not load_area.buffer(2.0).intersects(line):
                continue
            score, evidence = score_wall(wall, core_zones, out.openings)
            confidence = _confidence(score)
            if confidence is None:
                continue
            if confidence == "high" and "concrete material/type" not in evidence:
                confidence = "medium"
            out.walls.append(WallOut(
                line=line, width_ft=float(wall.get("width") or 0.0), score=score, confidence=confidence,
                evidence=evidence, type_name=wall.get("typeName") or "", source=wall.get("source", "host"),
                element_id=wall.get("id", -1),
            ))
        if not any(w.confidence in {"high", "medium"} for w in out.walls):
            out.warnings.append(
                "Shear walls ambiguous: no wall reached medium confidence (no structural/concrete walls near cores). "
                "Tributary areas will go to columns only; review Z-REVIEW-LOWCONF."
            )
        results.append(out)
    return results


def core_groups(level: LevelOut) -> List[dict]:
    walls = [w for w in level.walls if w.confidence in {"high", "medium"}]
    if not walls:
        return []
    merged = unary_union([w.line.buffer(2.0) for w in walls])
    groups = []
    for zone in _polys(merged):
        members = [w for w in walls if zone.intersects(w.line)]
        markers = [m["name"] for m in level.core_markers if zone.buffer(3.0).intersects(box(*m["bbox"]))]
        minx, miny, maxx, maxy = zone.bounds
        groups.append({
            "walls": len(members),
            "length_ft": round(sum(w.line.length for w in members), 1),
            "bbox_ft": [round(v, 1) for v in (minx + 2, miny + 2, maxx - 2, maxy - 2)],
            "confidence": "high" if all(w.confidence == "high" for w in members) else "medium",
            "near": sorted(set(n for n in markers if n)),
        })
    return groups


def write_dxf(levels: List[LevelOut], out_path: str | Path, grids: Sequence[dict] = ()) -> Path:
    import ezdxf
    from ezdxf import units

    usable = [lvl for lvl in levels if lvl.slab is not None and not lvl.slab.is_empty]
    if not usable:
        raise ValueError("No level produced a slab; nothing to write.")
    extents = unary_union([lvl.slab if lvl.balcony is None else lvl.slab.union(lvl.balcony) for lvl in usable]).bounds
    gminx, gminy, gmaxx, gmaxy = extents
    width = gmaxx - gminx

    doc = ezdxf.new("R2018", setup=True)
    doc.units = units.IN
    doc.header["$INSUNITS"] = 1
    for layer, color in LAYERS.values():
        doc.layers.add(layer, color=color)
    msp = doc.modelspace()

    for index, level in enumerate(usable):
        dx = index * (width + FLOOR_GAP_FT) - gminx
        dy = -gminy

        def tf(pt):
            return ((pt[0] + dx) * 12.0, (pt[1] + dy) * 12.0)

        def ring(coords, layer):
            pts = [tf(p) for p in list(coords)[:-1]]
            if len(pts) >= 3:
                msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})

        for poly in _polys(level.slab):
            ring(poly.exterior.coords, LAYERS["slab"][0])
            for hole in poly.interiors:
                ring(hole.coords, LAYERS["opening"][0])
        for poly in _polys(level.balcony):
            ring(poly.exterior.coords, LAYERS["balcony"][0])
        for column in level.columns:
            ring(column.footprint.exterior.coords, LAYERS["column"][0])
            minx, miny, maxx, maxy = column.footprint.bounds
            msp.add_text(
                column.label, height=TEXT_HEIGHT_IN * 0.6,
                dxfattribs={"layer": LAYERS["column_label"][0], "insert": tf((column.xy[0], maxy + 0.75))},
            )
        for wall in level.walls:
            layer = LAYERS["shear_wall"][0] if wall.confidence in {"high", "medium"} else LAYERS["review_wall"][0]
            msp.add_lwpolyline([tf(p) for p in wall.line.coords], dxfattribs={"layer": layer})
        for marker in level.core_markers:
            x0, y0, x1, y1 = marker["bbox"]
            ring([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], LAYERS["core_marker"][0])
        for grid in grids:
            if len(grid.get("points", [])) >= 2:
                msp.add_lwpolyline([tf(p) for p in grid["points"]], dxfattribs={"layer": LAYERS["grid"][0]})
                msp.add_text(grid["name"], height=TEXT_HEIGHT_IN,
                             dxfattribs={"layer": LAYERS["grid_text"][0], "insert": tf(grid["points"][0])})
        label_point = level.slab.representative_point()
        msp.add_text(level.name, height=TEXT_HEIGHT_IN * 2,
                     dxfattribs={"layer": LAYERS["level_label"][0], "insert": tf((label_point.x, label_point.y))})
        msp.add_point(tf((gminx, gminy)), dxfattribs={"layer": LAYERS["datum"][0]})

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out_path)
    return out_path


def report(levels: List[LevelOut], data: dict) -> dict:
    links = data.get("links", [])
    out = {
        "document": data.get("document", {}),
        "links": links,
        "exporter_warnings": data.get("warnings", []),
        "levels": [],
    }
    for level in levels:
        walls_by_conf: Dict[str, int] = {}
        for wall in level.walls:
            walls_by_conf[wall.confidence] = walls_by_conf.get(wall.confidence, 0) + 1
        sizes: Dict[str, int] = {}
        for column in level.columns:
            sizes[column.size] = sizes.get(column.size, 0) + 1
        out["levels"].append({
            "level": level.name,
            "elevation_ft": round(level.elevation, 3),
            "slab_area_sf": round(level.slab.area, 1) if level.slab is not None else 0.0,
            "balcony_area_sf": round(level.balcony.area, 1) if level.balcony is not None else 0.0,
            "openings": [round(o.area, 1) for o in level.openings],
            "column_join_holes_dropped": level.dropped_holes,
            "floors_used": level.floors_used,
            "floors_skipped": level.floors_skipped,
            "columns": len(level.columns),
            "column_sizes": sizes,
            "column_label_sources": sorted({c.label_source for c in level.columns}),
            "columns_outside_slab": level.columns_outside_slab,
            "shear_walls_by_confidence": walls_by_conf,
            "cores": core_groups(level),
            "walls": [
                {"id": w.element_id, "type": w.type_name, "confidence": w.confidence, "score": w.score,
                 "length_ft": round(w.line.length, 1), "width_in": round(w.width_ft * 12, 1), "evidence": w.evidence,
                 "source": w.source}
                for w in sorted(level.walls, key=lambda w: -w.score)
            ],
            "core_markers": level.core_markers,
            "warnings": level.warnings,
        })
    return out


def convert(json_source: str | Path | dict, out_dxf: str | Path, level_names: Optional[Iterable[str]] = None) -> dict:
    data = load(json_source)
    levels = build_levels(data, level_names)
    path = write_dxf(levels, out_dxf, data.get("grids", []))
    result = report(levels, data)
    result["dxf"] = str(path)
    result["app_layer_mapping"] = APP_LAYER_MAPPING
    return result
