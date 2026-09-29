---
name: rvt-to-dxf
description: Convert a Revit .rvt model into a per-level takedown DXF (slab, openings, columns, shear walls) with Autodesk Platform Services Design Automation, then validate it with the takedown engine. Use when a user supplies a .rvt/.zip Revit model, asks for Revit to DXF, or needs columns/slab/cores extracted from Revit without a local Revit install.
---

# Revit (.rvt) → takedown DXF

Pipeline: `scripts/rvt2dxf.py` → APS OSS upload → Design Automation (`revit-appbundle/TakedownExport`, Revit 2026) → `takedown.json` → `web/api/_engine/revit_takedown_dxf.py` → `S-*` DXF → `scripts/validate_takedown_dxf.py`.
Design notes, limits and costs: `canonical-docs/REVIT_PIPELINE.md`.

## 1. Preconditions
```bash
python3 -m venv .venv && .venv/bin/pip install -r web/api/requirements.txt matplotlib olefile
test -n "$APS_CLIENT_ID" -a -n "$APS_CLIENT_SECRET" || echo "missing APS creds"
```
If the creds are missing, stop and give the user one line: aps.autodesk.com → Applications → Create application (Server-to-Server; Automation, Model Derivative, Data Management) → copy Client ID/Secret → Cursor Dashboard → Cloud Agents → Secrets → add `APS_CLIENT_ID`, `APS_CLIENT_SECRET`. Never print secret values.

.NET 8 SDK is only needed to (re)build the AppBundle:
```bash
curl -sSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 8.0 --install-dir ~/.dotnet
```

## 2. One-time APS setup
```bash
.venv/bin/python scripts/rvt2dxf.py check   # token, DA nickname, engine Autodesk.Revit+2026, limits, activity present?
.venv/bin/python scripts/rvt2dxf.py setup   # dotnet build → bundle zip → AppBundle + Activity (alias prod)
```
Rerun `setup` after any change to `TakedownExportApp.cs`.

## 3. Convert
```bash
.venv/bin/python scripts/rvt2dxf.py run "MODEL.rvt" --levels "Level 3" --out out/model
# with links: zip host + links (keep relative folders)
.venv/bin/python scripts/rvt2dxf.py run model.zip --host "HOST.rvt" --levels "Level 3" --out out/model
```
- Pass the main `.rvt`, not `.0001.rvt` backups. A purged/detached copy uploads and opens faster.
- Large files: the upload is resumable (rerun the same command) and skips if the object already exists. `--part-mb 64 --workers 6`.
- `--no-views` skips the plan-view DXF export (faster, cheaper). `--timeout-h 3` sets the DA processing limit.
- If the run is interrupted after submit: `rvt2dxf.py status <workitem id from out/workitem.json>`.

Outputs in `--out`: `takedown_<level>.dxf` (feed to the app, units = inches), `report.json`, `validation.json`, `preview.png`, `engine/` (tributary DXF, XLSX, geometry.json), `result/views/*.dxf` (Revit plan views for visual QA), `da-report.txt`.

## 4. Check and report (always)
Read `report.json` for each level:
- `slab_area_sf`, `openings`, `floors_used`/`floors_skipped` (finish floors must be skipped)
- `columns`, `column_sizes`, `column_label_sources`, `columns_outside_slab`
- `shear_walls_by_confidence`, `cores[]`, `walls[].evidence`, `warnings` (ambiguous cores, architectural columns used)
- top-level `links` status: unloaded links mean missing elements. Say so.

Then read `validation.json` (see skill `takedown-dxf-validate`). Report columns found, slab area, openings, shear-wall confidence with evidence, and anything ambiguous.

## Offline / debugging
- `rvt2dxf.py build-dxf takedown.json --levels "Level 3" --out out/` rebuilds the DXF without APS. Use it when tuning scoring in `revit_takedown_dxf.py`.
- Fixture for tests: `tests/rvt2dxf/fixture.py`. Run `pytest tests/rvt2dxf` (fake APS server, no creds needed).
- Workitem `failedInstructions`/`failedLimitProcessingTime`: open `da-report.txt`. Common causes are a model newer than 2026, dialogs on open, or missing links.
- Sheet-only fallback: `rvt2dxf.py md-dwg MODEL.rvt --out out/md` (Model Derivative → DWG → DXF; needs `ODA_FILE_CONVERTER` or `dwg2dxf`).
