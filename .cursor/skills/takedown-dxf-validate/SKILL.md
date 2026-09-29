---
name: takedown-dxf-validate
description: Validate any DXF against the column-load-takedown engine headlessly - layer mapping the app would suggest, columns found, slab area and openings, tributary sums, outliers, labels - plus a PNG preview. Use after producing or receiving a DXF (Revit export, cleanup output, client file) and before telling a user the takedown is right.
---

# Validate a DXF for the takedown app

```bash
.venv/bin/python scripts/validate_takedown_dxf.py plan.dxf [--units in|ft] [--mapping map.json] \
    --json out/validation.json --plot out/preview.png --keep out/engine [--expect-columns N] [--expect-area SF]
```
This runs the same path as the web app: `inspection_utils.inspect_dxf_bytes` (heuristic suggestions, or OpenAI if `OPENAI_API_KEY` is set) → `extract_dxf_data.py` → `tributary.py` in a temp workspace. The exit code is 1 on a hard failure.

## Checks
| Check | Hard? | Meaning when it fails |
|---|---|---|
| floors found / columns > 0 | yes | wrong layer mapping, or column geometry not closed |
| slab area plausible (500–80k sf per floor) | yes | wrong units (try `--units ft`/`in`), or the page border was taken as the slab |
| tributary sums to slab (±2%) | no | the solve lost area, e.g. columns outside the slab or walls not on the slab |
| columns inside slab | no | stray columns from another level or a link |
| outliers (>3× median) | no | missing supports (unmodeled shear wall or column) |
| columns labelled | no | the label layer is not mapped or the labels are too far away |

`summary.floors[]` gives per floor `slab_area_sf` (includes additional-load zones), `slab_openings`, `columns`, `labelled_columns`, `column_tributary_sf`, `wall_tributary_sf`, `median_column_sf` and the outlier lists.

## Mapping
- Without `--mapping`, the app's own suggestion is used. This tests what a user would see.
- Pass `--mapping` (role → [layers]) to test an intended mapping.
- Roles: `boundary`, `additional_load`, `opening` (subtracted from slabs), `wall`, `beam`, `support_point`, `column_label`, `floor_label`, `datum`.
- NCS/AIA aliases are recognised as whole tokens: `COL`/`COLS`/`S-COLS`, `IDEN`, `FLOR`, `OPNG`/`SHAFT`.

## Report back
Per floor, give the column count (labelled/total), slab sf, openings, and column + wall tributary sf. List the WARN/FAIL lines and look at `preview.png`: slab fill, black column footprints, coloured tributary regions, red walls.

Regression baseline for the demos in `web/api/_engine/demo/` (heuristic mapping):

| Demo | Columns | Slab sf | Column tributary sf |
|---|---|---|---|
| 356 Fulton | 542 | 187,495 | 113,916 |
| 246 Franklin | 83 | 19,704 | 12,185 |
| INPUT | 116 | 29,799 | 19,533 |
| geom_clean_1 | 175 | 42,371 | 28,243 |
