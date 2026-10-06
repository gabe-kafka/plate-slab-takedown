"""Layer-suggestion regressions. Run: python -m pytest web/api/_engine/test_inspection_utils.py
or plainly: python web/api/_engine/test_inspection_utils.py"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspection_utils import _sanitize_ai_suggestions, suggest_layers  # noqa: E402

# 1300 Manhattan upload: canonical layer names, closed column footprints.
MANHATTAN_COUNTS = {
    "BOUNDARY": {"LWPOLYLINE": 7},
    "COLS": {"LWPOLYLINE": 671},
    "COL-LABEL": {"TEXT": 655},
    "FLOOR NUMBER": {"TEXT": 7},
    "DATUM": {"POINT": 7},
}


def _metadata(counts):
    return [{"layer": layer, "counts": dict(c)} for layer, c in counts.items()]


def test_heuristic_maps_manhattan_layers():
    s = suggest_layers(MANHATTAN_COUNTS)
    assert s["boundary"] == ["BOUNDARY"]
    assert s["support_point"] == ["COLS"]
    assert s["column_label"] == ["COL-LABEL"]
    assert s["floor_label"] == ["FLOOR NUMBER"]
    assert s["datum"] == ["DATUM"]
    assert s["wall"] == []


def test_ai_filing_cols_under_wall_is_corrected():
    ai = {
        "boundary": ["BOUNDARY"],
        "additional_load": [],
        "wall": ["COLS"],
        "beam": [],
        "support_point": [],
        "column_label": ["COL-LABEL"],
        "floor_label": ["FLOOR NUMBER"],
        "datum": ["DATUM"],
    }
    fixed = _sanitize_ai_suggestions(ai, _metadata(MANHATTAN_COUNTS), suggest_layers(MANHATTAN_COUNTS))
    assert fixed["support_point"] == ["COLS"]
    assert fixed["wall"] == []
    assert fixed["boundary"] == ["BOUNDARY"]


def test_ai_empty_support_falls_back_to_heuristic():
    counts = {"SLAB": {"LWPOLYLINE": 3}, "PIERS": {"CIRCLE": 40}, "WALL": {"LINE": 20}}
    ai = {role: [] for role in ("boundary", "additional_load", "wall", "beam", "support_point", "column_label", "floor_label", "datum")}
    ai["boundary"] = ["SLAB"]
    ai["wall"] = ["WALL", "PIERS"]
    fixed = _sanitize_ai_suggestions(ai, _metadata(counts), suggest_layers(counts))
    assert fixed["support_point"] == ["PIERS"]
    assert fixed["wall"] == ["WALL"]


def test_text_only_column_layer_is_not_a_support():
    counts = {"BOUNDARY": {"LWPOLYLINE": 2}, "COLUMN NUMBER": {"MTEXT": 50}, "POINT": {"POINT": 50}}
    ai = {role: [] for role in ("boundary", "additional_load", "wall", "beam", "support_point", "column_label", "floor_label", "datum")}
    ai["boundary"] = ["BOUNDARY"]
    ai["column_label"] = ["COLUMN NUMBER"]
    ai["support_point"] = ["POINT"]
    fixed = _sanitize_ai_suggestions(ai, _metadata(counts), suggest_layers(counts))
    assert fixed["support_point"] == ["POINT"]
    assert fixed["column_label"] == ["COLUMN NUMBER"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
