"""Synthetic TakedownExport JSON shaped like a real Revit export (feet, internal coordinates).

Mimics the failure modes seen in architect-authored models: finish floors stacked on the slab,
architectural column wraps around structural columns, floor holes left by column joins, a core
where only some walls are typed/flagged as concrete, partitions, and an unloaded link.
"""
from __future__ import annotations

X_GRID = {"A": 0.0, "B": 24.0, "C": 48.0, "D": 72.0, "E": 96.0}
Y_GRID = {"1": 0.0, "2": 22.0, "3": 44.0, "4": 66.0}
ORIGIN = (1250.0, -340.0)
LEVELS = [("Level 1", 0.0), ("Level 2", 12.0), ("Level 3", 22.5), ("Roof", 33.0)]


def _p(x, y):
    return [ORIGIN[0] + x, ORIGIN[1] + y]


def _rect(x0, y0, x1, y1):
    return [_p(x0, y0), _p(x1, y0), _p(x1, y1), _p(x0, y1)]


def _column_positions():
    for gx, x in X_GRID.items():
        for gy, y in Y_GRID.items():
            if gx == "E" and gy == "4":
                continue
            yield gx, gy, x, y


ELEVATOR = (30.0, 26.0, 38.0, 34.0)
STAIR = (54.0, 26.0, 64.0, 42.0)


def make_fixture() -> dict:
    floors, columns, walls = [], [], []
    slab_outline = [_p(-1, -1), _p(97, -1), _p(97, 45), _p(73, 45), _p(73, 67), _p(-1, 67)]
    for index, (name, elevation) in enumerate(LEVELS[1:], start=1):
        column_holes = [_rect(x - 0.75, y - 0.75, x + 0.75, y + 0.75) for gx, gy, x, y in _column_positions() if gy in "12"]
        floors.append({
            "id": 1000 + index, "source": "host", "typeName": 'Concrete Slab 8"', "familyName": "Floor",
            "levelName": name, "isStructural": index != 2, "thickness": 8 / 12,
            "materials": [{"name": "Concrete, Cast-in-Place gray", "materialClass": "Concrete", "function": "Structure", "width": 8 / 12}],
            "bottomElevation": elevation - 8 / 12, "topElevation": elevation,
            "loops": [slab_outline, _rect(*ELEVATOR), _rect(*STAIR), *column_holes],
        })
        floors.append({
            "id": 2000 + index, "source": "host", "typeName": 'Finish Floor - Tile 1/2"', "familyName": "Floor",
            "levelName": name, "isStructural": False, "thickness": 0.5 / 12, "materials": [],
            "bottomElevation": elevation, "topElevation": elevation + 0.5 / 12,
            "loops": [_rect(2, 2, 40, 20)],
        })
        if name != "Roof":
            floors.append({
                "id": 3000 + index, "source": "host", "typeName": 'Balcony Slab 6"', "familyName": "Floor",
                "levelName": name, "isStructural": False, "thickness": 0.5, "materials": [],
                "bottomElevation": elevation - 0.5, "topElevation": elevation - 0.25,
                "loops": [_rect(97, 10, 103, 34)],
            })

    for (base_name, base), (top_name, top) in zip(LEVELS[:-1], LEVELS[1:]):
        for gx, gy, x, y in _column_positions():
            size = 2.0 if gx in "BCD" and gy in "23" else 1.5
            half = size / 2
            columns.append({
                "id": len(columns) + 5000, "source": "host", "category": "structural",
                "familyName": "Concrete-Rectangular-Column", "typeName": f"{int(size * 12)}x{int(size * 12)}",
                "mark": None, "gridMark": f"{gx}-{gy}" if (gx, gy) != ("C", "3") else "C(-1'-0\")-3",
                "baseLevel": base_name, "topLevel": top_name, "baseElevation": base, "topElevation": top - 8 / 12,
                "xy": _p(x, y), "rotation": 0.0, "slanted": False, "material": "Concrete - 5000 psi",
                "materialClass": "Concrete", "structuralMaterial": "Concrete",
                "footprint": _rect(x - half, y - half, x + half, y + half),
            })
            if gy == "1":
                columns.append({
                    "id": len(columns) + 5000, "source": "host", "category": "architectural",
                    "familyName": "Column Wrap", "typeName": "GWB wrap", "mark": None, "gridMark": None,
                    "baseLevel": base_name, "topLevel": top_name, "baseElevation": base, "topElevation": top,
                    "xy": _p(x, y), "rotation": 0.0, "slanted": False, "material": None, "materialClass": None,
                    "structuralMaterial": "Other",
                    "footprint": _rect(x - half - 0.1, y - half - 0.1, x + half + 0.1, y + half + 0.1),
                })

        def wall(curve, type_name, width, flag, usage, materials, function="Interior"):
            walls.append({
                "id": len(walls) + 8000, "source": "host", "typeName": type_name, "familyName": "Basic Wall",
                "kind": "Basic", "function": function, "structuralFlag": flag, "structuralUsage": usage,
                "width": width, "baseLevel": base_name, "baseElevation": base, "topElevation": top - 8 / 12,
                "structuralMaterial": None, "structuralMaterialClass": None, "materials": materials,
                "curve": [_p(*pt) for pt in curve],
            })

        concrete = [{"name": "Concrete, Cast-in-Place", "materialClass": "Concrete", "function": "Structure", "width": 8 / 12}]
        gwb = [{"name": "Gypsum Wall Board", "materialClass": "Gypsum", "function": "Finish1", "width": 5 / 8 / 12},
               {"name": "Metal Stud Layer", "materialClass": "Metal", "function": "Structure", "width": 3.625 / 12}]
        x0, y0, x1, y1 = ELEVATOR
        wall([(x0 - 0.33, y0 - 0.33), (x1 + 0.33, y0 - 0.33)], 'Concrete Shear Wall 8"', 8 / 12, True, "Shear", concrete)
        wall([(x1 + 0.33, y0 - 0.33), (x1 + 0.33, y1 + 0.33)], 'Concrete Shear Wall 8"', 8 / 12, True, "Shear", concrete)
        wall([(x1 + 0.33, y1 + 0.33), (x0 - 0.33, y1 + 0.33)], 'Generic - 8"', 8 / 12, False, "NonBearing",
             [{"name": "Default Wall", "materialClass": "Generic", "function": "Structure", "width": 8 / 12}])
        wall([(x0 - 0.33, y1 + 0.33), (x0 - 0.33, y0 - 0.33)], 'Generic - 8"', 8 / 12, False, "NonBearing",
             [{"name": "Default Wall", "materialClass": "Generic", "function": "Structure", "width": 8 / 12}])
        sx0, sy0, sx1, sy1 = STAIR
        wall([(sx0 - 0.33, sy0), (sx0 - 0.33, sy1)], 'CMU 8"', 8 / 12, True, "Bearing",
             [{"name": "Masonry - Concrete Masonry Units", "materialClass": "Masonry", "function": "Structure", "width": 7.625 / 12}])
        wall([(sx1 + 0.33, sy0), (sx1 + 0.33, sy1)], 'CMU 8"', 8 / 12, True, "Bearing",
             [{"name": "Masonry - Concrete Masonry Units", "materialClass": "Masonry", "function": "Structure", "width": 7.625 / 12}])
        wall([(4, 10), (20, 10)], 'Interior - 4 7/8" Partition (1-hr)', 4.875 / 12, False, "NonBearing", gwb)
        wall([(10, 50), (10, 64)], 'Interior - 4 7/8" Partition (1-hr)', 4.875 / 12, False, "NonBearing", gwb)
        wall([(40, 27), (40, 41)], 'Interior - 6" Shaft Wall', 6 / 12, False, "NonBearing", gwb)
        wall([(-1, -1), (97, -1)], 'Exterior - Brick on CMU', 14 / 12, False, "NonBearing",
             [{"name": "Brick, Common", "materialClass": "Masonry", "function": "Finish1", "width": 3.625 / 12},
              {"name": "Masonry - Concrete Masonry Units", "materialClass": "Masonry", "function": "Structure", "width": 7.625 / 12}],
             function="Exterior")

    grids = [{"name": g, "source": "host", "points": [_p(x, -8), _p(x, 74)]} for g, x in X_GRID.items()]
    grids += [{"name": g, "source": "host", "points": [_p(-8, y), _p(104, y)]} for g, y in Y_GRID.items()]
    cores = []
    for name, elevation in LEVELS[:-1]:
        cores.append({"kind": "room", "name": "ELEV.", "source": "host", "bbox": [*_p(ELEVATOR[0], ELEVATOR[1]), *_p(ELEVATOR[2], ELEVATOR[3])],
                      "baseElevation": elevation, "topElevation": elevation + 9})
        cores.append({"kind": "room", "name": "STAIR 1", "source": "host", "bbox": [*_p(STAIR[0], STAIR[1]), *_p(STAIR[2], STAIR[3])],
                      "baseElevation": elevation, "topElevation": elevation + 9})
    return {
        "schema": "takedown-revit/1",
        "units": "ft",
        "exporter": {"version": "1.0.0", "revitVersion": "2026", "revitBuild": "fixture"},
        "document": {"title": "fixture", "pathName": "fixture.rvt", "isWorkshared": True},
        "links": [{"name": "MEP.rvt", "savedPath": "S:\\Projects\\MEP.rvt", "status": "NotFound", "loadedFrom": None}],
        "levels": [{"id": i, "name": n, "elevation": e} for i, (n, e) in enumerate(LEVELS)],
        "floors": floors,
        "columns": columns,
        "walls": walls,
        "cores": cores,
        "openings": [],
        "grids": grids,
        "views": [],
        "warnings": ["Link 'MEP.rvt' not loaded (NotFound); its elements are missing. Upload a zip with the host and this file."],
    }


# Expected per supported level: 19 columns; slab = outline - elevator - stair openings.
EXPECTED_COLUMNS = 19
EXPECTED_SLAB_SF = 98 * 68 - 24 * 22 - 8 * 8 - 10 * 16
