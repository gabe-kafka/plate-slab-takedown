"""DXF inspection utilities adapted for in-memory processing (no filesystem writes)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, List

import ezdxf
from shapely.ops import unary_union

from geometry_utils import build_floor_surfaces, extract_entity_text, polygons_from_entities

GEOMETRY_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC"}
TEXT_TYPES = {"TEXT", "MTEXT"}

ROLE_KEYWORDS = {
    "boundary": ["boundary", "slab", "outline", "perimeter", "deck", "edge"],
    "additional_load": [
        "additional-load",
        "additional load",
        "additional",
        "load",
        "roof",
        "bulkhead",
        "terrace",
        "balcony",
    ],
    "wall": ["wall", "shear", "core"],
    "beam": ["transfer beam", "beam transfer", "beam", "beams", "girder", "transfer"],
    "support_point": [
        "point",
        "points",
        "pts",
        "support",
        "column",
        "columns",
        "column center",
        "column pt",
        "footprint",
        "pier",
    ],
    "column_label": ["column number", "col no", "column id", "column label", "column"],
    "floor_label": ["floor number", "level", "story", "storey", "floor"],
    "datum": ["datum"],
    "opening": ["opening", "openings", "shaft opening", "void"],
}

# Whole-token aliases (layer names split on non-alphanumerics), e.g. NCS/AIA
# "S-COLS", "COL-IDEN", "S-SLAB-OPNG". Substring matching would let "col" hit
# unrelated names, so these only score as exact tokens.
ROLE_TOKENS = {
    "boundary": ["flor"],
    "support_point": ["col", "cols"],
    "column_label": ["iden", "tag", "col", "cols"],
    "opening": ["opng", "shaft", "hole"],
}

ROLE_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


def inspect_dxf_bytes(payload: bytes, filename: str) -> Dict:
    """Parse DXF from bytes and return draft metadata dict."""
    # ezdxf.read() expects a text stream; write to temp file and use readfile
    tmp = Path(tempfile.mktemp(suffix=".dxf"))
    try:
        tmp.write_bytes(payload)
        doc = ezdxf.readfile(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)

    layer_counts = _layer_counts(doc)
    layer_metadata = _layer_metadata(doc, layer_counts)
    layers = sorted(layer_counts.keys())
    heuristic_suggestions = suggest_layers(layer_counts)
    suggestions, suggestion_source = suggest_layers_with_ai(
        layer_metadata,
        heuristic_suggestions,
    )
    inferred_floor_area_sf = infer_floor_area(doc, suggestions.get("boundary", []))

    return {
        "id": uuid.uuid4().hex,
        "filename": filename,
        "layers": layers,
        "layer_counts": layer_counts,
        "suggestions": suggestions,
        "suggestion_source": suggestion_source,
        "inferred_floor_area_sf": inferred_floor_area_sf,
        "require_unit_confirmation": inferred_floor_area_sf > 10_000.0,
    }


def _layer_counts(doc) -> Dict[str, Dict[str, int]]:
    counts: Dict[str, Dict[str, int]] = {}
    for entity in doc.modelspace():
        layer = getattr(entity.dxf, "layer", "0")
        counts.setdefault(layer, {})
        kind = entity.dxftype()
        counts[layer][kind] = counts[layer].get(kind, 0) + 1
    return counts


def _layer_metadata(doc, layer_counts: Dict[str, Dict[str, int]]) -> List[Dict]:
    metadata = {
        layer: {
            "layer": layer,
            "counts": dict(counts),
            "closed_polylines": 0,
            "open_polylines": 0,
            "circles": counts.get("CIRCLE", 0),
            "points": counts.get("POINT", 0),
            "text_samples": [],
        }
        for layer, counts in layer_counts.items()
    }

    for entity in doc.modelspace():
        layer = getattr(entity.dxf, "layer", "0")
        item = metadata.setdefault(
            layer,
            {
                "layer": layer,
                "counts": {},
                "closed_polylines": 0,
                "open_polylines": 0,
                "circles": 0,
                "points": 0,
                "text_samples": [],
            },
        )
        kind = entity.dxftype()
        if kind == "LWPOLYLINE":
            if bool(entity.closed):
                item["closed_polylines"] += 1
            else:
                item["open_polylines"] += 1
        elif kind == "POLYLINE":
            if bool(entity.is_closed):
                item["closed_polylines"] += 1
            else:
                item["open_polylines"] += 1
        elif kind in TEXT_TYPES and len(item["text_samples"]) < 8:
            text = extract_entity_text(entity)
            if text:
                item["text_samples"].append(text[:80])

    return sorted(metadata.values(), key=lambda item: item["layer"])


def suggest_layers_with_ai(
    layer_metadata: List[Dict],
    fallback_suggestions: Dict[str, List[str]],
) -> tuple[Dict[str, List[str]], str]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return fallback_suggestions, "heuristic"

    try:
        ai_suggestions = _request_ai_layer_suggestions(
            layer_metadata,
            fallback_suggestions,
            api_key,
        )
        sanitized = _sanitize_ai_suggestions(ai_suggestions, layer_metadata)
        if any(sanitized.get(role) for role in ROLE_KEYWORDS):
            return sanitized, "ai"
    except Exception as exc:
        print(f"WARNING: AI layer suggestion failed; using deterministic fallback: {exc}")

    return fallback_suggestions, "heuristic"


def _request_ai_layer_suggestions(
    layer_metadata: List[Dict],
    fallback_suggestions: Dict[str, List[str]],
    api_key: str,
) -> Dict[str, List[str]]:
    model = os.environ.get("OPENAI_LAYER_MODEL", "gpt-5.4-mini")
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You classify DXF layers for a structural concrete slab "
                    "tributary-area app. Return only the requested JSON. "
                    "Use only exact layer names from the provided metadata."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "roles": {
                            "boundary": (
                                "Slab/load boundary closed regions. Multiple layers are valid "
                                "when they represent adjacent loading zones. Exclude column footprints."
                            ),
                            "additional_load": (
                                "Secondary slab/load regions such as roof, bulkhead, terrace, "
                                "balcony, or layers explicitly named additional load. These are "
                                "closed load-bearing regions, not walls, columns, beams, or labels."
                            ),
                            "wall": (
                                "Wall or shear/core support linework. Do not include slab/load "
                                "boundaries, beam layers, or column footprint layers."
                            ),
                            "beam": (
                                "Beam, girder, or transfer-beam linework. Select this for beams "
                                "that visually transfer load between supports. Do not include slab "
                                "boundaries, walls, or column footprint layers."
                            ),
                            "support_point": (
                                "Column/support layers. Include POINT layers and closed column "
                                "footprint layers. Select all column support layers if points and "
                                "footprints are both present."
                            ),
                            "column_label": "Text layer containing column labels or column numbers.",
                            "floor_label": "Text layer containing floor, level, story, or roof labels.",
                            "datum": (
                                "POINT entities marking the user-set alignment datum for "
                                "each floor (one point per floor, typically an inside corner "
                                "of an elevator/stair core). Used to align floors for "
                                "cross-floor column-continuity checks."
                            ),
                            "opening": (
                                "Closed slab openings (shafts, stair and elevator holes) that are "
                                "subtracted from the slab. Not column footprints or slab edges."
                            ),
                        },
                        "layer_metadata": layer_metadata,
                        "deterministic_baseline": fallback_suggestions,
                    },
                    separators=(",", ":"),
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "layer_mapping",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        role: {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                        for role in ROLE_KEYWORDS
                    },
                    "required": list(ROLE_KEYWORDS.keys()),
                },
            }
        },
        "max_output_tokens": 800,
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {detail}") from exc

    text = _extract_response_text(body)
    if not text:
        raise RuntimeError("OpenAI response did not include JSON text")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI response JSON was not an object")
    return parsed


def _extract_response_text(response: Dict) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    fragments = []
    for output_item in response.get("output", []):
        for content_item in output_item.get("content", []):
            text = content_item.get("text")
            if isinstance(text, str):
                fragments.append(text)
    return "".join(fragments).strip()


def _sanitize_ai_suggestions(
    suggestions: Dict,
    layer_metadata: List[Dict],
) -> Dict[str, List[str]]:
    allowed_layers = {item["layer"] for item in layer_metadata}
    layer_counts = {item["layer"]: item.get("counts", {}) for item in layer_metadata}
    sanitized = {}
    for role in ROLE_KEYWORDS:
        values = suggestions.get(role, [])
        if not isinstance(values, list):
            values = []
        deduped = []
        for value in values:
            layer = str(value).strip()
            if layer in allowed_layers and layer not in deduped:
                deduped.append(layer)
        sanitized[role] = deduped[:5]

    _deconflict_opening_suggestions(sanitized)
    boundary_layers = set(sanitized.get("boundary", []))
    additional_load_layers = set(sanitized.get("additional_load", [])) - boundary_layers
    sanitized["additional_load"] = [
        layer
        for layer in sanitized.get("additional_load", [])
        if layer in additional_load_layers
    ]
    additional_load_layers = set(sanitized.get("additional_load", []))
    support_layers = set(sanitized.get("support_point", []))
    beam_layers = set(sanitized.get("beam", []))
    sanitized["wall"] = [
        layer
        for layer in sanitized.get("wall", [])
        if layer not in boundary_layers
        and layer not in additional_load_layers
        and layer not in support_layers
        and layer not in beam_layers
    ]
    wall_layers = set(sanitized.get("wall", []))
    sanitized["beam"] = [
        layer
        for layer in sanitized.get("beam", [])
        if layer not in boundary_layers
        and layer not in additional_load_layers
        and layer not in support_layers
        and layer not in wall_layers
    ]
    _deconflict_datum_suggestions(sanitized)
    _deconflict_label_suggestions(sanitized, layer_counts)
    return sanitized


def suggest_layers(layer_counts: Dict[str, Dict[str, int]]) -> Dict[str, List[str]]:
    suggestions: Dict[str, List[str]] = {}
    layers = list(layer_counts.keys())
    for role, keywords in ROLE_KEYWORDS.items():
        ranked = sorted(
            layers,
            key=lambda layer: _score_layer(role, layer, layer_counts[layer], keywords),
            reverse=True,
        )
        positive = [
            layer
            for layer in ranked
            if _score_layer(role, layer, layer_counts[layer], keywords) > 0
        ]
        if positive:
            if role == "support_point":
                suggestions[role] = [
                    layer
                    for layer in positive
                    if _score_layer(role, layer, layer_counts[layer], keywords) >= 8
                ][:5]
                continue

            top_score = _score_layer(role, positive[0], layer_counts[positive[0]], keywords)
            suggestions[role] = [
                layer
                for layer in positive
                if _score_layer(role, layer, layer_counts[layer], keywords) == top_score
            ][:3]
        else:
            suggestions[role] = []

    # Closed slab/load boundaries and wall linework are both polylines. When a
    # layer is selected as a boundary candidate, do not also preselect it as a
    # wall support layer; users can still opt into a wall layer manually.
    _deconflict_opening_suggestions(suggestions)
    boundary_layers = set(suggestions.get("boundary", []))
    additional_load_layers = set(suggestions.get("additional_load", [])) - boundary_layers
    suggestions["additional_load"] = [
        layer
        for layer in suggestions.get("additional_load", [])
        if layer in additional_load_layers
    ]
    additional_load_layers = set(suggestions.get("additional_load", []))
    support_layers = set(suggestions.get("support_point", []))
    beam_layers = set(suggestions.get("beam", []))
    if boundary_layers:
        suggestions["wall"] = [
            layer for layer in suggestions.get("wall", [])
            if layer not in boundary_layers
        ]
    if additional_load_layers:
        suggestions["wall"] = [
            layer for layer in suggestions.get("wall", [])
            if layer not in additional_load_layers
        ]
    if support_layers:
        suggestions["wall"] = [
            layer for layer in suggestions.get("wall", [])
            if layer not in support_layers
        ]
    if beam_layers:
        suggestions["wall"] = [
            layer for layer in suggestions.get("wall", [])
            if layer not in beam_layers
        ]
    wall_layers = set(suggestions.get("wall", []))
    if wall_layers or boundary_layers or support_layers:
        suggestions["beam"] = [
            layer for layer in suggestions.get("beam", [])
            if layer not in wall_layers
            and layer not in boundary_layers
            and layer not in additional_load_layers
            and layer not in support_layers
        ]
    _deconflict_datum_suggestions(suggestions)
    _deconflict_label_suggestions(suggestions, layer_counts)
    return suggestions


def _deconflict_opening_suggestions(suggestions: Dict[str, List[str]]) -> None:
    """Openings are subtracted from slabs, so they cannot also be slab/load layers."""
    opening_layers = set(suggestions.get("opening", []))
    if not opening_layers:
        return
    for role in ("boundary", "additional_load"):
        suggestions[role] = [
            layer for layer in suggestions.get(role, []) if layer not in opening_layers
        ]


def _deconflict_datum_suggestions(suggestions: Dict[str, List[str]]) -> None:
    """Datum points align floors; they are not structural supports."""
    datum_layers = set(suggestions.get("datum", []))
    if not datum_layers:
        return

    suggestions["support_point"] = [
        layer
        for layer in suggestions.get("support_point", [])
        if layer not in datum_layers
    ]


def _deconflict_label_suggestions(
    suggestions: Dict[str, List[str]],
    layer_counts: Dict[str, Dict[str, int]],
) -> None:
    """Keep a text layer from being both column labels and floor labels."""
    column_layers = list(suggestions.get("column_label", []))
    floor_layers = list(suggestions.get("floor_label", []))
    overlap = set(column_layers) & set(floor_layers)
    if not overlap:
        return

    for layer in overlap:
        preference = _label_layer_preference(layer, layer_counts.get(layer, {}))
        if preference == "floor":
            suggestions["column_label"] = [
                value for value in suggestions.get("column_label", [])
                if value != layer
            ]
        else:
            suggestions["floor_label"] = [
                value for value in suggestions.get("floor_label", [])
                if value != layer
            ]


def _label_layer_preference(layer: str, counts: Dict[str, int]) -> str:
    value = layer.lower()
    column_named = any(token in value for token in ["col", "column"])
    floor_named = any(token in value for token in ["floor", "level", "story", "storey", "roof"])

    if floor_named and not column_named:
        return "floor"
    if column_named and not floor_named:
        return "column"

    text_count = counts.get("TEXT", 0) + counts.get("MTEXT", 0)
    if text_count and text_count <= 80:
        return "floor"
    return "column"


def _score_layer(role: str, layer: str, counts: Dict[str, int], keywords: List[str]) -> int:
    value = layer.lower()
    score = 0
    for keyword in keywords:
        if keyword in value:
            score += 10 if keyword == value else 5
    tokens = set(ROLE_TOKEN_SPLIT.split(value))
    score += 5 * len(tokens & set(ROLE_TOKENS.get(role, [])))

    has_geometry = any(kind in counts for kind in GEOMETRY_TYPES)
    has_text = any(kind in counts for kind in TEXT_TYPES)

    if role == "boundary" and has_geometry:
        score += 3
    if role == "additional_load":
        has_additional_name = any(keyword in value for keyword in keywords)
        if has_additional_name and has_geometry:
            score += 6
        if counts.get("POINT", 0):
            score -= 8
        if has_text and not has_geometry:
            score -= 8
    if role == "wall" and any(kind in counts for kind in {"LINE", "LWPOLYLINE", "POLYLINE"}):
        score += 3
    if role == "beam":
        has_beam_name = any(keyword in value for keyword in keywords)
        if has_beam_name and any(kind in counts for kind in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC"}):
            score += 5
        if counts.get("POINT", 0):
            score -= 8
        if has_text and not has_geometry:
            score -= 8
    if role == "support_point":
        if counts.get("POINT", 0):
            score += 20
        if has_geometry:
            score += 6
        if has_text:
            score -= 8
    if role == "opening" and score > 0:
        if has_geometry:
            score += 3
        if counts.get("POINT", 0) or (has_text and not has_geometry):
            score -= 8
    if role in {"column_label", "floor_label"} and has_text:
        score += 4
    if role in {"column_label", "floor_label"} and not has_text:
        score -= 20
    if role == "column_label" and counts.get("POINT", 0):
        score -= 6
    if role == "column_label" and has_geometry and not has_text:
        score -= 8
    if role == "floor_label" and counts.get("POINT", 0):
        score -= 6
    return score


def infer_floor_area(doc, boundary_layers: List[str]) -> float:
    if not boundary_layers:
        return 0.0

    entities = [
        entity
        for entity in doc.modelspace()
        if getattr(entity.dxf, "layer", "") in set(boundary_layers)
    ]
    loop_polygons = polygons_from_entities(entities, factor=1.0 / 12.0)
    surfaces = build_floor_surfaces(loop_polygons)
    if not surfaces:
        return 0.0

    geometry = unary_union(surfaces)
    return float(geometry.area)
