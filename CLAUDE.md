# Working in this repo

## Communication

- Verbosity 3/10. Lead with the result. One short paragraph or a few bullets; no recaps, no restating the question, no closing offers.
- Numbers go in a table only when they change a decision; otherwise leave them out.
- Name a file or command only when the reader has to go there.
- Questions to the user: one at a time, with a recommended answer.

## Vercel

- Project `conc-slab-tributary-area-public` under team `gabe-kafkas-projects`, production branch `main`, root directory `web`.
- The project has no Git link in Vercel (it is absent from the team's linked-projects list), so pushes and merges do not start builds. Until the repo is connected in the dashboard (project Settings → Git), deploy after every merge to `main` with the Vercel connector's `create_deployment` (gitSource github, org `gabe-kafka`, repo `plate-slab-takedown`, ref `main`, target `production`) and verify on the live site.
- Deploys, redeploys and rollbacks are routine: do them when the work calls for it, verify on the live site.
- Environment variables, domains and deployment protection: ask first.

## Project map

- `web/`: the Next.js app and its Python engine (`web/api/_engine/`). Deployed to Vercel from `main`, root directory `web`.
- `scripts/dxf_prep.py`: architect CAD to formatted input DXF (`inspect`, `prep`, `check`, `stack`, `render`).
- `scripts/run_engine_local.py`: run the engine on a formatted DXF locally.
- `canonical-docs/INPUT_DXF_CONTRACT.md`: what the engine reads.
- `.claude/skills/format-dxf/`: the drafting process.
- `tasks/<project>/`: per-project map, plan and outputs (`out/` is ignored).
