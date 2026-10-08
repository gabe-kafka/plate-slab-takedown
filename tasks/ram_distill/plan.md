# Distilling RAM Concept's FEM — plan

Goal: gravity unbalanced moments into columns within 10% of RAM Concept, from
the digital twin's geometry alone, so the app can report them the moment a
formatted DXF is uploaded. RAM is the teacher: we run both on the same floors,
measure, and fix whatever assumption explains the residual.

## Why this is tractable

RAM Concept's gravity model of a flat plate is a single-floor plate FE with
columns as rotational and axial springs (far end fixed by default) and walls
as line supports. Its column reactions (P, Mx, My per load case) are exactly
the unbalanced moment we want, and the CONNECT edition exposes everything
through its Python API, so hundreds of calibration cases are scriptable.

## What RAM Concept actually does (from Bentley's help)

- Slab: Robert Cook's 1972 hybrid plate element, triangles and quads, five
  DOF per node (bending plus in-plane). Ours is a DKT bending-only triangle;
  for gravity on a flat plate the in-plane DOFs matter only where walls or
  restrained columns pull the slab sideways, so expect the residual to show
  up near the cores first.
- Columns: `fixed_near` and `fixed_far` (moment connection at slab and far
  end, else pinned), `roller` (zero horizontal shear), `compressible`
  (axial by Hooke's law, default on) and a bending stiffness factor
  `i_factor` (help suggests 0.5 at edge columns expected to crack). Our
  springs map one to one: far end fixed = 4EI/L, pinned = 3EI/L, axial
  EA/L, modifier = `i_factor`.
- Walls: same fixities, no stiffness factor, `shear_wall` locks the slab
  horizontally.
- Reactions: column reactions per loading or load-combo layer; when a column
  above and below share a location the report sums them, so the export reads
  `column_elements_below` only.

## How RAM is driven

RAM Concept runs only on the Windows machine with the licence; this repo's
cloud sessions cannot open it. `scripts/ram_concept_bridge.py` runs there,
following agentic-ram's Concept skills (official localhost API, attach by
port to a `-apiServerWithGui` window, never calc or save an attached
session, headless process for anything that writes). The Claude Code skill
`ram-reactions` in this repo walks the steps:

- `export` reads column reactions and settings from the open model or a
  `.cpt` copy.
- `build` makes the twin's floor as a new model from the published
  geometry (`/demos/<slug>/result.json` on the live site), meshes, calcs,
  exports.
- `--probe` prints attribute names, because the reaction call itself has
  not been exercised in agentic-ram yet; the first run tells us its name.

Then `scripts/ram_compare.py`. When proven, the script moves to agentic-ram
as `ram-concept/skills/export-ram-concept-column-reactions/`.

## What we build

1. `scripts/plate_fem.py`: thin-plate FE (DKT triangles, Batoz 1980) on the
   engine's floor geometry. Mesh with Shewchuk's `triangle` on the slab
   polygon (openings as holes), column footprints inserted as rigid patches
   tied to the column centre node, wall polylines inserted as segments and
   pinned. Column springs: `4EI/L` above plus below (far end fixed) or
   `3EI/L` (pinned), `EA/L` axial, with stiffness modifiers. Loads: self
   weight plus SDL and LL per load zone. Output: per-column P, Mx, My per
   load case and combination, CSV + JSON.
2. `scripts/ram_compare.py`: joins our CSV with a RAM column-reaction export
   (by label or nearest xy) and reports the error distribution.
3. A structure file per project (`tasks/<project>/structure.json`): slab
   thickness per zone, f'c, story heights, loads, modifiers. Everything the
   DXF contract does not carry. These are the inputs that must match RAM's.

## What we measure

- Per column: `|M_ours − M_ram| / |M_ram|` on the resultant unbalanced
  moment, and on each component, per load case.
- Pass: within 10% for columns whose RAM moment exceeds 20% of the floor's
  median; absolute tolerance (10% of that median) below that. Interior
  columns on regular bays are a difference of two near-equal spans, so a
  percentage target there is noise in any software.
- Report separately: edge, corner, interior, columns within one span of a
  wall, columns on the curved bar.

## Calibration order (each is a documented RAM setting, not a fudge factor)

1. Mesh density and element type (RAM uses thick-plate elements; at
   span/thickness over 20 the difference is small, but check).
2. Column far-end fixity and column stiffness modifier.
3. Slab stiffness modifier and whether RAM's self weight uses the same unit weight.
4. Rigid column zone: footprint tied rigidly vs point support.
5. Wall modelling: pinned line vs wall rotational stiffness vs wall as shell.
6. Live load patterning (RAM only patterns when pattern areas are defined;
   start with none on both sides).
7. Openings and drops, if the RAM model has them.

Stop when the residual is explained; a fitted multiplier means the element
formulation is wrong, not the constant.

## Steps

- [x] Prototype solver verified against closed-form plates (Timoshenko).
- [x] Run on 1300 Manhattan floor `4-5` from the precomputed demo geometry.
- [x] Comparison harness with a defined RAM export CSV schema.
- [x] RAM-side script (`scripts/ram_concept_bridge.py`): export reactions
      from an existing `.cpt`, or build the floor from the twin and calc.
- [ ] On the RAM machine: run `export` on the engineer's model, or `build`
      on floor 4-5; copy RAM's settings into `structure.json`.
- [ ] First comparison; work the calibration list above.
- [ ] Repeat on the four rectangular demos (regular bays) so the curved bar
      is not the only evidence.
- [ ] Decide: the solver into `web/api/_engine/` behind a per-floor function
      (a floor of this building is about 30k DOF and solves in seconds), or
      precompute.

## First run: 1300 Manhattan floor 4-5 (prototype, assumed inputs)

`tasks/1300_manhattan/structure.json` (8 in plate, 6 ksi, 10.5 ft storeys,
SDL 20, LL 40 / 100 on balconies, far end fixed, no modifiers). Output in
`tasks/1300_manhattan/fem_column_reactions_4-5.csv` at the 1.5 ft mesh;
sign convention: P compression positive, Mx and My right-hand about global
x and y, moment from slab into column, kip-ft.

- 62 columns, 21 wall lines (681 pinned nodes), 39k nodes; 5 s per run
  after vectorising the element assembly and the node lookups.
- Applied and reacted load agree to the kip. Max elastic deflection 0.23 in
  at D. Median unbalanced moment 59 kip-ft at D+L, largest 165 kip-ft
  (C60 and C61, next to the Stair 2 / elevator core), then C36 at 158.
- Mesh convergence against a 1.0 ft run, columns above the 20% floor:

  | mesh | median | p90 | max |
  |---|---|---|---|
  | 2.5 ft | 0.8% | 1.6% | 4.3% |
  | 1.5 ft | 0.5% | 1.0% | 5.5% |

  So 1.5 ft is fine for calibration; discretisation is not where a 10%
  miss will come from.
- A first version of this run had only half the wall nodes pinned (the
  linework is snapped to 0.001 ft before meshing and the node lookup used a
  tighter tolerance); deflections were 1.1 in and the core columns carried
  270 kip-ft. Fixed: tolerances are twice the snap grid for walls and rigid
  patches. Worth remembering when RAM and we disagree near a core.
- Meshing robustness, learned on all 52 floors of the five demos (all run,
  none over 11 s, mesh area within 0.4% of the slab everywhere): linework
  is noded, snapped to a 1/8 in grid, vertices closer than 0.03 ft merged;
  footprints that straddle the edge are unioned into the slab (clipping
  leaves slivers) and hair-thin holes from that union filled; walls within
  0.05 ft of the slab edge are meshed as the edge and pinned by distance;
  nodes no triangle uses are fixed; unsupported slab pieces are fixed out
  with a warning; a singular stiffness is stabilised with weak springs and
  reported. A mesh that covers less than 99% of the slab is an error.
- Warnings worth reading: a floor whose deflection exceeds 2 in reports
  where. On 1300 Manhattan floors 6 to 9 that is the garage section
  (Section B on fill at 6th, the east parking bays): slab drawn with no
  columns under it, which is the project plan's open question 1 and 3, not
  a solver problem.

## Out of scope

Lateral load contribution to unbalanced moment, post-tensioning, two-way
beam systems. Reverse-engineering the `.cpt` file format: use the API.
