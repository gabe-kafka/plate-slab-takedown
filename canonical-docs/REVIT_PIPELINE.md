# REVIT_PIPELINE

## Goal
`.rvt` → per-level takedown DXF with no local Revit. Uses Autodesk Platform Services (APS).

```
.rvt (or .zip host+links) → OAuth 2-legged → OSS bucket (signed S3 multipart) → Design Automation (Revit 2026 engine + TakedownExport AppBundle)
  → result.zip {takedown.json, views/*.dxf} → revit_takedown_dxf.py → S-* layer DXF → existing inspect/extract/tributary engine
```

## Approach chosen: Design Automation + AppBundle (element export)
| | Design Automation for Revit + AppBundle (chosen) | Model Derivative RVT→DWG, then DWG→DXF (fallback) |
|---|---|---|
| What comes out | Revit elements: Floors (top faces + openings), Structural Columns (point, footprint, grid mark), Walls with structural flag/usage/materials, elevator/stair/shaft markers, grids, levels | 2D linework of **sheets only** (views not on sheets are skipped), using the model's DWG export setup |
| Per-level control | Exact: every level, whether or not it has a view or sheet | Only what the architect put on sheets; titleblocks, annotation, hatch, paper-space scale |
| Role of each element | From the category and parameters; no layer guessing | Depends on the export layer table (AIA `S-COLS`, `A-FLOR`…), and then the DXF cleanup heuristics |
| Extra tools | .NET 8 AppBundle (in repo, compiles on Linux) | ODA File Converter or LibreDWG for DWG→DXF; not available on Vercel |
| Cost (Flex) | 2 tokens per processing hour (~$6/h at $3/token). A 1.7 GB model is estimated at 0.3–1 h, about 1–2 tokens | Revit counts as a "complex" job, about 1–1.5 tokens per translation. The free tier has monthly job limits |
| Risk | Custom code runs headless; a model that pops a dialog or has missing links can fail | Output is only as clean as the sheet set; a structural plan is rarely on the architect's sheets |

The Model Derivative route stays available as `rvt2dxf.py md-dwg` for sheet DXFs used in visual QA.

## Extraction rules (`web/api/_engine/revit_takedown_dxf.py`)
- **Slab**: union of Floors at the level, matched by the Floor's level or by top elevation within 1 ft.
  - Finish floors are skipped: tile, topping and similar names, or <3" thick.
  - A floor counts as structural if the Structural checkbox is on, the type name contains CONC/SLAB/CIP/PT, or the material is concrete.
  - Balcony/terrace floors go to `S-BALCONY`.
  - Openings are the top-face inner loops plus shaft openings.
  - Notches and holes from column/floor joins are filled with the column footprint.
- **Columns**: Structural Columns whose base is below the level and whose top is within 1.5 ft of it (they support that slab).
  - Architectural Columns are used only when no structural column is within 1 ft.
  - Label comes from Column Location Mark (off-grid offsets stripped), then Mark, then the nearest grid pair, then `C#`.
  - Footprint is the lowest horizontal solid face; if there is none, a 12x12 square is assumed and a warning is raised.
- **Shear walls** (best effort): each wall gets a score from these signals, and the evidence is kept per wall.

  | Signal | Score |
  |---|---|
  | Structural flag or usage | +3 |
  | Concrete material/type | +3 |
  | CMU | +1 |
  | SHEAR/CORE/STRUCT in type name | +2 |
  | ≥8" thick | +1 |
  | <5" thick | −2 |
  | Within 1.5 ft of an elevator/stair room, stair, elevator family, shaft or slab opening | +2 |
  | Partition/cladding keyword | −3 |
  | Exterior and not concrete | −1 |

  Score ≥7 is high confidence and ≥5 is medium; both go to `S-SHEARWALL`. Non-concrete walls are capped at medium. Score 3–4 is low and goes to `Z-REVIEW-LOWCONF`, which is not mapped. If no wall reaches medium, the report says the cores are ambiguous.
- **Ignored**: partitions, furniture, MEP, annotation, and anything outside the categories above.

## Output DXF (inches, floors side by side, ascending elevation left→right)
| Layer | App role | Content |
|---|---|---|
| `S-SLAB` | boundary | slab outline |
| `S-SLAB-OPNG` | opening (new role, subtracted) | shafts, stairs, elevator holes |
| `S-BALCONY` | additional_load | balcony/terrace floors |
| `S-COLS` | support_point | closed column footprints |
| `S-COLS-IDEN` | column_label | grid mark / mark |
| `S-SHEARWALL` | wall | wall centerlines (high/medium) |
| `S-LEVEL-IDEN` | floor_label | Revit level name |
| `S-DATUM` | datum | same model point on every level |
| `S-GRID`, `S-GRID-TEXT`, `Z-REVIEW-LOWCONF`, `Z-REF-ELEV-STAIR` | – | reference only |

## Large files, links, versions
- **Upload**: OSS direct-to-S3 multipart. Parts are 64 MB (≥5 MB, ≤10,000 parts), up to 25 signed URLs per request (valid 60 min), with 6 parallel PUTs.
  - Progress is checkpointed per part to `~/.cache/rvt2dxf/uploads/`, so a rerun resends only the missing parts.
  - Expired URLs (403) are re-signed. Re-running after success skips the upload when the object size matches.
  - A 1.7 GB file is 27 parts.
  - OSS has no hard object-size cap; trial accounts have 5 GB total storage.
- **Jobs**: the workitem uses OSS URNs, so Design Automation fetches the input and refreshes the token itself; there is no 60-minute URL expiry problem.
  - `limitProcessingTimeSec` defaults to 3 h (`--timeout-h`).
  - Polling backs off from 10 s to 60 s.
  - The DA report is saved to `da-report.txt`.
  - The workitem payload must be ≤16 KB (checked before submit).
- **Web**: the browser uploads directly to S3 (no Vercel body limit). `/api/rvt` requires sign-in and APS env vars. It polls every 15 s, and `/api/rvt_convert` builds the draft from `results/` keys only.
- **Links**: the host is opened detached with all worksets. Unloaded links are loaded from any matching `.rvt` in the upload; to include them, zip the host with its links and pass `--host NAME.rvt`. Every link's status is reported, and missing links are warned about.
- **Revit versions**: the AppBundle targets Revit 2026 (.NET 8). Older files are upgraded on open, which is slower. Newer files need a rebuild for the new engine.
- **Backups**: `NAME.0001.rvt` backups are ignored; upload the main file.
- **Compacting**: purged, detached or compacted copies upload and open faster; element data is unchanged.

## Setup (once)
aps.autodesk.com → Applications → Create application (Server-to-Server, APIs: Automation + Model Derivative + Data Management) → copy Client ID/Secret → Cursor Dashboard → Cloud Agents → Secrets (`APS_CLIENT_ID`, `APS_CLIENT_SECRET`) and Vercel project env (same names) → `python scripts/rvt2dxf.py setup`
