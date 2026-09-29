#!/usr/bin/env python3
"""Revit (.rvt) -> takedown DXF via Autodesk Platform Services. No local Revit needed.

  python scripts/rvt2dxf.py check                       # creds, token, DA nickname, engine, limits
  python scripts/rvt2dxf.py setup                       # build + upload AppBundle, create Activity
  python scripts/rvt2dxf.py run MODEL.rvt --levels "Level 3" --out out/
  python scripts/rvt2dxf.py upload MODEL.rvt            # resumable multipart upload only
  python scripts/rvt2dxf.py status WORKITEM_ID
  python scripts/rvt2dxf.py build-dxf takedown.json --levels "Level 3" --out out/
  python scripts/rvt2dxf.py md-dwg MODEL.rvt --out out/ # fallback: Model Derivative sheets -> DWG -> DXF

Needs APS_CLIENT_ID / APS_CLIENT_SECRET (server-to-server app with Automation + Model Derivative).
Linked models: zip the host with its links (relative paths kept) and pass the .zip plus --host NAME.rvt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "web" / "api" / "_engine"
sys.path.insert(0, str(ENGINE_DIR))

import aps_client  # noqa: E402
from aps_client import ApsClient, ApsError  # noqa: E402

BUNDLE_PROJECT = ROOT / "revit-appbundle" / "TakedownExport"
APPBUNDLE = "TakedownExport"
ACTIVITY = "TakedownExport"
ALIAS = "prod"
DEFAULT_ENGINE = "Autodesk.Revit+2026"
SUPPORTED_ENGINES = {2026: "Autodesk.Revit+2026"}
CACHE = Path(os.environ.get("RVT2DXF_CACHE", Path.home() / ".cache" / "rvt2dxf"))


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def detect_revit_version(path: Path) -> int | None:
    """Read 'Format: 20xx' from the RVT's BasicFileInfo OLE stream (needs `pip install olefile`)."""
    if path.suffix.lower() != ".rvt":
        return None
    try:
        import olefile
    except ImportError:
        return None
    with olefile.OleFileIO(str(path)) as ole:
        if not ole.exists("BasicFileInfo"):
            return None
        raw = ole.openstream("BasicFileInfo").read()
    text = raw.decode("utf-16-le", errors="ignore") + raw.decode("latin-1", errors="ignore")
    match = re.search(r"Format:\s*(20\d\d)", text) or re.search(r"Autodesk Revit (20\d\d)", text)
    return int(match.group(1)) if match else None


def engine_for(path: Path, override: str | None) -> str:
    if override:
        return override
    version = detect_revit_version(path)
    if version is None:
        log(f"Revit version not detected (install olefile to check); assuming {DEFAULT_ENGINE}")
        return DEFAULT_ENGINE
    log(f"Model saved in Revit {version}")
    if version > max(SUPPORTED_ENGINES):
        raise SystemExit(f"Model is Revit {version}; rebuild the AppBundle for Autodesk.Revit+{version} first.")
    if version < min(SUPPORTED_ENGINES):
        log(f"Model will be upgraded to {DEFAULT_ENGINE} on open (slower for large files).")
    return SUPPORTED_ENGINES.get(version, DEFAULT_ENGINE)


def build_bundle_zip(out_zip: Path) -> Path:
    dll = BUNDLE_PROJECT / "bin" / "Release" / "net8.0-windows" / "TakedownExport.dll"
    dotnet = shutil.which("dotnet") or str(Path.home() / ".dotnet" / "dotnet")
    if Path(dotnet).exists():
        subprocess.run([dotnet, "build", "-c", "Release", "-nologo", "-v", "q"], cwd=BUNDLE_PROJECT, check=True)
    if not dll.exists():
        raise SystemExit("TakedownExport.dll not built; install the .NET 8 SDK (see skill rvt-to-dxf).")
    bundle = BUNDLE_PROJECT / "TakedownExport.bundle"
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(bundle / "PackageContents.xml", "TakedownExport.bundle/PackageContents.xml")
        zf.write(bundle / "Contents" / "TakedownExport.addin", "TakedownExport.bundle/Contents/TakedownExport.addin")
        zf.write(dll, "TakedownExport.bundle/Contents/TakedownExport.dll")
    return out_zip


def activity_spec(nickname: str, engine: str) -> dict:
    return {
        "id": ACTIVITY,
        "engine": engine,
        "appbundles": [f"{nickname}.{APPBUNDLE}+{ALIAS}"],
        "commandLine": [f'$(engine.path)\\\\revitcoreconsole.exe /al "$(appbundles[{APPBUNDLE}].path)"'],
        "parameters": {
            "rvtFile": {"verb": "get", "localName": "input.rvt", "required": True,
                        "description": "Host .rvt, or a .zip of host + linked models"},
            "params": {"verb": "get", "localName": "params.json", "required": False},
            "result": {"verb": "put", "localName": "result", "zip": True, "required": True},
        },
        "description": "Export floors, structural columns, shear-wall candidates and plan DXFs per level",
    }


def cmd_check(client: ApsClient, args) -> int:
    client.token()
    log("OAuth 2-legged token: OK")
    nickname = client.nickname()
    log(f"Design Automation nickname: {nickname}")
    try:
        engine = client.engine(args.engine or DEFAULT_ENGINE)
        log(f"Engine {engine.get('id')}: available ({engine.get('description', '')[:60]})")
    except ApsError as exc:
        log(f"Engine {args.engine or DEFAULT_ENGINE}: NOT available ({exc.status})")
    try:
        limits = client.service_limits()
        log(f"Service limits: {json.dumps(limits)[:600]}")
    except ApsError as exc:
        log(f"Service limits unavailable ({exc.status})")
    try:
        client.call("GET", f"{aps_client.DA_URL}/activities/{nickname}.{ACTIVITY}+{ALIAS}")
        log(f"Activity {nickname}.{ACTIVITY}+{ALIAS}: present")
    except ApsError:
        log(f"Activity {ACTIVITY}: missing -> run `setup`")
    return 0


def cmd_setup(client: ApsClient, args) -> int:
    engine = args.engine or DEFAULT_ENGINE
    zip_path = Path(args.bundle_zip) if args.bundle_zip else build_bundle_zip(CACHE / "TakedownExport.zip")
    nickname = client.nickname()
    bundle = client.ensure_appbundle(APPBUNDLE, engine, zip_path, ALIAS, "Takedown export for Revit")
    log(f"AppBundle {nickname}.{APPBUNDLE} v{bundle['version']} -> +{ALIAS}")
    activity = client.ensure_activity(activity_spec(nickname, engine), ALIAS)
    log(f"Activity {nickname}.{ACTIVITY} v{activity['version']} -> +{ALIAS} ({engine})")
    return 0


def _progress_printer(label: str):
    state = {"last": 0.0}

    def report(sent: int, total: int) -> None:
        now = time.time()
        if now - state["last"] > 5 or sent == total:
            state["last"] = now
            log(f"{label}: {sent / 2**20:,.0f} / {total / 2**20:,.0f} MB ({100 * sent / max(total, 1):.0f}%)")
    return report


def upload(client: ApsClient, path: Path, args) -> tuple[str, str]:
    bucket = args.bucket or aps_client.default_bucket_key(client.client_id)
    client.ensure_bucket(bucket)
    key = aps_client.safe_object_key(path.name)
    digest = hashlib.sha1(f"{bucket}/{key}/{path.resolve()}".encode()).hexdigest()[:16]
    size_mb = path.stat().st_size / 2**20
    log(f"Uploading {path.name} ({size_mb:,.0f} MB) -> oss://{bucket}/{key} "
        f"({args.part_mb} MB parts, {args.workers} parallel, resumable)")
    client.upload_file(
        bucket, key, path, part_bytes=args.part_mb * 2**20, workers=args.workers,
        state_path=CACHE / "uploads" / f"{digest}.json", progress=_progress_printer("upload"),
    )
    log("Upload complete")
    return bucket, key


def cmd_upload(client: ApsClient, args) -> int:
    bucket, key = upload(client, Path(args.model), args)
    print(json.dumps({"bucket": bucket, "object": key, "objectId": client.object_urn(bucket, key)}))
    return 0


def _on_status(status: dict) -> None:
    progress = status.get("progress") or ""
    log(f"workitem {status.get('id', '')[:8]}: {status.get('status')} {progress}")


def cmd_run(client: ApsClient, args) -> int:
    model = Path(args.model)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    engine = engine_for(model, args.engine)
    nickname = client.nickname()
    activity_id = f"{nickname}.{ACTIVITY}+{ALIAS}"
    try:
        client.call("GET", f"{aps_client.DA_URL}/activities/{activity_id}")
    except ApsError:
        log("Activity missing; running setup")
        cmd_setup(client, args)

    bucket, key = upload(client, model, args)
    auth = {"Authorization": f"Bearer {client.token()}"}
    rvt_arg = {"url": client.object_urn(bucket, key), "verb": "get", "headers": auth}
    if model.suffix.lower() == ".zip":
        if not args.host:
            raise SystemExit("--host NAME.rvt is required for a .zip upload")
        rvt_arg.update({"pathInZip": args.host, "localName": "input"})
    params = {"levels": args.levels or [], "includeLinks": not args.no_links, "exportViews": not args.no_views,
              "includeArchColumns": True, "hostFile": args.host}
    result_key = f"results/{Path(key).stem}-{int(time.time())}.zip"
    workitem = client.submit_workitem(activity_id, {
        "rvtFile": rvt_arg,
        "params": {"url": "data:application/json," + json.dumps(params, separators=(",", ":"))},
        "result": {"url": client.object_urn(bucket, result_key), "verb": "put", "headers": auth},
    }, limit_seconds=int(args.timeout_h * 3600))
    log(f"Workitem {workitem['id']} queued on {engine} (limit {args.timeout_h} h)")
    (out / "workitem.json").write_text(json.dumps({"id": workitem["id"], "bucket": bucket, "result": result_key}))
    status = client.wait_workitem(workitem["id"], timeout_s=args.timeout_h * 3600 + 1800, on_status=_on_status)
    if status.get("reportUrl"):
        (out / "da-report.txt").write_text(client.fetch_text(status["reportUrl"]))
    stats = status.get("stats", {})
    if stats.get("timeDownloadStarted") and stats.get("timeUploadEnded"):
        log(f"DA timing: {json.dumps(stats)}")
    if status.get("status") != "success":
        log(f"Workitem ended {status.get('status')}; see {out / 'da-report.txt'}")
        return 2
    return finish(client, bucket, result_key, out, args.levels)


def finish(client: ApsClient, bucket: str, result_key: str, out: Path, levels) -> int:
    result_zip = client.download_object(bucket, result_key, out / "result.zip")
    with zipfile.ZipFile(result_zip) as zf:
        zf.extractall(out / "result")
    takedown_json = next((out / "result").rglob("takedown.json"))
    return build_dxf(takedown_json, out, levels)


def build_dxf(takedown_json: Path, out: Path, levels) -> int:
    import revit_takedown_dxf

    name = "takedown" + ("_" + "_".join(re.sub(r"\W+", "-", n) for n in levels) if levels else "") + ".dxf"
    report = revit_takedown_dxf.convert(takedown_json, out / name, levels or None)
    (out / "report.json").write_text(json.dumps(report, indent=2))
    for level in report["levels"]:
        log(f"{level['level']}: slab {level['slab_area_sf']:,.0f} sf, {len(level['openings'])} openings, "
            f"{level['columns']} columns {level['column_sizes']}, shear walls {level['shear_walls_by_confidence']}")
        for warning in level["warnings"]:
            log(f"  warning: {warning}")
    for warning in report["exporter_warnings"]:
        log(f"exporter warning: {warning}")
    log(f"DXF: {report['dxf']}")
    validate = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_takedown_dxf.py"), report["dxf"],
         "--json", str(out / "validation.json"), "--plot", str(out / "preview.png"), "--keep", str(out / "engine")],
    )
    return validate.returncode


def cmd_status(client: ApsClient, args) -> int:
    status = client.workitem(args.workitem)
    print(json.dumps(status, indent=2))
    return 0


def cmd_build_dxf(_client, args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    return build_dxf(Path(args.json), out, args.levels)


def dwg_to_dxf(dwg_dir: Path, dxf_dir: Path) -> list[Path]:
    dxf_dir.mkdir(parents=True, exist_ok=True)
    oda = shutil.which("ODAFileConverter") or os.environ.get("ODA_FILE_CONVERTER")
    if oda:
        subprocess.run([oda, str(dwg_dir), str(dxf_dir), "ACAD2018", "DXF", "1", "1", "*.DWG"], check=True)
    elif shutil.which("dwg2dxf"):
        for dwg in dwg_dir.rglob("*.dwg"):
            subprocess.run(["dwg2dxf", "-y", "-o", str(dxf_dir / (dwg.stem + ".dxf")), str(dwg)], check=False)
    else:
        raise SystemExit("No DWG->DXF converter: install ODA File Converter (set ODA_FILE_CONVERTER) or LibreDWG dwg2dxf.")
    return sorted(dxf_dir.rglob("*.dxf"))


def cmd_md_dwg(client: ApsClient, args) -> int:
    model = Path(args.model)
    out = Path(args.out)
    bucket, key = upload(client, model, args)
    urn = aps_client.urn_for(f"urn:adsk.objects:os.object:{bucket}/{key}")
    fmt = {"type": "dwg", "views": ["2d"]}
    if args.export_setting:
        fmt["advanced"] = {"exportSettingName": args.export_setting}
    client.start_translation(urn, [fmt], root_filename=args.host if model.suffix.lower() == ".zip" else None, force=True)
    log("Model Derivative RVT->DWG job started (exports sheets only)")
    manifest = client.wait_manifest(urn, on_status=lambda m: log(f"manifest: {m.get('status')} {m.get('progress')}"))
    if manifest.get("status") != "success":
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
        log("Translation failed; see manifest.json")
        return 2
    dwg_dir = out / "dwg"
    for node in aps_client.iter_derivatives(manifest, output_type="dwg"):
        target = dwg_dir / Path(node["urn"].split("/")[-1]).name
        client.download_derivative(urn, node["urn"], target)
        log(f"downloaded {target.name}")
    for dxf in dwg_to_dxf(dwg_dir, out / "dxf"):
        log(f"DXF: {dxf}")
    log("Sheet DXFs are paper-space layouts; run cleanup before the takedown (see skill rvt-to-dxf).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, model=True):
        if model:
            p.add_argument("model", help=".rvt, or .zip of host + links")
            p.add_argument("--host", help="host .rvt name inside the zip")
        p.add_argument("--bucket")
        p.add_argument("--part-mb", type=int, default=64)
        p.add_argument("--workers", type=int, default=6)
        p.add_argument("--engine", help=f"DA engine (default {DEFAULT_ENGINE}, auto from file version)")
        p.add_argument("--bundle-zip", help="prebuilt TakedownExport.zip (skips dotnet build)")

    common(sub.add_parser("check"), model=False)
    common(sub.add_parser("setup"), model=False)
    common(sub.add_parser("upload"))
    run = sub.add_parser("run")
    common(run)
    run.add_argument("--levels", nargs="*", help="level names (default: all levels with a slab)")
    run.add_argument("--out", default="rvt2dxf-out")
    run.add_argument("--no-views", action="store_true", help="skip plan-view DXF export (faster, cheaper)")
    run.add_argument("--no-links", action="store_true")
    run.add_argument("--timeout-h", type=float, default=3.0)
    status = sub.add_parser("status")
    status.add_argument("workitem")
    build = sub.add_parser("build-dxf")
    build.add_argument("json")
    build.add_argument("--levels", nargs="*")
    build.add_argument("--out", default="rvt2dxf-out")
    md = sub.add_parser("md-dwg")
    common(md)
    md.add_argument("--out", default="rvt2dxf-md")
    md.add_argument("--export-setting", help="named DWG export setup saved in the model")

    args = parser.parse_args()
    if args.command == "build-dxf":
        return cmd_build_dxf(None, args)
    try:
        client = ApsClient()
    except ApsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    handlers = {"check": cmd_check, "setup": cmd_setup, "upload": cmd_upload, "run": cmd_run,
                "status": cmd_status, "md-dwg": cmd_md_dwg}
    try:
        return handlers[args.command](client, args)
    except ApsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
