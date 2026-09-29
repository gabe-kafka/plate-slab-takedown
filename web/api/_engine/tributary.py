from pathlib import Path
import multiprocessing
import math
import re
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd
from shapely.affinity import translate
from shapely.geometry import Polygon, Point, LineString
from shapely.ops import polygonize, unary_union
import ezdxf
from ezdxf import colors

from geometry_export import export_geometry_json
from export_column_loads import export_column_load_takedown, compute_floor_datums
from fascade_utils import (
    compute_fascade_assignments,
    FASCADE_DISTANCE_THRESHOLD,
    FASCADE_SAMPLE_SPACING,
)
from geometry_utils import (
    EDGE_TOLERANCE_FEET,
    entity_to_lines,
    line_intersects,
    load_job_config,
    nearest_label,
    point_is_inside,
    sanitized_floor_token,
    unit_factor,
)
from tributary_solver import WALL_SUPPORT_SPACING_FEET, sample_wall_support_points, solve_floor_tributary


class NeedsReviewError(RuntimeError):
    pass


AREA_TOLERANCE_SF = 1e-4
DISPLAY_LINEWORK_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE"}
BOUNDARY_CONNECT_TOLERANCE_FEET = 0.05
PRIMARY_LOAD_LAYER = "BOUNDARY"


def collect_display_linework_entities(modelspace, allowed_layers):
    """Collect visible linework from selected layers, including block contents."""
    layer_set = set(allowed_layers)
    if not layer_set:
        return []

    entities = []
    for entity in modelspace:
        if not hasattr(entity.dxf, "layer"):
            continue

        layer = entity.dxf.layer
        kind = entity.dxftype()
        if layer in layer_set:
            if kind in DISPLAY_LINEWORK_TYPES:
                entities.append((entity, layer))
            elif kind == "INSERT":
                try:
                    for child in entity.virtual_entities():
                        if child.dxftype() in DISPLAY_LINEWORK_TYPES:
                            entities.append((child, layer))
                except Exception as exc:
                    print(
                        f"Warning: Could not explode block '{entity.dxf.name}' "
                        f"on selected layer: {exc}"
                    )
            continue

        if kind == "INSERT":
            try:
                for child in entity.virtual_entities():
                    child_layer = getattr(child.dxf, "layer", "")
                    if child_layer in layer_set and child.dxftype() in DISPLAY_LINEWORK_TYPES:
                        entities.append((child, child_layer))
            except Exception:
                pass

    return entities


def polygon_parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type == "MultiPolygon":
        return [part for part in geometry.geoms if not part.is_empty]
    return []


def boundary_surface_groups(boundary_surfaces):
    """Group load-zone polygons that touch or slightly overlap into one slab domain."""
    groups = []

    for record in boundary_surfaces:
        polygon = record["polygon"].buffer(0)
        if polygon.is_empty or polygon.area <= AREA_TOLERANCE_SF:
            continue

        touching_groups = []
        for index, group in enumerate(groups):
            if group["union"].distance(polygon) <= BOUNDARY_CONNECT_TOLERANCE_FEET:
                touching_groups.append(index)

        if not touching_groups:
            groups.append(
                {
                    "surfaces": [record],
                    "union": polygon,
                }
            )
            continue

        target_index = touching_groups[0]
        target = groups[target_index]
        target["surfaces"].append(record)
        target["union"] = target["union"].union(polygon).buffer(0)

        for merge_index in reversed(touching_groups[1:]):
            merged = groups.pop(merge_index)
            target["surfaces"].extend(merged["surfaces"])
            target["union"] = target["union"].union(merged["union"]).buffer(0)

    return groups


def load_zones_from_boundary_surfaces(boundary_surfaces):
    zones_by_layer = {}
    for surface in boundary_surfaces:
        layer = str(surface.get("load_layer") or "BOUNDARY")
        polygon = surface.get("polygon")
        if polygon is None or polygon.is_empty:
            continue
        zones_by_layer.setdefault(layer, []).append(polygon.buffer(0))

    load_zones = []
    for layer, polygons in zones_by_layer.items():
        merged = unary_union(polygons).buffer(0)
        if not merged.is_empty and merged.area > AREA_TOLERANCE_SF:
            load_zones.append({"layer": layer, "polygon": merged})

    return load_zones


def load_zones_from_floor_plans(floor_plans_for_group):
    zones_by_layer = {}

    for floor_plan in floor_plans_for_group:
        source_zones = floor_plan.get("load_zones") or [
            {
                "layer": floor_plan.get("load_layer", "BOUNDARY"),
                "polygon": floor_plan["slab_polygon"],
            }
        ]
        for zone in source_zones:
            layer = str(zone.get("layer") or "BOUNDARY")
            polygon = zone.get("polygon")
            if polygon is None or polygon.is_empty:
                continue
            zones_by_layer.setdefault(layer, []).append(polygon.buffer(0))

    load_zones = []
    for layer, polygons in zones_by_layer.items():
        merged = unary_union(polygons).buffer(0)
        if not merged.is_empty and merged.area > AREA_TOLERANCE_SF:
            load_zones.append({"layer": layer, "polygon": merged})

    return load_zones


def floor_plan_has_primary_boundary(floor_plan):
    return any(
        str(zone.get("layer") or PRIMARY_LOAD_LAYER) == PRIMARY_LOAD_LAYER
        for zone in floor_plan.get("load_zones", [])
    )


def assign_user_datums_to_floor_plans(floor_plans, datum_points):
    if not datum_points:
        return

    for fp in floor_plans:
        slab = fp.get('slab_polygon')
        if slab is None or slab.is_empty:
            continue
        matching_datums = [
            dp
            for dp in datum_points
            if slab.covers(dp) or slab.distance(dp) <= EDGE_TOLERANCE_FEET
        ]
        if matching_datums:
            dp = min(matching_datums, key=lambda point: slab.distance(point))
            fp['user_datum'] = (dp.x, dp.y)


def attach_secondary_only_floor_plans(floor_plans):
    """Attach detached additional-load-only drawings to matching primary slabs.

    Some source DXFs draft a same-floor secondary load plan as its own drawing
    off to the side. It has a floor label and DATUM, but no primary slab layer.
    Treating it as a floor creates an orphaned roof/bulkhead plate. When a
    matching primary slab exists, align by DATUM and merge only the load zone.
    """
    primary_by_floor = {}
    secondary_only = []

    for floor_plan in floor_plans:
        floor_number = floor_plan.get('floor_number')
        if not floor_number:
            continue

        if floor_plan_has_primary_boundary(floor_plan):
            primary_by_floor.setdefault(floor_number, []).append(floor_plan)
        else:
            secondary_only.append(floor_plan)

    attached_ids = set()
    attached_source_polygons = []

    for secondary in secondary_only:
        candidates = primary_by_floor.get(secondary.get('floor_number'), [])
        if not candidates:
            continue

        source_datum = secondary.get('user_datum')
        if source_datum is None:
            print(
                f"  WARNING: Additional-load-only floor '{secondary.get('floor_number')}' "
                "has no DATUM; leaving it as a separate floor."
            )
            continue

        candidates_with_datum = [
            candidate for candidate in candidates if candidate.get('user_datum') is not None
        ]
        if not candidates_with_datum:
            print(
                f"  WARNING: Primary floor '{secondary.get('floor_number')}' has no DATUM; "
                "leaving detached additional load as a separate floor."
            )
            continue

        target = min(
            candidates_with_datum,
            key=lambda candidate: candidate['slab_polygon'].distance(
                Point(source_datum[0], source_datum[1])
            ),
        )
        target_datum = target['user_datum']
        dx = target_datum[0] - source_datum[0]
        dy = target_datum[1] - source_datum[1]

        translated_zones = []
        for zone in secondary.get('load_zones', []):
            polygon = zone.get('polygon')
            if polygon is None or polygon.is_empty:
                continue
            translated_zones.append(
                {
                    "layer": str(zone.get("layer") or PRIMARY_LOAD_LAYER),
                    "polygon": translate(polygon, xoff=dx, yoff=dy).buffer(0),
                }
            )

        if not translated_zones:
            continue

        target["load_zones"] = load_zones_from_floor_plans(
            [target, {"load_zones": translated_zones}]
        )
        target["slab_polygon"] = unary_union(
            [target["slab_polygon"]]
            + [zone["polygon"] for zone in translated_zones]
        ).buffer(0)

        attached_ids.add(id(secondary))
        attached_source_polygons.append(secondary["slab_polygon"])
        print(
            f"  Attached additional-load-only '{secondary.get('floor_number')}' "
            f"to primary slab by DATUM offset ({dx:.1f}, {dy:.1f}) ft"
        )

    if not attached_ids:
        return floor_plans, []

    return (
        [floor_plan for floor_plan in floor_plans if id(floor_plan) not in attached_ids],
        attached_source_polygons,
    )


def geometry_intersects_any(geometry, polygons):
    if geometry is None or getattr(geometry, "is_empty", True):
        return False
    return any(polygon.intersects(geometry) for polygon in polygons)


def calculate_load_zone_areas(region, load_zones):
    if region is None or region.is_empty or not load_zones:
        return []

    areas_by_layer = {}
    for zone in load_zones:
        zone_polygon = zone.get("polygon")
        if zone_polygon is None or zone_polygon.is_empty:
            continue

        intersection = region.intersection(zone_polygon)
        area = intersection.area if not intersection.is_empty else 0.0
        if area > AREA_TOLERANCE_SF:
            layer = str(zone.get("layer") or "BOUNDARY")
            areas_by_layer[layer] = areas_by_layer.get(layer, 0.0) + area

    return [
        {"layer": layer, "area": area}
        for layer, area in sorted(areas_by_layer.items(), key=lambda item: item[0])
    ]


def assign_load_zone_areas(floor_plan):
    load_zones = floor_plan.get("load_zones", [])
    floor_plan["column_load_areas"] = []

    for region in floor_plan.get("regions", []):
        floor_plan["column_load_areas"].append(
            calculate_load_zone_areas(region, load_zones)
        )

    for wall_data in floor_plan.get("walls", []):
        wall_data["load_areas"] = calculate_load_zone_areas(
            wall_data.get("merged_region"),
            load_zones,
        )


def load_column_footprints(path="dxf_column_footprints.csv"):
    footprint_path = Path(path)
    if not footprint_path.exists():
        return {}

    try:
        footprints_df = pd.read_csv(footprint_path)
    except Exception as exc:
        print(f"WARNING: Could not load column footprints: {exc}")
        return {}

    required_columns = {"footprint_id", "ring_role", "vertex_index", "x", "y"}
    if footprints_df.empty or not required_columns.issubset(footprints_df.columns):
        return {}

    footprints = {}
    for footprint_id in footprints_df["footprint_id"].dropna().unique():
        footprint_rows = footprints_df[footprints_df["footprint_id"] == footprint_id]
        shell_rows = footprint_rows[footprint_rows["ring_role"] == "shell"].sort_values("vertex_index")
        shell = [(row["x"], row["y"]) for _, row in shell_rows.iterrows()]
        if len(shell) < 3:
            continue
        if shell[0] != shell[-1]:
            shell.append(shell[0])

        holes = []
        hole_roles = [
            role
            for role in footprint_rows["ring_role"].dropna().unique()
            if str(role).startswith("hole")
        ]
        for role in hole_roles:
            hole_rows = footprint_rows[footprint_rows["ring_role"] == role].sort_values("vertex_index")
            hole = [(row["x"], row["y"]) for _, row in hole_rows.iterrows()]
            if len(hole) < 3:
                continue
            if hole[0] != hole[-1]:
                hole.append(hole[0])
            holes.append(hole)

        polygon = Polygon(shell, holes).buffer(0)
        if not polygon.is_empty and polygon.area > AREA_TOLERANCE_SF:
            footprints[str(footprint_id)] = polygon

    return footprints

# --- Label-Point Association Function ---
def associate_labels_with_points(
    column_points,
    column_labels,
    max_distance=10.0,
    auto_expand=True,
    auto_expand_limit=35.0,
    threshold_step=5.0,
):
    """
    Associate column labels with their nearest column points.
    
    Args:
        column_points: List of shapely.geometry.Point objects
        column_labels: List of dicts with keys: 'label', 'x', 'y'
        max_distance: Maximum distance (in feet) for valid association
        auto_expand: Whether to increase the search radius when some labels remain unmatched
        auto_expand_limit: Upper bound for auto-expanded search radius
        threshold_step: Minimum increment when expanding the search radius
        
    Returns:
        dict with keys:
            'associations': Dict mapping point_index to label
            'unlabeled_points': List of point indices without labels
            'orphaned_labels': List of label dicts that couldn't be associated
            'summary': Dict with statistics
    """
    num_points = len(column_points)
    num_labels = len(column_labels)
    
    if num_points == 0 or num_labels == 0:
        unlabeled_points = list(range(num_points))
        orphaned_labels = [dict(label_dict) for label_dict in column_labels]
        summary = {
            'total_labels': num_labels,
            'associated_count': 0,
            'unlabeled_count': len(unlabeled_points),
            'orphaned_count': num_labels,
            'average_distance': 0.0,
            'max_association_distance': 0.0,
            'threshold_used': max_distance
        }
        return {
            'associations': {},
            'unlabeled_points': unlabeled_points,
            'orphaned_labels': orphaned_labels,
            'summary': summary
        }
    
    # Precompute distances between every label and column point
    point_coords = [(pt.x, pt.y) for pt in column_points]
    candidate_pairs = []
    label_nearest_distances = [float('inf')] * num_labels
    
    for label_idx, label_dict in enumerate(column_labels):
        lx = label_dict['x']
        ly = label_dict['y']
        for point_idx, (px, py) in enumerate(point_coords):
            distance = math.hypot(lx - px, ly - py)
            candidate_pairs.append((distance, label_idx, point_idx))
            if distance < label_nearest_distances[label_idx]:
                label_nearest_distances[label_idx] = distance
    
    # Sort once so we can reuse for different thresholds
    candidate_pairs.sort(key=lambda item: item[0])
    
    def run_assignment(distance_limit):
        """Assign labels to points up to a distance limit."""
        associations = {}
        association_distances = {}
        used_points = set()
        used_labels = set()
        assigned_distances = []
        
        for distance, label_idx, point_idx in candidate_pairs:
            if distance > distance_limit:
                break
            if point_idx in used_points or label_idx in used_labels:
                continue
            
            label_entry = column_labels[label_idx]
            canonical_label = label_entry.get('label', '')
            raw_label = label_entry.get('raw_label', '')
            assigned_label = canonical_label or raw_label or f"LABEL_{label_idx}"
            associations[point_idx] = assigned_label
            association_distances[point_idx] = distance
            used_points.add(point_idx)
            used_labels.add(label_idx)
            assigned_distances.append(distance)
        
        unlabeled_points = [idx for idx in range(num_points) if idx not in associations]
        
        orphaned_labels = []
        for label_idx, label_dict in enumerate(column_labels):
            if label_idx not in used_labels:
                label_copy = dict(label_dict)
                nearest = label_nearest_distances[label_idx]
                if not math.isinf(nearest):
                    label_copy['nearest_distance'] = nearest
                orphaned_labels.append(label_copy)
        
        avg_distance = sum(assigned_distances) / len(assigned_distances) if assigned_distances else 0.0
        max_association_distance = max(assigned_distances) if assigned_distances else 0.0
        
        summary = {
            'total_labels': num_labels,
            'associated_count': len(associations),
            'unlabeled_count': len(unlabeled_points),
            'orphaned_count': len(orphaned_labels),
            'average_distance': avg_distance,
            'max_association_distance': max_association_distance,
            'threshold_used': distance_limit
        }
        
        return {
            'associations': associations,
            'association_distances': association_distances,
            'unlabeled_points': unlabeled_points,
            'orphaned_labels': orphaned_labels,
            'summary': summary
        }, used_labels
    
    current_threshold = max_distance
    best_result, best_used_labels = run_assignment(current_threshold)
    
    if auto_expand and num_points > 0 and best_result['orphaned_labels']:
        while best_result['orphaned_labels'] and current_threshold < auto_expand_limit:
            remaining_indices = [idx for idx in range(num_labels) if idx not in best_used_labels]
            candidate_distances = [
                label_nearest_distances[idx]
                for idx in remaining_indices
                if not math.isinf(label_nearest_distances[idx])
            ]
            
            if not candidate_distances:
                break
            
            min_needed = min(candidate_distances)
            proposed_threshold = max(current_threshold + threshold_step, min_needed + 0.5)
            next_threshold = min(auto_expand_limit, proposed_threshold)
            
            if next_threshold <= current_threshold + 1e-6:
                break
            
            current_threshold = next_threshold
            best_result, best_used_labels = run_assignment(current_threshold)
    
    return best_result


# --- Column Deduplication Helper ---
COLUMN_DUPLICATE_TOLERANCE = 0.25  # feet (~3 inches)
POINT_FOOTPRINT_DUPLICATE_TOLERANCE = 1.0  # feet; handles mixed point + footprint drafting noise.
FOOTPRINT_FRAGMENT_MERGE_TOLERANCE = 0.1
FOOTPRINT_FRAGMENT_CENTROID_TOLERANCE = 3.0
FOOTPRINT_FRAGMENT_MAX_AREA = 3.0


def _footprint_overlap_ratio(a, b):
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    smaller_area = min(a.area, b.area)
    if smaller_area <= AREA_TOLERANCE_SF:
        return 0.0
    intersection = a.intersection(b)
    return (intersection.area if not intersection.is_empty else 0.0) / smaller_area


def _column_records_match(record, entry):
    distance = record["point"].distance(entry["point"])
    record_footprint = record.get("footprint")
    entry_footprint = entry.get("footprint")

    if record_footprint is None and entry_footprint is None:
        return distance <= COLUMN_DUPLICATE_TOLERANCE, distance, "point proximity"

    if record_footprint is not None and entry_footprint is not None:
        if _footprint_overlap_ratio(record_footprint, entry_footprint) >= 0.5:
            return True, distance, "footprint overlap"
        footprint_distance = record_footprint.distance(entry_footprint)
        smaller_area = min(record_footprint.area, entry_footprint.area)
        if (
            footprint_distance <= FOOTPRINT_FRAGMENT_MERGE_TOLERANCE
            and distance <= FOOTPRINT_FRAGMENT_CENTROID_TOLERANCE
            and smaller_area <= FOOTPRINT_FRAGMENT_MAX_AREA
        ):
            return True, footprint_distance, "footprint fragment"
        return distance <= COLUMN_DUPLICATE_TOLERANCE, distance, "footprint centroid proximity"

    footprint = record_footprint or entry_footprint
    point_record = entry if record_footprint is not None else record
    distance_to_footprint = point_record["point"].distance(footprint)
    if distance_to_footprint <= POINT_FOOTPRINT_DUPLICATE_TOLERANCE:
        return True, distance_to_footprint, "point near footprint"

    return False, distance, "separate supports"


def _merge_column_record(entry, record):
    entry["source_indices"].append(record["original_index"])
    entry["source_types"].add(record["source_type"])

    record_footprint = record.get("footprint")
    entry_footprint = entry.get("footprint")

    if entry_footprint is not None and record_footprint is not None:
        merged = entry_footprint.union(record_footprint).buffer(0)
        if not merged.is_empty:
            entry["footprint"] = merged
            centroid = merged.centroid
            if not merged.buffer(1e-6).covers(centroid):
                centroid = merged.representative_point()
            entry["point"] = centroid
        return

    # Prefer the actual footprint as the canonical support location and display
    # geometry when a point and closed column outline describe the same support.
    if entry_footprint is None and record_footprint is not None:
        entry["point"] = record["point"]
        entry["footprint"] = record_footprint
        entry["original_index"] = record["original_index"]


def deduplicate_column_records(records):
    """
    Remove column supports that are effectively duplicates.

    Point-only supports use a tight tolerance. Mixed point + footprint supports
    merge when the point falls inside or near the footprint, which prevents one
    physical column from producing both a labeled footprint tributary and a
    nearby unlabeled point tributary.

    Returns a tuple of (unique_columns, duplicate_records) where unique_columns is a list of
    dicts with keys:
        - 'point': shapely.geometry.Point for the canonical location
        - 'original_index': index from the original CSV data
        - 'source_indices': list of original indices merged into this point
    and duplicate_records is a list of mappings describing which original indices were merged.
    """
    unique_columns = []
    duplicate_records = []
    
    for record in records:
        matched_entry = None
        for entry in unique_columns:
            is_duplicate, distance, reason = _column_records_match(record, entry)
            if is_duplicate:
                matched_entry = entry
                kept_index = entry["original_index"]
                _merge_column_record(matched_entry, record)
                duplicate_records.append({
                    'kept_index': kept_index,
                    'removed_index': record["original_index"],
                    'distance': distance,
                    'reason': reason,
                })
                break
        
        if matched_entry is None:
            unique_columns.append({
                'point': record["point"],
                'original_index': record["original_index"],
                'source_indices': [record["original_index"]],
                'source_types': {record["source_type"]},
                'footprint': record.get("footprint"),
            })
    
    return unique_columns, duplicate_records


def normalize_column_label_text(label):
    """
    Normalize raw column label text to a canonical alphanumeric format.
    Handles labels such as '5a', '5-A', '  7  B', etc.
    """
    if label is None:
        return ""
    
    # Decode bytes safely
    if isinstance(label, bytes):
        try:
            label = label.decode('utf-8', errors='ignore')
        except Exception:
            label = label.decode('latin1', errors='ignore')
    
    # Convert anything else to string
    if not isinstance(label, str):
        label = str(label)
    
    raw_text = label.strip()
    raw_text = raw_text.replace("\\P", " ").replace("\\~", " ")
    raw_text = re.sub(r"\{\\[^;]*;([^}]*)\}", r"\1", raw_text)
    raw_text = re.sub(r"\\[A-Za-z][^;]*;", "", raw_text)
    raw_text = raw_text.replace("{", "").replace("}", "")
    raw_text = " ".join(raw_text.split())
    if raw_text.lower() == 'nan':
        raw_text = ''
    if not raw_text:
        return ""
    
    # Remove whitespace, underscores, and hyphens between characters
    normalized = re.sub(r'[\s_-]+', '', raw_text)
    # Drop any non-alphanumeric characters
    normalized = re.sub(r'[^0-9A-Za-z]+', '', normalized)
    normalized = normalized.upper()
    
    # If normalization stripped everything, fall back to cleaned raw text
    if not normalized:
        normalized = raw_text.upper()
    
    return normalized

# --- Load data ---
points_df = pd.read_csv("dxf_points.csv")
boundary_df = pd.read_csv("dxf_boundaries.csv")
try:
    drafting_errors_df = pd.read_csv("dxf_drafting_errors.csv")
except FileNotFoundError:
    drafting_errors_df = pd.DataFrame(columns=["x", "y", "min_x", "min_y", "max_x", "max_y", "kind", "source_layer", "reason"])
column_footprints_by_id = load_column_footprints()
job_config = load_job_config()
INCHES_TO_FEET = unit_factor(job_config["source_units"])
wall_layers = set(job_config["layers"]["wall"])
beam_layers = set(job_config["layers"].get("beam", []))

# --- Load original DXF to preserve WALL layer entities ---
INPUT_DXF = Path("INPUT.DXF")
if not INPUT_DXF.exists():
    raise FileNotFoundError(f"Missing input DXF: {INPUT_DXF}")

input_dxf = ezdxf.readfile(str(INPUT_DXF))
input_msp = input_dxf.modelspace()

# Extract configured wall entities from input, including entities inside block references
wall_entities = []
for entity in input_msp:
    if not hasattr(entity.dxf, 'layer'):
        continue
    if entity.dxf.layer in wall_layers:
        if entity.dxftype() in {'LINE', 'LWPOLYLINE', 'POLYLINE'}:
            wall_entities.append(entity)
        elif entity.dxftype() == 'INSERT':
            # Explode block references to get contained geometry with transforms applied
            try:
                for child in entity.virtual_entities():
                    if child.dxftype() in {'LINE', 'LWPOLYLINE', 'POLYLINE'}:
                        wall_entities.append(child)
            except Exception as e:
                print(f"Warning: Could not explode block '{entity.dxf.name}' on WALL layer: {e}")
    elif entity.dxftype() == 'INSERT':
        # Also check INSERT entities on other layers whose block contains wall-layer geometry
        try:
            for child in entity.virtual_entities():
                if hasattr(child.dxf, 'layer') and child.dxf.layer in wall_layers and child.dxftype() in {'LINE', 'LWPOLYLINE', 'POLYLINE'}:
                    wall_entities.append(child)
        except Exception:
            pass
print(f"Found {len(wall_entities)} WALL entities in input DXF (including exploded blocks)")

# Extract DATUM POINTs (user-set per-floor alignment markers). Coordinates
# are converted to feet so they match floor_plan['slab_polygon'].
datum_layers = set(job_config["layers"].get("datum", []) or [])
user_datum_points = []
for entity in input_msp:
    if not hasattr(entity.dxf, 'layer'):
        continue
    if entity.dxf.layer in datum_layers and entity.dxftype() == 'POINT':
        loc = entity.dxf.location
        user_datum_points.append(
            Point(loc.x * INCHES_TO_FEET, loc.y * INCHES_TO_FEET)
        )
if user_datum_points:
    print(f"Found {len(user_datum_points)} DATUM point(s) in input DXF")

# --- Extract wall entities and generate support points ---
wall_data_list = []

for wall_idx, entity in enumerate(wall_entities):
    try:
        wall_line = None
        
        if entity.dxftype() in {'LWPOLYLINE', 'POLYLINE'}:
            # Get polyline vertices
            if entity.dxftype() == 'LWPOLYLINE':
                points = list(entity.get_points())
                if len(points) >= 2:
                    vertices = [(p[0] * INCHES_TO_FEET, p[1] * INCHES_TO_FEET) for p in points]
                    wall_line = LineString(vertices)
            else:
                vertices = []
                for vertex in entity.vertices:
                    location = vertex.dxf.location
                    vertices.append((location.x * INCHES_TO_FEET, location.y * INCHES_TO_FEET))
                if len(vertices) >= 2:
                    wall_line = LineString(vertices)
        
        elif entity.dxftype() == 'LINE':
            # Single line entity
            start = entity.dxf.start
            end = entity.dxf.end
            wall_line = LineString([
                (start.x * INCHES_TO_FEET, start.y * INCHES_TO_FEET),
                (end.x * INCHES_TO_FEET, end.y * INCHES_TO_FEET)
            ])
        
        # Generate wall support sites at a coarser spacing to keep the solve tractable.
        if wall_line and wall_line.length > 0:
            support_points = sample_wall_support_points(wall_line, spacing=WALL_SUPPORT_SPACING_FEET)
            wall_data = {
                'wall_index': wall_idx,
                'wall_line': wall_line,
                'support_points': support_points,
                'merged_region': None,
                'total_area': 0.0,
            }
            wall_data_list.append(wall_data)
    
    except Exception as e:
        print(f"Warning: Could not process WALL entity {wall_idx}: {e}")

print(f"Extracted {len(wall_data_list)} WALL entities with support points")

# --- Extract beam entities for display only ---
beam_entities = collect_display_linework_entities(input_msp, beam_layers)
beam_data_list = []
for entity, source_layer in beam_entities:
    try:
        for beam_line in entity_to_lines(entity, INCHES_TO_FEET):
            if beam_line and beam_line.length > 0:
                beam_data_list.append(
                    {
                        "beam_index": len(beam_data_list),
                        "beam_line": beam_line,
                        "source_layer": source_layer,
                    }
                )
    except Exception as exc:
        print(f"Warning: Could not process BEAM entity on layer '{source_layer}': {exc}")

print(f"Extracted {len(beam_data_list)} BEAM display segment(s)")

# --- Reconstruct slab boundary and identify multiple floor plans ---
def _ring_polygon(ring_df):
    vertices = [(row['x'], row['y']) for _, row in ring_df.iterrows()]
    if len(vertices) < 3:
        return None
    if vertices[0] != vertices[-1]:
        vertices.append(vertices[0])
    return Polygon(vertices).buffer(0)


boundary_surfaces = []
if not boundary_df.empty and 'boundary_id' in boundary_df.columns:
    hole_polygons_by_parent = {}
    shell_ids = []
    for boundary_id in boundary_df['boundary_id'].unique():
        ring_df = boundary_df[boundary_df['boundary_id'] == boundary_id]
        is_hole = 'ring_role' in ring_df.columns and str(ring_df['ring_role'].iloc[0]) == 'hole'
        if not is_hole:
            shell_ids.append(boundary_id)
            continue
        hole = _ring_polygon(ring_df.sort_values('vertex_index'))
        if hole is not None and not hole.is_empty:
            parent_id = str(boundary_id).rsplit('_H', 1)[0]
            hole_polygons_by_parent.setdefault(parent_id, []).append(hole)

    for boundary_id in shell_ids:
        ring_df = boundary_df[boundary_df['boundary_id'] == boundary_id].sort_values('vertex_index')
        polygon = _ring_polygon(ring_df)
        if polygon is not None:
            for hole in hole_polygons_by_parent.get(str(boundary_id), []):
                polygon = polygon.difference(hole)
            if not polygon.is_empty and polygon.area > 1e-6:
                load_layer = "BOUNDARY"
                if "load_layer" in ring_df.columns:
                    layer_values = ring_df["load_layer"].dropna().astype(str)
                    if not layer_values.empty:
                        load_layer = layer_values.iloc[0]
                for part in polygon_parts(polygon):
                    if part.area > AREA_TOLERANCE_SF:
                        boundary_surfaces.append(
                            {"polygon": part, "load_layer": load_layer}
                        )

if not boundary_surfaces:
    raise ValueError("No valid floor plans could be created from boundary data")

surface_groups = boundary_surface_groups(boundary_surfaces)
floor_plans = []
for idx, group in enumerate(surface_groups):
    slab_polygon = unary_union(
        [surface["polygon"] for surface in group["surfaces"]]
    ).buffer(0)
    if slab_polygon.is_empty or slab_polygon.area <= AREA_TOLERANCE_SF:
        continue

    floor_plans.append(
        {
            'index': idx,
            'boundary_id': f'FLOOR_{idx}',
            'slab_polygon': slab_polygon,
            'load_layer': "BOUNDARY",
            'load_zones': load_zones_from_boundary_surfaces(group["surfaces"]),
        }
    )

print(f"Identified {len(floor_plans)} floor plan(s)")

# --- Load and associate floor numbers with floor plans ---
print("\n=== Floor Number Association ===")

floor_numbers_list = []
try:
    floor_numbers_df = pd.read_csv("dxf_floor_numbers.csv")
    floor_numbers_list = floor_numbers_df.to_dict('records')
    print(f"Loaded {len(floor_numbers_list)} floor numbers from dxf_floor_numbers.csv")
except FileNotFoundError:
    print("WARNING: dxf_floor_numbers.csv not found - using default floor numbering")
except Exception as e:
    print(f"WARNING: Could not load floor numbers: {e}")

for floor_plan in floor_plans:
    nearest = nearest_label(floor_plan['slab_polygon'], floor_numbers_list, 'floor_number')
    floor_plan['floor_number'] = nearest or floor_plan['boundary_id']

# Print initial floor number assignment
print("\n--- Initial Floor Number Assignment ---")
for floor_plan in floor_plans:
    floor_idx = floor_plan['index']
    boundary_id = floor_plan['boundary_id']
    floor_number = floor_plan.get('floor_number', boundary_id)
    print(f"Floor {floor_idx} (boundary_id: {boundary_id}) -> Floor Number: {floor_number}")

assign_user_datums_to_floor_plans(floor_plans, user_datum_points)

print("\n--- Detached Additional Load Attachment ---")
previous_floor_count = len(floor_plans)
floor_plans, detached_additional_source_polygons = attach_secondary_only_floor_plans(floor_plans)
attached_floor_count = previous_floor_count - len(floor_plans)
if attached_floor_count:
    print(f"Attached {attached_floor_count} detached additional-load floor(s)")
else:
    print("No detached additional-load floor plans required attachment")

# Group floor plans by floor number (consolidate multiple boundaries per floor)
print("\n--- Consolidating Floor Plans by Floor Number ---")
floor_groups = {}
for floor_plan in floor_plans:
    floor_number = floor_plan.get('floor_number')
    if floor_number not in floor_groups:
        floor_groups[floor_number] = []
    floor_groups[floor_number].append(floor_plan)

print(f"Consolidated {len(floor_plans)} boundaries into {len(floor_groups)} floor groups")

# Create new consolidated floor plans list
# Handle edge cases:
#   - If floor A fully contains floor B (same floor number): use the larger one
#   - If floors are adjacent/overlapping (same floor number): merge into one
consolidated_floor_plans = []
for floor_number, group in floor_groups.items():
    if len(group) == 1:
        # Single boundary for this floor - use as-is
        consolidated_floor_plans.append(group[0])
    else:
        print(f"  Floor '{floor_number}': Processing {len(group)} boundaries")

        # Sort by area descending so the largest boundary comes first
        group.sort(key=lambda fp: fp['slab_polygon'].area, reverse=True)

        # Remove boundaries fully contained within a larger boundary in this group
        kept = []
        for fp in group:
            poly = fp['slab_polygon']
            contained = False
            for larger in kept:
                if larger['slab_polygon'].buffer(EDGE_TOLERANCE_FEET).contains(poly):
                    print(f"    Dropping boundary (area {poly.area:.0f} SF) — contained in larger ({larger['slab_polygon'].area:.0f} SF)")
                    contained = True
                    break
            if not contained:
                kept.append(fp)

        # Merge remaining boundaries (adjacent/overlapping)
        if len(kept) == 1:
            consolidated_floor_plans.append(kept[0])
        else:
            merged_polygon = unary_union([fp['slab_polygon'] for fp in kept])
            print(f"    Merged {len(kept)} boundaries into one ({merged_polygon.area:.0f} SF)")
            load_zones = load_zones_from_floor_plans(kept)
            consolidated_floor_plan = {
                'index': len(consolidated_floor_plans),
                'boundary_id': floor_number,
                'floor_number': floor_number,
                'slab_polygon': merged_polygon,
                'load_zones': load_zones,
                'column_points': [],
                'column_indices': [],
                'walls': [],
                'beams': [],
                'column_labels': [],
                'label_associations': {},
                'unlabeled_points': []
            }
            consolidated_floor_plans.append(consolidated_floor_plan)

# Replace floor_plans with consolidated version
floor_plans = consolidated_floor_plans

print(f"\nFinal floor plan count: {len(floor_plans)}")
print("\n--- Consolidated Floor Plan Summary ---")
for floor_plan in floor_plans:
    floor_idx = floor_plan['index']
    floor_number = floor_plan.get('floor_number')
    print(f"Floor {floor_idx} -> Floor Number: {floor_number}")
    load_zone_layers = sorted(
        {
            str(zone.get("layer"))
            for zone in floor_plan.get("load_zones", [])
            if zone.get("layer")
        }
    )
    if load_zone_layers:
        print(f"  Load zones: {', '.join(load_zone_layers)}")
    slab_parts = polygon_parts(floor_plan.get("slab_polygon"))
    if len(slab_parts) > 1:
        part_summaries = [
            f"{part.area:.0f} SF @ ({part.bounds[0]:.1f}, {part.bounds[1]:.1f})"
            for part in sorted(slab_parts, key=lambda part: part.area, reverse=True)
        ]
        print(
            f"  WARNING: disconnected slab domain has {len(slab_parts)} components: "
            + "; ".join(part_summaries)
        )

# --- Assign column points to floor plans based on spatial containment ---
# Load all column points first and deduplicate near-identical ones
raw_column_footprints = {}
if "footprint_id" in points_df.columns:
    for row_index, footprint_id in enumerate(points_df["footprint_id"].fillna("").astype(str)):
        if footprint_id and footprint_id in column_footprints_by_id:
            raw_column_footprints[row_index] = column_footprints_by_id[footprint_id]

raw_column_records = []
ignored_detached_context_columns = []
for row_index, row in points_df.iterrows():
    point = Point(row["x"], row["y"])
    source_type = str(row.get("source_type", "POINT") or "POINT").upper()
    footprint = raw_column_footprints.get(row_index)
    if (
        geometry_intersects_any(point, detached_additional_source_polygons)
        or geometry_intersects_any(footprint, detached_additional_source_polygons)
    ):
        ignored_detached_context_columns.append(row_index)
        continue
    raw_column_records.append(
        {
            "point": point,
            "original_index": row_index,
            "source_type": source_type,
            "footprint": footprint,
        }
    )

unique_column_entries, duplicate_column_records = deduplicate_column_records(raw_column_records)
all_column_points = [entry['point'] for entry in unique_column_entries]
all_column_original_indices = [entry['original_index'] for entry in unique_column_entries]
all_column_footprints = [entry.get('footprint') for entry in unique_column_entries]

if duplicate_column_records:
    print("\n=== Column Deduplication ===")
    print(
        f"Detected {len(duplicate_column_records)} overlapping column instance(s) "
        f"across point/footprint inputs. "
        f"Reduced {len(raw_column_records)} raw supports to {len(all_column_points)} unique columns."
    )
    for record in duplicate_column_records[:5]:
        print(
            f"  - Original column {record['removed_index']} merged into {record['kept_index']} "
            f"({record['reason']}, distance {record['distance']:.2f} ft)"
        )
    if len(duplicate_column_records) > 5:
        print(f"    ... and {len(duplicate_column_records) - 5} more duplicates merged")

if ignored_detached_context_columns:
    print(
        f"Ignored {len(ignored_detached_context_columns)} column support(s) from detached "
        "additional-load context"
    )

# Initialize column storage and wall data for each floor plan
for floor_plan in floor_plans:
    floor_plan['column_points'] = []
    floor_plan['column_footprints'] = []
    floor_plan['column_indices'] = []
    floor_plan['walls'] = []
    floor_plan['beams'] = []
    floor_plan['column_labels'] = []  # NEW: Labels for each column point (parallel to column_points)
    floor_plan['label_associations'] = {}  # NEW: Mapping of point_index → label
    floor_plan['unlabeled_points'] = []  # NEW: Indices of points without labels

# --- Assign wall entities to floor plans based on slab intersection ---
orphaned_walls = []
ignored_detached_context_walls = []

for wall_data in wall_data_list:
    wall_line = wall_data['wall_line']
    if geometry_intersects_any(wall_line, detached_additional_source_polygons):
        ignored_detached_context_walls.append(wall_data['wall_index'])
        continue
    matches = [floor_plan for floor_plan in floor_plans if line_intersects(floor_plan['slab_polygon'], wall_line)]
    if len(matches) > 1:
        raise NeedsReviewError(
            f"Wall support {wall_data['wall_index']} touches {len(matches)} slab polygons. Review required."
        )
    if len(matches) == 1:
        matches[0]['walls'].append(wall_data)
    else:
        orphaned_walls.append(wall_data['wall_index'])

# Print wall assignment summary
print("\n=== Wall Assignment Summary ===")
for floor_plan in floor_plans:
    if floor_plan['walls']:
        total_support_points = sum(len(w['support_points']) for w in floor_plan['walls'])
        print(f"Floor {floor_plan['index']} ({floor_plan['boundary_id']}): {len(floor_plan['walls'])} wall(s) with {total_support_points} support points")

if orphaned_walls:
    print(f"\nWARNING: {len(orphaned_walls)} wall(s) not contained in any floor plan: {orphaned_walls}")

if ignored_detached_context_walls:
    print(
        f"Ignored {len(ignored_detached_context_walls)} wall(s) from detached "
        f"additional-load context: {ignored_detached_context_walls}"
    )

# --- Assign beam display linework to floor plans based on slab intersection ---
orphaned_beams = []
ignored_detached_context_beams = []

for beam_data in beam_data_list:
    beam_line = beam_data["beam_line"]
    if geometry_intersects_any(beam_line, detached_additional_source_polygons):
        ignored_detached_context_beams.append(beam_data["beam_index"])
        continue
    matches = [
        floor_plan
        for floor_plan in floor_plans
        if line_intersects(floor_plan["slab_polygon"], beam_line)
    ]
    if matches:
        for floor_plan in matches:
            floor_plan["beams"].append(beam_data)
    else:
        orphaned_beams.append(beam_data["beam_index"])

print("\n=== Beam Display Assignment Summary ===")
for floor_plan in floor_plans:
    if floor_plan["beams"]:
        print(
            f"Floor {floor_plan['index']} ({floor_plan['boundary_id']}): "
            f"{len(floor_plan['beams'])} beam display segment(s)"
        )

if orphaned_beams:
    print(f"\nWARNING: {len(orphaned_beams)} beam segment(s) not contained in any floor plan: {orphaned_beams}")

if ignored_detached_context_beams:
    print(
        f"Ignored {len(ignored_detached_context_beams)} beam segment(s) from detached "
        f"additional-load context: {ignored_detached_context_beams}"
    )

# Track orphaned columns (not contained in any floor plan)
orphaned_columns = []

# For each column point, check which floor plan polygon contains it
for col_point, original_idx, footprint in zip(
    all_column_points,
    all_column_original_indices,
    all_column_footprints,
):
    matches = [floor_plan for floor_plan in floor_plans if point_is_inside(floor_plan['slab_polygon'], col_point)]
    if len(matches) > 1:
        raise NeedsReviewError(
            f"Column support {original_idx} touches {len(matches)} slab polygons. Review required."
        )
    if len(matches) == 1:
        matches[0]['column_points'].append(col_point)
        matches[0]['column_footprints'].append(footprint)
        matches[0]['column_indices'].append(original_idx)
    else:
        orphaned_columns.append((original_idx, col_point, footprint))

# Rescue orphans by assigning them to the nearest floor's slab. A column
# can sit outside the slab boundary on a given floor (perimeter stepback,
# cantilever) but still belong to that floor structurally — dropping them
# breaks continuity tracking and loses cross-section data.
if orphaned_columns and floor_plans:
    print(f"\nRescued {len(orphaned_columns)} orphan column(s) — outside any slab, "
          f"assigning to nearest floor:")
    for idx, col_point, footprint in orphaned_columns:
        nearest = min(
            floor_plans,
            key=lambda fp: fp['slab_polygon'].distance(col_point),
        )
        nearest['column_points'].append(col_point)
        nearest['column_footprints'].append(footprint)
        nearest['column_indices'].append(idx)
        gap_ft = nearest['slab_polygon'].distance(col_point)
        print(f"  Column {idx} at ({col_point.x:.2f}, {col_point.y:.2f}) "
              f"-> floor {nearest['index']} ({nearest['boundary_id']}, gap {gap_ft:.1f} ft)")

# Print summary of column assignments
print("\n=== Column Assignment Summary ===")
for floor_plan in floor_plans:
    print(f"Floor {floor_plan['index']} ({floor_plan['boundary_id']}): {len(floor_plan['column_points'])} column(s) assigned")

# --- Load column labels and associate with points ---
print("\n=== Column Label Association ===")

# Load column labels from CSV
column_labels = []
label_normalization_summary = None
try:
    labels_df = pd.read_csv("dxf_column_labels.csv")
    if 'label' in labels_df.columns:
        raw_series = labels_df['label']
        labels_df['raw_label'] = raw_series.fillna('').astype(str)
        labels_df['label'] = labels_df['raw_label'].apply(normalize_column_label_text)
        label_normalization_summary = labels_df[labels_df['raw_label'] != labels_df['label']]
    else:
        labels_df['label'] = ""
        labels_df['raw_label'] = ""
    column_labels = labels_df.to_dict('records')
    print(f"Loaded {len(column_labels)} column labels from dxf_column_labels.csv")
    if label_normalization_summary is not None and not label_normalization_summary.empty:
        print(f"  Normalized {len(label_normalization_summary)} label(s) to handle alphanumeric variants (e.g., '5a' -> '5A').")
        for _, row in label_normalization_summary.head(5).iterrows():
            print(f"    - '{row['raw_label']}' -> '{row['label']}'")
        remaining = len(label_normalization_summary) - min(5, len(label_normalization_summary))
        if remaining > 0:
            print(f"    ... and {remaining} more label(s) normalized")
except FileNotFoundError:
    print("WARNING: dxf_column_labels.csv not found - continuing with unlabeled columns")
except Exception as e:
    print(f"WARNING: Could not load column labels: {e}")

# Default search radius for initial association pass
LABEL_ASSOCIATION_DISTANCE = 10.0

# Associate labels with points for each floor plan
for floor_plan in floor_plans:
    floor_idx = floor_plan['index']
    column_points = floor_plan['column_points']
    slab_polygon = floor_plan['slab_polygon']
    
    if not column_labels or not column_points:
        # No labels or no points - store empty association results
        floor_plan['label_associations'] = {}
        floor_plan['unlabeled_points'] = list(range(len(column_points)))
        floor_plan['orphaned_labels'] = []
        floor_plan['association_summary'] = None
        # Populate column_labels with auto-generated labels for unlabeled points
        floor_token = sanitized_floor_token(floor_plan.get('floor_number', floor_plan['boundary_id']))
        floor_plan['column_labels'] = [f"{floor_token}_UNLABELED_{idx:02d}" for idx in range(len(column_points))]
        floor_plan['low_confidence_points'] = []
        continue
    
    # Filter labels to only those within or near this floor plan's boundary
    # Use a buffer to catch labels that might be just outside the boundary
    floor_buffer = slab_polygon.buffer(50.0)  # 50 ft buffer to catch nearby labels
    floor_labels = []
    for label_dict in column_labels:
        label_point = Point(label_dict['x'], label_dict['y'])
        if floor_buffer.contains(label_point):
            floor_labels.append(label_dict)
    
    # Call association function with filtered labels
    association_results = associate_labels_with_points(
        column_points,
        floor_labels,
        max_distance=LABEL_ASSOCIATION_DISTANCE
    )
    
    # Store results in floor plan dictionary
    floor_plan['label_associations'] = association_results['associations']
    floor_plan['unlabeled_points'] = association_results['unlabeled_points']
    floor_plan['orphaned_labels'] = association_results['orphaned_labels']
    floor_plan['association_summary'] = association_results['summary']
    floor_plan['low_confidence_points'] = [
        point_idx
        for point_idx, distance in association_results.get('association_distances', {}).items()
        if distance > LABEL_ASSOCIATION_DISTANCE
    ]
    
    # Populate column_labels list (parallel to column_points)
    floor_plan['column_labels'] = []
    floor_token = sanitized_floor_token(floor_plan.get('floor_number', floor_plan['boundary_id']))
    for point_idx in range(len(column_points)):
        if point_idx in floor_plan['label_associations']:
            # Use the associated label
            floor_plan['column_labels'].append(floor_plan['label_associations'][point_idx])
        else:
            # Use auto-generated label for unlabeled points
            floor_plan['column_labels'].append(f"{floor_token}_UNLABELED_{point_idx:02d}")

# Print association summary report
print("\n--- Association Summary Report ---")
for floor_plan in floor_plans:
    floor_idx = floor_plan['index']
    boundary_id = floor_plan['boundary_id']
    summary = floor_plan.get('association_summary')
    
    print(f"\nFloor {floor_idx} ({boundary_id}):")
    
    if summary is None:
        print("  No labels or points to associate")
        continue
    
    # Print count of successfully associated labels
    print(f"  Successfully associated labels: {summary['associated_count']}")
    
    # Print average association distance
    if summary['associated_count'] > 0:
        print(f"  Average association distance: {summary['average_distance']:.2f} ft")
        max_assoc_distance = summary.get('max_association_distance', 0.0)
        if max_assoc_distance > 0:
            print(f"  Max association distance: {max_assoc_distance:.2f} ft")
        threshold_used = summary.get('threshold_used')
        if (threshold_used is not None and 
                threshold_used > LABEL_ASSOCIATION_DISTANCE + 1e-6):
            print(f"  Association radius auto-expanded to {threshold_used:.1f} ft")
    
    # Print unlabeled column points
    unlabeled_points = floor_plan['unlabeled_points']
    if unlabeled_points:
        print(f"  Unlabeled column points: {len(unlabeled_points)}")
        column_points = floor_plan['column_points']
        for point_idx in unlabeled_points[:5]:  # Show first 5
            point = column_points[point_idx]
            print(f"    - Point {point_idx} at ({point.x:.2f}, {point.y:.2f})")
        if len(unlabeled_points) > 5:
            print(f"    ... and {len(unlabeled_points) - 5} more")
    
    # Print orphaned labels
    orphaned_labels = floor_plan['orphaned_labels']
    if orphaned_labels:
        print(f"  Orphaned labels (too far from any point): {len(orphaned_labels)}")
        for label_dict in orphaned_labels[:5]:  # Show first 5
            raw_label = label_dict.get('raw_label', '')
            canonical_label = label_dict.get('label', raw_label)
            label_display = raw_label or canonical_label or '<empty>'
            print(f"    - Label '{label_display}' at ({label_dict['x']:.2f}, {label_dict['y']:.2f})")
        if len(orphaned_labels) > 5:
            print(f"    ... and {len(orphaned_labels) - 5} more")
    
    low_confidence_points = floor_plan.get('low_confidence_points', [])
    if low_confidence_points:
        print(f"  Low-confidence label matches (>10 ft): {len(low_confidence_points)}")

def assign_fascade_lengths(floor_plans):
    print("\n=== Facade Length Attribution ===")
    for floor_plan in floor_plans:
        floor_idx = floor_plan['index']
        boundary_id = floor_plan['boundary_id']
        try:
            fascade_result = compute_fascade_assignments(
                floor_plan,
                distance_threshold=FASCADE_DISTANCE_THRESHOLD,
                sample_spacing=FASCADE_SAMPLE_SPACING,
            )
            floor_plan['fascade_data'] = fascade_result

            perimeter = fascade_result.get('perimeter', 0.0)
            assigned = fascade_result.get('assigned_total', 0.0)
            coverage_pct = fascade_result.get('coverage_ratio', 0.0) * 100.0
            max_gap = fascade_result.get('max_distance_seen', 0.0)
            candidates = fascade_result.get('candidate_count', 0)
            method = fascade_result.get('assignment_method', 'nearest_boundary_sample')

            if candidates == 0:
                print(f"Floor {floor_idx} ({boundary_id}): No façade participants found by {method}.")
            else:
                print(
                    f"Floor {floor_idx} ({boundary_id}): {assigned:.1f} ft / {perimeter:.1f} ft "
                    f"(coverage {coverage_pct:.1f}%, method {method}, max gap {max_gap:.1f} ft, "
                    f"{candidates} participant(s))"
                )
        except Exception as exc:
            floor_plan['fascade_data'] = None
            print(f"Floor {floor_idx} ({boundary_id}): Facade attribution failed: {exc}")

# --- Define half-plane polygon function (used for tributary calculation) ---
def half_plane_polygon(P, Q, bbox):
    Px, Py, Qx, Qy = P.x, P.y, Q.x, Q.y
    A = Qx - Px
    B = Qy - Py
    C = (Qx**2 + Qy**2 - Px**2 - Py**2) / 2.0
    corners = [(bbox['minx'], bbox['miny']), (bbox['maxx'], bbox['miny']),
               (bbox['maxx'], bbox['maxy']), (bbox['minx'], bbox['maxy'])]
    pts = [(cx, cy) for (cx, cy) in corners if A*cx + B*cy <= C + 1e-9]
    if abs(B) < 1e-9 and abs(A) > 1e-9:
        x0 = C / A
        if bbox['minx'] <= x0 <= bbox['maxx']:
            pts += [(x0, bbox['miny']), (x0, bbox['maxy'])]
    if abs(A) < 1e-9 and abs(B) > 1e-9:
        y0 = C / B
        if bbox['miny'] <= y0 <= bbox['maxy']:
            pts += [(bbox['minx'], y0), (bbox['maxx'], y0)]
    if abs(B) > 1e-9:
        for y0 in [bbox['miny'], bbox['maxy']]:
            x0 = (C - B*y0) / A if abs(A) > 1e-9 else None
            if x0 and bbox['minx'] <= x0 <= bbox['maxx']:
                pts.append((x0, y0))
    if abs(A) > 1e-9:
        for x0 in [bbox['minx'], bbox['maxx']]:
            y0 = (C - A*x0) / B if abs(B) > 1e-9 else None
            if y0 and bbox['miny'] <= y0 <= bbox['maxy']:
                pts.append((x0, y0))
    if len(pts) < 3:
        return None
    return Polygon(pts).convex_hull

# --- Resample wall support points with column clearance ---
# Now that both walls and columns are assigned to floors, resample wall support
# points to skip any that are too close to a column. This prevents wall Voronoi
# cells from crowding out columns near balconies and edges.
for floor_plan in floor_plans:
    col_pts = floor_plan.get('column_points', [])
    for wall_data in floor_plan.get('walls', []):
        wall_line = wall_data.get('wall_line')
        if wall_line and col_pts:
            wall_data['support_points'] = sample_wall_support_points(
                wall_line, spacing=WALL_SUPPORT_SPACING_FEET, column_points=col_pts
            )

# --- Process each floor plan independently to calculate tributary regions ---
print("\n=== Tributary Region Calculation ===")

threshold = 30.0
worker_count = min(len(floor_plans), multiprocessing.cpu_count() or 1)
print(f"Using {worker_count} floor worker(s) with {WALL_SUPPORT_SPACING_FEET:.1f} ft wall spacing")

if worker_count > 1:
    try:
        mp_context = multiprocessing.get_context("fork")
    except ValueError:
        mp_context = None

    with ProcessPoolExecutor(max_workers=worker_count, mp_context=mp_context) as executor:
        futures = {executor.submit(solve_floor_tributary, floor_plan, threshold): floor_plan['index'] for floor_plan in floor_plans}
        solve_results = {}
        for future in as_completed(futures):
            solve_results[futures[future]] = future.result()
    ordered_results = [solve_results[floor_plan['index']] for floor_plan in floor_plans]
else:
    ordered_results = [solve_floor_tributary(floor_plan, threshold) for floor_plan in floor_plans]

for floor_plan, solve_result in zip(floor_plans, ordered_results):
    for line in solve_result['logs']:
        print(line)

    floor_plan['regions'] = solve_result['column_regions']
    floor_plan['areas'] = solve_result['column_areas']

    wall_results = solve_result['wall_results']
    for wall_data in floor_plan['walls']:
        wall_result = wall_results.get(wall_data['wall_index'], {'merged_region': None, 'total_area': 0.0})
        wall_data['merged_region'] = wall_result['merged_region']
        wall_data['total_area'] = wall_result['total_area']

    assign_load_zone_areas(floor_plan)

assign_fascade_lengths(floor_plans)

# --- Set up DXF output infrastructure ---
# Create new DXF document with R2010 format
dxf_doc = ezdxf.new('R2010')

# Get modelspace reference for adding entities
msp = dxf_doc.modelspace()

# Scale factor to convert from internal feet back to the input DXF unit system.
OUTPUT_SCALE = 12.0 if job_config["source_units"] == "in" else 1.0

# --- Copy layer definitions from input DXF with color coding ---
print("\n=== Copying Original DXF Content with Color Coding ===")
for layer in input_dxf.layers:
    if layer.dxf.name not in dxf_doc.layers:
        # Create layer with color-coded properties
        new_layer = dxf_doc.layers.add(layer.dxf.name)
        
        # Apply color coding based on layer name
        layer_name = layer.dxf.name.upper()
        if 'WALL' in layer_name:
            new_layer.color = colors.CYAN  # Cyan for walls
        elif 'BEAM' in layer_name or 'GIRDER' in layer_name or 'TRANSFER' in layer_name:
            new_layer.color = colors.YELLOW  # Yellow for beams/transfers
        elif 'BOUNDARY' in layer_name:
            new_layer.color = colors.BLUE  # Blue for boundaries
        elif 'COLUMN' in layer_name or 'POINT' in layer_name:
            new_layer.color = colors.MAGENTA  # Magenta for columns
        else:
            # Keep original color if no match
            if hasattr(layer.dxf, 'color'):
                new_layer.dxf.color = layer.dxf.color
        
        # Copy other layer properties
        if hasattr(layer.dxf, 'linetype'):
            new_layer.dxf.linetype = layer.dxf.linetype
        if hasattr(layer.dxf, 'lineweight'):
            new_layer.dxf.lineweight = layer.dxf.lineweight

print(f"Copied {len(input_dxf.layers)} layer definitions with color coding")

# --- Copy all original DXF entities to output file ---
# Iterate through all entities in the input DXF modelspace
entities_copied = 0
entities_skipped = 0

for entity in input_msp:
    try:
        entity_type = entity.dxftype()
        
        # Handle TEXT entities
        if entity_type == 'TEXT':
            new_text = msp.add_text(entity.dxf.text)
            new_text.dxf.insert = entity.dxf.insert
            new_text.dxf.height = entity.dxf.height
            if hasattr(entity.dxf, 'layer'):
                new_text.dxf.layer = entity.dxf.layer
            if hasattr(entity.dxf, 'color'):
                new_text.dxf.color = entity.dxf.color
            if hasattr(entity.dxf, 'rotation'):
                new_text.dxf.rotation = entity.dxf.rotation
            if hasattr(entity.dxf, 'halign'):
                new_text.dxf.halign = entity.dxf.halign
            if hasattr(entity.dxf, 'valign'):
                new_text.dxf.valign = entity.dxf.valign
            if hasattr(entity.dxf, 'align_point'):
                new_text.dxf.align_point = entity.dxf.align_point
            entities_copied += 1
        
        # Handle MTEXT entities
        elif entity_type == 'MTEXT':
            new_mtext = msp.add_mtext(entity.text)
            new_mtext.dxf.insert = entity.dxf.insert
            new_mtext.dxf.char_height = entity.dxf.char_height
            if hasattr(entity.dxf, 'layer'):
                new_mtext.dxf.layer = entity.dxf.layer
            if hasattr(entity.dxf, 'color'):
                new_mtext.dxf.color = entity.dxf.color
            if hasattr(entity.dxf, 'rotation'):
                new_mtext.dxf.rotation = entity.dxf.rotation
            if hasattr(entity.dxf, 'attachment_point'):
                new_mtext.dxf.attachment_point = entity.dxf.attachment_point
            if hasattr(entity.dxf, 'width'):
                new_mtext.dxf.width = entity.dxf.width
            entities_copied += 1
        
        # Handle LINE entities
        elif entity_type == 'LINE':
            new_line = msp.add_line(entity.dxf.start, entity.dxf.end)
            if hasattr(entity.dxf, 'layer'):
                new_line.dxf.layer = entity.dxf.layer
            if hasattr(entity.dxf, 'color'):
                new_line.dxf.color = entity.dxf.color
            if hasattr(entity.dxf, 'linetype'):
                new_line.dxf.linetype = entity.dxf.linetype
            if hasattr(entity.dxf, 'lineweight'):
                new_line.dxf.lineweight = entity.dxf.lineweight
            entities_copied += 1
        
        # Handle LWPOLYLINE entities
        elif entity_type == 'LWPOLYLINE':
            points = list(entity.get_points())
            new_poly = msp.add_lwpolyline(points, close=entity.closed)
            if hasattr(entity.dxf, 'layer'):
                new_poly.dxf.layer = entity.dxf.layer
            if hasattr(entity.dxf, 'color'):
                new_poly.dxf.color = entity.dxf.color
            if hasattr(entity.dxf, 'linetype'):
                new_poly.dxf.linetype = entity.dxf.linetype
            if hasattr(entity.dxf, 'lineweight'):
                new_poly.dxf.lineweight = entity.dxf.lineweight
            if hasattr(entity.dxf, 'const_width'):
                new_poly.dxf.const_width = entity.dxf.const_width
            entities_copied += 1
        
        # Handle POLYLINE entities
        elif entity_type == 'POLYLINE':
            points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            new_poly = msp.add_lwpolyline(points, close=entity.is_closed)
            if hasattr(entity.dxf, 'layer'):
                new_poly.dxf.layer = entity.dxf.layer
            if hasattr(entity.dxf, 'color'):
                new_poly.dxf.color = entity.dxf.color
            if hasattr(entity.dxf, 'linetype'):
                new_poly.dxf.linetype = entity.dxf.linetype
            if hasattr(entity.dxf, 'lineweight'):
                new_poly.dxf.lineweight = entity.dxf.lineweight
            entities_copied += 1
        
        # Handle POINT entities - SKIP these as they will be added per-floor later
        elif entity_type == 'POINT':
            # Skip POINT entities - they will be added to floor-specific layers later
            entities_skipped += 1
        
        # Handle CIRCLE entities
        elif entity_type == 'CIRCLE':
            new_circle = msp.add_circle(entity.dxf.center, entity.dxf.radius)
            if hasattr(entity.dxf, 'layer'):
                new_circle.dxf.layer = entity.dxf.layer
            if hasattr(entity.dxf, 'color'):
                new_circle.dxf.color = entity.dxf.color
            if hasattr(entity.dxf, 'linetype'):
                new_circle.dxf.linetype = entity.dxf.linetype
            if hasattr(entity.dxf, 'lineweight'):
                new_circle.dxf.lineweight = entity.dxf.lineweight
            entities_copied += 1
        
        # Handle ARC entities
        elif entity_type == 'ARC':
            new_arc = msp.add_arc(entity.dxf.center, entity.dxf.radius, 
                                  entity.dxf.start_angle, entity.dxf.end_angle)
            if hasattr(entity.dxf, 'layer'):
                new_arc.dxf.layer = entity.dxf.layer
            if hasattr(entity.dxf, 'color'):
                new_arc.dxf.color = entity.dxf.color
            if hasattr(entity.dxf, 'linetype'):
                new_arc.dxf.linetype = entity.dxf.linetype
            if hasattr(entity.dxf, 'lineweight'):
                new_arc.dxf.lineweight = entity.dxf.lineweight
            entities_copied += 1
        
        # Handle DIMENSION entities (copy as-is using copy method if available)
        elif entity_type in ['DIMENSION', 'INSERT', 'HATCH']:
            # For complex entities, try to use ezdxf's copy functionality
            try:
                new_entity = entity.copy()
                msp.add_entity(new_entity)
                entities_copied += 1
            except:
                # If copy fails, skip this entity
                entities_skipped += 1
        
        # Skip other entity types that are not in the requirements
        else:
            entities_skipped += 1
    
    except Exception as e:
        print(f"Warning: Could not copy {entity_type} entity: {e}")
        entities_skipped += 1

print(f"Copied {entities_copied} entities from input DXF")
if entities_skipped > 0:
    print(f"Skipped {entities_skipped} entities (unsupported types or errors)")

# --- Assign drafting-error candidates to their floor by slab containment ---
for fp in floor_plans:
    fp['drafting_errors'] = []
if not drafting_errors_df.empty:
    for _, row in drafting_errors_df.iterrows():
        pt = Point(float(row['x']), float(row['y']))
        for fp in floor_plans:
            slab = fp.get('slab_polygon')
            if slab is None or slab.is_empty:
                continue
            if slab.covers(pt):
                fp['drafting_errors'].append({
                    'x': float(row['x']),
                    'y': float(row['y']),
                    'min_x': float(row['min_x']),
                    'min_y': float(row['min_y']),
                    'max_x': float(row['max_x']),
                    'max_y': float(row['max_y']),
                    'kind': str(row['kind']),
                    'source_layer': str(row['source_layer']),
                    'reason': str(row['reason']),
                })
                break
    total = sum(len(fp['drafting_errors']) for fp in floor_plans)
    if total:
        print(f"Drafting-error candidates assigned to floors: {total}")

# --- Assign user-supplied DATUM points to their floor by slab containment ---
if user_datum_points:
    for fp in floor_plans:
        slab = fp.get('slab_polygon')
        if slab is None or slab.is_empty:
            continue
        matching_datums = [
            dp
            for dp in user_datum_points
            if slab.covers(dp) or slab.distance(dp) <= EDGE_TOLERANCE_FEET
        ]
        if matching_datums:
            dp = min(matching_datums, key=lambda point: slab.distance(point))
            fp['user_datum'] = (dp.x, dp.y)

# --- Compute alignment datums once, attach to each floor_plan ---
_floor_datums = compute_floor_datums(floor_plans)
for _fp in floor_plans:
    _fid = _fp.get('floor_number', _fp.get('boundary_id', 'UNKNOWN'))
    _fp['alignment_datum'] = _floor_datums.get(_fid, {})

# --- Process each floor plan for DXF output ---
for floor_plan in floor_plans:
    floor_idx = floor_plan['index']
    slab_polygon = floor_plan['slab_polygon']
    column_points = floor_plan['column_points']
    column_regions = floor_plan['regions']
    column_areas = floor_plan['areas']
    walls = floor_plan['walls']
    
    # Create all required layers for this floor plan using FLOOR_{index}_* naming pattern
    boundary_layer = f'FLOOR_{floor_idx}_BOUNDARY'
    columns_layer = f'FLOOR_{floor_idx}_COLUMNS'
    area_labels_layer = f'FLOOR_{floor_idx}_AREA_LABELS'
    large_tributary_layer = f'FLOOR_{floor_idx}_LARGE_TRIBUTARY'
    wall_layer = f'FLOOR_{floor_idx}_WALL'
    
    # Create layers with color coding
    boundary_layer_obj = dxf_doc.layers.add(boundary_layer)
    boundary_layer_obj.color = colors.BLUE
    
    columns_layer_obj = dxf_doc.layers.add(columns_layer)
    columns_layer_obj.color = colors.WHITE
    
    area_labels_layer_obj = dxf_doc.layers.add(area_labels_layer)
    area_labels_layer_obj.color = colors.GREEN
    
    large_tributary_layer_obj = dxf_doc.layers.add(large_tributary_layer)
    large_tributary_layer_obj.color = colors.MAGENTA
    
    wall_layer_obj = dxf_doc.layers.add(wall_layer)
    wall_layer_obj.color = colors.CYAN

    datum_layer = f'FLOOR_{floor_idx}_DATUM'
    datum_layer_obj = dxf_doc.layers.add(datum_layer)
    datum_layer_obj.color = colors.CYAN

    # --- Add alignment datum marker (crosshair + ring + label) ---
    datum_info = floor_plan.get('alignment_datum') or {}
    datum_point = datum_info.get('point')
    if datum_point is not None:
        dx_in = datum_point[0] * OUTPUT_SCALE
        dy_in = datum_point[1] * OUTPUT_SCALE
        # Ring + crosshair sized in DXF units (inches). Slab dims are
        # ~2000 inches across, so a 24-inch marker reads cleanly.
        ring_r = 24.0
        arm = 36.0
        ring = msp.add_circle((dx_in, dy_in), ring_r)
        ring.dxf.layer = datum_layer
        h_arm = msp.add_line((dx_in - arm, dy_in), (dx_in + arm, dy_in))
        h_arm.dxf.layer = datum_layer
        v_arm = msp.add_line((dx_in, dy_in - arm), (dx_in, dy_in + arm))
        v_arm.dxf.layer = datum_layer
        label_text = f"DATUM·{(datum_info.get('source') or 'NONE').upper()}"
        label = msp.add_text(label_text, dxfattribs={"height": 12.0})
        label.dxf.layer = datum_layer
        label.dxf.insert = (dx_in + arm + 6.0, dy_in - 6.0)

    # --- Add slab boundary to DXF output ---
    if slab_polygon.geom_type == 'Polygon':
        slab_polygons = [slab_polygon]
    elif slab_polygon.geom_type == 'MultiPolygon':
        slab_polygons = list(slab_polygon.geoms)
    else:
        slab_polygons = []

    for slab_part in slab_polygons:
        shell_coords = list(slab_part.exterior.coords)
        shell_coords_inches = [(x * OUTPUT_SCALE, y * OUTPUT_SCALE) for x, y in shell_coords]
        slab_polyline = msp.add_lwpolyline(shell_coords_inches, close=True)
        slab_polyline.dxf.layer = boundary_layer

        for interior in slab_part.interiors:
            hole_coords = list(interior.coords)
            hole_coords_inches = [(x * OUTPUT_SCALE, y * OUTPUT_SCALE) for x, y in hole_coords]
            hole_polyline = msp.add_lwpolyline(hole_coords_inches, close=True)
            hole_polyline.dxf.layer = boundary_layer
    
    # --- Add column points to DXF output ---
    # Iterate through column_points list
    for col_point in column_points:
        # Scale coordinates from feet to inches for DXF output
        # Create POINT entity for each column on FLOOR_{f}_COLUMNS layer
        point_entity = msp.add_point((col_point.x * OUTPUT_SCALE, col_point.y * OUTPUT_SCALE))
        point_entity.dxf.layer = columns_layer
    
    # --- Add tributary region polygons to DXF output (columns only) ---
    # Iterate through calculated regions with their indices
    for col_idx, region in enumerate(column_regions):
        # Skip empty regions or non-polygon geometries
        if region.is_empty or region.geom_type not in ['Polygon', 'MultiPolygon']:
            continue
        
        # Handle MultiPolygon by taking the largest polygon
        if region.geom_type == 'MultiPolygon':
            region = max(region.geoms, key=lambda p: p.area)
        
        # Extract exterior coordinates from the region
        region_coords = list(region.exterior.coords)
        # Scale coordinates from feet to inches for DXF output
        region_coords_inches = [(x * OUTPUT_SCALE, y * OUTPUT_SCALE) for x, y in region_coords]
        
        # Create layer name for this tributary region using FLOOR_{f}_TRIBUTARY_COL_{i} pattern
        layer_name = f"FLOOR_{floor_idx}_TRIBUTARY_COL_{col_idx}"
        
        # Create the layer if it doesn't exist with color coding
        if layer_name not in dxf_doc.layers:
            col_layer = dxf_doc.layers.add(layer_name)
            col_layer.color = colors.YELLOW  # Yellow for column tributary regions
        
        # Create LWPOLYLINE entity on the tributary column layer
        tributary_polyline = msp.add_lwpolyline(region_coords_inches, close=True)
        tributary_polyline.dxf.layer = layer_name
        
        # If region area exceeds 30 SF, also assign to FLOOR_{f}_LARGE_TRIBUTARY layer
        if column_areas[col_idx] > threshold:
            # Create another polyline on the LARGE_TRIBUTARY layer
            large_tributary_polyline = msp.add_lwpolyline(region_coords_inches, close=True)
            large_tributary_polyline.dxf.layer = large_tributary_layer
    
    # --- Output wall tributary regions to DXF ---
    for wall_data in walls:
        wall_idx = wall_data['wall_index']
        merged_region = wall_data['merged_region']
        total_area = wall_data['total_area']
        
        if merged_region is None or merged_region.is_empty:
            continue
        
        # Create layer for each wall: FLOOR_{f}_WALL_TRIBUTARY_{wall_index}
        wall_layer_name = f"FLOOR_{floor_idx}_WALL_TRIBUTARY_{wall_idx}"
        if wall_layer_name not in dxf_doc.layers:
            wall_trib_layer = dxf_doc.layers.add(wall_layer_name)
            wall_trib_layer.color = colors.RED  # Red for wall tributary regions
        
        # Handle both Polygon and MultiPolygon geometries
        polygons_to_output = []
        if merged_region.geom_type == 'Polygon':
            polygons_to_output.append(merged_region)
        elif merged_region.geom_type == 'MultiPolygon':
            polygons_to_output.extend(merged_region.geoms)
        
        # Output merged wall tributary polygon as LWPOLYLINE entity
        for poly in polygons_to_output:
            if poly.is_empty:
                continue
            
            wall_coords = list(poly.exterior.coords)
            # Scale coordinates from feet to inches for DXF output
            wall_coords_inches = [(x * OUTPUT_SCALE, y * OUTPUT_SCALE) for x, y in wall_coords]
            wall_polyline = msp.add_lwpolyline(wall_coords_inches, close=True)
            wall_polyline.dxf.layer = wall_layer_name
            
            # Apply LARGE_TRIBUTARY layer if wall tributary area exceeds 30 SF
            if total_area > threshold:
                large_wall_polyline = msp.add_lwpolyline(wall_coords_inches, close=True)
                large_wall_polyline.dxf.layer = large_tributary_layer
        
        # Add text annotation showing total tributary area for each wall
        wall_centroid = merged_region.centroid
        wall_area_text = f"{math.ceil(total_area):.0f} SF"
        
        # Scale text position from feet to inches for DXF output
        wall_text_entity = msp.add_text(wall_area_text)
        wall_text_entity.dxf.layer = area_labels_layer
        wall_text_entity.dxf.height = 2.0 * OUTPUT_SCALE
        wall_text_entity.dxf.insert = (wall_centroid.x * OUTPUT_SCALE, wall_centroid.y * OUTPUT_SCALE)
        wall_text_entity.dxf.halign = 1  # center horizontal alignment
        wall_text_entity.dxf.valign = 2  # middle vertical alignment
        wall_text_entity.dxf.align_point = (wall_centroid.x * OUTPUT_SCALE, wall_centroid.y * OUTPUT_SCALE)
    
    # --- Add area text annotations to DXF output (columns only) ---
    # For each column tributary region, calculate centroid coordinates and add text
    for col_idx, region in enumerate(column_regions):
        # Skip empty regions or non-polygon geometries
        if region.is_empty or region.geom_type not in ['Polygon', 'MultiPolygon']:
            continue
        
        # Handle MultiPolygon by taking the largest polygon
        if region.geom_type == 'MultiPolygon':
            region = max(region.geoms, key=lambda p: p.area)
        
        # Calculate centroid coordinates
        centroid = region.centroid
        
        # Format area value rounded up to nearest whole square foot
        area_text = f"{math.ceil(column_areas[col_idx]):.0f} SF"
        
        # Scale text position from feet to inches for DXF output
        # Create TEXT entity at centroid on FLOOR_{f}_AREA_LABELS layer with height 2.0
        text_entity = msp.add_text(area_text)
        text_entity.dxf.layer = area_labels_layer
        text_entity.dxf.height = 2.0 * OUTPUT_SCALE
        text_entity.dxf.insert = (centroid.x * OUTPUT_SCALE, centroid.y * OUTPUT_SCALE)
        
        # Set text alignment to center-middle
        text_entity.dxf.halign = 1  # 1 = center horizontal alignment
        text_entity.dxf.valign = 2  # 2 = middle vertical alignment
        text_entity.dxf.align_point = (centroid.x * OUTPUT_SCALE, centroid.y * OUTPUT_SCALE)

    # --- Add facade segments visualization ---
    fascade_data = floor_plan.get('fascade_data')
    if fascade_data and fascade_data.get('segments'):
        fascade_columns_layer = f'FLOOR_{floor_idx}_FASCADE_COLUMNS'
        fascade_walls_layer = f'FLOOR_{floor_idx}_FASCADE_WALLS'

        if fascade_columns_layer not in dxf_doc.layers:
            column_layer = dxf_doc.layers.add(fascade_columns_layer)
            column_layer.color = 30  # orange

        if fascade_walls_layer not in dxf_doc.layers:
            wall_layer = dxf_doc.layers.add(fascade_walls_layer)
            wall_layer.color = colors.CYAN

        for segment in fascade_data.get('segments', []):
            coords = segment.get('polyline_points', [])
            if not coords or len(coords) < 2:
                continue

            coords_inches = [(x * OUTPUT_SCALE, y * OUTPUT_SCALE) for x, y in coords]
            segment_poly = msp.add_lwpolyline(coords_inches, close=False)

            if segment.get('type') == 'wall':
                segment_poly.dxf.layer = fascade_walls_layer
            else:
                segment_poly.dxf.layer = fascade_columns_layer

            try:
                segment_line = LineString(coords)
                seg_length_ft = segment.get('length', segment_line.length)
                if segment_line.length <= 0:
                    continue

                midpoint = segment_line.interpolate(segment_line.length / 2.0)
                dx = coords[-1][0] - coords[0][0]
                dy = coords[-1][1] - coords[0][1]
                angle_deg = math.degrees(math.atan2(dy, dx)) if not math.isclose(dx, 0.0) or not math.isclose(dy, 0.0) else 0.0

                norm = math.hypot(dx, dy)
                offset_distance = 0.5  # feet
                if norm > 0:
                    offset_x = midpoint.x + (-dy / norm) * offset_distance
                    offset_y = midpoint.y + (dx / norm) * offset_distance
                else:
                    offset_x = midpoint.x
                    offset_y = midpoint.y

                text_point = (offset_x * OUTPUT_SCALE, offset_y * OUTPUT_SCALE)
                length_text = f"{math.ceil(seg_length_ft):.0f} FT"

                length_entity = msp.add_text(length_text)
                length_entity.dxf.layer = segment_poly.dxf.layer
                length_entity.dxf.height = 1.0 * OUTPUT_SCALE
                length_entity.dxf.insert = text_point
                length_entity.dxf.rotation = angle_deg
                length_entity.dxf.halign = 1  # center
                length_entity.dxf.valign = 2
                length_entity.dxf.align_point = text_point
            except Exception:
                continue
    
# --- Calculate and set drawing extents ---
# This ensures the DXF file opens with proper zoom extents
print("\n=== Calculating Drawing Extents ===")

try:
    # Manually calculate extents from all entities
    min_x, min_y = float('inf'), float('inf')
    max_x, max_y = float('-inf'), float('-inf')
    
    entity_count = 0
    for entity in msp:
        try:
            if hasattr(entity, 'get_points'):
                # For polylines
                for point in entity.get_points():
                    min_x = min(min_x, point[0])
                    max_x = max(max_x, point[0])
                    min_y = min(min_y, point[1])
                    max_y = max(max_y, point[1])
                    entity_count += 1
            elif hasattr(entity.dxf, 'insert'):
                # For text entities
                point = entity.dxf.insert
                min_x = min(min_x, point.x)
                max_x = max(max_x, point.x)
                min_y = min(min_y, point.y)
                max_y = max(max_y, point.y)
                entity_count += 1
            elif hasattr(entity.dxf, 'start') and hasattr(entity.dxf, 'end'):
                # For line entities
                min_x = min(min_x, entity.dxf.start.x, entity.dxf.end.x)
                max_x = max(max_x, entity.dxf.start.x, entity.dxf.end.x)
                min_y = min(min_y, entity.dxf.start.y, entity.dxf.end.y)
                max_y = max(max_y, entity.dxf.start.y, entity.dxf.end.y)
                entity_count += 1
            elif hasattr(entity.dxf, 'location'):
                # For point entities
                point = entity.dxf.location
                min_x = min(min_x, point.x)
                max_x = max(max_x, point.x)
                min_y = min(min_y, point.y)
                max_y = max(max_y, point.y)
                entity_count += 1
        except:
            pass
    
    if entity_count > 0 and min_x != float('inf'):
        # Add some padding
        padding = 100
        # Set extents using proper ezdxf Vec3 objects
        from ezdxf.math import Vec3
        dxf_doc.header['$EXTMIN'] = Vec3(min_x - padding, min_y - padding, 0)
        dxf_doc.header['$EXTMAX'] = Vec3(max_x + padding, max_y + padding, 0)
        # Also set PEXTMIN and PEXTMAX for paperspace
        dxf_doc.header['$PEXTMIN'] = Vec3(min_x - padding, min_y - padding, 0)
        dxf_doc.header['$PEXTMAX'] = Vec3(max_x + padding, max_y + padding, 0)
        # Set modelspace viewport
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2
        height = max_y - min_y + 2 * padding
        dxf_doc.set_modelspace_vport(height=height, center=(center_x, center_y))
        print(f"Drawing extents calculated from {entity_count} entities:")
        print(f"  Min: ({min_x:.2f}, {min_y:.2f})")
        print(f"  Max: ({max_x:.2f}, {max_y:.2f})")
        print(f"  Center: ({center_x:.2f}, {center_y:.2f})")
    else:
        print("Warning: Could not calculate extents - no valid entities found")
except Exception as e:
    print(f"Warning: Could not calculate drawing extents: {e}")
    import traceback
    traceback.print_exc()

# --- Save DXF file ---
output_filename = "tributary_output_fixed.dxf"
dxf_doc.saveas(output_filename)

# --- Fix extents in saved file (ezdxf resets them on save) ---
print("\n=== Fixing Drawing Extents in Saved File ===")
try:
    import re
    # Read the saved file with explicit encoding
    with open(output_filename, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Calculate extents values (already calculated above)
    if entity_count > 0 and min_x != float('inf'):
        # Find and replace EXTMIN/EXTMAX values line by line
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Replace EXTMIN
            if line == '$EXTMIN':
                if i + 6 < len(lines) and lines[i+1].strip() == '10':
                    lines[i+2] = f'{min_x - padding}\n'
                    lines[i+4] = f'{min_y - padding}\n'
                    lines[i+6] = '0.0\n'
            
            # Replace EXTMAX
            elif line == '$EXTMAX':
                if i + 6 < len(lines) and lines[i+1].strip() == '10':
                    lines[i+2] = f'{max_x + padding}\n'
                    lines[i+4] = f'{max_y + padding}\n'
                    lines[i+6] = '0.0\n'
            
            # Replace PEXTMIN
            elif line == '$PEXTMIN':
                if i + 6 < len(lines) and lines[i+1].strip() == '10':
                    lines[i+2] = f'{min_x - padding}\n'
                    lines[i+4] = f'{min_y - padding}\n'
                    lines[i+6] = '0.0\n'
            
            # Replace PEXTMAX
            elif line == '$PEXTMAX':
                if i + 6 < len(lines) and lines[i+1].strip() == '10':
                    lines[i+2] = f'{max_x + padding}\n'
                    lines[i+4] = f'{max_y + padding}\n'
                    lines[i+6] = '0.0\n'
            
            i += 1
        
        # Write back with explicit encoding
        with open(output_filename, 'w', encoding='utf-8', newline='\r\n') as f:
            f.writelines(lines)
        
        print(f"✓ Drawing extents fixed in {output_filename}")
        print(f"  Extents: ({min_x - padding:.2f}, {min_y - padding:.2f}) to ({max_x + padding:.2f}, {max_y + padding:.2f})")
except Exception as e:
    print(f"Warning: Could not fix extents in saved file: {e}")
    import traceback
    traceback.print_exc()

# --- Final summary ---
print("\n" + "="*60)
print("=== PROCESSING COMPLETE ===")
print("="*60)
print(f"\nFloor plans processed: {len(floor_plans)}")

for floor_plan in floor_plans:
    floor_idx = floor_plan['index']
    boundary_id = floor_plan['boundary_id']
    num_columns = len(floor_plan['column_points'])
    num_regions = len(floor_plan['regions'])
    areas = floor_plan['areas']
    walls = floor_plan['walls']
    beams = floor_plan.get('beams', [])
    
    # Count large tributary areas for columns only
    large_column_areas = [area for area in areas if area > threshold]
    
    # Count large wall tributary areas
    large_wall_areas = [w['total_area'] for w in walls if w['total_area'] > threshold]
    
    print(f"\nFloor {floor_idx} ({boundary_id}):")
    print(f"  - Columns: {num_columns}")
    print(f"  - Walls: {len(walls)}")
    print(f"  - Beam display segments: {len(beams)}")
    print(f"  - Tributary regions: {num_regions}")
    print(f"  - Large column tributary areas (>{threshold} SF): {len(large_column_areas)}")
    print(f"  - Large wall tributary areas (>{threshold} SF): {len(large_wall_areas)}")
    
    if large_column_areas:
        total_large_area = sum(large_column_areas)
        avg_large_area = total_large_area / len(large_column_areas)
        print(f"  - Total large column tributary area: {math.ceil(total_large_area):.0f} SF")
        print(f"  - Average large column tributary area: {math.ceil(avg_large_area):.0f} SF")
    
    if large_wall_areas:
        total_wall_area = sum(large_wall_areas)
        avg_wall_area = total_wall_area / len(large_wall_areas)
        print(f"  - Total large wall tributary area: {math.ceil(total_wall_area):.0f} SF")
        print(f"  - Average large wall tributary area: {math.ceil(avg_wall_area):.0f} SF")

print(f"\n✓ DXF output file created: {output_filename}")
print(f"✓ All floor plans output to original coordinate locations")
print(f"✓ Layer naming: FLOOR_{{index}}_{{layer_type}}")

# --- Export column load takedown to Excel ---
try:
    export_column_load_takedown(floor_plans, output_filename="column_load_takedown.xlsx")
except Exception as e:
    print(f"\n⚠ WARNING: Excel export failed: {e}")
    print("  DXF output was successful, but Excel file could not be created.")

# --- Export geometry JSON for web frontend ---
try:
    export_geometry_json(floor_plans, output_path="geometry.json")
except Exception as e:
    print(f"\n⚠ WARNING: Geometry JSON export failed: {e}")

print("\n" + "="*60)
print("LAYER COLOR LEGEND")
print("="*60)
print("Input layers (original data):")
print("  - WALL: Cyan")
print("  - BOUNDARY: Blue")
print("  - COLUMNS/POINTS: Magenta")
print("\nOutput layers (tributary regions):")
print("  - FLOOR_*_WALL_TRIBUTARY_*: Red")
print("  - FLOOR_*_TRIBUTARY_COL_*: Yellow")
print("  - FLOOR_*_LARGE_TRIBUTARY: Magenta")
print("  - FLOOR_*_AREA_LABELS: Green")
print("="*60)
