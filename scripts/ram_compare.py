#!/usr/bin/env python3
"""Compare plate_fem column reactions with a RAM Concept column-reaction export.

    python scripts/ram_compare.py ours.csv ram.csv --combo D+L [--ram-combo "Service"] [--out compare.csv]

RAM export schema (columns are matched case-insensitively; use --map to rename):
    label, x, y, P, Mx, My            one row per column per load combination,
    combo                              or one file per combination (then omit --ram-combo)
Units: kips, kip-ft, feet; x, y in the same model coordinates as the DXF (feet).
Rows join by label when the labels agree, else by nearest xy within 1 ft.

Pass rule (tasks/ram_distill/plan.md): within 10% on the resultant for columns
whose RAM moment exceeds 20% of the floor median; absolute tolerance of 10% of
that median below it.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from pathlib import Path


def read(path: Path, mapping: dict) -> list[dict]:
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        low = {k.strip().lower(): v for k, v in r.items()}
        out.append({mapping.get(k, k): v for k, v in low.items()})
    return out


def num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ours")
    ap.add_argument("ram")
    ap.add_argument("--combo", default="D+L", help="our combination suffix (P_<combo>, Mx_<combo>...)")
    ap.add_argument("--ram-combo", help="value of the RAM file's combo column to keep")
    ap.add_argument("--map", action="append", default=[], help="ram_header=schema_name, e.g. 'moment about x=mx'")
    ap.add_argument("--out")
    args = ap.parse_args()

    mapping = {}
    for m in args.map:
        k, v = m.split("=", 1)
        mapping[k.strip().lower()] = v.strip().lower()
    ours = read(Path(args.ours), {})
    ram = read(Path(args.ram), mapping)
    if args.ram_combo:
        ram = [r for r in ram if r.get("combo", "").strip() == args.ram_combo]
    if not ram:
        print("no RAM rows after filtering", file=sys.stderr)
        return 1

    by_label = {r["label"].strip().upper(): r for r in ram if r.get("label")}
    rows = []
    for o in ours:
        lab = o["label"].strip().upper()
        r = by_label.get(lab)
        if r is None:
            ox, oy = num(o["x"]), num(o["y"])
            best = min(ram, key=lambda q: math.hypot(num(q.get("x")) - ox, num(q.get("y")) - oy))
            if math.hypot(num(best.get("x")) - ox, num(best.get("y")) - oy) <= 1.0:
                r = best
        if r is None:
            rows.append({"label": o["label"], "matched": "no"})
            continue
        mo = (num(o[f"Mx_{args.combo}"]), num(o[f"My_{args.combo}"]))
        mr = (num(r.get("mx")), num(r.get("my")))
        rows.append({
            "label": o["label"], "matched": r.get("label", "xy"),
            "P_ours": num(o[f"P_{args.combo}"]), "P_ram": num(r.get("p")),
            "Mx_ours": mo[0], "Mx_ram": mr[0], "My_ours": mo[1], "My_ram": mr[1],
            "M_ours": math.hypot(*mo), "M_ram": math.hypot(*mr),
        })

    matched = [r for r in rows if r["matched"] != "no" and not math.isnan(r["M_ram"])]
    if not matched:
        print("nothing matched; check labels or coordinates", file=sys.stderr)
        return 1
    median = statistics.median(r["M_ram"] for r in matched)
    floor = 0.2 * median
    tol_abs = 0.1 * median
    passed = 0
    for r in matched:
        err = r["M_ours"] - r["M_ram"]
        r["err_abs"] = err
        r["err_pct"] = 100.0 * err / r["M_ram"] if r["M_ram"] else float("nan")
        r["P_err_pct"] = 100.0 * (r["P_ours"] - r["P_ram"]) / r["P_ram"] if r["P_ram"] else float("nan")
        if r["M_ram"] >= floor:
            r["pass"] = abs(err) <= 0.10 * r["M_ram"]
            r["rule"] = "10%"
        else:
            r["pass"] = abs(err) <= tol_abs
            r["rule"] = "abs"
        passed += r["pass"]

    big = [r for r in matched if r["rule"] == "10%"]
    pcts = sorted(abs(r["err_pct"]) for r in big)
    print(f"matched {len(matched)} of {len(rows)}; median RAM |M| {median:.1f} kip-ft; "
          f"{len(big)} columns above the 20% floor")
    if pcts:
        print(f"|error| on those: median {pcts[len(pcts)//2]:.1f}%  p90 {pcts[int(0.9*(len(pcts)-1))]:.1f}%  max {pcts[-1]:.1f}%")
    print(f"pass: {passed}/{len(matched)}")
    p_pcts = sorted(abs(r["P_err_pct"]) for r in matched if not math.isnan(r["P_err_pct"]))
    if p_pcts:
        print(f"axial |error|: median {p_pcts[len(p_pcts)//2]:.1f}%  max {p_pcts[-1]:.1f}%")
    worst = sorted(big, key=lambda r: -abs(r["err_pct"]))[:10]
    print("worst columns:")
    for r in worst:
        print(f"  {r['label']:>6}  ours {r['M_ours']:8.1f}  ram {r['M_ram']:8.1f}  {r['err_pct']:+6.1f}%")

    if args.out:
        keys = ["label", "matched", "P_ours", "P_ram", "P_err_pct", "Mx_ours", "Mx_ram", "My_ours", "My_ram",
                "M_ours", "M_ram", "err_abs", "err_pct", "rule", "pass"]
        with Path(args.out).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
