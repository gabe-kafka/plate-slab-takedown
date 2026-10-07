"""Precompute the web app's demo results so /demo/<slug> opens instantly.

Runs the engine on each bundled demo DXF (web/api/_engine/demo) and writes,
per demo, into web/public/demos/<slug>/:
  result.json                  the /api/process response shape, geometry inline
  tributary_output.dxf         engine output DXF
  column_load_takedown.xlsx    takedown workbook
The demo page fetches result.json first and only runs the engine live when
the file is missing. Re-run after changing a demo DXF or the engine.

usage: python scripts/precompute_demos.py [slug ...]
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "web" / "api" / "_engine" / "demo"
OUT_DIR = ROOT / "web" / "public" / "demos"
RUNNER = ROOT / "scripts" / "run_engine_local.py"

# slug -> (bundled file, source units); mirrors web/src/lib/demos.ts and web/api/upload.py
DEMOS = {
    "358-flatbush": ("INPUT.dxf", "in"),
    "1025-atlantic": ("geom_clean_1.dxf", "in"),
    "356-fulton": ("356_fulton.dxf", "in"),
    "246-franklin": ("246_franklin.dxf", "in"),
    "1300-manhattan": ("1300_manhattan.dxf", "in"),
}


def main(argv):
    slugs = argv or list(DEMOS)
    failed = []
    for slug in slugs:
        filename, units = DEMOS[slug]
        src = DEMO_DIR / filename
        work = Path(tempfile.mkdtemp(prefix=f"demo-{slug}-"))
        print(f"== {slug}: {filename}")
        proc = subprocess.run(
            [sys.executable, str(RUNNER), str(src), "--out", str(work / "engine"), "--units", units, "--quiet"],
            capture_output=True, text=True,
        )
        logs = (proc.stdout + proc.stderr).splitlines()
        eng = work / "engine"
        geometry_path = eng / "geometry.json"
        if proc.returncode != 0 or not geometry_path.exists():
            print("\n".join(logs[-15:]))
            failed.append(slug)
            shutil.rmtree(work, ignore_errors=True)
            continue
        dest = OUT_DIR / slug
        dest.mkdir(parents=True, exist_ok=True)
        geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        artifacts = {"dxf_url": None, "xlsx_url": None}
        dxf = eng / "tributary_output_fixed.dxf"
        if dxf.exists():
            shutil.copyfile(dxf, dest / "tributary_output.dxf")
            artifacts["dxf_url"] = f"/demos/{slug}/tributary_output.dxf"
        xlsx = eng / "column_load_takedown.xlsx"
        if xlsx.exists():
            shutil.copyfile(xlsx, dest / "column_load_takedown.xlsx")
            artifacts["xlsx_url"] = f"/demos/{slug}/column_load_takedown.xlsx"
        warnings = [line for line in logs if "warning" in line.lower()]
        result = {
            "status": "completed",
            "geometry": geometry,
            "artifacts": artifacts,
            "logs": logs[-200:],
            "warnings": warnings,
            "precomputed_from": filename,
        }
        (dest / "result.json").write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
        size = (dest / "result.json").stat().st_size
        print(f"   floors {geometry.get('floor_count')}  result.json {size // 1024} KB  warnings {len(warnings)}")
        shutil.rmtree(work, ignore_errors=True)
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
