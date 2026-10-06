# 1300 Manhattan Ave — formatted DXF plan

Source: CPA Architecture background set `23-037A`, issued 2026-09-30
(sheets dated 07/07/26 to 08/14/26). Union City, NJ (Hudson County), Block 185
Lot 1.01 and Block 187 Lot 2. 29 sheets: A-100 through A-109 with `a`/`b`
blow-ups, plus A-200 east elevation.

Process and tool: `.claude/skills/format-dxf/SKILL.md`, `scripts/dxf_prep.py`,
target format `canonical-docs/INPUT_DXF_CONTRACT.md`.

## What the building is

A hillside building on the Palisades. Two sections joined at a vertical match
line on every plan:

- **Section A (west)**: an 11-storey curved residential bar. Stair 1 at the
  west end, Stair 2 with passenger elevators PE#1/PE#2 and refuse room with
  service elevator SE#1 at the east end against the match line. Columns show
  as small filled rectangles along both long faces, on a radial rhythm, at the
  balcony/party-wall line. Balconies project on the downhill (south) face.
- **Section B (east)**: the garage and amenity block that only exists from the
  6th floor up, because the hill rises under it. Lobby, mail, co-work and
  vestibule at the 10th floor (SL1, 179.5 ft) which is the Manhattan Avenue
  street entry; kids room, fitness, clubroom, pool, courtyard and the amenity
  roof over the bar at the 11th (SL2, 191 ft). Parking with EV and compact
  stalls on 7th to 11th. Stairs 3 and 4 are in this section. A round feature
  (about 60 ft diameter, likely a ramp or a planted court) sits at the match
  line on 10th and 11th.

Floors and elevations from A-200 and the plan titles:

| Floor label | Elev (ft) | Sheet | Content |
|---|---|---|---|
| `1` | 85.0 | A-108 | bar, tenant storage, courtyard, compactor, amenity at east end |
| `2-3` | 95.5 / 106.0 | A-107 | bar only (one plan for both) |
| `4-5` | 116.5 / 127.0 | A-106 | bar only (one plan for both) |
| `6` | 137.5 | A-105 | bar + 3 units east of match line; garage column grid on fill in Section B |
| `7` | 148.0 | A-104 | bar + units + first full parking level |
| `8` | 158.5 | A-103 | bar + units + parking |
| `9` | 169.0 | A-102 | bar + units + parking, recycle, bike storage |
| `10` | 179.5 | A-101 | bar + package room + lobby/mail/co-work + parking (SL1, street entry) |
| `11` | 191.0 | A-100 | amenity roof over bar + kids room, fitness, clubroom, pool, courtyard, parking (SL2) |
| `ROOF` | 201.5 | A-109 | main roof, with mechanical/parapet tops 194 to 208 |

Floor-to-floor is 10'-6" throughout except 11'-6" from 10th to 11th. Plans
are drawn at 1/16" on the overview sheets and 1/8" on the `a` (Section A)
and `b` (Section B) sheets. Spot elevations on the plans (e.g. `179.50 ft`,
`183.50 ft`) show split levels inside the garage: the east parking bay on 9th
sits at 163.5 ft, on 8th at 153.0 ft, on 7th at 142.5 ft. Those are ramps or
half-levels and need an engineering decision (see open questions).

Unit labels on the plans carry areas (`1B1B-A 780 SF`, `2B2B-B 1177 SF`);
these are not column labels and will be dropped.

## What the formatted DXF should contain

- Ten floors stacked bottom-up along +Y: `1`, `2-3`, `4-5`, `6`, `7`, `8`,
  `9`, `10`, `11`, `ROOF`. One floor = Section A + Section B joined at the
  match line as one set of `BOUNDARY` loops.
- `ADDITIONAL-LOAD`: the south balconies on the bar (every floor), the
  amenity roof over the bar at 11th, pool deck/courtyard at 11th if the
  engineer wants them as a separate zone.
- `COLS`: closed rectangles. Expect roughly 25 to 30 in the bar per floor and
  a 24 ft x 25 ft-ish garage grid in Section B from 6th up.
- `WALL`: stair and elevator cores (Stairs 1 to 4, PE#1/PE#2, SE#1) and any
  retaining/shear walls the engineer designates.
- `DATUM`: one point per floor, suggested at the north-west inside corner of
  the Stair 2 / PE#1 core, which exists on every floor including the roof.
- Curves: the bar's slab edge and corridor are arcs; they must be flattened
  (prep does it) so the engine does not chord them.

## What the CAD turned out to be

Twenty DWGs (AutoCAD 2018 format) received so far: A-100 to A-106 overview
sheets and their `a`/`b` blow-ups. They are Revit per-view exports.

- **Which files matter: the overview sheets only.** Every overview sheet's
  model space holds the whole floor (both sections) in shared model
  coordinates, about 575 ft by 280 ft. The `a` and `b` files are the same
  model cropped to one section, so they add nothing. Floor identity is
  confirmed from the paper-space title: A-100 = 11th, A-101 = 10th,
  A-102 = 9th, A-103 = 8th, A-104 = 7th, A-105 = 6th, A-106 = 5th and 4th.
- **Still needed:** A-107 (3rd and 2nd), A-108 (1st), A-109 (main roof).
- Units are inches. About 40 layers per sheet; roughly 85 percent of the
  linework is site contours (`C-TOPO-*`), furniture, fixtures and mullions,
  all dropped.
- **Columns:** `S-COLS` holds one Revit block per column, named with its
  size (`Concrete-Rectangular-Column - 14 x 36-...`), inserted at the column
  centre with the column's rotation. Each block is four lines plus a hatch.
  Some blocks (14 on the 10th, 24 on the 11th) came through conversion with
  empty definitions, so the tool builds every footprint from the name, insert
  and rotation instead, which matched the drawn rectangle on 97 of 101
  checked. Four columns per sheet on 7th to 10th are loose lines, not blocks;
  the tool closes those too. Per floor: 62 (4-5), 105 (6), 107 (7), 107 (8),
  109 (9), 115 (10), 80 (11).
- **Slab edge:** Revit's `A-FLOR`, `A-FLOR-OTLN`, `A-FLOR-OVHD` are only the
  edge fragments visible between walls (about 1000 ft of line in 78 pieces
  per floor; nothing closes at any snap up to 12 ft). They are kept on
  `BG-HINT` as a tracing aid. `BOUNDARY` is traced by the engineer.
  `A-FLOR-OTLN` outlines the balconies, so it is the aid for
  `ADDITIONAL-LOAD` as well. `S-FNDN` lines on the garage floors mark the
  retaining-wall side.
- **Datum:** the `PE#1` elevator label sits at exactly `(-11089.5, -6829.4)`
  on every sheet; the map uses that point. It is inside the elevator shaft,
  so if the shaft gets cut out of `BOUNDARY` as an opening, shift
  `datum_xy` a few feet onto the corridor slab.
- **Conversion:** the container has no apt or pip DWG converter and GitHub is
  proxied off, but conda-forge is reachable, so `libredwg 0.11` was pulled as
  a plain archive and its `dwg2dxf` converted all twenty files in about two
  minutes with no errors. The same package works for the three missing
  sheets.

## Steps

- [x] Read the PDF set; record floors, elevations, sections, cores.
- [x] Write the input contract from the engine code.
- [x] Build `scripts/dxf_prep.py` (`inspect`, `prep`, `check`) and verify on the demos.
- [x] Write the `format-dxf` skill.
- [x] Receive the CAD; convert DWG to DXF; identify the overview sheets.
- [x] `inspect`; write `tasks/1300_manhattan/map.json`.
- [x] `prep` the seven floors in hand to `tasks/1300_manhattan/out/1300_manhattan_working.dxf`.
- [ ] Get A-107, A-108, A-109; convert; re-run `prep` with all ten floors.
- [ ] Engineer traces `BOUNDARY`, `ADDITIONAL-LOAD`, `WALL`, adds `COL-LABEL`
      in AutoCAD; save as `tasks/1300_manhattan/out/1300_manhattan_formatted.dxf`.
- [ ] `check` until READY; upload; confirm inches.
- [ ] Compare the app's floor list and column counts against the table above.

## Open questions

1. Garage split levels (163.5 / 153.0 / 142.5 ft bays): treat the east bay as
   part of the same floor plate, or as its own floor label (`9E`)? The engine
   has no notion of elevation; it only knows separate loops with labels.
2. The round feature at the match line on 10th and 11th: an opening in the
   slab (cut out of the `BOUNDARY` loop) or a planted court on slab?
3. Section B at 6th: the column grid in hatched fill is foundation or
   columns rising through fill; no slab there, so no `BOUNDARY` on 6th east of
   the match line unless the engineer says otherwise.
4. Do the 2-3 and 4-5 plans really repeat column by column, so range labels
   are safe, or do balconies differ?
5. Column labels: the architect has none. Decide a scheme (grid-based or
   sequential per floor) before tracing; unlabeled columns get generated
   names, which is workable but not pretty in the takedown sheet.
