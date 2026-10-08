#!/usr/bin/env python3
"""Thin-plate finite element model of one floor of the digital twin.

Gravity only. Columns are rotational and axial springs at the slab (far end
fixed or pinned), their footprints rigid patches; walls are pinned line
supports. The output is what RAM Concept reports as column reactions: P, Mx,
My per load case, which is the unbalanced moment into the column.

    python scripts/plate_fem.py verify
    python scripts/plate_fem.py run web/public/demos/1300-manhattan/result.json \
        --floor 4-5 --structure tasks/1300_manhattan/structure.json --out tasks/1300_manhattan/out/fem

Element: DKT triangle (Batoz, Bathe, Ho 1980). Units: feet, kips. Nodal DOFs
(w, tx, ty) with w positive up, tx = dw/dy, ty = -dw/dx (right-hand rotations
about x and y). `verify` checks the sign convention and the closed-form
plates and fails loudly if either is off.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import triangle as tr
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union

CONCRETE_KCF = 0.150
NU = 0.2
SNAP_FT = 1e-3  # linework snapped to this grid before meshing

DEFAULT_STRUCTURE = {
    "fc_psi": 6000,
    "slab_thickness_in": 8.0,
    "zones": {"BOUNDARY": {"sdl_psf": 20.0, "ll_psf": 40.0}, "ADDITIONAL-LOAD": {"sdl_psf": 20.0, "ll_psf": 100.0}},
    "story_height_ft": 10.5,
    "column_far_end": "fixed",
    "column_above": True,
    "column_stiffness_modifier": 1.0,
    "slab_stiffness_modifier": 1.0,
    "column_rigid_patch": True,
    "wall_rotation": "free",
    "wall_thickness_in": 10.0,
    "mesh_edge_ft": 1.5,
    "combos": {"D": {"DL": 1.0}, "L": {"LL": 1.0}, "D+L": {"DL": 1.0, "LL": 1.0}, "1.2D+1.6L": {"DL": 1.2, "LL": 1.6}},
}


# ---------------------------------------------------------------- DKT element


def dkt_stiffness(xy: np.ndarray, D: np.ndarray) -> np.ndarray:
    """9x9 stiffness of a DKT triangle. xy: (3,2) node coordinates."""
    x = xy[:, 0]
    y = xy[:, 1]
    # side k: 4 -> 23, 5 -> 31, 6 -> 12 ; x_ij = x_i - x_j
    pairs = {4: (1, 2), 5: (2, 0), 6: (0, 1)}
    P: Dict[int, float] = {}
    q: Dict[int, float] = {}
    t: Dict[int, float] = {}
    r: Dict[int, float] = {}
    for k, (i, j) in pairs.items():
        xij = x[i] - x[j]
        yij = y[i] - y[j]
        l2 = xij * xij + yij * yij
        P[k] = -6.0 * xij / l2
        q[k] = 3.0 * xij * yij / l2
        t[k] = -6.0 * yij / l2
        r[k] = 3.0 * yij * yij / l2

    x21, x31, x12 = x[1] - x[0], x[2] - x[0], x[0] - x[1]
    y21, y31, y12 = y[1] - y[0], y[2] - y[0], y[0] - y[1]
    twoA = x21 * y31 - x31 * y21
    if twoA <= 0:
        raise ValueError("clockwise or degenerate triangle")

    def hxy(xi: float, eta: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        P4, P5, P6 = P[4], P[5], P[6]
        q4, q5, q6 = q[4], q[5], q[6]
        t4, t5, t6 = t[4], t[5], t[6]
        r4, r5, r6 = r[4], r[5], r[6]
        Hx_xi = np.array([
            P6 * (1 - 2 * xi) + (P5 - P6) * eta,
            q6 * (1 - 2 * xi) - (q5 + q6) * eta,
            -4 + 6 * (xi + eta) + r6 * (1 - 2 * xi) - eta * (r5 + r6),
            -P6 * (1 - 2 * xi) + eta * (P4 + P6),
            q6 * (1 - 2 * xi) - eta * (q6 - q4),
            -2 + 6 * xi + r6 * (1 - 2 * xi) + eta * (r4 - r6),
            -eta * (P5 + P4),
            eta * (q4 - q5),
            -eta * (r5 - r4),
        ])
        Hy_xi = np.array([
            t6 * (1 - 2 * xi) + eta * (t5 - t6),
            1 + r6 * (1 - 2 * xi) - eta * (r5 + r6),
            -q6 * (1 - 2 * xi) + eta * (q5 + q6),
            -t6 * (1 - 2 * xi) + eta * (t4 + t6),
            -1 + r6 * (1 - 2 * xi) + eta * (r4 - r6),
            -q6 * (1 - 2 * xi) - eta * (q4 - q6),
            -eta * (t4 + t5),
            eta * (r4 - r5),
            -eta * (q4 - q5),
        ])
        Hx_eta = np.array([
            -P5 * (1 - 2 * eta) - xi * (P6 - P5),
            q5 * (1 - 2 * eta) - xi * (q5 + q6),
            -4 + 6 * (xi + eta) + r5 * (1 - 2 * eta) - xi * (r5 + r6),
            xi * (P4 + P6),
            xi * (q4 - q6),
            -xi * (r6 - r4),
            P5 * (1 - 2 * eta) - xi * (P4 + P5),
            q5 * (1 - 2 * eta) + xi * (q4 - q5),
            -2 + 6 * eta + r5 * (1 - 2 * eta) + xi * (r4 - r5),
        ])
        Hy_eta = np.array([
            -t5 * (1 - 2 * eta) - xi * (t6 - t5),
            1 + r5 * (1 - 2 * eta) - xi * (r5 + r6),
            -q5 * (1 - 2 * eta) + xi * (q5 + q6),
            xi * (t4 + t6),
            xi * (r4 - r6),
            -xi * (q4 - q6),
            t5 * (1 - 2 * eta) - xi * (t4 + t5),
            -1 + r5 * (1 - 2 * eta) + xi * (r4 - r5),
            -q5 * (1 - 2 * eta) - xi * (q4 - q5),
        ])
        return Hx_xi, Hy_xi, Hx_eta, Hy_eta

    K = np.zeros((9, 9))
    # 3-point rule, exact for the quadratic integrand
    for xi, eta in ((0.5, 0.0), (0.0, 0.5), (0.5, 0.5)):
        Hx_xi, Hy_xi, Hx_eta, Hy_eta = hxy(xi, eta)
        B = np.vstack([
            y31 * Hx_xi + y12 * Hx_eta,
            -x31 * Hy_xi - x12 * Hy_eta,
            -x31 * Hx_xi - x12 * Hx_eta + y31 * Hy_xi + y12 * Hy_eta,
        ]) / twoA
        K += (B.T @ D @ B) * (twoA / 2.0) / 3.0
    return K


def dkt_stiffness_batch(xy: np.ndarray, D: np.ndarray) -> np.ndarray:
    """(n,9,9) stiffness for n DKT triangles at once. xy: (n,3,2); D: (3,3) or (n,3,3).
    Same algebra as dkt_stiffness, vectorised over elements."""
    x = xy[:, :, 0]
    y = xy[:, :, 1]
    n = len(xy)
    pairs = {4: (1, 2), 5: (2, 0), 6: (0, 1)}
    P: Dict[int, np.ndarray] = {}
    q: Dict[int, np.ndarray] = {}
    t: Dict[int, np.ndarray] = {}
    r: Dict[int, np.ndarray] = {}
    for k, (i, j) in pairs.items():
        xij = x[:, i] - x[:, j]
        yij = y[:, i] - y[:, j]
        l2 = xij * xij + yij * yij
        P[k] = -6.0 * xij / l2
        q[k] = 3.0 * xij * yij / l2
        t[k] = -6.0 * yij / l2
        r[k] = 3.0 * yij * yij / l2
    x31, x12 = x[:, 2] - x[:, 0], x[:, 0] - x[:, 1]
    y31, y12 = y[:, 2] - y[:, 0], y[:, 0] - y[:, 1]
    x21, y21 = x[:, 1] - x[:, 0], y[:, 1] - y[:, 0]
    twoA = x21 * y31 - x31 * y21
    if np.any(twoA <= 0):
        raise ValueError("clockwise or degenerate triangle")
    P4, P5, P6 = P[4], P[5], P[6]
    q4, q5, q6 = q[4], q[5], q[6]
    t4, t5, t6 = t[4], t[5], t[6]
    r4, r5, r6 = r[4], r[5], r[6]
    one = np.ones(n)
    Dm = np.broadcast_to(D, (n, 3, 3))
    K = np.zeros((n, 9, 9))
    for xi, eta in ((0.5, 0.0), (0.0, 0.5), (0.5, 0.5)):
        Hx_xi = np.stack([
            P6 * (1 - 2 * xi) + (P5 - P6) * eta,
            q6 * (1 - 2 * xi) - (q5 + q6) * eta,
            (-4 + 6 * (xi + eta)) * one + r6 * (1 - 2 * xi) - eta * (r5 + r6),
            -P6 * (1 - 2 * xi) + eta * (P4 + P6),
            q6 * (1 - 2 * xi) - eta * (q6 - q4),
            (-2 + 6 * xi) * one + r6 * (1 - 2 * xi) + eta * (r4 - r6),
            -eta * (P5 + P4),
            eta * (q4 - q5),
            -eta * (r5 - r4),
        ], axis=1)
        Hy_xi = np.stack([
            t6 * (1 - 2 * xi) + eta * (t5 - t6),
            one + r6 * (1 - 2 * xi) - eta * (r5 + r6),
            -q6 * (1 - 2 * xi) + eta * (q5 + q6),
            -t6 * (1 - 2 * xi) + eta * (t4 + t6),
            -one + r6 * (1 - 2 * xi) + eta * (r4 - r6),
            -q6 * (1 - 2 * xi) - eta * (q4 - q6),
            -eta * (t4 + t5),
            eta * (r4 - r5),
            -eta * (q4 - q5),
        ], axis=1)
        Hx_eta = np.stack([
            -P5 * (1 - 2 * eta) - xi * (P6 - P5),
            q5 * (1 - 2 * eta) - xi * (q5 + q6),
            (-4 + 6 * (xi + eta)) * one + r5 * (1 - 2 * eta) - xi * (r5 + r6),
            xi * (P4 + P6),
            xi * (q4 - q6),
            -xi * (r6 - r4),
            P5 * (1 - 2 * eta) - xi * (P4 + P5),
            q5 * (1 - 2 * eta) + xi * (q4 - q5),
            (-2 + 6 * eta) * one + r5 * (1 - 2 * eta) + xi * (r4 - r5),
        ], axis=1)
        Hy_eta = np.stack([
            -t5 * (1 - 2 * eta) - xi * (t6 - t5),
            one + r5 * (1 - 2 * eta) - xi * (r5 + r6),
            -q5 * (1 - 2 * eta) + xi * (q5 + q6),
            xi * (t4 + t6),
            xi * (r4 - r6),
            -xi * (q4 - q6),
            t5 * (1 - 2 * eta) - xi * (t4 + t5),
            -one + r5 * (1 - 2 * eta) + xi * (r4 - r5),
            -q5 * (1 - 2 * eta) - xi * (q4 - q5),
        ], axis=1)
        B = np.stack([
            y31[:, None] * Hx_xi + y12[:, None] * Hx_eta,
            -x31[:, None] * Hy_xi - x12[:, None] * Hy_eta,
            -x31[:, None] * Hx_xi - x12[:, None] * Hx_eta + y31[:, None] * Hy_xi + y12[:, None] * Hy_eta,
        ], axis=1) / twoA[:, None, None]
        K += np.einsum("nki,nkl,nlj->nij", B, Dm, B) * (twoA / 6.0)[:, None, None]
    return K


def plate_D(E_ksf: float, t_ft: float, nu: float = NU) -> np.ndarray:
    d = E_ksf * t_ft ** 3 / (12.0 * (1.0 - nu * nu))
    return d * np.array([[1.0, nu, 0.0], [nu, 1.0, 0.0], [0.0, 0.0, (1.0 - nu) / 2.0]])


# ---------------------------------------------------------------- model


class PlateModel:
    def __init__(self, nodes: np.ndarray, tris: np.ndarray):
        self.nodes = nodes
        self.tris = tris
        self.n = len(nodes)
        self.ndof = 3 * self.n
        self.K_struct: Optional[sp.csr_matrix] = None
        self.spring = np.zeros(self.ndof)
        self.fixed = np.zeros(self.ndof, dtype=bool)
        self.master = -np.ones(self.n, dtype=int)  # slave node -> master node
        self.loads: Dict[str, np.ndarray] = {}

    def assemble(self, D_of_tri) -> None:
        """D_of_tri: callable e -> (3,3), or a (3,3) array for a uniform plate."""
        if callable(D_of_tri):
            D = np.stack([D_of_tri(e) for e in range(len(self.tris))])
        else:
            D = np.asarray(D_of_tri)
        Ke = dkt_stiffness_batch(self.nodes[self.tris], D)
        dofs = (3 * self.tris[:, :, None] + np.arange(3)[None, None, :]).reshape(len(self.tris), 9)
        rows = np.repeat(dofs, 9, axis=1).ravel()
        cols = np.tile(dofs, (1, 9)).ravel()
        self.K_struct = sp.coo_matrix((Ke.ravel(), (rows, cols)), shape=(self.ndof, self.ndof)).tocsr()

    def area_load(self, name: str, q_of_tri) -> None:
        """Uniform pressure q (ksf, positive down) per triangle, lumped to w DOFs."""
        f = self.loads.setdefault(name, np.zeros(self.ndof))
        for e, tri in enumerate(self.tris):
            q = q_of_tri(e)
            if q == 0.0:
                continue
            p = self.nodes[tri]
            a = 0.5 * abs((p[1, 0] - p[0, 0]) * (p[2, 1] - p[0, 1]) - (p[2, 0] - p[0, 0]) * (p[1, 1] - p[0, 1]))
            for n in tri:
                f[3 * n] -= q * a / 3.0

    def tie_rigid(self, slaves: Sequence[int], master: int) -> None:
        for s in slaves:
            if s != master:
                self.master[s] = master

    def _transform(self) -> sp.csr_matrix:
        """T maps reduced (master) DOFs to full DOFs: u_full = T u_red."""
        keep = np.where(self.master < 0)[0]
        red_index = -np.ones(self.n, dtype=int)
        red_index[keep] = np.arange(len(keep))
        rows, cols, vals = [], [], []
        for s in range(self.n):
            m = self.master[s] if self.master[s] >= 0 else s
            mr = red_index[m]
            if mr < 0:
                raise ValueError("master is itself a slave")
            dx = self.nodes[s, 0] - self.nodes[m, 0]
            dy = self.nodes[s, 1] - self.nodes[m, 1]
            # w_s = w_m + tx*dy - ty*dx ; rotations equal
            rows += [3 * s, 3 * s, 3 * s, 3 * s + 1, 3 * s + 2]
            cols += [3 * mr, 3 * mr + 1, 3 * mr + 2, 3 * mr + 1, 3 * mr + 2]
            vals += [1.0, dy, -dx, 1.0, 1.0]
        return sp.coo_matrix((vals, (rows, cols)), shape=(self.ndof, 3 * len(keep))).tocsr(), keep

    def solve(self) -> Dict[str, np.ndarray]:
        assert self.K_struct is not None
        T, keep = self._transform()
        K = T.T @ (self.K_struct + sp.diags(self.spring)) @ T
        fixed_red = (T.T @ self.fixed.astype(float)) > 0
        free = np.where(~fixed_red)[0]
        Kff = K[free][:, free].tocsc()
        lu = spla.splu(Kff)
        out = {}
        for name, f in self.loads.items():
            fr = T.T @ f
            u_red = np.zeros(K.shape[0])
            u_red[free] = lu.solve(fr[free])
            out[name] = T @ u_red
        self.displacements = out
        return out

    def reactions(self, u: np.ndarray, f: np.ndarray) -> np.ndarray:
        """Force from the slab into its supports, per DOF (w up, right-hand
        rotations). At a rigid-patch master this is the patch resultant; slave
        DOFs read zero. Equal to spring*u at spring DOFs."""
        assert self.K_struct is not None
        T, keep = self._transform()
        R_red = T.T @ (f - self.K_struct @ u)
        R = np.zeros(self.ndof)
        for i, n in enumerate(keep):
            R[3 * n: 3 * n + 3] = R_red[3 * i: 3 * i + 3]
        return R


# ---------------------------------------------------------------- meshing


def _ring(coords) -> List[Tuple[float, float]]:
    pts = [tuple(c[:2]) for c in coords]
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _clean_lines(geom) -> List[LineString]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom] if geom.length > 1e-3 else []
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        out: List[LineString] = []
        for g in geom.geoms:
            out.extend(_clean_lines(g))
        return out
    return []


def _clean_polys(geom) -> List[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom] if geom.area > 1e-4 else []
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        out: List[Polygon] = []
        for g in geom.geoms:
            out.extend(_clean_polys(g))
        return out
    return []


def mesh_floor(slab: Polygon, footprints: List[Optional[Polygon]], centres: List[Tuple[float, float]],
               walls: List[LineString], edge_ft: float) -> Tuple[np.ndarray, np.ndarray, List[int]]:
    """Constrained Delaunay mesh of the slab with footprints and walls as segments.
    Returns nodes, triangles and the node index of each column centre.

    Footprints and walls are clipped to the slab, footprints are carved out of
    the walls (the prep tool places walls over columns), and coincident
    vertices are merged, because overlapping input crashes `triangle`."""
    verts: List[Tuple[float, float]] = []
    index: Dict[Tuple[float, float], int] = {}
    segs: set = set()
    holes: List[Tuple[float, float]] = []

    def vid(pt) -> int:
        key = (round(round(pt[0] / SNAP_FT) * SNAP_FT, 6), round(round(pt[1] / SNAP_FT) * SNAP_FT, 6))
        if key not in index:
            index[key] = len(verts)
            verts.append(key)
        return index[key]

    def add_path(path, closed: bool):
        ids = [vid(p) for p in path]
        if closed:
            ids.append(ids[0])
        for a, b in zip(ids, ids[1:]):
            if a != b:
                segs.add((min(a, b), max(a, b)))

    slab = slab.buffer(0)
    polys = _clean_polys(slab)
    linework = []
    for poly in polys:
        linework.append(LineString(poly.exterior.simplify(1e-4).coords))
        for hole in poly.interiors:
            linework.append(LineString(hole.coords))
            holes.append(tuple(Polygon(hole).representative_point().coords[0]))

    patches: List[Optional[Polygon]] = []
    for fp in footprints:
        if fp is None or fp.is_empty:
            patches.append(None)
            continue
        clipped = _clean_polys(fp.buffer(0).simplify(0.02).intersection(slab))
        patches.append(max(clipped, key=lambda g: g.area) if clipped else None)
    patch_union = unary_union([p for p in patches if p is not None]) if any(p is not None for p in patches) else None
    for fp in patches:
        if fp is not None:
            linework.append(LineString(fp.exterior.coords))

    for w in walls:
        g = w.intersection(slab)
        if patch_union is not None:
            g = g.difference(patch_union.buffer(0.05))
        linework.extend(_clean_lines(g))

    # node everything together: shared and crossing segments become one set
    # of non-overlapping pieces, which is what triangle needs
    noded = shapely.set_precision(unary_union(linework), SNAP_FT)
    noded = unary_union(noded)
    for line in _clean_lines(noded):
        add_path([tuple(c[:2]) for c in line.coords], False)

    centre_idx = []
    for c in centres:
        key = (round(round(c[0] / SNAP_FT) * SNAP_FT, 6), round(round(c[1] / SNAP_FT) * SNAP_FT, 6))
        if key in index:
            centre_idx.append(index[key])
        elif noded.distance(Point(c)) < 2 * SNAP_FT:
            centre_idx.append(-1)  # on a segment: resolved to the nearest node after meshing
        else:
            centre_idx.append(vid(c))

    A = {"vertices": np.array(verts, dtype=float), "segments": np.array(sorted(segs), dtype=int)}
    if holes:
        A["holes"] = np.array(holes, dtype=float)
    max_area = (edge_ft ** 2) * math.sqrt(3) / 4.0
    B = tr.triangulate(A, f"pq30a{max_area:.6f}")
    nodes = B["vertices"]
    tris = B["triangles"]
    # triangle keeps input vertices first, in order
    assert np.allclose(nodes[: len(verts)], np.array(verts), atol=1e-6)
    for i, c in enumerate(centres):
        if centre_idx[i] < 0:
            centre_idx[i] = int(np.argmin(np.hypot(nodes[:, 0] - c[0], nodes[:, 1] - c[1])))
    return nodes, tris, centre_idx


# ---------------------------------------------------------------- column properties


def polygon_second_moments(poly: Polygon) -> Tuple[float, float, float]:
    """Area, Ixx, Iyy about the centroid (ft^2, ft^4)."""
    pts = _ring(poly.exterior.coords)
    cx, cy = poly.centroid.x, poly.centroid.y
    A = Ixx = Iyy = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i][0] - cx, pts[i][1] - cy
        x1, y1 = pts[(i + 1) % n][0] - cx, pts[(i + 1) % n][1] - cy
        cross = x0 * y1 - x1 * y0
        A += cross / 2.0
        Ixx += cross * (y0 * y0 + y0 * y1 + y1 * y1) / 12.0
        Iyy += cross * (x0 * x0 + x0 * x1 + x1 * x1) / 12.0
    s = 1.0 if A > 0 else -1.0
    return abs(A), s * Ixx, s * Iyy


def concrete_E_ksf(fc_psi: float) -> float:
    return 57000.0 * math.sqrt(fc_psi) * 144.0 / 1000.0


# ---------------------------------------------------------------- floor run


def load_floor(result_json: Path, floor_id: str) -> Dict:
    data = json.loads(result_json.read_text())
    geom = data.get("geometry", data)
    for fl in geom["floors"]:
        if str(fl["floor_id"]) == str(floor_id):
            return fl
    raise SystemExit(f"floor {floor_id!r} not in {[f['floor_id'] for f in geom['floors']]}")


def run_floor(fl: Dict, st: Dict, out_dir: Path) -> List[Dict]:
    E = concrete_E_ksf(st["fc_psi"])
    t = st["slab_thickness_in"] / 12.0
    slab = shape(fl["slab_boundary"])
    zones = [(z["layer"], shape(z["boundary"])) for z in fl.get("load_zones", [])]
    cols = fl["columns"]
    centres = [tuple(c["point"]) for c in cols]
    footprints = [shape(c["footprint"]) if c.get("footprint") else None for c in cols]
    if not st.get("column_rigid_patch", True):
        footprints = [None] * len(cols)
    walls = [shape(w["wall_line"]) for w in fl.get("walls", [])]
    walls = [w for w in walls if w.length > 0.5]

    nodes, tris, centre_idx = mesh_floor(slab, footprints, centres, walls, st["mesh_edge_ft"])
    model = PlateModel(nodes, tris)
    D = plate_D(E * st["slab_stiffness_modifier"], t)
    model.assemble(lambda e: D)

    # loads by zone: a triangle belongs to the smallest zone containing its centroid
    cen = nodes[tris].mean(axis=1)
    zone_of_tri = ["BOUNDARY"] * len(tris)
    for layer, poly in sorted(zones, key=lambda z: -z[1].area):  # smallest zone wins
        for e in np.where(shapely.contains_xy(poly, cen[:, 0], cen[:, 1]))[0]:
            zone_of_tri[e] = layer
    sw = CONCRETE_KCF * t
    zone_loads = st["zones"]

    def zl(layer, key):
        return zone_loads.get(layer, zone_loads.get("BOUNDARY", {})).get(key, 0.0) / 1000.0

    model.area_load("DL", lambda e: sw + zl(zone_of_tri[e], "sdl_psf"))
    model.area_load("LL", lambda e: zl(zone_of_tri[e], "ll_psf"))

    # columns: rigid patch + springs at centre node
    H = st["story_height_ft"]
    far = 4.0 if st["column_far_end"] == "fixed" else 3.0
    for i, c in enumerate(cols):
        m = centre_idx[i]
        fp = footprints[i]
        if fp is not None:
            # linework was snapped to SNAP_FT before meshing, so nodes sit up to SNAP_FT/2 off the footprint
            inside = np.where(shapely.distance(fp, shapely.points(nodes)) <= 2 * SNAP_FT)[0]
            model.tie_rigid([int(k) for k in inside], m)
            A, Ixx, Iyy = polygon_second_moments(fp)
        else:
            src = shape(c["footprint"]) if c.get("footprint") else Point(c["point"]).buffer(1.0, 4)
            A, Ixx, Iyy = polygon_second_moments(src)
        n_legs = 1 + (1 if (st.get("column_above", True) and not c.get("ends_here", False)) else 0)
        mod = st["column_stiffness_modifier"]
        model.spring[3 * m] += n_legs * E * A / H * mod
        model.spring[3 * m + 1] += n_legs * far * E * Ixx / H * mod
        model.spring[3 * m + 2] += n_legs * far * E * Iyy / H * mod

    # walls: pin every node on the wall line
    wall_nodes: List[int] = []
    if walls:
        wall_geom = unary_union(walls)
        wall_nodes = [int(k) for k in np.where(shapely.distance(wall_geom, shapely.points(nodes)) <= 2 * SNAP_FT)[0]]
    wall_nodes = [k for k in wall_nodes if model.master[k] < 0 and k not in centre_idx]
    for k in wall_nodes:
        model.fixed[3 * k] = True
        if st.get("wall_rotation") == "fixed":
            model.fixed[3 * k + 1] = True
            model.fixed[3 * k + 2] = True

    U = model.solve()
    R_case = {name: model.reactions(u, model.loads[name]) for name, u in U.items()}
    results = []
    for i, c in enumerate(cols):
        m = centre_idx[i]
        row = {"label": c.get("label", f"C{i}"), "x": c["point"][0], "y": c["point"][1],
               "trib_sf": c.get("area_sf")}
        cases = {}
        for name, R in R_case.items():
            cases[name] = (-R[3 * m], R[3 * m + 1], R[3 * m + 2])
        for combo, factors in st["combos"].items():
            P = sum(f * cases[k][0] for k, f in factors.items())
            Mx = sum(f * cases[k][1] for k, f in factors.items())
            My = sum(f * cases[k][2] for k, f in factors.items())
            row[f"P_{combo}"] = round(P, 3)
            row[f"Mx_{combo}"] = round(Mx, 3)
            row[f"My_{combo}"] = round(My, 3)
            row[f"M_{combo}"] = round(math.hypot(Mx, My), 3)
        results.append(row)

    out_dir.mkdir(parents=True, exist_ok=True)
    fid = str(fl["floor_id"]).replace("/", "_")
    with (out_dir / f"column_reactions_{fid}.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    summary = {
        "floor_id": fl["floor_id"], "nodes": int(len(nodes)), "triangles": int(len(tris)),
        "columns": len(cols), "wall_nodes": len(wall_nodes), "structure": st,
        "load_check": {
            name: {"applied_kips": round(-float(model.loads[name][0::3].sum()), 2),
                   "reacted_kips": round(float(-R_case[name][0::3].sum()), 2)}
            for name in U
        },
        "max_deflection_in": {name: round(float(-U[name][0::3].min() * 12.0), 3) for name in U},
    }
    (out_dir / f"summary_{fid}.json").write_text(json.dumps(summary, indent=2))
    return results, summary


# ---------------------------------------------------------------- verification


def _rect_mesh(a: float, b: float, n: int) -> Tuple[np.ndarray, np.ndarray]:
    xs = np.linspace(0, a, n + 1)
    ys = np.linspace(0, b, n + 1)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    nodes = np.column_stack([X.ravel(), Y.ravel()])
    tris = []
    for i in range(n):
        for j in range(n):
            n00 = i * (n + 1) + j
            n10 = (i + 1) * (n + 1) + j
            n01 = n00 + 1
            n11 = n10 + 1
            tris.append((n00, n10, n11))
            tris.append((n00, n11, n01))
    return nodes, np.array(tris)


def verify() -> int:
    ok = True
    E, t, q, a = 1000.0, 0.1, 1.0, 1.0
    Dm = plate_D(E, t)
    Dflex = E * t ** 3 / (12 * (1 - NU * NU))

    def plate(n, clamped):
        nodes, tris = _rect_mesh(a, a, n)
        m = PlateModel(nodes, tris)
        m.assemble(lambda e: Dm)
        m.area_load("q", lambda e: q)
        edge = (np.isclose(nodes[:, 0], 0) | np.isclose(nodes[:, 0], a) | np.isclose(nodes[:, 1], 0) | np.isclose(nodes[:, 1], a))
        for k in np.where(edge)[0]:
            m.fixed[3 * k] = True
            if clamped:
                m.fixed[3 * k + 1] = m.fixed[3 * k + 2] = True
            else:
                # simply supported: rotation about the edge free, along the edge zero
                if np.isclose(nodes[k, 0], 0) or np.isclose(nodes[k, 0], a):
                    m.fixed[3 * k + 1] = True
                else:
                    m.fixed[3 * k + 2] = True
        u = m.solve()["q"]
        centre = np.argmin(np.hypot(nodes[:, 0] - a / 2, nodes[:, 1] - a / 2))
        return -u[3 * centre] * Dflex / (q * a ** 4)

    for clamped, target in ((False, 0.00406), (True, 0.00126)):
        got = plate(16, clamped)
        err = abs(got - target) / target
        print(f"square plate {'clamped' if clamped else 'simply supported'}: "
              f"w_c D/(q a^4) = {got:.5f}  Timoshenko {target}  err {err*100:.2f}%")
        ok &= err < 0.02

    # sign convention: cantilever strip, tip load down; ty should equal -dw/dx
    nodes, tris = _rect_mesh(4.0, 0.5, 16)
    m = PlateModel(nodes, tris)
    m.assemble(lambda e: Dm)
    f = np.zeros(m.ndof)
    tip = np.where(np.isclose(nodes[:, 0], 4.0))[0]
    f[3 * tip] = -0.01 / len(tip)
    m.loads["tip"] = f
    for k in np.where(np.isclose(nodes[:, 0], 0))[0]:
        m.fixed[3 * k: 3 * k + 3] = True
    u = m.solve()["tip"]
    row = np.where(np.isclose(nodes[:, 1], 0.25))[0]
    row = row[np.argsort(nodes[row, 0])]
    dwdx = np.gradient(u[3 * row], nodes[row, 0])
    ty = u[3 * row + 2]
    rel = np.abs(ty[2:-2] + dwdx[2:-2]).max() / np.abs(dwdx).max()
    print(f"cantilever: max |ty + dw/dx| / max|dw/dx| = {rel:.3f} (ty = -dw/dx expected)")
    ok &= rel < 0.05
    # and the fixed-end moment reaction equals P*L
    R = m.reactions(u, f)
    root = np.where(np.isclose(nodes[:, 0], 0))[0]
    My_root = R[3 * root + 2].sum() + (R[3 * root] * (nodes[root, 0] - 0.0)).sum()
    print(f"cantilever root moment {My_root:.5f} kip-ft, P*L = {0.01*4.0:.5f}")
    ok &= abs(abs(My_root) - 0.04) / 0.04 < 0.02

    # rigid patch + column spring: an interior column of a 4-bay flat plate, symmetric, M ~ 0;
    # shift the column off-centre and the moment must be nonzero and equilibrate the patch
    nodes, tris = _rect_mesh(20.0, 20.0, 20)
    m = PlateModel(nodes, tris)
    m.assemble(lambda e: Dm)
    m.area_load("q", lambda e: q)
    for k in np.where(np.isclose(nodes[:, 0], 0) | np.isclose(nodes[:, 0], 20) | np.isclose(nodes[:, 1], 0) | np.isclose(nodes[:, 1], 20))[0]:
        m.fixed[3 * k] = True
    patch = np.where((np.abs(nodes[:, 0] - 8.0) <= 1.0 + 1e-9) & (np.abs(nodes[:, 1] - 10.0) <= 1.0 + 1e-9))[0]
    master = patch[np.argmin(np.hypot(nodes[patch, 0] - 8.0, nodes[patch, 1] - 10.0))]
    m.tie_rigid(patch, master)
    m.spring[3 * master] = 1e6
    m.spring[3 * master + 1] = m.spring[3 * master + 2] = 1e3
    u = m.solve()["q"]
    R = m.reactions(u, m.loads["q"])
    Pm = -R[3 * master]
    Mx, My = R[3 * master + 1], R[3 * master + 2]
    tot = -m.loads["q"][0::3].sum()
    print(f"off-centre column: P = {Pm:.3f} of {tot:.1f} total, Mx = {Mx:.4f} (symmetric, ~0), My = {My:.4f} (nonzero)")
    ok &= abs(Mx) < 1e-2 * abs(My) and abs(My) > 0 and 0.2 * tot < Pm < 0.8 * tot
    # the reaction equals the spring force, and all supports carry the whole load
    ks = m.spring[3 * master + 2] * u[3 * master + 2]
    print(f"  spring My k*theta = {ks:.4f}; reaction My = {My:.4f}; total reaction {R[0::3].sum():.3f} vs load {-tot:.3f}")
    ok &= abs(ks - My) / abs(My) < 1e-6 and abs(R[0::3].sum() + tot) < 1e-6 * tot

    rng = np.random.default_rng(0)
    pts = rng.random((50, 3, 2)) * 5
    for k in range(len(pts)):
        a, b = pts[k, 1] - pts[k, 0], pts[k, 2] - pts[k, 0]
        if a[0] * b[1] - a[1] * b[0] < 0:
            pts[k, [1, 2]] = pts[k, [2, 1]]
    Kb = dkt_stiffness_batch(pts, Dm)
    Ks = np.stack([dkt_stiffness(p, Dm) for p in pts])
    dev = np.abs(Kb - Ks).max() / np.abs(Ks).max()
    print(f"batched vs scalar element stiffness: max relative deviation {dev:.2e}")
    ok &= dev < 1e-12

    print("VERIFY", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------- cli


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify")
    r = sub.add_parser("run")
    r.add_argument("result_json", help="result.json or geometry.json from the engine")
    r.add_argument("--floor", required=True, help="floor_id, e.g. 4-5")
    r.add_argument("--structure", help="structure.json (thickness, loads, heights); defaults used when absent")
    r.add_argument("--out", required=True)
    r.add_argument("--mesh", type=float, help="target element edge, ft")
    args = ap.parse_args()
    if args.cmd == "verify":
        return verify()
    st = dict(DEFAULT_STRUCTURE)
    if args.structure:
        st.update(json.loads(Path(args.structure).read_text()))
    if args.mesh:
        st["mesh_edge_ft"] = args.mesh
    fl = load_floor(Path(args.result_json), args.floor)
    results, summary = run_floor(fl, st, Path(args.out))
    print(json.dumps({k: v for k, v in summary.items() if k != "structure"}, indent=2))
    top = sorted(results, key=lambda r: -r["M_D+L"])[:8]
    print("largest unbalanced moments, D+L (kip-ft):")
    for r in top:
        print(f"  {r['label']:>6}  P {r['P_D+L']:8.1f}  Mx {r['Mx_D+L']:8.1f}  My {r['My_D+L']:8.1f}  |M| {r['M_D+L']:8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
