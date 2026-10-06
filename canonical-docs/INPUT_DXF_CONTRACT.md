# INPUT_DXF_CONTRACT

What a "formatted DXF" is: the one file the tributary engine accepts. This is
written from the engine code and the demo files, not from the legacy docs, so
it is what the app actually does today. `scripts/dxf_prep.py check` enforces it.

## One file, one modelspace, all floors

- One `.dxf` per project. All floors live in the same modelspace, laid out side
  by side so that no two floors overlap in plan. Overlapping floors polygonize
  together and become one wrong slab.
- Convention: stack floors bottom-up along +Y with a uniform pitch, same X for
  every floor. Every floor then keeps the architect's model coordinates, so one
  datum point is the same offset on every floor.
- Units: inches by default (`$INSUNITS = 1`). The app assumes inches and asks
  for confirmation when a floor reads over 10,000 sf. Feet are accepted when
  the user says so at upload.
- Saved as DXF 2018 (`AC1032`) or 2007+. Older DXF is fine; DWG is not read.

## Canonical layers

The app infers layers from names, but a known set removes the guessing.

| Layer | Role | Entities the engine reads | Rule |
|---|---|---|---|
| `BOUNDARY` | primary slab | closed `LWPOLYLINE` / `POLYLINE`, or `LINE`/`ARC` chains that close within 1 ft | one or more closed loops per floor. A loop inside another loop on the same floor is a separate surface, not an opening. |
| `ADDITIONAL-LOAD` | secondary load zone (balcony, terrace, roof-only plate) | same as above | closed loops. Attribution of area to this zone is reported separately. Internal edges do not count as facade. |
| `WALL` | bearing / shear wall support | `LINE`, `LWPOLYLINE`, `POLYLINE` (open or closed outline) | sampled every 0.1 ft as line support. A wall touching two slab polygons triggers review. |
| `BEAM` | transfer beam linework (display + continuity) | `LINE`, `LWPOLYLINE`, `POLYLINE`, `ARC` | optional. |
| `COLS` | column supports | closed `LWPOLYLINE` footprint, `CIRCLE`, or `POINT` | a footprint must be at least 0.5 ft in its short dimension and 0.25 sf; smaller shapes are skipped, open shapes are flagged as drafting errors. Footprint centroid is the support point. Column labels in the output read the footprint size (`24x24`, `d30`). |
| `COL-LABEL` | column id text | `TEXT`, `MTEXT` | placed within 10 ft of its column (greedy nearest-unused match, radius 10 ft expanding to 35 ft). Over 10 ft is low confidence. Text is normalised (`5-a` becomes `5A`). Unlabeled columns get generated floor-based names. |
| `FLOOR NUMBER` | floor id text | `TEXT`, `MTEXT` | one per floor, placed inside or near that floor so it is the closest label to that floor's centroid. Ranges are expanded: `2-3`, `4-5`, `7-25`. Words sort specially: `ROOF`, `MAIN ROOF`, `BULKHEAD`, `PH`, `EMR1`, `B1`. |
| `DATUM` | per-floor alignment point | `POINT` | exactly one per floor, at the same physical spot on every floor (an inside corner of the stair or elevator core). Used to align floors for column continuity and to attach roof-only plates to their primary slab. |

Anything on another layer is ignored by the engine and is harmless as
background. Do not name background layers with role words (`wall`, `slab`,
`column`, `floor`, `level`, `roof`, `beam`, `point`): the app's layer guesser
matches substrings and will preselect them. `BG-ARCH`, `BG-GRID` are safe.

## Geometry rules the engine imposes

- Blocks are not read on structural layers. Explode them. (`prep` does.)
- Polyline arc bulges are not read; the engine takes vertices only and chords
  the curve. Flatten curves on `BOUNDARY`, `ADDITIONAL-LOAD`, `WALL`, `COLS`
  into short straight segments (0.01 ft chord tolerance). Stand-alone `ARC`
  entities on boundary layers are flattened correctly by the engine.
- Boundary gaps up to 1 ft self-heal; larger gaps leave an open chain and no
  floor.
- A column on the slab edge counts as inside. A column inside an opening is
  dropped. A column touching two floor polygons triggers user review.
- Duplicate column points within 0.25 ft merge into one.
- Curve flattening and healing both work in feet, so a file in inches is
  converted first; the tolerances above are in feet.

## What the app produces from it

- `tributary_output.dxf`: the input geometry plus per-column tributary regions
  and `123 SF` labels, layered `FLOOR_{i}_TRIBUTARY_COL_{j}`.
- `column_load_takedown.xlsx`: floors as rows, column labels as columns,
  rounded-up areas; plus a facade-length sheet.

## Minimal working example

Three closed loops on `BOUNDARY` stacked at Y = 0, 1200, 2400 in; a `TEXT`
`1`, `2-3`, `ROOF` on `FLOOR NUMBER` inside each; closed 24x24 in rectangles on
`COLS` with `MTEXT` `C1`, `C2`... on `COL-LABEL` beside each; one `POINT` on
`DATUM` at the same core corner on each floor. `demo/246_franklin.dxf` is this
pattern with its own layer names.
