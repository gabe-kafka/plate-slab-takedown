#!/usr/bin/env python3
"""Run a DXF through the takedown engine headlessly and sanity-check the result.

    python scripts/validate_takedown_dxf.py plan.dxf [--units in|ft] [--mapping map.json]
        [--json out.json] [--plot out.png] [--expect-columns N] [--expect-area SF]

Uses the same code path as the web app: inspection_utils (heuristic layer
suggestions; OpenAI only if OPENAI_API_KEY is set) -> extract_dxf_data.py ->
tributary.py. Exit code 1 when a hard check fails.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1] / "web" / "api" / "_engine"
sys.path.insert(0, str(ENGINE_DIR))

PLAUSIBLE_FLOOR_SF = (500.0, 80_000.0)


def run_engine(dxf_path: Path, mapping: dict, units: str, keep: Path | None = None) -> dict:
    workspace = Path(tempfile.mkdtemp(prefix="takedown-"))
    try:
        shutil.copyfile(dxf_path, workspace / "INPUT.DXF")
        (workspace / "job_config.json").write_text(
            json.dumps({"layers": mapping, "source_units": units}, indent=2), encoding="utf-8"
        )
        env = {**os.environ, "PYTHONPATH": str(ENGINE_DIR)}
        logs = []
        for script in ("extract_dxf_data.py", "tributary.py"):
            proc = subprocess.run(
                [sys.executable, str(ENGINE_DIR / script)],
                cwd=workspace, env=env, capture_output=True, text=True,
            )
            logs.extend(line for line in (proc.stdout + proc.stderr).splitlines() if line.strip())
            if proc.returncode != 0:
                return {"ok": False, "failed_step": script, "logs": logs[-40:]}
        geometry = json.loads((workspace / "geometry.json").read_text(encoding="utf-8"))
        if keep:
            keep.mkdir(parents=True, exist_ok=True)
            for name in ("tributary_output_fixed.dxf", "column_load_takedown.xlsx", "geometry.json"):
                if (workspace / name).exists():
                    shutil.copyfile(workspace / name, keep / name)
        return {"ok": True, "geometry": geometry, "logs": logs}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def summarize(geometry: dict) -> dict:
    from shapely.geometry import Point, shape

    floors = []
    for floor in geometry.get("floors", []):
        slab = shape(floor["slab_boundary"]) if floor.get("slab_boundary") else None
        columns = floor.get("columns", [])
        areas = [c.get("area_sf", 0.0) for c in columns]
        wall_area = sum(w.get("area_sf", 0.0) for w in floor.get("walls", []))
        outside = [c["label"] for c in columns if slab is not None and not slab.buffer(0.5).covers(Point(c["point"]))]
        median = statistics.median(areas) if areas else 0.0
        holes = sum(len(p.interiors) for p in getattr(slab, "geoms", [slab])) if slab is not None else 0
        floors.append({
            "floor": floor.get("floor_id"),
            "slab_area_sf": round(slab.area, 1) if slab is not None else 0.0,
            "slab_openings": holes,
            "columns": len(columns),
            "labelled_columns": sum(1 for c in columns if c.get("label") and not str(c["label"]).startswith("UNLABELED")),
            "column_tributary_sf": round(sum(areas), 1),
            "wall_tributary_sf": round(wall_area, 1),
            "walls": len(floor.get("walls", [])),
            "median_column_sf": round(median, 1),
            "outlier_columns": [c["label"] for c in columns if median and c.get("area_sf", 0) > 3 * median],
            "columns_outside_slab": outside,
            "drafting_errors": len(floor.get("drafting_errors", [])),
        })
    return {"floor_count": len(floors), "floors": floors}


def checks(summary: dict, expect_columns: int | None, expect_area: float | None) -> list[dict]:
    results = []

    def add(name, ok, detail, hard=True):
        results.append({"check": name, "ok": bool(ok), "hard": hard, "detail": detail})

    add("floors found", summary["floor_count"] > 0, f"{summary['floor_count']} floor(s)")
    for f in summary["floors"]:
        tag = f"[{f['floor']}]"
        add(f"{tag} columns > 0", f["columns"] > 0, f"{f['columns']} columns")
        lo, hi = PLAUSIBLE_FLOOR_SF
        add(f"{tag} slab area plausible", lo <= f["slab_area_sf"] <= hi,
            f"{f['slab_area_sf']:,.0f} sf (expect {lo:,.0f}-{hi:,.0f}; wrong units if far off)")
        solved = f["column_tributary_sf"] + f["wall_tributary_sf"]
        add(f"{tag} tributary sums to slab", abs(solved - f["slab_area_sf"]) <= 0.02 * max(f["slab_area_sf"], 1),
            f"columns {f['column_tributary_sf']:,.0f} + walls {f['wall_tributary_sf']:,.0f} vs slab {f['slab_area_sf']:,.0f}",
            hard=False)
        add(f"{tag} columns inside slab", not f["columns_outside_slab"], f"outside: {f['columns_outside_slab'][:10]}", hard=False)
        add(f"{tag} no tributary outliers (>3x median)", not f["outlier_columns"], f"{f['outlier_columns'][:10]}", hard=False)
        add(f"{tag} columns labelled", f["labelled_columns"] == f["columns"],
            f"{f['labelled_columns']}/{f['columns']}", hard=False)
    if expect_columns is not None:
        total = sum(f["columns"] for f in summary["floors"])
        add("expected column count", total == expect_columns, f"{total} vs expected {expect_columns}")
    if expect_area is not None:
        total = sum(f["slab_area_sf"] for f in summary["floors"])
        add("expected slab area (±1%)", abs(total - expect_area) <= 0.01 * expect_area, f"{total:,.0f} vs {expect_area:,.0f}")
    return results


def plot(geometry: dict, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from shapely.geometry import shape

    floors = geometry.get("floors", [])
    fig, axes = plt.subplots(1, max(1, len(floors)), figsize=(9 * max(1, len(floors)), 9), squeeze=False)
    for ax, floor in zip(axes[0], floors):
        def draw(geom, **kw):
            for poly in getattr(geom, "geoms", [geom]):
                if poly.geom_type != "Polygon":
                    continue
                ax.fill(*poly.exterior.xy, **kw)
                for ring in poly.interiors:
                    ax.fill(*ring.xy, color="white", zorder=kw.get("zorder", 1) + 0.1)
        slab = shape(floor["slab_boundary"])
        draw(slab, color="#e8eef7", ec="#1f3b73", lw=1.5, zorder=1)
        cmap = plt.get_cmap("tab20")
        for i, col in enumerate(floor.get("columns", [])):
            if col.get("tributary_region"):
                draw(shape(col["tributary_region"]), color=cmap(i % 20), alpha=0.35, ec="#555", lw=0.4, zorder=2)
            if col.get("footprint"):
                draw(shape(col["footprint"]), color="#222", zorder=4)
            x, y = col["point"]
            ax.annotate(f"{col['label']}\n{col['area_sf']:.0f} sf", (x, y), fontsize=6, ha="center", va="bottom", zorder=5)
        for wall in floor.get("walls", []):
            if wall.get("tributary_region"):
                draw(shape(wall["tributary_region"]), color="#c0392b", alpha=0.12, ec="#c0392b", lw=0.4, zorder=2)
            if wall.get("wall_line"):
                xs, ys = zip(*wall["wall_line"]["coordinates"])
                ax.plot(xs, ys, color="#c0392b", lw=2.5, zorder=3)
        ax.set_title(f"{floor.get('floor_id')}: slab {slab.area:,.0f} sf, {len(floor.get('columns', []))} columns")
        ax.set_aspect("equal")
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("--units", choices=["in", "ft"], default="in")
    parser.add_argument("--mapping", type=Path, help="layer mapping JSON (role -> [layers]); default: app suggestions")
    parser.add_argument("--json", type=Path, help="write full report JSON here")
    parser.add_argument("--plot", type=Path, help="write a PNG of slab/columns/tributary regions")
    parser.add_argument("--keep", type=Path, help="copy engine outputs (DXF, XLSX, geometry.json) here")
    parser.add_argument("--expect-columns", type=int)
    parser.add_argument("--expect-area", type=float)
    args = parser.parse_args()

    from inspection_utils import inspect_dxf_bytes

    draft = inspect_dxf_bytes(args.dxf.read_bytes(), args.dxf.name)
    mapping = json.loads(args.mapping.read_text()) if args.mapping else draft["suggestions"]
    run = run_engine(args.dxf, mapping, args.units, keep=args.keep)
    report = {
        "dxf": str(args.dxf),
        "units": args.units,
        "suggestion_source": draft["suggestion_source"],
        "layer_mapping": mapping,
        "layers": draft["layer_counts"],
    }
    if not run["ok"]:
        report.update({"ok": False, "failed_step": run["failed_step"], "logs": run["logs"]})
        print(json.dumps(report, indent=2))
        if args.json:
            args.json.write_text(json.dumps(report, indent=2))
        return 1

    summary = summarize(run["geometry"])
    results = checks(summary, args.expect_columns, args.expect_area)
    report.update({"ok": all(r["ok"] for r in results if r["hard"]), "summary": summary, "checks": results})
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
    if args.plot:
        plot(run["geometry"], args.plot)

    print(f"mapping ({draft['suggestion_source']}): " + json.dumps({k: v for k, v in mapping.items() if v}))
    for f in summary["floors"]:
        print(f"{f['floor']}: {f['columns']} columns ({f['labelled_columns']} labelled), slab {f['slab_area_sf']:,.0f} sf "
              f"({f['slab_openings']} openings), column trib {f['column_tributary_sf']:,.0f} sf, "
              f"walls {f['walls']} ({f['wall_tributary_sf']:,.0f} sf)")
    for r in results:
        mark = "PASS" if r["ok"] else ("FAIL" if r["hard"] else "WARN")
        print(f"  {mark} {r['check']}: {r['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
