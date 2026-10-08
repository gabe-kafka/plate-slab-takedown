---
name: ram-reactions
description: Get RAM Concept column reactions (P, Mx, My per load combo) out of the open Concept session or a .cpt, or build a floor of the digital twin in RAM Concept and calc it, then compare with the plate FE solver. Use when the user says RAM reactions, distill RAM, compare with RAM, export column reactions, or build the floor in Concept. Windows only; needs RAM Concept CONNECT and its Python API.
---

# RAM reactions (distillation)

Plan and metric: `tasks/ram_distill/plan.md`. Tool: `scripts/ram_concept_bridge.py`.
Follows agentic-ram's Concept rules: the official localhost API only, attach
to a window started with `-apiServerWithGui`, never calc, mesh, save or shut
down an attached session, work on a separately named copy.

## Export from the open model (read-only)

1. The window must serve the API. Probe with agentic-ram:
   `python .\ram-concept\scripts\attach-live-concept.py --probe --format markdown`.
   A Start-menu or RAM Manager window has no API: ask the engineer to close
   it, then `.\ram-concept\scripts\start-ram-concept-api.cmd` on a copy.
2. From this repo's root:
   `python scripts\ram_concept_bridge.py export --out tasks\1300_manhattan\out\ram\ram_4-5.csv --probe`
   `--port N` if more than one API session is running. If it times out, the
   Scripting Server is paused: click Resume.
3. First run: read the `--probe` lines. If `read_reaction` raised, the API
   spells the reaction call differently; add the name to `REACTION_METHODS`.
   Check one column by hand against `Layers > Load Combo > ... > Reactions`.

## Build the twin's floor in RAM

`python scripts\ram_concept_bridge.py build --floor 4-5 --geometry https://conc-slab-tributary-area-public.vercel.app/demos/1300-manhattan/result.json --structure tasks\1300_manhattan\structure.json --out tasks\1300_manhattan\out\ram\ram_4-5.csv --save tasks\1300_manhattan\out\ram\ram_4-5.cpt --probe`

Headless, its own process, nothing open is touched. Report the "attributes
not on this API version" line; an empty dict means every setting landed.

## Compare

`python scripts\ram_compare.py tasks\1300_manhattan\fem_column_reactions_4-5.csv tasks\1300_manhattan\out\ram\ram_4-5.csv --combo D+L --ram-combo "<combo layer name>" --out tasks\1300_manhattan\out\ram\compare_4-5.csv`

Pick the RAM layer whose factors match ours (`<out>.settings.json` lists
them). Report: matched count, median and p90 error above the floor, pass
count, worst five columns. Then work the calibration list in the plan in
order; change one setting in `structure.json` at a time, rerun
`scripts/plate_fem.py run`, compare again.

## Promotion

Once `export` has run clean on a real model, this belongs in agentic-ram as
`ram-concept/skills/export-ram-concept-column-reactions/` with the same
guards; copy the script and this file there.
