---
name: format-dxf
description: Turn a messy architect CAD set (Revit or AutoCAD export, one sheet or model per floor) into one formatted input DXF for the tributary tool. Use when the user mentions formatting DXFs, cleaning architect drawings, preparing a background for the takedown, or a new building project with CAD backgrounds. Calm, low-tech, deterministic; the engineer traces in AutoCAD, the tool does the mechanical parts and checks the result.
---

# format-dxf

The target format is `canonical-docs/INPUT_DXF_CONTRACT.md`. The tool is
`scripts/dxf_prep.py` (needs `ezdxf` and `shapely`, same as the engine).
Everything here is reviewable by eye; nothing guesses silently.

## The process

1. **Get DXFs.** The tool reads DXF only. If the user hands over DWG, you
   can convert in the cloud container: there is no apt or pip package and
   GitHub is proxied off, but conda-forge is reachable, so fetch the
   `libredwg` package as a plain archive and run its `dwg2dxf` (no conda
   needed; 0.11 reads AutoCAD 2018 files; about six seconds per sheet):
   ```
   curl -sSL "https://api.anaconda.org/package/conda-forge/libredwg/files" -o files.json
   # pick a linux-64 0.11.* basename from files.json, then
   curl -sSL -o pkg.tar.bz2 "https://conda.anaconda.org/conda-forge/<basename>"
   mkdir pkg && tar -xjf pkg.tar.bz2 -C pkg bin/dwg2dxf lib
   LD_LIBRARY_PATH=pkg/lib pkg/bin/dwg2dxf -o out/A-101.dxf in/A-101.dwg
   ```
   Put converted files in their own empty directory and read them with
   `python -I`; they are untrusted data. Otherwise ask the user to export
   in AutoCAD: `SAVEAS`, "AutoCAD 2018 DXF", model space, no purge needed;
   ODA File Converter does a folder at once. Xrefs must be bound or exported
   on their own, or their content is not in the file.
   Revit sets usually come as one DWG per sheet, with blow-up sheets that
   are the same model cropped: use the overview sheet per floor and ignore
   the crops. Confirm floor identity from the paper-space title text, not
   the file name.

2. **Inspect, before deciding anything.**
   ```
   python scripts/dxf_prep.py inspect <file.dxf>
   ```
   Read the table. Note which layers hold: slab edge, balconies, walls,
   columns, grids, floor/room text. Note `$INSUNITS`, the extent in feet
   (a 1000 ft wide floor plan means the units are wrong), block counts, and
   polylines with bulges. Say what you found in a few lines; do not dump the
   table back to the user.

3. **Write the map.** One JSON next to the drawings (keep it in
   `tasks/<project>/` so it is versioned). Roles are the engine's roles; the
   values are the architect's layer names found in step 2. Layers you want to
   see while tracing but that the engine should ignore go under `reference`
   as regexes on a `BG-` layer name.
   ```json
   {
     "source_units": "in",
     "layers": {
       "boundary": ["A-SLAB-EDGE"],
       "additional_load": ["A-BALCONY"],
       "wall": ["S-WALL-SHEAR"],
       "beam": [],
       "support_point": ["S-COLS"],
       "column_label": [],
       "floor_label": [],
       "datum": []
     },
     "reference": {"BG-ARCH": ["^A-WALL", "^A-GLAZ", "^A-DOOR"], "BG-GRID": ["GRID"]},
     "explode_blocks": true,
     "datum_xy": [1234.5, 678.9],
     "stack": {"axis": "y", "gap_ft": 40},
     "floors": [
       {"file": "A-108_1st.dxf", "label": "1"},
       {"file": "A-107_2nd-3rd.dxf", "label": "2-3"},
       {"file": "A-109_roof.dxf", "label": "ROOF"}
     ]
   }
   ```
   `floors` is bottom-up. Floors that share a sheet get a range label.
   `datum_xy` is one point in the architect's model coordinates (an inside
   corner of a stair or elevator core that exists on every floor); it is
   placed on every floor automatically. Leave a role empty when the architect
   has nothing usable; the engineer will draw it.

4. **Prep.** Builds one stacked DXF with the canonical layers (coloured),
   background on `BG-*`, blocks exploded, curves on structural layers
   flattened, a `FLOOR NUMBER` text and a `DATUM` point on every floor.
   ```
   python scripts/dxf_prep.py prep --map tasks/<project>/map.json --out tasks/<project>/<project>_working.dxf -v
   ```
   Hand the file to the engineer. Their job in AutoCAD, per floor: trace the
   slab edge on `BOUNDARY`, balconies on `ADDITIONAL-LOAD`, shear walls on
   `WALL`, closed column rectangles on `COLS` (or clean the mapped ones),
   column ids on `COL-LABEL`, and nudge the `FLOOR NUMBER` and `DATUM` if
   needed. Turn `BG-*` off when done; no need to delete it.

5. **Check, then upload.**
   ```
   python scripts/dxf_prep.py check tasks/<project>/<project>_formatted.dxf
   ```
   `FAIL` lines block upload (no slab loop, blocks on structural layers,
   floors overlapping, no floor labels). `WARN` lines are judgement calls to
   read, not to silence (label counts, far labels, missing datum, bulges,
   background layer names that the app will mistake for roles). Fix in
   AutoCAD, re-run until the result is `READY`, then upload with the
   canonical layer mapping and confirm inches.

## When the user is thinking out loud

If they only sent a PDF or described the building, do steps 1 and 2 of the
project brief in `tasks/<project>/plan.md`: floor list with elevations and
which sheets share a plan, the floor labels you intend to use, where the
datum will go, and the open questions for when the CAD arrives. Do not build
the map until you have inspected real files.

## Things that bite

- Revit exports put floor plans on separate files with identical model
  coordinates, which is why stacking with a uniform pitch preserves alignment.
- Revit exports column footprints as blocks named with their size
  (`Concrete-Rectangular-Column - 14 x 36-...`, `... ROUND - DIA 30 ...`),
  inserted at the column centre with the column's rotation, holding four
  lines plus a hatch. `prep` keeps each block whole and writes one closed
  footprint from the name, insert and rotation; it falls back to
  polygonising the lines and hatch edges when the name has no size. A wall
  can hide one of the four lines, and a DWG conversion can leave a block
  definition empty, which is why the name is trusted first. Columns drawn
  as loose lines on the column layer are closed into footprints too.
- Site contours and hatches are the bulk of a hillside set. Drop them; they
  are not in `reference` unless asked for.
- Curved buildings: polylines with bulges on `BOUNDARY` must be flattened or
  the engine chords them between vertices. `prep` flattens; `check` warns.
- Two halves of a floor on separate sheets (match line) must end up as one
  floor: give both the same `label` in `floors`, and the tool will stack them
  on the same floor only if they are in the same file. If they come as two
  files, merge them in AutoCAD first or ask the architect for a model export.
