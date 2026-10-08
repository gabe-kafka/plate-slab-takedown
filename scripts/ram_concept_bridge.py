#!/usr/bin/env python3
"""RAM Concept side of the distillation: column reactions out of RAM, in the
schema scripts/ram_compare.py reads. Runs on the Windows machine with RAM
Concept CONNECT (2024 or newer) installed; uses the official localhost API
the way agentic-ram's Concept skills do (attach to a `-apiServerWithGui`
session by port, or start a headless process).

  export   column reactions and settings from the open model (attach) or a .cpt (headless)

      python scripts\\ram_concept_bridge.py export --out ram_4-5.csv            # attached session
      python scripts\\ram_concept_bridge.py export --cpt model.cpt --out ram.csv  # headless, own process

  build    the twin's floor as a new Concept model: slab, columns, walls, SDL
           and LL per zone, mesh, calc, export. Same inputs as plate_fem.py.

      python scripts\\ram_concept_bridge.py build --floor 4-5 ^
          --geometry https://conc-slab-tributary-area-public.vercel.app/demos/1300-manhattan/result.json ^
          --structure tasks\\1300_manhattan\\structure.json --out ram_4-5.csv --save ram_4-5.cpt

Both write <out>.settings.json beside the CSV: units token, loadings, combos,
every column's fixity and stiffness factor, slab areas and concretes, which
is what tasks/<project>/structure.json has to match before comparing.

Units: the API is put in US API units (inches, kips) while it runs; the CSV
is converted to feet, kips and kip-ft. Reaction sign follows RAM's sign
convention as read; check one column by hand on the first run.

`--probe` prints the attribute names of the first column element and its
reaction object, because the reaction call has not been exercised in this
repo yet: if the installed API spells it differently the probe shows the
name to use (see REACTION_METHODS).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

INSTALL = Path(r"C:\Program Files\Bentley\Engineering\RAM Concept")
REACTION_METHODS = ("column_reaction", "get_column_reaction", "reaction")
IN = 12.0


# ---------------------------------------------------------------- API session (agentic-ram pattern)


def _vendor_requests() -> None:
    """RAM Concept's package only needs requests.post(); provide it without a machine-wide install."""
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass
    import types
    from dataclasses import dataclass
    from urllib.error import HTTPError

    @dataclass
    class Response:
        status_code: int
        text: str
        reason: str = ""

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}: {self.reason or self.text}")

    def post(url, *, headers=None, data=None, timeout=None):
        req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Response(r.status, r.read().decode("utf-8"), getattr(r, "reason", ""))
        except HTTPError as e:
            return Response(e.code, e.read().decode("utf-8", "replace"), str(e.reason))

    mod = types.ModuleType("requests")
    mod.post = post
    mod.Response = Response
    sys.modules["requests"] = mod


def api():
    _vendor_requests()
    dirs = sorted(INSTALL.glob("RAM Concept */python"))
    if not dirs:
        raise SystemExit(f"RAM Concept Python API not found under {INSTALL}")
    sys.path.insert(1, str(dirs[-1]))
    from ram_concept.concept import Concept
    from ram_concept.model import Model
    return Concept, Model


def discover_port() -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='Concept.exe'\").CommandLine"],
        capture_output=True, text=True).stdout
    ports = sorted({int(p) for p in re.findall(r"-port\s+(\d+)", out)})
    if len(ports) != 1:
        raise SystemExit(f"Need exactly one API-enabled Concept (-apiServerWithGui); found ports {ports}. "
                         "Pass --port, or start one with agentic-ram's start-ram-concept-api.cmd.")
    return ports[0]


def live_model(port: int | None):
    Concept, Model = api()
    try:
        concept = Concept.attach_to_concept(port or discover_port())
    except TimeoutError:
        raise SystemExit("Concept did not answer. If its Scripting Server window shows Resume, click Resume and rerun.")
    model = Model(concept)
    concept._model = model
    return concept, model


def headless(cpt: str | None):
    Concept, _ = api()
    concept = Concept.start_concept(headless=True, start_timeout_seconds=90, inactivity_timeout_seconds=3600)
    model = concept.open_file(str(Path(cpt).resolve())) if cpt else concept.new_model()
    return concept, model


def probe(obj, label: str) -> None:
    print(f"{label}: " + ", ".join(n for n in dir(obj) if not n.startswith("_")))


# ---------------------------------------------------------------- reading


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def reaction_components(reaction) -> dict:
    """Pull Fz, Mx, My out of whatever the API returns (ColumnReaction-like object or tuple)."""
    if reaction is None:
        return {}
    if isinstance(reaction, (tuple, list)) and len(reaction) >= 5:
        return {"P": _num(reaction[2]), "Mx": _num(reaction[3]), "My": _num(reaction[4])}
    names = {n.lower(): n for n in dir(reaction) if not n.startswith("_")}

    def pick(*cands):
        for c in cands:
            if c in names:
                return _num(getattr(reaction, names[c]))
        return None

    return {"P": pick("z", "fz", "force_z", "axial"),
            "Mx": pick("rot_x", "mx", "moment_x", "rx"),
            "My": pick("rot_y", "my", "moment_y", "ry"),
            "Fx": pick("x", "fx"), "Fy": pick("y", "fy")}


def read_reaction(layer, col):
    for name in REACTION_METHODS:
        fn = getattr(layer, name, None)
        if callable(fn):
            return fn(col)
    cands = [n for n in dir(layer) if "reaction" in n.lower()]
    raise AttributeError(f"no column reaction method on {type(layer).__name__}; candidates: {cands}")


def column_rows(model, layer, layer_name: str, do_probe: bool) -> list[dict]:
    rows = []
    for i, col in enumerate(model.cad_manager.element_layer.column_elements_below):
        reaction = read_reaction(layer, col)
        if do_probe and i == 0:
            probe(col, "ColumnElement")
            probe(reaction, "reaction")
            print("reaction repr:", repr(reaction))
        r = reaction_components(reaction)
        loc = col.location
        rows.append({
            "label": getattr(col, "name", "") or f"RC{i + 1}",
            "x": round(loc.x / IN, 4), "y": round(loc.y / IN, 4),
            "P": round(r.get("P") or 0.0, 3),
            "Mx": round((r.get("Mx") or 0.0) / IN, 3), "My": round((r.get("My") or 0.0) / IN, 3),
            "combo": layer_name,
            "b_in": col.b, "d_in": col.d, "angle": col.angle, "height_in": col.height,
            "fixed_near": col.fixed_near, "fixed_far": col.fixed_far,
            "compressible": col.compressible, "roller": col.roller,
            "bending_stiffness_factor": getattr(col, "i_factor", None),
        })
    return rows


def settings(model) -> dict:
    out: dict = {}
    cm = model.cad_manager

    def grab(key, fn):
        try:
            out[key] = fn()
        except Exception as exc:  # noqa: BLE001 - best effort, every key independent
            out[key + "_error"] = f"{type(exc).__name__}: {exc}"

    grab("units_token", lambda: model.units.get_units())
    grab("signs_token", lambda: model.signs.get_signs())
    grab("desired_element_size_in", lambda: model.calc_options.desired_element_size)
    grab("slab_areas", lambda: [{"thickness_in": s.thickness, "toc_in": s.toc,
                                 "concrete": getattr(getattr(s, "concrete", None), "name", None)}
                                for s in cm.structure_layer.slab_areas])
    grab("concretes", lambda: [{"name": c.name, "fc_final": getattr(c, "fc_final", None),
                                "unit_mass": getattr(c, "unit_mass", None),
                                "user_E_final": getattr(c, "user_E_final", None)} for c in model.concretes.concretes])
    grab("loadings", lambda: [{"name": fl.name, "area_loads": [{"Fz": al.Fz} for al in fl.area_loads]}
                              for fl in cm.force_loading_layers])
    grab("load_combos", lambda: [{"name": lc.name, "factors": [
        {"loading": lf.loading_layer.name, "standard": lf.standard_load_factor,
         "alternate": lf.alternate_envelope_load_factor} for lf in lc.load_factors]}
        for lc in cm.load_combo_layers])
    grab("walls_below", lambda: [{"thickness_in": w.thickness, "height_in": w.height, "fixed_near": w.fixed_near,
                                  "fixed_far": w.fixed_far, "shear_wall": w.shear_wall, "compressible": w.compressible}
                                 for w in cm.element_layer.wall_elements_below])
    return out


def write_out(rows: list[dict], model, out: Path) -> None:
    if not rows:
        raise SystemExit("no rows read")
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    out.with_suffix(".settings.json").write_text(json.dumps(settings(model), indent=2, default=str))
    print(f"wrote {len(rows)} rows to {out}; settings in {out.with_suffix('.settings.json')}")


def result_layers(model) -> dict:
    cm = model.cad_manager
    layers = {lc.name: lc for lc in cm.load_combo_layers}
    layers.update({fl.name: fl for fl in cm.force_loading_layers})
    return layers


def with_api_units(model):
    """Switch to US API units (inches, kips) for the duration; returns a restore callable."""
    units = model.units
    saved = units.get_units()
    units.set_US_API_units()
    return lambda: units.set_units(saved)


# ---------------------------------------------------------------- commands


def cmd_export(a) -> int:
    attached = a.cpt is None
    concept, model = headless(a.cpt) if a.cpt else live_model(a.port)
    try:
        if a.probe:
            probe(model, "Model")
            probe(model.cad_manager, "CadManager")
        if a.calc:
            if attached:
                raise SystemExit("--calc on an attached session rewrites the engineer's open model; use --cpt on a copy")
            model.calc_all(timeout_seconds=3600)
        restore = with_api_units(model)
        try:
            layers = result_layers(model)
            wanted = a.combo or list(layers)
            print("result layers:", list(layers))
            rows = []
            for name in wanted:
                if name not in layers:
                    print(f"no layer named {name!r}", file=sys.stderr)
                    continue
                rows.extend(column_rows(model, layers[name], name, a.probe and not rows))
            write_out(rows, model, Path(a.out))
        finally:
            restore()
        return 0
    finally:
        if not attached:
            concept.shut_down()


def load_geometry(src: str) -> dict:
    if src.startswith("http://") or src.startswith("https://"):
        with urllib.request.urlopen(src, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    else:
        data = json.loads(Path(src).read_text(encoding="utf-8"))
    return data.get("geometry", data)


def _poly2d(coords, Point2D, Polygon2D):
    pts = [Point2D(float(x) * IN, float(y) * IN) for x, y in coords]
    if pts[0].x == pts[-1].x and pts[0].y == pts[-1].y:
        pts = pts[:-1]
    return Polygon2D(pts)


def _mrr(fp) -> tuple[float, float, float]:
    """Minimum rotated rectangle of a footprint polygon: (b, d) in inches, angle degrees."""
    if not fp:
        return 24.0, 24.0, 0.0
    pts = fp["coordinates"][0]
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    best = None
    for i in range(len(pts)):
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
        ang = math.atan2(y1 - y0, x1 - x0)
        c, s = math.cos(-ang), math.sin(-ang)
        xs = [x * c - y * s for x, y in pts]
        ys = [x * s + y * c for x, y in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if best is None or w * h < best[0]:
            best = (w * h, w, h, math.degrees(ang))
    _, w, h, ang = best
    return round(w * IN, 3), round(h * IN, 3), round(ang % 180.0, 3)


def _set(obj, **props) -> list[str]:
    """Set attributes that exist; return the names that do not (reported, not fatal)."""
    missing = []
    for k, v in props.items():
        if hasattr(obj, k):
            setattr(obj, k, v)
        else:
            missing.append(k)
    return missing


def cmd_build(a) -> int:
    geom = load_geometry(a.geometry)
    floor = next((f for f in geom["floors"] if str(f["floor_id"]) == str(a.floor)), None)
    if floor is None:
        raise SystemExit(f"floor {a.floor!r} not in {[f['floor_id'] for f in geom['floors']]}")
    st = json.loads(Path(a.structure).read_text())

    concept, model = headless(None)
    try:
        from ram_concept.line_segment_2D import LineSegment2D
        from ram_concept.model import DesignCode, StructureType
        from ram_concept.point_2D import Point2D
        from ram_concept.polygon_2D import Polygon2D
        from ram_concept.slab_area import SlabAreaBehavior

        model.setup_new_model(DesignCode.ACI318_19US, StructureType.ELEVATED)
        model.units.set_US_user_units()
        restore = with_api_units(model)
        missing: dict[str, list[str]] = {}

        concrete = model.concretes.concretes[0]
        missing["concrete"] = _set(concrete, fc_final=st["fc_psi"] / 1000.0, fc_initial=st["fc_psi"] / 1000.0,
                                   unit_mass=0.150 / 1728.0, poissons_ratio=0.2)
        cm = model.cad_manager
        structure = cm.structure_layer
        if a.probe:
            probe(structure, "StructureLayer")
            probe(cm, "CadManager")

        # slab
        default_slab = cm.default_slab_area
        missing["slab"] = _set(default_slab, thickness=st["slab_thickness_in"], toc=0.0,
                               behavior=SlabAreaBehavior.TWO_WAY_SLAB, concrete=concrete, priority=1, r_axis=0.0)
        sb = floor["slab_boundary"]
        polys = [sb["coordinates"]] if sb["type"] == "Polygon" else sb["coordinates"]
        for rings in polys:
            structure.add_slab_area(_poly2d(rings[0], Point2D, Polygon2D))
            for hole in rings[1:]:
                structure.add_slab_opening(_poly2d(hole, Point2D, Polygon2D))

        # columns
        H = st["story_height_ft"] * IN
        far_fixed = st.get("column_far_end", "fixed") == "fixed"
        above = bool(st.get("column_above", True))
        default_col = cm.default_column
        missing["column_defaults"] = _set(default_col, height=H, concrete=concrete, fixed_near=True,
                                          fixed_far=far_fixed, roller=False, compressible=True,
                                          i_factor=float(st.get("column_stiffness_modifier", 1.0)))
        n_cols = 0
        for c in floor["columns"]:
            x, y = c["point"]
            b, d, ang = _mrr(c.get("footprint"))
            legs = (True, False) if above and not c.get("ends_here", False) else (True,)
            for below in legs:
                _set(default_col, below_slab=below, b=b, d=d, angle=ang)
                col = structure.add_column(Point2D(x * IN, y * IN))
                m = _set(col, name=c.get("label", ""))
                missing.setdefault("column", m)
                n_cols += 1

        # walls: pinned top and bottom unless structure.json says fixed, like plate_fem.py
        default_wall = cm.default_wall
        missing["wall_defaults"] = _set(default_wall, below_slab=True, compressible=True, concrete=concrete, height=H,
                                        thickness=float(st.get("wall_thickness_in", 10.0)),
                                        fixed_near=st.get("wall_rotation") == "fixed", fixed_far=False,
                                        shear_wall=False, use_specified_LLR_parameters=False)
        n_walls = 0
        for w in floor.get("walls", []):
            coords = w["wall_line"]["coordinates"]
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                if math.hypot(x1 - x0, y1 - y0) < 0.5:
                    continue
                structure.add_wall(LineSegment2D(Point2D(x0 * IN, y0 * IN), Point2D(x1 * IN, y1 * IN)))
                n_walls += 1

        # loads: Concept adds slab self weight itself; SDL and LL per zone as area loads (ksi: kip/in^2)
        loadings = {fl.name: fl for fl in cm.force_loading_layers}
        print("loading layers:", list(loadings))
        dead = next((fl for n, fl in loadings.items() if "other dead" in n.lower()), None)
        live = next((fl for n, fl in loadings.items() if "live" in n.lower() and "reducible" in n.lower()
                     and "unreducible" not in n.lower()), None) \
            or next((fl for n, fl in loadings.items() if "live" in n.lower()), None)
        if dead is None or live is None:
            raise SystemExit(f"could not find the default dead and live loading layers in {list(loadings)}")
        zones = floor.get("load_zones") or [{"layer": "BOUNDARY", "boundary": sb}]
        for z in zones:
            zl = st["zones"].get(z["layer"], st["zones"]["BOUNDARY"])
            ring = z["boundary"]["coordinates"][0]
            al = dead.add_area_load(_poly2d(ring, Point2D, Polygon2D))
            missing.setdefault("area_load", _set(al, Fz=-zl["sdl_psf"] / 1000.0 / 144.0))
            al = live.add_area_load(_poly2d(ring, Point2D, Polygon2D))
            _set(al, Fz=-zl["ll_psf"] / 1000.0 / 144.0)

        _set(model.calc_options, desired_element_size=float(st.get("mesh_edge_ft", 1.5)) * IN)
        print(f"built: {n_cols} column legs, {n_walls} wall segments, {len(zones)} load zones; "
              f"attributes not on this API version: { {k: v for k, v in missing.items() if v} }")

        t0 = time.time()
        model.generate_mesh()
        print(f"meshed in {time.time() - t0:.0f}s: {len(cm.element_layer.slab_elements)} slab elements")
        t0 = time.time()
        model.calc_all(timeout_seconds=3600)
        print(f"calc in {time.time() - t0:.0f}s")

        layers = result_layers(model)
        print("result layers:", list(layers))
        wanted = a.combo or list(layers)
        rows = []
        for name in wanted:
            if name in layers:
                rows.extend(column_rows(model, layers[name], name, a.probe and not rows))
        write_out(rows, model, Path(a.out))
        restore()
        if a.save:
            Path(a.save).parent.mkdir(parents=True, exist_ok=True)
            model.save_file(str(Path(a.save).resolve()))
            print(f"saved {a.save}")
        return 0
    finally:
        concept.shut_down()


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--cpt", help="open this file in a headless process; default: attach to the running API session")
    e.add_argument("--port", type=int, help="API port of the running Concept (default: discover)")
    e.add_argument("--out", required=True)
    e.add_argument("--combo", action="append", help="result layer name(s); default all")
    e.add_argument("--calc", action="store_true", help="calc_all first (headless --cpt only)")
    e.add_argument("--probe", action="store_true")
    b = sub.add_parser("build")
    b.add_argument("--geometry", required=True, help="result.json / geometry.json path or URL")
    b.add_argument("--floor", required=True)
    b.add_argument("--structure", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--save")
    b.add_argument("--combo", action="append")
    b.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    return cmd_export(a) if a.cmd == "export" else cmd_build(a)


if __name__ == "__main__":
    sys.exit(main())
