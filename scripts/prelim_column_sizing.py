"""Build the preliminary column sizing workbook from the engine's takedown.

usage: python build_sizing.py <engine workspace dir> <out.xlsx>
"""
import csv
import math
import re
import sys
from pathlib import Path

import openpyxl
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as L

eng = Path(sys.argv[1])
out_path = sys.argv[2]

# ----------------------------------------------------------------- source data
src = openpyxl.load_workbook(eng / "column_load_takedown.xlsx", data_only=True)


def grid(sheet):
    ws = src[sheet]
    hdr = [c.value for c in ws[1]][1:]
    rows = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        rows[str(r[0])] = dict(zip(hdr, r[1:]))
    return hdr, rows


labels, trib = grid("MASTER TRIBUTARY AREA")
_, kll = grid("MASTER KLL")
_, sec = grid("MASTER CROSS SECTION")
fac_labels, fac = grid("FASCADE LENGTH")
engine_floors = ["11", "10", "9", "8", "7", "6", "5", "4"]

coords = {}
with open(eng / "dxf_column_labels.csv", newline="") as f:
    for row in csv.DictReader(f):
        coords.setdefault(row["label"], (float(row["x"]), float(row["y"])))
# unlabeled columns: x from the footprint id is not in the labels file; take the
# first engine point for them if present, else leave blank
with open(eng / "dxf_points.csv", newline="") as f:
    pts = list(csv.DictReader(f))


def sec_area(s):
    if not s:
        return None
    s = str(s).lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1)) * float(m.group(2))
    m = re.fullmatch(r"d(\d+(?:\.\d+)?)", s)
    if m:
        return math.pi / 4 * float(m.group(1)) ** 2
    return None


def label_sort(lab):
    m = re.fullmatch(r"C(\d+)", lab)
    return (0, int(m.group(1))) if m else (1, lab)


labels = sorted(labels, key=label_sort)
n = len(labels)

# ---------------------------------------------------------------- level model
# Levels top-down. "src" = engine floor whose areas stand in; "assumed" levels
# are toggled in INPUTS. Elevations from the architect's A-200.
LEVELS = [
    # level, elev, source floor, assumed, note
    ("ROOF", 201.5, "11", True, "A-109 not received; Section B of the 11th stands in, roof loads"),
    ("11", 191.0, "11", False, ""),
    ("10", 179.5, "10", False, ""),
    ("9", 169.0, "9", False, ""),
    ("8", 158.5, "8", False, ""),
    ("7", 148.0, "7", False, ""),
    ("6", 137.5, "6", False, ""),
    ("5", 127.0, "5", False, ""),
    ("4", 116.5, "4", False, ""),
    ("3", 106.0, "4", True, "A-107 not received; the 4th stands in"),
    ("2", 95.5, "4", True, "A-107 not received; the 4th stands in"),
    ("1", 85.0, "4", True, "A-108 not received; the 4th stands in"),
]
nl = len(LEVELS)

# -------------------------------------------------------------------- styles
FONT = "Arial"
f_norm = Font(name=FONT, size=10)
f_bold = Font(name=FONT, size=10, bold=True)
f_title = Font(name=FONT, size=12, bold=True)
f_in = Font(name=FONT, size=10, color="0000FF")
f_link = Font(name=FONT, size=10, color="008000")
f_note = Font(name=FONT, size=9, italic=True, color="555555")
fill_key = PatternFill("solid", fgColor="FFFF00")
fill_hdr = PatternFill("solid", fgColor="DDEBF7")
fill_ng = PatternFill("solid", fgColor="F8CBAD")
thin = Side(style="thin", color="999999")
box = Border(left=thin, right=thin, top=thin, bottom=thin)
center = Alignment(horizontal="center", vertical="center", wrap_text=True)

wb = openpyxl.Workbook()


def style_sheet(ws):
    for row in ws.iter_rows():
        for c in row:
            if c.font == Font():
                c.font = f_norm


def hdr(ws, r, c, text, width=None):
    cell = ws.cell(row=r, column=c, value=text)
    cell.font = f_bold
    cell.fill = fill_hdr
    cell.alignment = center
    cell.border = box
    if width:
        ws.column_dimensions[L(c)].width = width
    return cell


def put(ws, r, c, v, font=None, fmt=None, fill=None, comment=None):
    cell = ws.cell(row=r, column=c, value=v)
    cell.font = font or f_norm
    if fmt:
        cell.number_format = fmt
    if fill:
        cell.fill = fill
    if comment:
        cell.comment = Comment(comment, "sizing")
    return cell


# ======================================================================= INPUTS
wi = wb.active
wi.title = "INPUTS"
wi.column_dimensions["A"].width = 44
wi.column_dimensions["B"].width = 14
wi.column_dimensions["C"].width = 60
put(wi, 1, 1, "1300 Manhattan Ave: preliminary column sizing, inputs", f_title)
put(wi, 2, 1, "Blue = input. Yellow = assumption to confirm. Everything else is a formula. Loads in psf / plf / kips, lengths in ft, sections in inches.", f_note)

I = {}  # name -> absolute ref
r = 4
put(wi, r, 1, "Materials and ACI 318 axial capacity", f_bold)
r += 1
items = [
    ("fc", "f'c, concrete strength (ksi)", 6, "Confirm with the engineer; 6 ksi assumed for all columns"),
    ("fy", "fy, reinforcing steel (ksi)", 60, ""),
    ("phi", "phi, tied columns (ACI 318-19 21.2.2)", 0.65, ""),
    ("alpha", "Maximum axial factor, tied (ACI 318-19 22.4.2.1: 0.80)", 0.80, ""),
    ("rho_t", "Target reinforcement ratio for sizing", 0.02, "Section is picked so this ratio carries Pu"),
    ("rho_max", "Maximum ratio before a section is flagged NG", 0.04, "ACI permits 8 %; 4 % keeps splices buildable"),
    ("rho_min", "Minimum ratio (ACI 318-19 10.6.1.1)", 0.01, ""),
    ("wc", "Concrete unit weight (pcf)", 150, ""),
    ("fD", "Load factor, dead (ASCE 7-22 2.3.1 combination 2)", 1.2, ""),
    ("fL", "Load factor, live", 1.6, ""),
    ("facade", "Facade weight (plf of slab edge)", 150, "15 psf of wall over a 10 ft storey; applies to the facade length the engine attributes to each column"),
    ("h_found", "Storey height below the 1st floor slab to the footing (ft)", 12, "Not on the drawings received"),
    ("match_x", "Match line x (ft, engine coordinates); columns east of it are Section B", 540, "The PE#1 datum sits on the match line"),
    ("kll_default", "KLL where the engine gives none (interior column, ASCE 7-22 table 4.7-1)", 4, ""),
    ("ag_default", "Column self-weight section where the architect shows none (in2)", 336, "14 x 24"),
    ("gar_red", "Garage live-load factor for columns carrying 2+ garage floors (ASCE 7-22 4.7.4)", 0.8, ""),
]
for key, text, val, note in items:
    put(wi, r, 1, text)
    put(wi, r, 2, val, f_in, fill=fill_key if key in ("fc", "facade", "h_found", "match_x") else None)
    put(wi, r, 3, note, f_note)
    I[key] = f"INPUTS!$B${r}"
    r += 1

r += 1
put(wi, r, 1, "Levels and occupancies (top down). Section A = the residential bar west of the match line; Section B = the garage / amenity block east of it.", f_bold)
r += 1
LV_HDR = ["Level", "Elev (ft)", "Storey below (ft)", "Use areas of engine floor", "Assumed level (1 = include)",
          "A occupancy", "A slab (in)", "A SDL (psf)", "A LL (psf)", "A LL class",
          "B occupancy", "B slab (in)", "B SDL (psf)", "B LL (psf)", "B LL class", "Note"]
widths = [8, 9, 10, 12, 12, 16, 9, 9, 9, 9, 16, 9, 9, 9, 9, 50]
for c, (h, w) in enumerate(zip(LV_HDR, widths), start=1):
    hdr(wi, r, c, h, w)
lv_hdr_row = r
r += 1
# occupancy defaults per level
occ = {
    "ROOF": (("none", 0, 0, 0, "NR"), ("roof", 10, 30, 30, "NR")),
    "11": (("amenity roof terrace", 8, 30, 100, "NR"), ("amenity / pool / fitness", 10, 30, 100, "NR")),
    "10": (("residential", 8, 20, 40, "RES"), ("lobby / co-work / parking (SL1)", 10, 20, 100, "NR")),
    "9": (("residential", 8, 20, 40, "RES"), ("parking + units", 10, 5, 40, "GAR")),
    "8": (("residential", 8, 20, 40, "RES"), ("parking + units", 10, 5, 40, "GAR")),
    "7": (("residential", 8, 20, 40, "RES"), ("parking + units", 10, 5, 40, "GAR")),
    "6": (("residential", 8, 20, 40, "RES"), ("residential (3 units)", 8, 20, 40, "RES")),
    "5": (("residential", 8, 20, 40, "RES"), ("none", 8, 20, 40, "RES")),
    "4": (("residential", 8, 20, 40, "RES"), ("none", 8, 20, 40, "RES")),
    "3": (("residential", 8, 20, 40, "RES"), ("none", 8, 20, 40, "RES")),
    "2": (("residential", 8, 20, 40, "RES"), ("none", 8, 20, 40, "RES")),
    "1": (("residential / storage", 8, 20, 40, "RES"), ("none", 8, 20, 40, "RES")),
}
LV_ROW = {}
for i, (lev, elev, srcf, assumed, note) in enumerate(LEVELS):
    rr = r + i
    LV_ROW[lev] = rr
    put(wi, rr, 1, lev, f_bold)
    put(wi, rr, 2, elev, f_in, "0.0")
    if i < nl - 1:
        put(wi, rr, 3, f"=B{rr}-B{rr + 1}", fmt="0.0")
    else:
        put(wi, rr, 3, f"={I['h_found']}", fmt="0.0")
    put(wi, rr, 4, srcf, f_in)
    put(wi, rr, 5, 1 if assumed else 0, f_in, fill=fill_key if assumed else None)
    a, b = occ[lev]
    for j, v in enumerate(a):
        put(wi, rr, 6 + j, v, f_in, fill=fill_key if j == 3 else None)
    for j, v in enumerate(b):
        put(wi, rr, 11 + j, v, f_in, fill=fill_key if j == 3 else None)
    put(wi, rr, 16, note, f_note)
r += nl + 1
put(wi, r, 1, "LL class: RES = reducible per ASCE 7-22 4.7.2 (floor of 0.4 for 2+ floors, 0.5 for one); GAR = passenger garage, at most the factor above for 2+ floors; NR = not reduced (assembly, roof, L > 100 psf).", f_note)
r += 1
put(wi, r, 1, "Slab self-weight = thickness / 12 x unit weight. 'Assumed level' rows reuse the areas, KLL, facade and sections of the engine floor named; set the toggle to 0 to leave them out.", f_note)
r += 2

put(wi, r, 1, "Section families for the recommendation (smallest section whose area carries Pu at the target ratio)", f_bold)
r += 1
hdr(wi, r, 1, "Family A (14 in wide, as the architect drew)", None)
hdr(wi, r, 2, "Ag (in2)")
hdr(wi, r, 4, "Family B (square, where the architect drew other shapes)")
hdr(wi, r, 5, "Ag (in2)")
r += 1
fam_a = ["14x24", "14x30", "14x36", "14x42", "14x48", "14x54", "14x60", "18x60", "24x60"]
fam_b = ["18x18", "24x24", "24x30", "24x36", "30x30", "30x36", "36x36", "36x42", "42x42"]
FAM_A = (f"INPUTS!$A${r}:$A${r + len(fam_a) - 1}", f"INPUTS!$B${r}:$B${r + len(fam_a) - 1}")
FAM_B = (f"INPUTS!$D${r}:$D${r + len(fam_b) - 1}", f"INPUTS!$E${r}:$E${r + len(fam_b) - 1}")
for i, s in enumerate(fam_a):
    put(wi, r + i, 1, s, f_in)
    put(wi, r + i, 2, sec_area(s), fmt="0")
for i, s in enumerate(fam_b):
    put(wi, r + i, 4, s, f_in)
    put(wi, r + i, 5, sec_area(s), fmt="0")
r += max(len(fam_a), len(fam_b)) + 1
put(wi, r, 1, "Families must stay sorted by area, smallest first. Edit the names and areas to the firm's standard sizes.", f_note)
wi.freeze_panes = "A4"

# --------------------------------------------------------------- grid sheets
# All per-column sheets share the layout: row 1 header, rows 2.. one per
# column; A label, B x, C y, D section (A/B), E.. levels top-down.
LC0 = 5  # first level column


def lev_col(i):
    return LC0 + i


def new_grid(title, note):
    ws = wb.create_sheet(title)
    hdr(ws, 1, 1, "Column", 11)
    hdr(ws, 1, 2, "x (ft)", 8)
    hdr(ws, 1, 3, "y (ft)", 8)
    hdr(ws, 1, 4, "Section", 8)
    for i, (lev, *_rest) in enumerate(LEVELS):
        hdr(ws, 1, lev_col(i), lev, 9)
    ws.cell(row=1, column=lev_col(nl) + 1, value=note).font = f_note
    for k, lab in enumerate(labels):
        rr = 2 + k
        if title == "TRIB":
            put(ws, rr, 1, lab, f_bold)
            x, y = coords.get(lab, (None, None))
            put(ws, rr, 2, x, fmt="0")
            put(ws, rr, 3, y, fmt="0")
            put(ws, rr, 4, f'=IF(B{rr}="","A",IF(B{rr}>={I["match_x"]},"B","A"))')
        else:
            put(ws, rr, 1, f"=TRIB!A{rr}", f_link)
            put(ws, rr, 2, f"=TRIB!B{rr}", f_link, "0")
            put(ws, rr, 3, f"=TRIB!C{rr}", f_link, "0")
            put(ws, rr, 4, f"=TRIB!D{rr}", f_link)
    ws.freeze_panes = "E2"
    return ws


def src_cell(sheet, lev_index, rr):
    """Reference to the engine floor's cell on `sheet` for an assumed level."""
    lev, _e, srcf, _a, _n = LEVELS[lev_index]
    j = next(i for i, l in enumerate(LEVELS) if l[0] == srcf)
    return f"{sheet}!{L(lev_col(j))}{rr}"


# TRIB: tributary slab area per column per level (sf)
wt = new_grid("TRIB", "Tributary slab area (sf) per column per level, from the engine's takedown (Voronoi with walls). Assumed levels reuse an engine floor times the INPUTS toggle; ROOF uses Section B only.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, _e, srcf, assumed, _n) in enumerate(LEVELS):
        c = lev_col(i)
        if not assumed:
            v = trib[lev].get(lab)
            put(wt, rr, c, v if isinstance(v, (int, float)) else None, fmt="0")
        else:
            toggle = f"INPUTS!$E${LV_ROW[lev]}"
            base = src_cell("TRIB", i, rr)
            if lev == "ROOF":
                put(wt, rr, c, f'=IF(AND({toggle}=1,$D{rr}="B",{base}<>""),{base},"")', fmt="0")
            else:
                put(wt, rr, c, f'=IF(AND({toggle}=1,{base}<>""),{base},"")', fmt="0")

# KLL
wk = new_grid("KLL", "Live-load element factor per column per level from the engine (4 interior, 3 edge, 2 corner). Blank = engine gave none; INPUTS default applies.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, _e, srcf, assumed, _n) in enumerate(LEVELS):
        c = lev_col(i)
        if not assumed:
            v = kll[lev].get(lab)
            put(wk, rr, c, v if isinstance(v, (int, float)) else None, fmt="0")
        else:
            put(wk, rr, c, f'=IF({src_cell("KLL", i, rr)}="","",{src_cell("KLL", i, rr)})', fmt="0")

# FACADE
wf = new_grid("FACADE", "Facade length (ft) attributed to each column per level by the engine's perimeter attribution.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, _e, srcf, assumed, _n) in enumerate(LEVELS):
        c = lev_col(i)
        if not assumed:
            v = fac.get(lev, {}).get(lab)
            put(wf, rr, c, v if isinstance(v, (int, float)) else None, fmt="0")
        else:
            if lev == "ROOF":
                put(wf, rr, c, "", fmt="0")  # parapet only; ignored
            else:
                put(wf, rr, c, f'=IF({src_cell("FACADE", i, rr)}="","",{src_cell("FACADE", i, rr)})', fmt="0")

# ARCH: architect's section per level (text) and its area
wa = new_grid("ARCH", "Column section the architect drew below each slab (from the Revit block name), and its gross area on sheet ARCH_AG.")
wg = new_grid("ARCH_AG", "Gross area (in2) of the architect's section; INPUTS default where none is drawn.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, _e, srcf, assumed, _n) in enumerate(LEVELS):
        c = lev_col(i)
        if not assumed:
            s = sec[lev].get(lab)
            put(wa, rr, c, str(s) if s else None)
        else:
            put(wa, rr, c, f'=IF({src_cell("ARCH", i, rr)}="","",{src_cell("ARCH", i, rr)})')
        a_ref = f"ARCH!{L(c)}{rr}"
        # parse "WxH" or "dD" in-sheet
        put(wg, rr, c,
            f'=IF({a_ref}="",{I["ag_default"]},IF(LEFT({a_ref},1)="d",PI()/4*VALUE(MID({a_ref},2,9))^2,'
            f'VALUE(LEFT({a_ref},FIND("x",{a_ref})-1))*VALUE(MID({a_ref},FIND("x",{a_ref})+1,9))))', fmt="0")

# CLASS: LL class per column per level
wcl = new_grid("CLASS", "Live-load class per column per level from INPUTS (by Section and level): RES, GAR or NR. Blank where the column carries no slab there.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        t = f"TRIB!{L(c)}{rr}"
        lr = LV_ROW[lev]
        put(wcl, rr, c, f'=IF({t}="","",IF($D{rr}="A",INPUTS!$J${lr},INPUTS!$O${lr}))')

# DEAD: dead load per level (kips) = slab + SDL on the tributary area + facade + column self-weight of the storey below
wd = new_grid("DEAD", "Dead load (kips) delivered at each level: tributary area x (slab self-weight + SDL) + facade plf x storey + column self-weight of the storey below (architect's section). Zero where the column carries no slab.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        t = f"TRIB!{L(c)}{rr}"
        fl = f"FACADE!{L(c)}{rr}"
        ag = f"ARCH_AG!{L(c)}{rr}"
        lr = LV_ROW[lev]
        slab_a = f"(INPUTS!$G${lr}/12*{I['wc']}+INPUTS!$H${lr})"
        slab_b = f"(INPUTS!$L${lr}/12*{I['wc']}+INPUTS!$M${lr})"
        storey = f"INPUTS!$C${lr}"
        put(wd, rr, c,
            f'=IF({t}="",0,({t}*IF($D{rr}="A",{slab_a},{slab_b})+IF({fl}="",0,{fl})*{I["facade"]})/1000'
            f'+{ag}/144*{I["wc"]}*{storey}/1000)', fmt="0.0")

# LIVE: unreduced live load per level (kips)
wl = new_grid("LIVE", "Unreduced live load (kips) at each level: tributary area x LL for the column's Section.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        t = f"TRIB!{L(c)}{rr}"
        lr = LV_ROW[lev]
        put(wl, rr, c, f'=IF({t}="",0,{t}*IF($D{rr}="A",INPUTS!$I${lr},INPUTS!$N${lr})/1000)', fmt="0.0")

# RED: live-load reduction factor for the RES share at each level
wr = new_grid("RED", "ASCE 7-22 4.7.2 reduction factor on the reducible (RES) live load carried down to this level: AT = reducible tributary area from the top to here, KLL at this level; floor 0.4 (2+ floors) / 0.5 (one); none below KLL x AT = 400 sf.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        first = L(lev_col(0))
        here = L(c)
        cls = f"CLASS!${first}{rr}:${here}{rr}"
        area = f"TRIB!${first}{rr}:${here}{rr}"
        at = f'SUMPRODUCT(({cls}="RES")*1,{area})'
        nres = f'SUMPRODUCT(({cls}="RES")*1)'
        kll_ref = f"KLL!{here}{rr}"
        kllv = f'IF({kll_ref}="",{I["kll_default"]},{kll_ref})'
        put(wr, rr, c,
            f'=IF({kllv}*{at}<400,1,MAX(IF({nres}>=2,0.4,0.5),MIN(1,0.25+15/SQRT({kllv}*{at}))))', fmt="0.00")

# PU: factored axial load at the base of the storey below each level (kips)
wp = new_grid("PU", "Factored axial load Pu (kips) at the bottom of the storey below each slab: 1.2 x dead above + 1.6 x live above, RES share reduced by RED, GAR share by the garage factor when 2+ garage floors are carried, NR share unreduced.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        first = L(lev_col(0))
        here = L(c)
        cls = f"CLASS!${first}{rr}:${here}{rr}"
        live = f"LIVE!${first}{rr}:${here}{rr}"
        dead = f"DEAD!${first}{rr}:${here}{rr}"
        ngar = f'SUMPRODUCT(({cls}="GAR")*1)'
        put(wp, rr, c,
            f'={I["fD"]}*SUM({dead})+{I["fL"]}*('
            f'RED!{here}{rr}*SUMPRODUCT(({cls}="RES")*1,{live})'
            f'+IF({ngar}>=2,{I["gar_red"]},1)*SUMPRODUCT(({cls}="GAR")*1,{live})'
            f'+SUMPRODUCT(({cls}="NR")*1,{live}))', fmt="0")

# AG_REQ: required gross area at the target ratio
wq = new_grid("AG_REQ", "Required gross section (in2) at the target ratio: Pu / (alpha x phi x (0.85 f'c (1 - rho) + fy rho)), ACI 318-19 22.4.2.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        put(wq, rr, c,
            f'=PU!{L(c)}{rr}/({I["alpha"]}*{I["phi"]}*(0.85*{I["fc"]}*(1-{I["rho_t"]})+{I["fy"]}*{I["rho_t"]}))', fmt="0")

# RHO_ARCH: ratio the architect's section would need
wro = new_grid("RHO_ARCH", "Reinforcement ratio the architect's section needs to carry Pu: (Pu / (alpha phi) - 0.85 f'c Ag) / (Ag (fy - 0.85 f'c)). Red = above the maximum ratio; negative means below minimum steel (fine).")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        ag = f"ARCH_AG!{L(c)}{rr}"
        put(wro, rr, c,
            f'=IF(TRIB!{L(c)}{rr}="","",(PU!{L(c)}{rr}/({I["alpha"]}*{I["phi"]})-0.85*{I["fc"]}*{ag})/({ag}*({I["fy"]}-0.85*{I["fc"]})))', fmt="0.0%")

# RECOMMEND: smallest family section at the target ratio
wrec = new_grid("RECOMMEND", "Smallest section whose area is at least AG_REQ, from family A (14 in wide) where the architect drew a 14-wide column or none, else family B (INPUTS); 'LARGER' when none is big enough. The architect's section is kept in ARCH for comparison.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        req = f"AG_REQ!{L(c)}{rr}"
        names_a, areas_a = FAM_A
        names_b, areas_b = FAM_B

        def pick(names, areas):
            # MATCH(req, areas, 1) gives the largest area <= req; the next one is the first that carries it
            return (f'IF({req}<=INDEX({areas},1),INDEX({names},1),'
                    f'IF({req}>INDEX({areas},ROWS({areas})),"LARGER",INDEX({names},MATCH({req},{areas},1)+1)))')
        arch = f"ARCH!{L(c)}{rr}"
        put(wrec, rr, c, f'=IF(TRIB!{L(c)}{rr}="","",IF(OR({arch}="",LEFT({arch},3)="14x"),{pick(names_a, areas_a)},{pick(names_b, areas_b)}))')

# STATUS: architect's section vs Pu
wst = new_grid("STATUS", "OK: architect's section carries Pu at or below the target ratio. HEAVY: needs more than the target but no more than the maximum ratio. NG: needs more than the maximum ratio; use RECOMMEND.")
for k, lab in enumerate(labels):
    rr = 2 + k
    for i, (lev, *_r) in enumerate(LEVELS):
        c = lev_col(i)
        rho = f"RHO_ARCH!{L(c)}{rr}"
        put(wst, rr, c, f'=IF({rho}="","",IF({rho}>{I["rho_max"]},"NG",IF({rho}>{I["rho_t"]},"HEAVY","OK")))')

# ===================================================================== SUMMARY
ws = wb.create_sheet("SUMMARY", 1)
ws.column_dimensions["A"].width = 11
put(ws, 1, 1, "1300 Manhattan Ave: preliminary column sizing by load takedown", f_title)
put(ws, 2, 1, "Preliminary. Axial load only (no moment, no slenderness, no lateral); loads and occupancies per INPUTS; tributary areas from the engine on the architect's background set 23-037A, seven floors in hand (4 to 11), floors 1 to 3 and the roof assumed. Check the yellow cells in INPUTS before quoting any number.", f_note)
r = 4
put(ws, r, 1, "Counts at the base of each column line", f_bold)
r += 1
S_HDR = ["Column", "Section", "Lowest level", "Pu at base (kips)", "Architect section at base", "Ratio it needs", "Status", "Recommended at base", "Floors carried", "Note"]
S_W = [11, 8, 10, 12, 14, 10, 8, 14, 9, 40]
for c, (h, w) in enumerate(zip(S_HDR, S_W), start=1):
    hdr(ws, r, c, h, w)
s_hdr_row = r
r += 1
first = L(lev_col(0))
last = L(lev_col(nl - 1))
for k, lab in enumerate(labels):
    rr = 2 + k
    sr = r + k
    rng = lambda sheet: f"{sheet}!${first}{rr}:${last}{rr}"
    put(ws, sr, 1, f"=TRIB!A{rr}", f_link)
    put(ws, sr, 2, f"=TRIB!D{rr}", f_link)
    # lowest level with an area: last non-blank in TRIB row (levels are top-down)
    trow = f"TRIB!${first}{rr}:${last}{rr}"
    put(ws, sr, 3, f'=IF(COUNTIF({trow},">0")=0,"",INDEX(TRIB!${first}$1:${last}$1,SUMPRODUCT(MAX(({trow}<>"")*(COLUMN({trow})-COLUMN(TRIB!${first}{rr})+1)))))')
    put(ws, sr, 4, f'=IFERROR(INDEX({rng("PU")},MATCH(C{sr},TRIB!${first}$1:${last}$1,0)),"")', fmt="0")
    put(ws, sr, 5, f'=IFERROR(INDEX({rng("ARCH")},MATCH(C{sr},TRIB!${first}$1:${last}$1,0)),"")')
    put(ws, sr, 6, f'=IFERROR(INDEX({rng("RHO_ARCH")},MATCH(C{sr},TRIB!${first}$1:${last}$1,0)),"")', fmt="0.0%")
    put(ws, sr, 7, f'=IFERROR(INDEX({rng("STATUS")},MATCH(C{sr},TRIB!${first}$1:${last}$1,0)),"")')
    put(ws, sr, 8, f'=IFERROR(INDEX({rng("RECOMMEND")},MATCH(C{sr},TRIB!${first}$1:${last}$1,0)),"")')
    put(ws, sr, 9, f'=COUNTIF({rng("TRIB")},">0")', fmt="0")
    note = ""
    if "UNLABELED" in lab:
        note = "Outside the draft slab on the 7th east bay; carries only that level in this takedown"
    put(ws, sr, 10, note, f_note)
s_first, s_last = r, r + n - 1
r = s_last + 2
put(ws, r, 1, "Status counts at the base", f_bold)
r += 1
for i, st in enumerate(["OK", "HEAVY", "NG"]):
    put(ws, r + i, 1, st)
    put(ws, r + i, 2, f'=COUNTIF($G${s_first}:$G${s_last},"{st}")', fmt="0")
r += 4
put(ws, r, 1, "Recommended section counts per level (how many columns the scheme needs at each size)", f_bold)
r += 1
hdr(ws, r, 1, "Section")
for i, (lev, *_r) in enumerate(LEVELS):
    hdr(ws, r, 2 + i, lev)
sched_hdr = r
r += 1
for s in fam_a + fam_b:
    put(ws, r, 1, s, f_bold)
    for i in range(nl):
        col = L(lev_col(i))
        put(ws, r, 2 + i, f'=COUNTIF(RECOMMEND!{col}$2:{col}${n + 1},$A{r})', fmt="0;;-")
    r += 1
put(ws, r, 1, "LARGER", f_bold)
for i in range(nl):
    col = L(lev_col(i))
    put(ws, r, 2 + i, f'=COUNTIF(RECOMMEND!{col}$2:{col}${n + 1},"LARGER")', fmt="0;;-")
r += 2
put(ws, r, 1, "Largest Pu per level (kips)", f_bold)
r += 1
for i, (lev, *_r) in enumerate(LEVELS):
    col = L(lev_col(i))
    put(ws, r, 2 + i, f"=MAX(PU!{col}$2:{col}${n + 1})", fmt="0")
    hdr(ws, r - 1, 2 + i, lev)
r += 2
put(ws, r, 1, "Method", f_bold)
r += 1
for line in [
    "1. Tributary area per column per slab from the tributary engine (Voronoi between columns and wall support points, clipped to the fitted slab edge).",
    "2. Dead = area x (slab self-weight + superimposed) + facade length x plf + column self-weight (architect's section) of the storey below. Live = area x occupancy live load from INPUTS by Section and level.",
    "3. Live load reduction, ASCE 7-22 4.7: residential share reduced on the cumulative tributary area with the engine's KLL; garage share limited to the garage factor; assembly, lobby and roof shares not reduced.",
    "4. Pu = 1.2 D + 1.6 L accumulated from the top down; the value at a level is at the bottom of the storey below that slab.",
    "5. Required gross area at the target ratio from ACI 318-19 22.4.2 (tied: 0.80 phi [0.85 f'c (Ag - Ast) + fy Ast]); the architect's section is checked for the ratio it would need; the recommendation is the smallest family section at the target ratio.",
    "6. Not included: moments from slab unbalance or lateral load, slenderness, shear walls taking axial load, transfer conditions at the hillside, the garage split levels, and floors 1 to 3 and the roof (assumed, see INPUTS).",
]:
    put(ws, r, 1, line, f_note)
    r += 1
ws.freeze_panes = f"A{s_hdr_row + 1}"

# conditional formatting for NG
from openpyxl.formatting.rule import CellIsRule, FormulaRule
wst.conditional_formatting.add(f"{first}2:{last}{n + 1}", CellIsRule(operator="equal", formula=['"NG"'], fill=fill_ng))
ws.conditional_formatting.add(f"G{s_first}:G{s_last}", CellIsRule(operator="equal", formula=['"NG"'], fill=fill_ng))
wro.conditional_formatting.add(f"{first}2:{last}{n + 1}", FormulaRule(formula=[f"AND({first}2<>\"\",{first}2>{I['rho_max']})"], fill=fill_ng))

for sh in wb.worksheets:
    style_sheet(sh)
wb.save(out_path)
print("wrote", out_path, "columns", n, "levels", nl)
