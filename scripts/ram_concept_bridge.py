#!/usr/bin/env python3
"""RAM Concept side of the distillation. Runs on the Windows machine that has
RAM Concept CONNECT Edition and its Python API (`ram_concept`, installed from
Help > Scripting API in RAM Concept; needs that machine's Python, 3.8+).

Two jobs:

  export   read column reactions out of an existing .cpt (the engineer's model)
           and write them in the schema scripts/ram_compare.py expects, with
           the model's settings alongside so structure.json can be matched.

           python ram_concept_bridge.py export model.cpt --out ram_floor.csv

  build    build the same floor RAM-side from the digital twin's geometry and
           tasks/<project>/structure.json, mesh, calc, export reactions. This
           is the calibration generator: same inputs on both sides.

           python ram_concept_bridge.py build result.json --floor 4-5 \
               --structure structure.json --out ram_4-5.csv --save ram_4-5.cpt

Units: the model is set to US units; lengths are feet, forces kips, moments
kip-ft, matching plate_fem.py. Reaction sign: RAM reports column reactions in
its own convention; the exported CSV carries them as read and the comparison
harness works on magnitudes and components, so check one column by hand on
the first run (see tasks/ram_distill/plan.md).

The API names below are the ones Bentley's reader examples and help use
(`Concept.start_concept`, `cad_manager.structure_layer`, `element_layer.
column_elements_below`, `ColumnElement.fixed_near/fixed_far/compressible/
roller/i_factor`). Anything that differs on the installed version raises an
AttributeError; the `--probe` flag prints the available names so the fix is a
one-line rename.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

try:
    from ram_concept.concept import Concept
    from ram_concept.line_segment_2D import LineSegment2D
    from ram_concept.point_2D import Point2D
    from ram_concept.polygon_2D import Polygon2D
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "ram_concept is not importable. Run this with the Python that RAM Concept's "
        "API installer set up (RAM Concept > Help > Scripting API)."
    ) from exc


def probe(obj, label: str) -> None:
    names = [n for n in dir(obj) if not n.startswith("_")]
    print(f"{label}: {', '.join(names)}")


# ---------------------------------------------------------------- reading


def column_rows(model, layer, combo_name: str, probe_names: bool = False) -> list[dict]:
    """One row per column below the slab: location, section, fixity, reaction."""
    element_layer = model.cad_manager.element_layer
    rows = []
    for i, col in enumerate(element_layer.column_elements_below):
        if probe_names and i == 0:
            probe(col, "ColumnElement")
        loc = col.location
        reaction = layer.column_reaction(col)
        if probe_names and i == 0:
            probe(reaction, "ColumnReaction")
        rows.append({
            "label": getattr(col, "name", "") or f"RC{i + 1}",
            "x": round(loc.x, 4), "y": round(loc.y, 4),
            "P": round(reaction.z, 3),
            "Mx": round(reaction.rot_x, 3), "My": round(reaction.rot_y, 3),
            "combo": combo_name,
            "b": col.b, "d": col.d, "angle": col.angle, "height": col.height,
            "fixed_near": col.fixed_near, "fixed_far": col.fixed_far,
            "compressible": col.compressible, "roller": col.roller,
            "bending_stiffness_factor": col.i_factor,
        })
    return rows


def model_settings(model) -> dict:
    """What structure.json has to match. Best effort: unknown attributes are skipped."""
    out: dict = {}
    cm = model.cad_manager
    try:
        slabs = []
        for sa in cm.structure_layer.slab_areas:
            slabs.append({"thickness": sa.thickness, "toc": sa.toc,
                          "concrete": getattr(getattr(sa, "concrete", None), "name", None)})
        out["slab_areas"] = slabs
    except AttributeError as exc:
        out["slab_areas_error"] = str(exc)
    try:
        out["concretes"] = [{"name": c.name, "fc": getattr(c, "fc_final", None),
                             "unit_mass": getattr(c, "unit_mass", None),
                             "E": getattr(c, "user_E_final", None)} for c in model.concretes.concretes]
    except AttributeError as exc:
        out["concretes_error"] = str(exc)
    try:
        loadings = []
        for fl in cm.force_loading_layers:
            loadings.append({"name": fl.name, "type": str(getattr(fl, "loading_type", "")),
                             "area_loads": [{"Fz": al.Fz} for al in fl.area_loads]})
        out["loadings"] = loadings
    except AttributeError as exc:
        out["loadings_error"] = str(exc)
    try:
        out["load_combos"] = [{"name": lc.name,
                               "factors": [{"loading": lf.loading_layer.name,
                                            "standard": lf.standard_load_factor,
                                            "alternate": lf.alternate_envelope_load_factor}
                                           for lf in lc.load_factors]}
                              for lc in cm.load_combo_layers]
    except AttributeError as exc:
        out["load_combos_error"] = str(exc)
    try:
        out["walls"] = [{"thickness": w.thickness, "height": w.height, "fixed_near": w.fixed_near,
                         "fixed_far": w.fixed_far, "shear_wall": w.shear_wall}
                        for w in cm.element_layer.wall_elements_below]
    except AttributeError as exc:
        out["walls_error"] = str(exc)
    return out


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def export(args) -> int:
    concept = Concept.start_concept(headless=not args.show)
    try:
        model = concept.open_file(str(Path(args.cpt).resolve()))
        if args.probe:
            probe(model, "Model")
            probe(model.cad_manager, "CadManager")
        if args.calc:
            model.calc_all()
        layers = {lc.name: lc for lc in model.cad_manager.load_combo_layers}
        layers.update({fl.name: fl for fl in model.cad_manager.force_loading_layers})
        wanted = args.combo or list(layers)
        rows = []
        for name in wanted:
            if name not in layers:
                print(f"no layer named {name!r}; have {list(layers)}", file=sys.stderr)
                continue
            rows.extend(column_rows(model, layers[name], name, probe_names=args.probe and not rows))
        if not rows:
            return 1
        out = Path(args.out)
        write_csv(rows, out)
        settings = model_settings(model)
        out.with_suffix(".settings.json").write_text(json.dumps(settings, indent=2, default=str))
        print(f"wrote {len(rows)} rows to {out} and settings to {out.with_suffix('.settings.json')}")
        return 0
    finally:
        concept.shut_down()


# ---------------------------------------------------------------- building


def _poly(coords) -> Polygon2D:
    pts = [Point2D(float(x), float(y)) for x, y in coords]
    if pts[0].x == pts[-1].x and pts[0].y == pts[-1].y:
        pts = pts[:-1]
    return Polygon2D(pts)


def build(args) -> int:
    data = json.loads(Path(args.result_json).read_text())
    geom = data.get("geometry", data)
    floor = next((f for f in geom["floors"] if str(f["floor_id"]) == str(args.floor)), None)
    if floor is None:
        raise SystemExit(f"floor {args.floor!r} not in {[f['floor_id'] for f in geom['floors']]}")
    st = json.loads(Path(args.structure).read_text())

    concept = Concept.start_concept(headless=not args.show)
    try:
        model = concept.new_model()
        if args.probe:
            probe(model, "Model")
        # US units, feet and kips, to match plate_fem.py
        try:
            from ram_concept.units import Units  # type: ignore
            model.set_units(Units.US)
        except (ImportError, AttributeError) as exc:
            print(f"units: {exc}; set them by hand in the saved model", file=sys.stderr)

        cm = model.cad_manager
        structure = cm.structure_layer
        if args.probe:
            probe(structure, "StructureLayer")

        # concrete
        concrete = model.concretes.add_concrete("slab")
        concrete.fc_initial = concrete.fc_final = st["fc_psi"] / 1000.0  # ksi
        concrete.unit_mass = 0.150  # kcf
        concrete.unit_mass_for_loads = 0.150
        concrete.poissons_ratio = 0.2

        t_ft = st["slab_thickness_in"] / 12.0
        slab_geom = floor["slab_boundary"]
        rings = slab_geom["coordinates"] if slab_geom["type"] == "Polygon" else [r for p in slab_geom["coordinates"] for r in p]
        exterior_rings = [slab_geom["coordinates"][0]] if slab_geom["type"] == "Polygon" else [p[0] for p in slab_geom["coordinates"]]
        hole_rings = [r for r in rings if r not in exterior_rings]
        for ring in exterior_rings:
            sa = structure.add_slab_area(_poly(ring))
            sa.thickness = t_ft
            sa.toc = 0.0
            sa.concrete = concrete
            sa.priority = 1
        for ring in hole_rings:
            structure.add_slab_opening(_poly(ring))

        # columns: footprint size and rotation from the engine's minimum rotated rectangle
        H = st["story_height_ft"]
        fixed_far = st.get("column_far_end", "fixed") == "fixed"
        above = bool(st.get("column_above", True))
        for c in floor["columns"]:
            x, y = c["point"]
            b, d, ang = _section_from_footprint(c.get("footprint"))
            for below in ((True, False) if above and not c.get("ends_here", False) else (True,)):
                col = structure.add_column(Point2D(x, y))
                col.below_slab = below
                col.height = H
                col.b = b
                col.d = d
                col.angle = ang
                col.concrete = concrete
                col.fixed_near = True
                col.fixed_far = fixed_far
                col.roller = False
                col.compressible = True
                col.i_factor = st.get("column_stiffness_modifier", 1.0)
                if hasattr(col, "name"):
                    col.name = c.get("label", "")

        # walls: the prep tool's wall outlines/lines, pinned top and bottom like plate_fem
        wt = st.get("wall_thickness_in", 10.0) / 12.0
        for w in floor.get("walls", []):
            coords = w["wall_line"]["coordinates"]
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                if math.hypot(x1 - x0, y1 - y0) < 0.5:
                    continue
                wall = structure.add_wall(LineSegment2D(Point2D(x0, y0), Point2D(x1, y1)))
                wall.below_slab = True
                wall.height = H
                wall.thickness = wt
                wall.concrete = concrete
                wall.fixed_near = st.get("wall_rotation") == "fixed"
                wall.fixed_far = False
                wall.shear_wall = False

        # loads: self weight is RAM's own; SDL and LL per zone as area loads
        loadings = {fl.name: fl for fl in cm.force_loading_layers}
        if args.probe:
            print("loading layers:", list(loadings))
        dead = loadings.get("Other Dead Loading") or loadings.get("Other Dead Load")
        live = loadings.get("Live (Reducible) Loading") or loadings.get("Live (Unreducible) Loading")
        if dead is None or live is None:
            raise SystemExit(f"could not find default dead/live loading layers in {list(loadings)}")
        zones = floor.get("load_zones") or [{"layer": "BOUNDARY", "boundary": slab_geom}]
        for z in zones:
            zl = st["zones"].get(z["layer"], st["zones"]["BOUNDARY"])
            ring = z["boundary"]["coordinates"][0]
            al = dead.add_area_load(_poly(ring))
            al.Fz = zl["sdl_psf"] / 1000.0  # ksf, down positive in RAM's area-load convention
            al = live.add_area_load(_poly(ring))
            al.Fz = zl["ll_psf"] / 1000.0

        model.generate_mesh()
        model.calc_all()

        layers = {lc.name: lc for lc in cm.load_combo_layers}
        layers.update(loadings)
        wanted = args.combo or [n for n in layers if "Service" in n or "Dead" in n or "Live" in n]
        rows = []
        for name in wanted:
            if name in layers:
                rows.extend(column_rows(model, layers[name], name, probe_names=args.probe and not rows))
        out = Path(args.out)
        write_csv(rows, out)
        out.with_suffix(".settings.json").write_text(json.dumps(model_settings(model), indent=2, default=str))
        if args.save:
            model.save_file(str(Path(args.save).resolve()))
        print(f"wrote {len(rows)} rows to {out}")
        return 0
    finally:
        concept.shut_down()


def _section_from_footprint(fp) -> tuple[float, float, float]:
    """Minimum rotated rectangle of the footprint: (b along local x, d, angle deg)."""
    if not fp:
        return 2.0, 2.0, 0.0
    pts = fp["coordinates"][0]
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    best = None
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        ang = math.atan2(y1 - y0, x1 - x0)
        c, s = math.cos(-ang), math.sin(-ang)
        xs = [x * c - y * s for x, y in pts]
        ys = [x * s + y * c for x, y in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if best is None or w * h < best[0]:
            best = (w * h, w, h, math.degrees(ang))
    _, w, h, ang = best
    return round(w, 4), round(h, 4), round(ang % 180.0, 3)


# ---------------------------------------------------------------- cli


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("cpt")
    e.add_argument("--out", required=True)
    e.add_argument("--combo", action="append", help="layer name(s) to export; default all")
    e.add_argument("--calc", action="store_true", help="run calc_all before reading")
    e.add_argument("--show", action="store_true", help="run with the GUI visible")
    e.add_argument("--probe", action="store_true", help="print the API names available on each object")
    b = sub.add_parser("build")
    b.add_argument("result_json")
    b.add_argument("--floor", required=True)
    b.add_argument("--structure", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--save")
    b.add_argument("--combo", action="append")
    b.add_argument("--show", action="store_true")
    b.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    return export(args) if args.cmd == "export" else build(args)


if __name__ == "__main__":
    sys.exit(main())
