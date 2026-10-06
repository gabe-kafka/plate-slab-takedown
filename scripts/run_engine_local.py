#!/usr/bin/env python3
"""Run the tributary engine on a formatted DXF, the way the web API does,
but locally and without blob storage.

    python scripts/run_engine_local.py formatted.dxf --out workdir [--map map.json] [--units in]

Writes INPUT.DXF and job_config.json into `workdir`, runs
extract_dxf_data.py then tributary.py in-process with that cwd, and leaves
tributary_output_fixed.dxf, column_load_takedown.xlsx and geometry.json
there. Exit code 1 on an engine error (NeedsReviewError included).
"""

from __future__ import annotations

import argparse
import io
import json
import multiprocessing
import os
import runpy
import shutil
import sys
import traceback
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT_DIR / "web" / "api" / "_engine"
sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(ROOT_DIR / "scripts"))

from dxf_prep import CANONICAL_LAYERS, load_map  # noqa: E402

SCRIPTS = ["extract_dxf_data.py", "tributary.py"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dxf")
    ap.add_argument("--out", required=True, help="workspace directory (created)")
    ap.add_argument("--map", help="map JSON; canonical layer names are used when omitted")
    ap.add_argument("--units", choices=["in", "ft"])
    ap.add_argument("--quiet", action="store_true", help="only print the last lines of each script")
    args = ap.parse_args()

    mapping = load_map(args.map) if args.map else None
    layers = {
        role: (mapping["layers"][role] if mapping and mapping["layers"].get(role) else [name])
        for role, name in CANONICAL_LAYERS.items()
    }
    units = args.units or (mapping["source_units"] if mapping else "in")

    work = Path(args.out).resolve()
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(args.dxf).resolve(), work / "INPUT.DXF")
    (work / "job_config.json").write_text(json.dumps({"layers": layers, "source_units": units}, indent=2), encoding="utf-8")

    saved_cwd = os.getcwd()
    os.chdir(work)
    try:
        for name in SCRIPTS:
            captured = io.StringIO()
            saved_stdout = sys.stdout
            sys.stdout = captured
            error = None
            try:
                runpy.run_path(str(ENGINE_DIR / name), run_name="__main__")
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    error = f"exit {exc.code}"
            except Exception:
                error = traceback.format_exc()
            finally:
                sys.stdout = saved_stdout
            lines = [ln for ln in captured.getvalue().splitlines() if ln.strip()]
            print(f"===== {name}: {len(lines)} log lines")
            for ln in (lines[-25:] if args.quiet else lines):
                print("  " + ln)
            (work / f"{name}.log").write_text("\n".join(lines), encoding="utf-8")
            if error:
                print(f"ENGINE ERROR in {name}:\n{error[-3000:]}")
                return 1
    finally:
        os.chdir(saved_cwd)

    for artifact in ("tributary_output_fixed.dxf", "column_load_takedown.xlsx", "geometry.json"):
        p = work / artifact
        print(f"{artifact:32s} {'OK ' + str(p.stat().st_size // 1024) + ' KB' if p.exists() else 'MISSING'}")
    return 0


if __name__ == "__main__":
    multiprocessing.set_start_method("fork", force=True)
    sys.exit(main())
