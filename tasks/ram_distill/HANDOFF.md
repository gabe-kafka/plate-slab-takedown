# Handoff: distill RAM Concept's FEM (local Windows session)

You are the local session on the Windows machine with RAM Concept and the
`agentic-ram` checkout (Desktop). The cloud session built the solver side;
you own the RAM side and the first comparison. Goal: gravity unbalanced
moments into columns from the digital twin's geometry within 10% of RAM
Concept on 1300 Manhattan, then on the four rectangular demos.

Read first, in this order: this file, `tasks/ram_distill/plan.md`,
`.claude/skills/ram-reactions/SKILL.md`, agentic-ram's `AGENTS.md` and
`ram-concept/AGENTS.md`. Their Concept rules bind you: official API only,
never calc, mesh, save or shut down an attached session, work on a
separately named copy, never kill Concept.exe.

## State on this branch (`claude/fervent-clarke-8z01dc`)

- `scripts/plate_fem.py`: DKT thin-plate solver on the twin's floor geometry;
  `verify` passes Timoshenko; `run` wrote
  `tasks/1300_manhattan/fem_column_reactions_4-5.csv` (62 columns, D+L
  median |M| 52 kip-ft) with the assumed inputs in
  `tasks/1300_manhattan/structure.json`.
- `scripts/ram_concept_bridge.py`: RAM side. `export` (attach or headless
  `--cpt`) and `build` (new model from the twin's geometry, mesh, calc,
  export). Written against the API surface agentic-ram's skills use plus
  Bentley's help; the column reaction call has NOT been exercised yet.
- `scripts/ram_compare.py`: scoring, with the pass rule from the plan.

## Do, in order

1. `python scripts\plate_fem.py verify` must print `VERIFY PASS`
   (`pip install numpy scipy shapely triangle` in a normal Python).
2. Reactions out of RAM. Prefer the engineer's 1300 Manhattan model if one
   exists (ask once; if none, go to 3):
   `python scripts\ram_concept_bridge.py export --cpt <copy>.cpt --calc --out tasks\1300_manhattan\out\ram\ram_4-5.csv --probe`
   on a COPY, headless. Read the `--probe` output. If `read_reaction`
   raises, find the real method on the load-combo / loading layer (the
   installed docs are at `C:\Program Files\Bentley\Engineering\RAM Concept\RAM Concept 20xx\python\` and Help > Scripting API) and add it to
   `REACTION_METHODS`; fix `reaction_components` to the object's real
   attribute names. Check one column against the GUI reactions plan by
   hand, including sign and units, before trusting the CSV.
3. Build the twin's floor in RAM, headless:
   `python scripts\ram_concept_bridge.py build --floor 4-5 --geometry https://conc-slab-tributary-area-public.vercel.app/demos/1300-manhattan/result.json --structure tasks\1300_manhattan\structure.json --out tasks\1300_manhattan\out\ram\ram_4-5.csv --save tasks\1300_manhattan\out\ram\ram_4-5.cpt --probe`
   Report the "attributes not on this API version" line and fix those
   names. Open the saved `.cpt` in Concept and look at the Mesh Input plan:
   slab outline, 62 columns, 21 walls, two load zones.
4. Match `structure.json` to RAM: `ram_4-5.settings.json` lists units,
   concretes, loadings, combos, column fixities. Same thickness, f'c, unit
   weight, SDL/LL, far-end fixity, modifiers on both sides, no patterning.
5. Compare:
   `python scripts\ram_compare.py tasks\1300_manhattan\fem_column_reactions_4-5.csv tasks\1300_manhattan\out\ram\ram_4-5.csv --combo D+L --ram-combo "<service combo layer>" --out tasks\1300_manhattan\out\ram\compare_4-5.csv`
   Rerun `scripts\plate_fem.py run ... --structure tasks\1300_manhattan\structure.json --out tasks\1300_manhattan\out\fem` after any structure change.
6. Work the calibration list in `plan.md` one setting at a time (mesh 1.0
   ft first; then far-end fixity and `i_factor`; slab modifier and self
   weight; rigid patch; walls; patterning). Record each step's numbers in
   `tasks/ram_distill/plan.md` under a "Calibration log" heading: setting
   changed, median and p90 error above the floor, pass count, worst
   column and why.
7. Commit on this branch as you go (`git pull` first; the cloud session
   also pushes here). Once `export` has run clean on a real model, copy
   the bridge and the skill into agentic-ram as
   `ram-concept/skills/export-ram-concept-column-reactions/` with the
   discovery shim under `.agents/skills/`, and commit there.

## Report back

Verbosity 3/10. After step 5, write the summary line of `ram_compare.py`
and the worst five columns into `plan.md` and push. Say plainly what
could not be verified.
