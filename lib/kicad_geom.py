#!/usr/bin/env python3
"""KiCad PCB -> multiport FastHenry input for power-stage parasitic extraction.

Runs under KiCad's bundled python (pcbnew). Builds ONE FastHenry model of the
half-bridge power copper with a handful of ports so a single solve yields the full
mutual-inductance matrix:

    Port  P_pwr  : across the nearest Cin (Vin+ <-> GND-)   -> commutation loop
    Port  P_ghs  : HS gate driver-end <-> HS gate return    -> HS gate loop
    Port  P_gls  : LS gate driver-end <-> LS gate return    -> LS gate loop

Plus conduction ports for the LF per-switch R split (see conduction_ref):
    Port  P_bulk : across the nearest BULK electrolytic     -> LF conduction loop
    Port  P_hs   : Vin(bulk) -> HS SW pad (via HS leads)     -> HS conduction R
    Port  P_ls   : LS SW pad -> GND(bulk) (via LS leads)     -> LS conduction R

The HF loop (P_pwr) anchors on the ceramic that sources the commutation edge; the
LF conduction R (P_hs/P_ls) anchors on the bulk cap that sources the 39 kHz
fundamental — R read at the lowest swept freq (~DC) vs the plateau for L/ring-R.

Both FET channels are shorted at the die plane (`.equiv drain_die source_die`)
and each gate is closed to its source at the die (`.equiv gate_die source_die`),
so P_pwr traces the full Cin->HS->SW->LS->GND->Cin shoot-through loop and each
gate loop shares that FET's source lead. The **common-source inductance** then
falls out as the mutual M(P_pwr, P_gate): the shared source-lead partial L. The
gate-return node position encodes Kelvin (die-source, CSI excluded) vs non-Kelvin
(power-source pad, CSI included).

Meshing: tracks -> filaments; copper pours -> a gridded filament mesh clipped to
the real filled polygon; vias -> vertical filaments; THT pads -> vertical stacks;
FET leads -> vertical stubs to a die plane. Nodes are interned by
(net, layer, snapped-xy) so coincident same-net endpoints merge (the coincident-
node fix that otherwise makes FastHenry return NaN). A union-find prune keeps only
copper reachable from the ports.

Output: a FastHenry `.inp` plus a sidecar `<inp>.ports.json` naming the ports and
the discovered topology for the reduce step.
"""
import argparse
import json
import math
import os
import re
import sys

import pcbnew

import fet_discovery

HERE = os.path.dirname(os.path.abspath(__file__))
NM = 1e6                       # KiCad internal units (nm) per mm
CU_T = 0.035                   # copper thickness, mm (1 oz)
SIGMA = 5.8e4                  # copper conductivity at 20 C, S/mm  (5.8e7 S/m)
ALPHA_CU = 0.0039              # copper temp coeff of resistance, 1/K (~0.39%/K)
SNAP = 0.02                    # node-identity grid, mm (merge coincident nodes)


def sigma_at(temp_c):
    """Copper conductivity (S/mm) at temp_c, from the 20 C reference. R scales as
    1/sigma, so this makes FastHenry report R at operating temperature; L (purely
    geometric) is unaffected. Isothermal — no self-heating / R(T) feedback."""
    return SIGMA / (1.0 + ALPHA_CU * (temp_c - 20.0))


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def mm(v):
    return v / NM


# Diagnostic: DCDC_ONLY_FB=1 restricts the copper stack to F.Cu + B.Cu only
# (inner layers ignored, through-vias collapse to an F<->B barrel). This makes
# the grid extractor model the same 2-layer copper KiPEX's polygon path sees,
# for a controlled mesher-vs-mesher comparison. Board surgery (deleting inner
# zones) does NOT work — it severs via landings and opens the loop — because the
# inner planes are load-bearing; this filter keeps the through-vias so the loop
# still closes. Not a product feature; off unless the env var is set.
_ONLY_FB = os.environ.get("DCDC_ONLY_FB") == "1"


def _cu_stack(board):
    """Enabled copper layers top->bottom, optionally filtered to F/B only."""
    cu = list(board.GetEnabledLayers().CuStack())
    if _ONLY_FB:
        keep = {pcbnew.F_Cu, pcbnew.B_Cu}
        cu = [l for l in cu if l in keep]
    return cu


def layer_z_map(board):
    """Return {layer_id: z_mm} for the copper stack (top = 0, going down)."""
    cu = _cu_stack(board)
    n = len(cu)
    thick = 1.6
    try:
        thick = mm(board.GetDesignSettings().GetBoardThickness())
    except Exception:
        pass
    # even distribution top(0) .. bottom(-thick); good enough w/o full stackup read
    if n == 1:
        return {cu[0]: 0.0}
    return {lid: -thick * i / (n - 1) for i, lid in enumerate(cu)}


class Model:
    """Accumulates FastHenry nodes/segments/equivs/ports with node interning."""

    def __init__(self, cu_thickness=CU_T, terminal_mode="padland"):
        self._nodes = {}          # key -> node name
        self._pos = {}            # node name -> (x,y,z)
        self.meta = {}            # node name -> (net, layer)
        self.zone_nodes = set()   # names created as pour-mesh nodes
        self.keep_nodes = set()   # unported conductor components intentionally retained
        self.distributed_terminals = set()  # pad-land terminals already bonded to zone nodes
        self.segs = []            # (name, na, nb, w, h)
        self.equivs = []          # (na, nb)
        self.ports = []           # (label, na, nb)  -> FastHenry .external (solved)
        self.aux_ports = {}       # label -> (na, nb): node pairs for downstream DC tools
                                  # (e.g. loss-density), NOT solved by FastHenry
        self.cin_ports = ["P_pwr"]  # labels of the commutation (Cin) ports
        self.terminal_regions = []  # diagnostics for pad-land distributed contacts
        self.terminal_fallbacks = []  # diagnostics for pad-land fallbacks
        self.zone_mesh_notes = []  # diagnostics for guarded polygon zone meshing
        self.via_merge = None      # provenance for --merge-vias clustering (None=off)
        self.pitch = None          # zone-mesh pitch (mm), set by build(); sizes the
                                   # proximity radius for pad-in-void terminal bonding
        self.cu_thickness = cu_thickness
        self.terminal_mode = terminal_mode
        self._ni = 0
        self._si = 0

    def node(self, net, layer, x, y, z, zone=False):
        key = (net, layer, round(x / SNAP), round(y / SNAP), round(z, 3))
        nm_ = self._nodes.get(key)
        if nm_ is None:
            nm_ = f"N{self._ni}"
            self._ni += 1
            self._nodes[key] = nm_
            self._pos[nm_] = (x, y, z)
            self.meta[nm_] = (net, layer)
        if zone:
            self.zone_nodes.add(nm_)
        return nm_

    def stitch_zones(self, pitch):
        """Bond every non-zone node to the nearest pour node on the SAME net+layer,
        so tracks / vias / pads fuse into the pour mesh (fixes fragmented copper)."""
        buckets = {}
        for zn in self.zone_nodes:
            net, lid = self.meta[zn]
            x, y, _ = self._pos[zn]
            buckets.setdefault((net, lid), []).append((x, y, zn))
        if not buckets:
            return
        thr = (3 * pitch) ** 2
        for nm_, (net, lid) in sorted(self.meta.items(), key=lambda kv: str(kv[0])):
            if nm_ in self.zone_nodes:
                continue
            if nm_ in self.distributed_terminals:
                continue
            cand = buckets.get((net, lid))
            if not cand:
                continue
            x, y, _ = self._pos[nm_]
            bx, by, bn = min(cand, key=lambda t: ((t[0] - x) ** 2 + (t[1] - y) ** 2, t[1]))
            if (bx - x) ** 2 + (by - y) ** 2 < thr:
                self.seg(nm_, bn, pitch)

    def weld(self, tol):
        """Fuse near-coincident nodes on the SAME net+layer that interning missed —
        a track ending inside a pad (pad-centre vs trace-end), or two touching pour
        fills / stubs. Each node bonds to its nearest same-net+layer neighbour within
        `tol` mm. Restricted to one net+layer, so it can never short unrelated copper;
        `tol` is kept well below the pour pitch, so it never welds across the mesh grid
        (only endpoints that should have coincided). Fixes gate nets (no pour for
        stitch_zones to bond to) and isolated power-fill islands."""
        if tol <= 0:
            return 0
        cell = tol
        grid = {}
        for nm_, (net, lid) in self.meta.items():
            x, y, _ = self._pos[nm_]
            grid.setdefault((net, lid, round(x / cell), round(y / cell)), []).append(nm_)
        tol2 = tol * tol
        done = set()
        welded = 0
        for nm_, (net, lid) in sorted(self.meta.items(), key=lambda kv: str(kv[0])):
            x, y, _ = self._pos[nm_]
            cx, cy = round(x / cell), round(y / cell)
            best, bd = None, tol2
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for b in sorted(grid.get((net, lid, cx + dx, cy + dy), ()), key=str):
                        if b == nm_:
                            continue
                        xb, yb, _ = self._pos[b]
                        dd = (x - xb) ** 2 + (y - yb) ** 2
                        if best is None or dd < bd or (dd == bd and str(b) < str(best)):
                            bd, best = dd, b
            if best is not None:
                key = frozenset((nm_, best))
                if key not in done:
                    done.add(key)
                    self.seg(nm_, best, tol)
                    welded += 1
        return welded

    def seg(self, na, nb, w, h=None):
        if na == nb:
            return
        pa = self._pos.get(na)
        pb = self._pos.get(nb)
        if pa is not None and pb is not None:
            dx, dy, dz = pa[0]-pb[0], pa[1]-pb[1], pa[2]-pb[2]
            d2 = dx*dx + dy*dy + dz*dz
            if d2 < 1e-12:
                return
            if d2 < (0.01 * 0.01) and w > 0.2 and (dx*dx + dy*dy) > dz*dz:
                return
        self._si += 1
        if h is None:
            h = self.cu_thickness
        self.segs.append((f"E{self._si}", na, nb, max(w, 0.05), h))

    def equiv(self, na, nb):
        if na != nb:
            self.equivs.append((na, nb))

    def port(self, label, na, nb):
        self.ports.append((label, na, nb))

    def component(self, seeds):
        """Return nodes connected to `seeds` by modeled copper/equivs."""
        adj = {}
        for _, a, b, _, _ in self.segs:
            adj.setdefault(a, set()).add(b); adj.setdefault(b, set()).add(a)
        for a, b in self.equivs:
            adj.setdefault(a, set()).add(b); adj.setdefault(b, set()).add(a)
        seen = set()
        stack = list(seeds)
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(adj.get(n, ()))
        return seen

    # ----- connectivity prune: keep nodes reachable from any port -----
    def prune(self):
        seeds = set()
        for _, a, b in self.ports:
            seeds.add(a); seeds.add(b)
        seeds.update(self.keep_nodes)
        seen = self.component(seeds)
        self.segs = [s for s in self.segs if s[1] in seen and s[2] in seen]
        self.equivs = [e for e in self.equivs if e[0] in seen and e[1] in seen]
        return seen

    def drop_floating_ports(self, seed_label="P_pwr"):
        """Remove any port whose copper is NOT in the same connected component as
        the seed port. A single floating/disconnected port (e.g. a distant bulk cap
        whose pad never bonds into the meshed pour at a coarse pitch) makes
        FastHenry's ENTIRE solve NaN — so drop it and report, never let it poison
        every result. Returns the dropped labels."""
        if not self.ports:
            return []
        seed = next(((a, b) for lbl, a, b in self.ports if lbl == seed_label), None)
        if seed is None:
            seed = (self.ports[0][1], self.ports[0][2])
        seen = self.component(seed)
        kept, dropped = [], []
        for lbl, a, b in self.ports:
            (kept if (a in seen and b in seen) else dropped).append(
                (lbl, a, b) if (a in seen and b in seen) else lbl)
        self.ports = kept
        return dropped

    def write(self, path, fmin=1e3, fmax=1e8, ndec=3, nwinc=1, nhinc=1, sigma=SIGMA):
        keep = self.prune()
        lines = [
            "* auto-generated by dcdc-tools/parasitics/kicad_geom.py",
            ".units mm",
            f".default sigma={sigma:g} nwinc={nwinc} nhinc={nhinc} z=0",
            "",
        ]
        for nm_, (x, y, z) in self._pos.items():
            if nm_ in keep:
                lines.append(f"{nm_} x={x:.4f} y={y:.4f} z={z:.4f}")
        lines.append("")
        for name, a, b, w, h in self.segs:
            lines.append(f"{name} {a} {b} w={w:.4f} h={h:.4f}")
        lines.append("")
        for a, b in self.equivs:
            lines.append(f".equiv {a} {b}")
        lines.append("")
        for _, a, b in self.ports:
            lines.append(f".external {a} {b}")
        lines.append("")
        lines.append(f".freq fmin={fmin:g} fmax={fmax:g} ndec={ndec}")
        lines.append(".end")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return dict(nodes=len(keep), segs=len(self.segs), ports=len(self.ports))


def freq_count(fmin=1e3, fmax=1e8, ndec=3):
    """Number of FastHenry sweep points for `.freq fmin=... fmax=... ndec=...`.

    FastHenry steps by 10**(1/ndec) and includes fmin, stopping before the next
    point would exceed fmax.
    """
    if fmin <= 0 or fmax < fmin or ndec <= 0:
        return 0
    return int(math.floor(math.log10(fmax / fmin) * ndec)) + 1


def mesh_complexity(stats, nwinc=1, nhinc=1, fmin=1e3, fmax=1e8, ndec=3):
    """Return a coarse, monotonic FastHenry complexity estimate.

    The exact runtime depends on FastHenry internals and matrix conditioning, but
    these values track the practical knobs: segment count, filament subdivision,
    port count and number of solved frequency points. `work_units` is intentionally
    dimensionless; use it only for comparing extractor runs on the same machine.
    """
    nodes = int(stats.get("nodes") or 0)
    segs = int(stats.get("segs") or 0)
    ports = int(stats.get("ports") or 0)
    sub = max(1, int(nwinc)) * max(1, int(nhinc))
    filaments = segs * sub
    freqs = freq_count(fmin, fmax, ndec)
    work_units = filaments * filaments * max(ports, 1) * max(freqs, 1)
    return dict(nodes=nodes, segs=segs, ports=ports, nwinc=int(nwinc),
                nhinc=int(nhinc), filament_subdivisions=sub,
                filaments_est=filaments, freq_points=freqs,
                work_units=work_units)


# --------------------------------------------------------------------------- #
# copper collection & meshing
# --------------------------------------------------------------------------- #
def _pip(x, y, outline):
    """Even-odd ray-cast point-in-polygon. `outline` = [(x,y), ...] (mm). Pure —
    no pcbnew — so the containment guard is unit-testable without KiCad."""
    inside = False
    n = len(outline)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = outline[i]
        xj, yj = outline[j]
        if ((yi > y) != (yj > y)) and \
                (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def point_in_poly_with_holes(x, y, outline, holes=()):
    """True if (x,y) is inside `outline` and NOT inside any of `holes` (each a
    [(x,y),...] mm ring). Models a filled region with thermal-relief / clearance
    voids: a point in a hole is bare board, not copper."""
    if not _pip(x, y, outline):
        return False
    return not any(_pip(x, y, h) for h in holes)


def point_in_polys(x, y, polys):
    """True if (x,y) is inside ANY polygon in `polys`. Each entry is either a bare
    outline [(x,y),...] or an (outline, holes) pair. Pure helper — the KiCad-
    independent containment fallback and the unit-test entry point."""
    for p in polys:
        if isinstance(p, tuple) and len(p) == 2 and p and isinstance(p[0], list):
            outline, holes = p
        else:
            outline, holes = p, ()
        if point_in_poly_with_holes(x, y, outline, holes):
            return True
    return False


def _point_on_segment(x, y, ax, ay, bx, by, tol=1e-6):
    """True if (x,y) lies on segment a-b within `tol` mm."""
    dx, dy = bx - ax, by - ay
    px, py = x - ax, y - ay
    cross = dx * py - dy * px
    if abs(cross) > tol * max(1.0, (dx * dx + dy * dy) ** 0.5):
        return False
    dot = px * dx + py * dy
    if dot < -tol:
        return False
    return dot <= dx * dx + dy * dy + tol


def _point_on_ring(x, y, ring, tol=1e-6):
    if len(ring) < 2:
        return False
    prev = ring[-1]
    for cur in ring:
        if _point_on_segment(x, y, prev[0], prev[1], cur[0], cur[1], tol=tol):
            return True
        prev = cur
    return False


def point_in_poly_with_holes_inclusive(x, y, outline, holes=()):
    """Like point_in_poly_with_holes, but treats the outer copper edge as copper.

    KiCad/Shapely containment APIs vary on boundary points. The polygon zone mesh
    intentionally adds outline vertex coordinates as grid cuts, so boundary
    points must be accepted or real edge-side elements can disappear. Hole
    interiors remain voids; hole boundaries are accepted as copper edges.
    """
    if not (_pip(x, y, outline) or _point_on_ring(x, y, outline)):
        return False
    for h in holes:
        if _pip(x, y, h):
            return False
    return True


def point_in_polys_inclusive(x, y, polys):
    """Boundary-inclusive containment over extracted KiCad filled polygons."""
    for p in polys:
        if isinstance(p, tuple) and len(p) == 2 and p and isinstance(p[0], list):
            outline, holes = p
        else:
            outline, holes = p, ()
        if point_in_poly_with_holes_inclusive(x, y, outline, holes):
            return True
    return False


def _chain_pts(chain):
    """SHAPE_LINE_CHAIN -> [(x,y),...] in mm."""
    return [(mm(chain.CPoint(i).x), mm(chain.CPoint(i).y))
            for i in range(chain.PointCount())]


def _extract_polys(sps_list):
    """SHAPE_POLY_SET list -> [(outline, [hole,...]), ...] in mm, for the pure
    point-in-polygon fallback when the native Contains API is unavailable. Holes
    (thermal-relief / clearance voids inside the fill) are extracted so the
    fallback excludes them — a track through a hole is NOT inside pour copper."""
    polys = []
    for s in sps_list:
        for oi in range(s.OutlineCount()):
            outline = _chain_pts(s.Outline(oi))
            if len(outline) < 3:
                continue
            holes = []
            # Narrow except: only swallow a genuinely-absent Hole API (old KiCad
            # SHAPE_POLY_SET without HoleCount/Hole). Any OTHER error must NOT be
            # silently treated as "no holes" — that would re-introduce the
            # hole-blind bug (a track through a void wrongly dropped) unnoticed.
            try:
                for hi in range(s.HoleCount(oi)):
                    h = _chain_pts(s.Hole(oi, hi))
                    if len(h) >= 3:
                        holes.append(h)
            except AttributeError:
                pass
            polys.append((outline, holes))
    return polys


def _sps_contains(sps_list):
    """Return a predicate contains(x_mm, y_mm) for a list of SHAPE_POLY_SET.

    Prefer the native `sps.Contains(VECTOR2I)` (handles holes correctly); if that
    API is missing/raises on this KiCad build, fall back to a pure ray-cast over
    the extracted outlines AND holes (so the fallback is not hole-blind)."""
    def _native(x_mm, y_mm):
        pt = pcbnew.VECTOR2I(int(round(x_mm * NM)), int(round(y_mm * NM)))
        return any(s.Contains(pt) for s in sps_list)
    try:
        sps_list[0].Contains(pcbnew.VECTOR2I(0, 0))
        return _native
    except Exception:
        polys = _extract_polys(sps_list)
        return lambda x, y: point_in_polys(x, y, polys)


def _pad_sps(pad, lid):
    """Best-effort KiCad pad copper polygon for one layer.

    KiCad has changed `PAD.GetEffectivePolygon` signatures across releases; try
    the current layer-aware form first, then older fallbacks. Returns None if the
    pad shape cannot be polygonized on this build.
    """
    for args in ((lid, pcbnew.ERROR_INSIDE), (lid,), ()):
        try:
            sps = pad.GetEffectivePolygon(*args)
            if sps is not None and sps.OutlineCount() > 0:
                return sps
        except Exception:
            continue
    return None


def _pad_contains(pad, lid):
    """Return contains(x_mm, y_mm) for a pad's physical copper land on `lid`."""
    sps = _pad_sps(pad, lid)
    if sps is None:
        return None
    return _sps_contains([sps])


def build_pour_index(board, zmap, nets):
    """Per-(net, layer) containment predicate for FILLED same-net copper pours.

    Used by add_tracks to skip tracks routed inside their own pour (issue #6):
    such copper merges into the fill on the real board, so meshing it as a thin
    parallel filament is redundant and seeds circulating-current artifacts. Only
    nets that actually have a pour on a layer get an entry, and callers pass ONLY
    the nets that add_zones will mesh (power nets) — a track is dropped only where
    a substitute mesh exists, so a pour-less net (gate traces) is never dropped."""
    index = {}
    zones = [board.GetArea(i) for i in range(board.GetAreaCount())]
    for z in zones:
        net = z.GetNetname()
        if net not in nets:
            continue
        for lid in z.GetLayerSet().Seq():
            if zmap.get(lid) is None:
                continue
            sps = z.GetFilledPolysList(lid)
            if sps is None or sps.OutlineCount() == 0:
                continue
            index.setdefault((net, lid), []).append(sps)
    return {k: _sps_contains(v) for k, v in index.items()}


def _in_roi(x, y, roi):
    return roi is None or (roi[0] <= x <= roi[2] and roi[1] <= y <= roi[3])


# along-track containment sampling step (mm): smaller than typical pour voids /
# clearance moats, so a track dipping through a void between its endpoints is
# detected (a sample lands in the void -> not "inside" -> track kept).
_SAMPLE_STEP = 0.25
_MAX_POLYGON_VERTEX_CUTS = 80


def _track_samples(ax, ay, bx, by, step=_SAMPLE_STEP):
    """Points along the segment (endpoints inclusive) spaced <= `step` mm; always
    at least start/mid/end (3 points)."""
    length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    n = max(2, int(length / step) + 1)
    return [(ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(n + 1)]


def _axis_cuts(lo, hi, pitch, extras=()):
    """Monotonic mesh-axis cuts from regular pitch positions plus polygon vertices.

    The old pour mesh used only `lo + n*pitch` cuts. Adding filled-polygon vertex
    coordinates lets the mesh run along real copper boundaries/notches while still
    keeping the pitch lattice in large featureless areas.
    """
    if hi < lo:
        lo, hi = hi, lo
    vals = [lo, hi]
    n = max(1, int(math.ceil((hi - lo) / pitch)))
    vals.extend(lo + i * pitch for i in range(1, n))
    vals.extend(v for v in extras if lo <= v <= hi)
    vals.sort()
    out = []
    # Avoid pathological near-zero sliver cells from coincident KiCad vertices,
    # but keep tolerance below ordinary mesh/pad dimensions.
    tol = min(0.02, max(pitch * 0.02, 1e-6))
    for v in vals:
        if not out or abs(v - out[-1]) > tol:
            out.append(v)
    if out[0] != lo:
        out.insert(0, lo)
    if out[-1] != hi:
        out.append(hi)
    return out


def _poly_axis_vertices(polys):
    """Return x/y coordinates from polygon outlines and holes."""
    xs, ys = [], []
    for outline, holes in polys:
        for ring in (outline, *holes):
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    return xs, ys


def _bounded_poly_axis_vertices(polys):
    """Return polygon vertex cuts only when they will not explode the grid.

    KiCad filled polygons can contain many chamfer/arc/thermal vertices. Adding
    every distinct x and y coordinate creates a cross-product of mesh cells, which
    is not what KiPEX's adaptive quadtree does and can make a simple board
    unsolvable. Keep vertex-aligned cuts for genuinely simple outlines; otherwise
    use the pitch lattice and rely on segment containment to clip to copper.
    """
    xs, ys = _poly_axis_vertices(polys)
    ux = {round(x / SNAP) for x in xs}
    uy = {round(y / SNAP) for y in ys}
    if len(ux) + len(uy) > _MAX_POLYGON_VERTEX_CUTS:
        return [], [], dict(reason="too_many_polygon_vertex_cuts",
                            unique_x=len(ux), unique_y=len(uy),
                            max_unique=_MAX_POLYGON_VERTEX_CUTS)
    return xs, ys, None


def _polys_contains_inclusive(polys):
    return lambda x, y: point_in_polys_inclusive(x, y, polys)


def _segment_inside(contains, ax, ay, bx, by, step=_SAMPLE_STEP):
    """True only when a side segment is fully in copper, not crossing a hole."""
    return all(contains(px, py) for px, py in _track_samples(ax, ay, bx, by, step=step))


def add_tracks(board, model, zmap, nets, pour_index=None, roi=None):
    """Straight track segments (skip vias for now) -> filaments.

    Issue #6: a track whose whole span lies inside a same-net filled pour on the
    SAME layer is redundant with the pour mesh (same-net copper merges into the
    fill) and only seeds circulating-current artifacts, so skip it. To decide
    "whole span inside" robustly on real non-convex/holed pours, the track is
    sampled densely along its length (`_track_samples`, step < typical void) and
    dropped only if EVERY sample is inside the pour copper — a trace straddling
    the pour edge, or dipping through a thermal-relief/clearance void, keeps a
    sample outside and is retained. The drop is also gated on the ROI: add_zones
    only meshes the pour within `roi`, so we drop only where a mesh actually
    replaces the filament; a same-net track outside the meshed region is kept so
    it can't disconnect copper. ARC tracks are never dropped (their curved path
    bows off the sampled chord). Vias / layer-changing traces are untouched.

    Known limitations (acceptable for this redundant-filament cleanup, not the
    loop-L path): (1) containment is sampled on the track CENTERLINE only, not
    across its width — a wide track whose centerline stays inside the pour while
    one edge overhangs a notch/cutout is still dropped; (2) the 0.25 mm sample
    step is a heuristic (< typical pour void), not a hard bound read from the
    board's clearance rules, so a void narrower than the step between two
    adjacent samples could be missed. Both are far narrower than the un-guarded
    original and did not arise on the Fugu2 test board."""
    arc_t = getattr(pcbnew, "PCB_ARC_T", None)
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            continue
        net = t.GetNetname()
        if net not in nets:
            continue
        lid = t.GetLayer()
        z = zmap.get(lid)
        if z is None:
            continue
        a = t.GetStart(); b = t.GetEnd()
        ax, ay, bx, by = mm(a.x), mm(a.y), mm(b.x), mm(b.y)
        contains = pour_index.get((net, lid)) if pour_index else None
        if contains is not None and t.Type() != arc_t:
            samples = _track_samples(ax, ay, bx, by)
            if all(_in_roi(px, py, roi) and contains(px, py) for px, py in samples):
                continue
        w = mm(t.GetWidth())
        na = model.node(net, lid, ax, ay, z)
        nb = model.node(net, lid, bx, by, z)
        model.seg(na, nb, w)


def _emit_via_barrel(model, zmap, cu, net, x, y, d, top, bot):
    """One vertical filament stack (width d) spanning the top..bot copper layers."""
    span = [l for l in cu if _between(l, top, bot, cu)]
    prev = None
    for lid in span:
        z = zmap[lid]
        n = model.node(net, lid, x, y, z)
        if prev is not None:
            model.seg(prev, n, d)     # vertical barrel
        prev = n


def _cluster_vias(members, r2):
    """Greedy clique clustering: a via joins an existing cluster only if it is
    within sqrt(r2) mm of EVERY member already in it, else opens a new cluster.
    This guarantees every pair of merged vias is within the radius (a true bounded
    diameter), so two loop-path vias farther apart than the radius can never be
    lumped — unlike leader/single-link clustering, whose pairwise reach is 2*radius
    and depends on via enumeration order. Grouping is still order-sensitive in
    which clique forms, but the safety bound (no merged pair exceeds the radius)
    holds for any order."""
    clusters = []   # each: list of member dicts
    for v in members:
        for cl in clusters:
            if all((v["x"] - m["x"]) ** 2 + (v["y"] - m["y"]) ** 2 <= r2 for m in cl):
                cl.append(v)
                break
        else:
            clusters.append([v])
    return clusters


def _via_in_pour(pour_index, net, x, y, top, bot):
    """True only if (x,y) lies inside same-net FILLED pour copper on BOTH the via's
    top and bottom layers. This is the precondition for a moved (centroid) barrel
    to re-bond into the meshed pour via stitch_zones: the barrel ends must land on
    real same-net fill. A power via out in routed-track land (no pour, e.g. a
    track-to-track stitch) fails this and is left per-via — merging it would move
    it off its copper and rely on synthetic weld/stitch to reconnect."""
    ft = pour_index.get((net, top))
    fb = pour_index.get((net, bot))
    return bool(ft and fb and ft(x, y) and fb(x, y))


def _zone_reach_buckets(model):
    """{(net, layer): [(x,y),...]} of the pour-mesh node positions add_zones made,
    for checking whether a merged centroid barrel will bond via stitch_zones."""
    buckets = {}
    for zn in getattr(model, "zone_nodes", ()):
        net, lid = model.meta[zn]
        x, y, _ = model._pos[zn]
        buckets.setdefault((net, lid), []).append((x, y))
    return buckets


def _centroid_reaches_mesh(buckets, pour_index, net, span_lids, x, y, thr2):
    """True iff the merged centroid bonds on EVERY spanned layer that carries
    same-net pour — where "carries pour" means pour_index puts (x,y) inside the
    filled pour on that layer, OR the layer has same-net mesh nodes. On each such
    layer some pour-mesh node must be within sqrt(thr2)=3*pitch of (x,y).

    Two subtleties this closes:
    - "any layer" is insufficient: reaching the top pour but not the bottom pour
      drops the bottom bond the individual vias had (require EVERY pour layer).
    - a missing mesh bucket is NOT "no pour": if pour_index says the layer is
      filled at the centroid but add_zones made zero mesh nodes there, the barrel
      cannot bond that pour -> UNREACHABLE (fall back), not skipped.
    If NO spanned layer carries same-net pour, there is nothing to bond to at all
    -> not reachable (keep per-via)."""
    saw_pour = False
    for lid in span_lids:
        fn = pour_index.get((net, lid))
        pour_here = bool(fn and fn(x, y))
        nodes = buckets.get((net, lid))
        if not pour_here and not nodes:
            continue                         # genuinely no same-net pour on this layer
        saw_pour = True
        # strict < to MATCH stitch_zones' bond test (< thr); an inclusive <= here
        # would pass a centroid exactly at (3*pitch)^2 that stitch_zones then does
        # NOT bond -> the barrel would float and be silently pruned.
        if not nodes or not any((bx - x) ** 2 + (by - y) ** 2 < thr2
                                for (bx, by) in nodes):
            return False                     # pour present but no mesh node in reach
    return saw_pour


def add_vias(board, model, zmap, nets, merge_vias=False, merge_radius=1.0,
             merge_nets=None, pour_index=None, roi=None, pitch=None):
    """Vias -> vertical filaments spanning the copper layers they connect.

    With merge_vias, same-(net, top, bot) vias whose centres are all within
    merge_radius mm of each other (a clique, see _cluster_vias) are collapsed into
    ONE equivalent barrel at the cluster centroid, with width = sum of the member
    widths — so its cross-section area (hence conductance) equals the N parallel
    vias: exact parallel R, and a self-L that is a close lower bound for a tight
    cluster (a via field is genuinely low-L). This targets the dense plane-stitch
    via fields that dominate FastHenry's filament count on multilayer boards,
    while spatially-separated loop-path vias — never within merge_radius of one
    another — stay individually modelled, so the commutation-loop geometry that
    sets L_loop is untouched. Records provenance in model.via_merge. Default off:
    byte-identical legacy per-via barrels.

    A via is merge-eligible ONLY if all of:
      - its net is in merge_nets (the power nets add_zones meshes). Gate-net vias
        are always per-via: gate nets have no pour, so a moved gate barrel could
        only reconnect via weld(weld_tol), and merge_radius can exceed weld_tol —
        a floated gate barrel opens the gate loop and silently reports 0 CSI.
      - it sits inside same-net FILLED pour copper on both endpoint layers
        (_via_in_pour). This is the plane-stitch condition; a routed power via not
        embedded in a pour is left per-via so merging never moves a via off its
        real copper.
      - it lies inside the meshed ROI. add_zones only meshes pour within roi, so
        the zone nodes that stitch_zones re-bonds the moved centroid barrel to
        exist ONLY inside the ROI. A via inside the full-board pour but outside the
        ROI passes _via_in_pour yet has no mesh nodes to bond to — merging it would
        move it to a centroid with nothing to reconnect. Both the members and the
        centroid must be in-ROI; otherwise the cluster falls back to per-via.
      - (when pitch is given, i.e. add_zones has already run) the centroid must be
        within stitch_zones' 3*pitch of an actual same-net pour-MESH node on a
        spanned layer. Polygon/ROI containment is necessary but not sufficient: a
        coarse pitch on a small pour island can leave the grid with no sample near
        the centroid, so the merged barrel would float and be silently pruned.
        This reachability gate is the sufficient condition; without it the cluster
        falls back to per-via (each member bonds at its own real location)."""
    cu = _cu_stack(board)
    merge_nets = merge_nets or set()
    pour_index = pour_index or {}
    # reachability index: pour-mesh nodes add_zones already created (None if pitch
    # not supplied — e.g. a direct call before meshing — which disables the gate).
    reach = _zone_reach_buckets(model) if (pitch and hasattr(model, "zone_nodes")) else None
    reach_thr2 = (3.0 * pitch) ** 2 if pitch else 0.0
    vias = []
    for t in board.GetTracks():
        if t.Type() != pcbnew.PCB_VIA_T:
            continue
        net = t.GetNetname()
        if net not in nets:
            continue
        p = t.GetPosition()
        vias.append(dict(net=net, x=mm(p.x), y=mm(p.y), d=mm(t.GetWidth()),
                         top=t.TopLayer(), bot=t.BottomLayer()))

    if not merge_vias:
        for v in vias:
            _emit_via_barrel(model, zmap, cu, v["net"], v["x"], v["y"], v["d"],
                             v["top"], v["bot"])
        return

    # Split: only power-net vias embedded in their own filled pour on both endpoint
    # layers are merge-eligible; everything else (gate vias, routed power vias) is
    # emitted per-via, unchanged from the legacy path.
    mergeable, per_via = [], []
    n_powernet = 0
    for v in vias:
        if v["net"] in merge_nets:
            n_powernet += 1
            if (_in_roi(v["x"], v["y"], roi)
                    and _via_in_pour(pour_index, v["net"], v["x"], v["y"],
                                     v["top"], v["bot"])):
                mergeable.append(v)
                continue
        per_via.append(v)
    for v in per_via:
        _emit_via_barrel(model, zmap, cu, v["net"], v["x"], v["y"], v["d"],
                         v["top"], v["bot"])

    r2 = merge_radius * merge_radius
    groups = {}
    for v in mergeable:
        groups.setdefault((v["net"], v["top"], v["bot"]), []).append(v)
    # barrels_after counts EVERY barrel this pass emits — the per-via ones (gate
    # vias + off-pour/off-ROI power vias) plus the ones from the merge-eligible
    # groups below — so it is a faithful "total via barrels after merge" figure.
    emitted = len(per_via)
    merged = pour_fallback = unreachable_fallback = 0
    max_extent2 = 0.0    # widest merged-cluster diameter^2, for provenance
    for members in groups.values():
        for cl in _cluster_vias(members, r2):
            n = len(cl)
            if n == 1:
                v = cl[0]
                emitted += 1
                _emit_via_barrel(model, zmap, cu, v["net"], v["x"], v["y"], v["d"],
                                 v["top"], v["bot"])
                continue
            cx = sum(v["x"] for v in cl) / n
            cy = sum(v["y"] for v in cl) / n
            # The centroid must land in same-net pour AND in the meshed ROI AND
            # (when reachability is known) within stitch range of an actual pour
            # mesh node — else the merged barrel would float off the mesh and be
            # silently pruned. Any failure -> keep the cluster per-via so each
            # member bonds at its own real location.
            span = [l for l in cu if _between(l, cl[0]["top"], cl[0]["bot"], cu)]
            reachable = reach is None or _centroid_reaches_mesh(
                reach, pour_index, cl[0]["net"], span, cx, cy, reach_thr2)
            in_pour_roi = (_in_roi(cx, cy, roi)
                           and _via_in_pour(pour_index, cl[0]["net"], cx, cy,
                                            cl[0]["top"], cl[0]["bot"]))
            if not (in_pour_roi and reachable):
                pour_fallback += 1
                if in_pour_roi and not reachable:
                    unreachable_fallback += 1
                for v in cl:
                    emitted += 1
                    _emit_via_barrel(model, zmap, cu, v["net"], v["x"], v["y"],
                                     v["d"], v["top"], v["bot"])
                continue
            merged += 1
            emitted += 1
            w = sum(v["d"] for v in cl)   # area-additive -> exact parallel R
            for i in range(n):
                for j in range(i + 1, n):
                    dd = (cl[i]["x"] - cl[j]["x"]) ** 2 + (cl[i]["y"] - cl[j]["y"]) ** 2
                    if dd > max_extent2:
                        max_extent2 = dd
            _emit_via_barrel(model, zmap, cu, cl[0]["net"], cx, cy, w,
                             cl[0]["top"], cl[0]["bot"])
    model.via_merge = dict(enabled=True, radius_mm=merge_radius,
                           powernet_vias=n_powernet, vias_eligible=len(mergeable),
                           excluded_off_pour_or_roi=n_powernet - len(mergeable),
                           barrels_after=emitted, clusters_merged=merged,
                           centroid_fallback_clusters=pour_fallback,
                           unreachable_fallback_clusters=unreachable_fallback,
                           max_cluster_extent_mm=round(max_extent2 ** 0.5, 4))


def _between(lid, top, bot, cu):
    i = cu.index(lid) if lid in cu else None
    it = cu.index(top) if top in cu else 0
    ib = cu.index(bot) if bot in cu else len(cu) - 1
    return i is not None and min(it, ib) <= i <= max(it, ib)


def add_zones_grid(board, model, zmap, nets, pitch, roi=None):
    """Copper pours -> historical center-point grid clipped to the filled polygon.

    This remains the default because the pad-land terminal slice is validated
    against KiPEX/PyPEEC on simple-hb. The polygon cell-edge mesher is available
    as an explicit experimental mode while its lower-bound shift is investigated.

    Returns {(net, layer): [(x,y,node)]} of created zone nodes, for stitching.
    """
    znodes = {}
    for i in range(board.GetAreaCount()):
        z = board.GetArea(i)
        net = z.GetNetname()
        if net not in nets:
            continue
        for lid in z.GetLayerSet().Seq():
            zc = zmap.get(lid)
            if zc is None:
                continue
            poly = z.GetFilledPolysList(lid)
            if poly is None or poly.OutlineCount() == 0:
                continue
            bb = poly.BBox()
            x0, y0, x1, y1 = mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())
            if roi is not None:
                x0, y0 = max(x0, roi[0]), max(y0, roi[1])
                x1, y1 = min(x1, roi[2]), min(y1, roi[3])
                if x1 <= x0 or y1 <= y0:
                    continue
            nx = max(1, int((x1 - x0) / pitch))
            ny = max(1, int((y1 - y0) / pitch))
            grid = {}
            for ix in range(nx + 1):
                for iy in range(ny + 1):
                    x = x0 + ix * pitch
                    y = y0 + iy * pitch
                    if poly.Contains(pcbnew.VECTOR2I(int(x * NM), int(y * NM))):
                        grid[(ix, iy)] = model.node(net, lid, x, y, zc, zone=True)
            # connect 4-neighbours
            for (ix, iy), n in grid.items():
                r = grid.get((ix + 1, iy))
                if r is not None:
                    model.seg(n, r, pitch)
                d = grid.get((ix, iy + 1))
                if d is not None:
                    model.seg(n, d, pitch)
            lst = znodes.setdefault((net, lid), [])
            for (ix, iy), n in grid.items():
                x = x0 + ix * pitch; y = y0 + iy * pitch
                lst.append((x, y, n))
    return znodes


def add_zones_polygon(board, model, zmap, nets, pitch, roi=None):
    """Copper pours -> polygon-aware cell-edge filament mesh.

    The mesh is still axis-aligned and pitch-bounded, but it also inserts every
    filled-polygon outline/hole vertex coordinate as an x/y cut. Segments are
    emitted along cell sides only when the side lies fully inside the filled
    copper. This is closer to KiPEX's polygon path than the historical pure
    pitch lattice, while preserving the existing FastHenry deck/reducer contract.

    Returns {(net, layer): [(x,y,node)]} of created zone nodes, for stitching.
    """
    znodes = {}
    for i in range(board.GetAreaCount()):
        z = board.GetArea(i)
        net = z.GetNetname()
        if net not in nets:
            continue
        for lid in z.GetLayerSet().Seq():
            zc = zmap.get(lid)
            if zc is None:
                continue
            poly = z.GetFilledPolysList(lid)
            if poly is None or poly.OutlineCount() == 0:
                continue
            polys = _extract_polys([poly])
            contains = _polys_contains_inclusive(polys)
            bb = poly.BBox()
            x0, y0, x1, y1 = mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())
            if roi is not None:
                x0, y0 = max(x0, roi[0]), max(y0, roi[1])
                x1, y1 = min(x1, roi[2]), min(y1, roi[3])
                if x1 <= x0 or y1 <= y0:
                    continue
            vx, vy, vertex_note = _bounded_poly_axis_vertices(polys)
            xs = _axis_cuts(x0, x1, pitch, vx)
            ys = _axis_cuts(y0, y1, pitch, vy)
            base_cells = max(1, int(math.ceil((x1 - x0) / pitch))) * \
                max(1, int(math.ceil((y1 - y0) / pitch)))
            poly_cells = max(1, len(xs) - 1) * max(1, len(ys) - 1)
            if vertex_note is not None:
                note = dict(net=net,
                            layer=int(lid) if isinstance(lid, int) else str(lid),
                            base_cells=base_cells, polygon_cells=poly_cells)
                note.update(vertex_note)
                model.zone_mesh_notes.append(note)
            elif poly_cells > max(4 * base_cells, base_cells + 2000):
                model.zone_mesh_notes.append(
                    dict(net=net,
                         layer=int(lid) if isinstance(lid, int) else str(lid),
                         reason="polygon_cut_cell_explosion",
                         base_cells=base_cells, polygon_cells=poly_cells,
                         x_cuts=len(xs), y_cuts=len(ys)))
                xs = _axis_cuts(x0, x1, pitch)
                ys = _axis_cuts(y0, y1, pitch)
            grid = {}
            edges = {}

            def node_at(x, y):
                key = (round(x / SNAP), round(y / SNAP))
                n = grid.get(key)
                if n is None:
                    n = model.node(net, lid, x, y, zc, zone=True)
                    grid[key] = n
                return n

            def edge(ax, ay, bx, by, width):
                if abs(ax - bx) < 1e-9 and abs(ay - by) < 1e-9:
                    return
                if not _segment_inside(contains, ax, ay, bx, by):
                    return
                a = (round(ax / SNAP), round(ay / SNAP))
                b = (round(bx / SNAP), round(by / SNAP))
                key = tuple(sorted((a, b)))
                item = edges.setdefault(key, [ax, ay, bx, by, []])
                item[4].append(width)

            for ix in range(len(xs) - 1):
                xa, xb = xs[ix], xs[ix + 1]
                cw = xb - xa
                if cw <= 0:
                    continue
                for iy in range(len(ys) - 1):
                    ya, yb = ys[iy], ys[iy + 1]
                    ch = yb - ya
                    if ch <= 0:
                        continue
                    # Emit the cell perimeter sides that are real copper. Shared
                    # sides are deduped below and get the average adjacent width.
                    edge(xa, ya, xa, yb, cw)
                    edge(xb, ya, xb, yb, cw)
                    edge(xa, ya, xb, ya, ch)
                    edge(xa, yb, xb, yb, ch)

            for ax, ay, bx, by, widths in edges.values():
                na = node_at(ax, ay)
                nb = node_at(bx, by)
                model.seg(na, nb, sum(widths) / len(widths))
            lst = znodes.setdefault((net, lid), [])
            for n in grid.values():
                x, y, _ = model._pos[n]
                lst.append((x, y, n))
    return znodes


def add_zones(board, model, zmap, nets, pitch, roi=None, mode="grid"):
    if mode == "grid":
        return add_zones_grid(board, model, zmap, nets, pitch, roi=roi)
    if mode == "polygon":
        return add_zones_polygon(board, model, zmap, nets, pitch, roi=roi)
    raise ValueError(f"unknown zone mesh mode {mode!r}")


# --------------------------------------------------------------------------- #
# pads, THT stacks, FET leads, ports
# --------------------------------------------------------------------------- #
def _pad_region_contacts(model, net, lid, contains):
    """Zone-mesh nodes that lie under the pad copper land for `net`/`lid`."""
    contacts = []
    for zn in model.zone_nodes:
        znet, zlid = model.meta.get(zn, (None, None))
        if znet != net or zlid != lid:
            continue
        x, y, _ = model._pos[zn]
        if contains(x, y):
            contacts.append((x, y, zn))
    contacts.sort(key=lambda t: (t[0], t[1], t[2]))
    return [zn for _, _, zn in contacts]


def _pad_proximity_contacts(model, net, lid, x, y, radius, cap=8):
    """Nearest same-net zone-mesh nodes within `radius` mm of (x, y) on `lid`.

    Fallback for a pad that has NO zone node strictly inside its land — either it
    is smaller than the mesh pitch, or it sits in a pour clearance void (the pour
    is pulled back around the pad). Rather than drop to a single welded pad-centre
    node (point injection, which inflates the loop L), bond the pad to the ring of
    surrounding pour nodes as a distributed contact patch. Nearest-first, capped to
    `cap` so a dense pour does not short the terminal across a large area."""
    r2 = radius * radius
    near = []
    for zn in model.zone_nodes:
        if model.meta.get(zn) != (net, lid):
            continue
        zx, zy, _ = model._pos[zn]
        d2 = (zx - x) ** 2 + (zy - y) ** 2
        if d2 <= r2:
            near.append((d2, zn))
    near.sort(key=lambda t: (t[0], t[1]))
    return [zn for _, zn in near[:cap]]


def _pad_via_top_contacts(model, net, lid, x, y, radius, cap=4):
    """Same-net via-barrel top nodes on `lid` within `radius` mm of (x, y).

    Cross-layer fallback for a pad in a clearance void where the pour is on a
    DIFFERENT layer than the pad (e.g. HSS pour on B.Cu, FET pad on F.Cu).
    Bonds to via-top nodes on the pad's layer — the via barrel then carries
    the inductance down to the pour layer, preserving the via impedance in
    the loop path (unlike a direct cross-layer bond which would short it).

    Searches ALL model nodes (not just zone_nodes) for same-net same-layer
    nodes within radius, excluding zone_nodes already tried by proximity.
    Deterministic tie-break: distance, then x, then y, then node-id."""
    r2 = radius * radius
    near = []
    zone_set = model.zone_nodes
    for n in model.meta:
        if n in zone_set:
            continue
        if model.meta.get(n) != (net, lid):
            continue
        nx, ny, _ = model._pos[n]
        d2 = (nx - x) ** 2 + (ny - y) ** 2
        if d2 <= r2:
            near.append((d2, nx, ny, n))
    near.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    return [n for _, _, _, n in near[:cap]]


def _same_net_zone_count(model, net, lid):
    return sum(1 for zn in model.zone_nodes
               if model.meta.get(zn) == (net, lid))


def _pad_ref(fp):
    try:
        return fp.GetReference()
    except Exception:
        return None


def _nearest_contact(model, x, y, contacts):
    return min(contacts, key=lambda n: (model._pos[n][0] - x) ** 2 +
               (model._pos[n][1] - y) ** 2)


def _pad_size_min(pad):
    try:
        return min(mm(pad.GetSizeX()), mm(pad.GetSizeY()))
    except Exception:
        try:
            s = pad.GetSize()
            return min(mm(s.x), mm(s.y))
        except Exception:
            return 0.5


def _finite_contact_width(model, contacts, pad):
    """Representative finite pad-copper width for center-to-mesh spokes."""
    pad_min = _pad_size_min(pad) or 0.5
    spacings = []
    pts = [(model._pos[n][0], model._pos[n][1]) for n in contacts]
    for i, (x, y) in enumerate(pts):
        best = None
        for j, (xb, yb) in enumerate(pts):
            if i == j:
                continue
            d = math.hypot(x - xb, y - yb)
            if d > SNAP and (best is None or d < best):
                best = d
        if best is not None:
            spacings.append(best)
    if spacings:
        spacings.sort()
        pitch = spacings[len(spacings) // 2]
    else:
        pitch = min(pad_min, 0.5)
    return max(0.05, min(pad_min, pitch))


def _pad_land_terminal(model, net, lid, x, y, z, pad, fp=None):
    """Terminal node distributed over the real pad land where it overlaps a pour.

    This is intentionally a pad-sized region, not the whole pour edge. If no
    gridded pour nodes fall inside the pad polygon at the current pitch, return
    None so the caller can use the legacy pad-centre node plus stitch fallback.
    """
    mode = getattr(model, "terminal_mode", "padland")
    if mode == "point":
        model.terminal_fallbacks.append(
            dict(ref=_pad_ref(fp), net=net,
                 layer=int(lid) if isinstance(lid, int) else str(lid),
                 x=x, y=y, reason="legacy_point_mode",
                 zone_nodes=_same_net_zone_count(model, net, lid)))
        return None
    contains = _pad_contains(pad, lid)
    if contains is None:
        model.terminal_fallbacks.append(
            dict(ref=_pad_ref(fp), net=net,
                 layer=int(lid) if isinstance(lid, int) else str(lid),
                 x=x, y=y, reason="polygon_unavailable",
                 zone_nodes=_same_net_zone_count(model, net, lid)))
        return None
    contacts = _pad_region_contacts(model, net, lid, contains)
    proximity = False
    if not contacts:
        zn = _same_net_zone_count(model, net, lid)
        # No zone node lies strictly inside the pad land — the pad is smaller than
        # the mesh pitch, or it sits in a pour clearance void (the fill is pulled
        # back around the pad). Bonding to a single detached pad-centre node here is
        # POINT injection, which inflates the loop L. Instead bond to the nearest
        # ring of same-net pour nodes as a distributed contact patch.
        pitch = getattr(model, "pitch", None) or 1.0
        try:
            half_diag = 0.5 * (mm(pad.GetSizeX()) ** 2 + mm(pad.GetSizeY()) ** 2) ** 0.5
        except Exception:
            half_diag = 0.5 * (_pad_size_min(pad) or 0.5)
        radius = half_diag + 1.5 * pitch
        contacts = _pad_proximity_contacts(model, net, lid, x, y, radius) if zn else []
        if not contacts:
            # Cross-layer fallback: pour may be on a different layer than the pad
            # (e.g. HSS pour on B.Cu, FET pad on F.Cu, connected via vias).
            # Bond to via-top nodes on the pad's layer — the via barrel carries
            # the inductance down to the pour, preserving via impedance in the loop.
            via_contacts = _pad_via_top_contacts(model, net, lid, x, y, radius)
            if via_contacts:
                contacts = via_contacts
                proximity = True
            else:
                model.terminal_fallbacks.append(
                    dict(ref=_pad_ref(fp), net=net,
                         layer=int(lid) if isinstance(lid, int) else str(lid),
                         x=x, y=y,
                         reason=("no_mesh_node_inside_pad" if zn else "no_same_net_zone_mesh"),
                         zone_nodes=zn))
                return None
        proximity = True
    if mode == "single":
        n = _nearest_contact(model, x, y, contacts)
        model.terminal_regions.append(
            dict(ref=_pad_ref(fp), net=net,
                 layer=int(lid) if isinstance(lid, int) else str(lid),
                 x=x, y=y, contacts=len(contacts), used_contacts=1,
                 mode="single", node=n, proximity=proximity))
        return n
    term = model.node(net, lid, x, y, z)
    model.distributed_terminals.add(term)
    if mode == "finite":
        width = _finite_contact_width(model, contacts, pad)
        for zn in contacts:
            model.seg(term, zn, width)
        model.terminal_regions.append(
            dict(ref=_pad_ref(fp), net=net,
                 layer=int(lid) if isinstance(lid, int) else str(lid),
                 x=x, y=y, contacts=len(contacts), used_contacts=len(contacts),
                 mode="finite", contact_width=width, proximity=proximity))
        return term
    if mode != "padland":
        raise ValueError(f"unknown terminal mode {mode!r}")
    for zn in contacts:
        if proximity:
            model.seg(term, zn, _pad_size_min(pad) or 0.5)
        else:
            model.seg(term, zn, _pad_size_min(pad) or 0.5)
    model.terminal_regions.append(
        dict(ref=_pad_ref(fp), net=net,
             layer=int(lid) if isinstance(lid, int) else str(lid),
             x=x, y=y, contacts=len(contacts), used_contacts=len(contacts),
             mode="padland", proximity=proximity))
    return term


def _pad_node_stack(board, model, zmap, fp, want_net):
    """For the pad(s) of `fp` on `want_net`, return the node on the TOP copper
    layer and connect the THT stack vertically across all layers the pad touches.

    If the pad overlaps a meshed same-net pour, the per-layer node is a terminal
    distributed across all zone mesh nodes inside the physical pad land. That
    avoids forcing the whole sheet current through a single detached pad-centre
    stitch while still keeping the terminal region limited to real contact copper.
    """
    cu = _cu_stack(board)
    top_node = None
    for pad in fp.Pads():
        if pad.GetNetname() != want_net:
            continue
        p = pad.GetPosition()
        x, y = mm(p.x), mm(p.y)
        touched = [l for l in cu if pad.IsOnLayer(l)]
        if not touched:
            touched = [cu[0]]
        prev = None
        for lid in touched:
            n = _pad_land_terminal(model, want_net, lid, x, y, zmap[lid], pad, fp=fp)
            if n is None:
                n = model.node(want_net, lid, x, y, zmap[lid])
            if prev is not None:
                model.seg(prev, n, mm(pad.GetSizeX()) or 1.0)
            prev = n
            if lid == cu[0] or top_node is None:
                top_node = n
        return top_node, (x, y)
    return None, None


def _port_ref(ref):
    return re.sub(r"[^A-Za-z0-9_]+", "_", ref)


def _gate_port_label(role, ref, parallel_mode):
    if parallel_mode == "per-device":
        return f"P_g{role}_{_port_ref(ref)}"
    return "P_ghs" if role == "hs" else "P_gls"


def _switch_port_label(role, ref, parallel_mode):
    if parallel_mode == "per-device":
        return f"P_{role}_{_port_ref(ref)}"
    return f"P_{role}"


def _require_unique_device_labels(role, refs, parallel_mode):
    if parallel_mode != "per-device":
        return
    gate_labels = [_gate_port_label(role, ref, parallel_mode) for ref in refs]
    switch_labels = [_switch_port_label(role, ref, parallel_mode) for ref in refs]
    for kind, labels in (("gate", gate_labels), ("switch", switch_labels)):
        dup = sorted({lbl for lbl in labels if labels.count(lbl) > 1})
        if dup:
            raise ValueError(
                f"{role.upper()} per-device {kind} port label collision for refs "
                f"{refs}: {', '.join(dup)}; rename refdes or adjust label generation")


def _require_valid_lead_parallel_mode(role, refs, lead_mm, parallel_mode):
    if lead_mm <= 0 and parallel_mode == "lumped" and len(refs) > 1:
        raise ValueError(
            "lead_mm<=0 with lumped parallel FETs would ideal-short distinct "
            f"{role.upper()} pad lands and bypass board spreading; use "
            "--parallel-fets per-device for no-lead fixtures")


def _plane_p_role_key(role):
    return "_drn_pad_node" if role == "hs" else "_src_pad_node"


def _plane_p_nodes(topo, role):
    d = topo.get(role, {})
    nodes = []
    for dev in d.get("_devices", []):
        n = dev.get(_plane_p_role_key(role))
        if n and n not in nodes:
            nodes.append(n)
    fallback = d.get(_plane_p_role_key(role))
    if fallback and fallback not in nodes:
        nodes.append(fallback)
    return nodes


def _merge_plane_group(model, nodes):
    if not nodes:
        return None
    rep = nodes[0]
    for n in nodes[1:]:
        model.equiv(rep, n)
    return rep


def _equiv_connected(model, a, b):
    if a == b:
        return True
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for na, nb in model.equivs:
        union(na, nb)
    return find(a) == find(b)


def setup_demarcation_plane(model, topo, mode, closure="cell_bridge"):
    """Record and, for cap-only, close the Cin/switch demarcation plane P.

    Plane P is the rail-to-switch-cell boundary. The closure parameter controls
    how shared switch-cell copper is bookkept for the comparison runs:
      - cell_bridge: one bridge from HS drain/Vin attach group to LS source/GND
        attach group, starving the full switch cell.
      - per_fet: per-package pad-land shorts, keeping SW-node board copper in
        the cap matrix.
    """
    hs_nodes = _plane_p_nodes(topo, "hs")
    ls_nodes = _plane_p_nodes(topo, "ls")
    if not hs_nodes or not ls_nodes:
        topo["demarcation_plane"] = dict(
            id="P", mode=mode, closure=closure, status="missing_attach_nodes",
            hs_vin_attach_nodes=hs_nodes, ls_gnd_attach_nodes=ls_nodes)
        if mode in ("cap_only", "switch_residual"):
            raise ValueError(
                f"{mode}: could not resolve demarcation plane P attach groups "
                f"(HS Vin nodes={len(hs_nodes)}, LS GND nodes={len(ls_nodes)})")
        return None, None
    if mode == "full_loop" or closure == "per_fet":
        hs_rep, ls_rep = hs_nodes[0], ls_nodes[0]
    else:
        hs_rep = _merge_plane_group(model, hs_nodes)
        ls_rep = _merge_plane_group(model, ls_nodes)
    plane = dict(
        id="P",
        mode=mode,
        closure=closure,
        status="ok",
        hs_vin_attach_nodes=hs_nodes,
        ls_gnd_attach_nodes=ls_nodes,
        hs_vin_attach_node=hs_rep,
        ls_gnd_attach_node=ls_rep,
        note=("cell_bridge uses hs_vin_attach_node<->ls_gnd_attach_node; "
              "per_fet uses individual drain<->source package pad shorts"))
    topo["demarcation_plane"] = plane
    if mode == "cap_only" and closure == "cell_bridge":
        model.equiv(hs_rep, ls_rep)
        plane["cap_only_bridge"] = [hs_rep, ls_rep]
        plane["intent"] = (
            "cell_bridge cap_only keeps the full package and SW pour in the "
            "filament set, drops the FET die shorts, and adds the plane-P ideal "
            "bridge. Opening the die shorts prevents a bridge-induced shorted "
            "turn while preserving the switch-cell copper geometry.")
        plane["die_shorts"] = "dropped"
        plane["floating_switch_cell"] = "retained_unported"
    elif mode == "switch_residual" and closure == "cell_bridge":
        model.port("P_sw_residual", hs_rep, ls_rep)
        plane["switch_residual_port"] = "P_sw_residual"
        plane["intent"] = (
            "whole-cell residual port over the same plane-P groups used by "
            "cell_bridge cap_only")
    elif mode == "switch_residual" and closure == "per_fet":
        ports = []
        skipped = []
        for role in ("hs", "ls"):
            for dev in topo.get(role, {}).get("_devices", []):
                a, b = dev.get("_drn_pad_node"), dev.get("_src_pad_node")
                if a and b:
                    label = f"P_sw_residual_{role}_{_port_ref(dev['ref'])}"
                    if _equiv_connected(model, a, b):
                        skipped.append(dict(label=label, ref=dev["ref"], role=role,
                                            reason="plane_p_endpoints_equiv"))
                        continue
                    model.port(label, a, b)
                    ports.append(label)
        plane["switch_residual_ports"] = ports
        if skipped:
            plane["switch_residual_ports_skipped"] = skipped
        if skipped and not ports:
            plane["gauge_fix_status"] = "structurally_not_required"
            plane["gauge_fix_reason"] = "zero_by_plane_p_equiv"
        plane["intent"] = (
            "per_fet residual ports are a coupled residual-port submatrix; do "
            "not interpret individual port self-L values as isolated package L")
    return hs_rep, ls_rep


def build_fet(board, model, zmap, topo, role, lead_mm, parallel_mode="lumped",
              closure_mode="full_loop"):
    """Add lead stubs + die shorts for one FET role; return port endpoints."""
    d = topo[role]
    refs = d["refs"]
    _require_unique_device_labels(role, refs, parallel_mode)
    _require_valid_lead_parallel_mode(role, refs, lead_mm, parallel_mode)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints() if fp.GetReference() in refs}
    by_ref = {dev.get("ref"): dev for dev in d.get("devices", [])}
    # Default mode preserves the historical lumped parallel-device model. The
    # issue #5 path is opt-in: each physical FET keeps its own die/source/gate
    # branch and gets its own FastHenry port labels.
    die_src = die_drn = die_gate = gate_pad_node = None
    src_pad_node = drn_pad_node = None
    devices = []
    for ref in refs:
        fp = fps[ref]
        dev = by_ref.get(ref, {})
        drain = dev.get("drain", d["drain"])
        source = dev.get("source", d["source"])
        gate = dev.get("gate", d["gate"])
        if closure_mode == "cap_only_per_fet":
            # Package-drop comparison basis: resolve both package attach pads,
            # drop lead/die/gate stubs, and close each FET at pad land.
            dn, dpos = _pad_node_stack(board, model, zmap, fp, drain)
            sn, spos = _pad_node_stack(board, model, zmap, fp, source)
            if not (dn and sn):
                raise ValueError(f"{ref}: could not resolve drain/source pads "
                                 f"for per-FET cap-only closure "
                                 f"(drain={drain}, source={source})")
            model.equiv(dn, sn)
            devices.append(dict(ref=ref, gate=gate, drain=drain, source=source,
                                gate_label=_gate_port_label(role, ref, parallel_mode),
                                switch_label=_switch_port_label(role, ref, parallel_mode),
                                _die_src=None, _die_drn=None, _die_gate=None,
                                _gate_pad_node=None, _src_pad_node=sn,
                                _drn_pad_node=dn))
            if die_src is None:
                src_pad_node, drn_pad_node = sn, dn
            continue
        dn, dpos = _pad_node_stack(board, model, zmap, fp, drain)
        sn, spos = _pad_node_stack(board, model, zmap, fp, source)
        if not (dn and sn):
            raise ValueError(f"{ref}: could not resolve drain/source pads "
                             f"(drain={drain}, source={source})")
        open_die = closure_mode == "cap_only_cell_bridge"
        # Vertical lead stubs up to a die plane at z = +lead_mm. A zero lead
        # length means the semiconductor/package model already owns the lead
        # inductance; close the ideal channel at the pad land instead of emitting
        # degenerate zero-length FastHenry filaments.
        die_layer = "DIE" if parallel_mode == "lumped" else f"DIE_{ref}"
        if lead_mm <= 0:
            # GaN / leadless package: no external leads, but the die-short must
            # NOT .equiv the pad nodes at z=0 — that shorts drain to source at
            # the board surface, bypassing all copper impedance and collapsing
            # loop R. Instead, create a die plane at a small epsilon above the
            # pad so the channel short is at the die, not the board, and the
            # pad-to-die seg carries a small finite impedance.
            eps = 0.001  # 1um die-plane offset — negligible L, preserves R
            dref = model.node(drain, die_layer, *dpos, eps)
            sref = model.node(source, die_layer, *spos, eps)
            model.seg(dn, dref, 1.0)
            model.seg(sn, sref, 1.0)
        else:
            dref = model.node(drain, die_layer, *dpos, lead_mm)
            sref = model.node(source, die_layer, *spos, lead_mm)
            model.seg(dn, dref, 1.0)
            model.seg(sn, sref, 1.0)
        if not open_die:
            model.equiv(dref, sref)  # channel short at the die
        else:
            model.keep_nodes.update((dref, sref))
        if parallel_mode == "per-device" or die_src is None:
            gn, gpos = _pad_node_stack(board, model, zmap, fp, gate)
            if not gn:
                raise ValueError(f"{ref}: could not resolve gate pad on {gate}")
            if lead_mm <= 0:
                gref = gn
            else:
                gref = model.node(gate, die_layer, *gpos, lead_mm)
                model.seg(gn, gref, 0.5)
            if not open_die:
                model.equiv(gref, sref)
        else:
            gn = gref = None
        if die_src is None:
            die_src, die_drn, die_gate = sref, dref, gref
            gate_pad_node, src_pad_node, drn_pad_node = gn, sn, dn
        elif parallel_mode == "lumped" and not open_die:
            model.equiv(die_src, sref)
            model.equiv(die_drn, dref)
        devices.append(dict(ref=ref, gate=gate, drain=drain, source=source,
                            gate_label=_gate_port_label(role, ref, parallel_mode),
                            switch_label=_switch_port_label(role, ref, parallel_mode),
                            _die_src=sref, _die_drn=dref, _die_gate=gref,
                            _gate_pad_node=gn, _src_pad_node=sn,
                            _drn_pad_node=dn))
    d["_die_src"] = die_src
    d["_die_gate"] = die_gate
    d["_gate_pad_node"] = gate_pad_node
    d["_src_pad_node"] = src_pad_node
    d["_drn_pad_node"] = drn_pad_node
    d["_devices"] = devices
    return d


def gate_driver_node(model, net, gate_pad_node):
    """Endpoint of the gate net copper farthest from the FET gate pad = driver/Rg end.

    The board can contain disconnected same-name gate-net islands. Pick only from
    the modeled component connected to the FET gate pad; otherwise a stray
    dangling endpoint can create a floating gate port that later gets dropped,
    making CSI report as zero.
    """
    if gate_pad_node is None:
        return None
    seen = model.component([gate_pad_node])
    gx, gy, _ = model._pos[gate_pad_node]
    candidates = []
    for n in seen:
        if n == gate_pad_node:
            continue
        nnet, layer = model.meta.get(n, (None, None))
        if nnet != net or layer == "DIE":
            continue
        x, y, _ = model._pos[n]
        candidates.append(((x - gx) ** 2 + (y - gy) ** 2, n))
    if not candidates:
        return None
    return max(candidates)[1]


def validate_required_ports(model, topo, allow_missing_gate_ports=False):
    labels = {lbl for lbl, _, _ in model.ports}
    missing = []
    if "P_pwr" not in labels:
        missing.append(
            "P_pwr input-cap commutation port missing: no valid Vin/GND Cin was "
            "ported; check input caps, --cin-refs, --include-bulk-cin, Vin/GND nets")
    for role, label in (("hs", "P_ghs"), ("ls", "P_gls")):
        gate_labels = _required_gate_labels(topo, role)
        if gate_labels:
            missing_dev = [l for l in gate_labels if l not in labels]
            if not missing_dev:
                continue
            missing.append(
                f"{role.upper()} per-device gate-loop port(s) missing: "
                f"{', '.join(missing_dev)}; check per-FET gate nets / routing")
            continue
        if label in labels:
            continue
        d = topo.get(role, {})
        gd = d.get("gate_drive") or {}
        drv = gd.get("driver_net") or "(no driver net discovered)"
        missing.append(
            f"{label} gate-loop port missing: {role.upper()} gate net "
            f"{d.get('gate')!r} is not connected in the modeled copper to a "
            f"driver endpoint (driver net {drv}); check gate routing / vias / "
            f"FET pad layer or override gate nets")
    if missing:
        hard = [m for m in missing if m.startswith("P_pwr")]
        if allow_missing_gate_ports:
            soft = [m for m in missing if not m.startswith("P_pwr")]
            for m in soft:
                sys.stderr.write(f"WARNING (skipped): {m}\n")
        else:
            hard = missing
        if hard:
            raise ValueError("invalid half-bridge topology for parasitic "
                             "extraction:\n  - " + "\n  - ".join(hard))


def validate_cap_only_ports(model):
    labels = {lbl for lbl, _, _ in model.ports}
    if "P_pwr" not in labels:
        raise ValueError(
            "invalid cap-only Cin basis: P_pwr input-cap port missing; check input "
            "caps, --cin-refs, Vin/GND nets")


def validate_switch_residual_ports(model, topo):
    plane = topo.get("demarcation_plane") or {}
    declared = []
    if plane.get("switch_residual_port"):
        declared.append(plane["switch_residual_port"])
    declared.extend(plane.get("switch_residual_ports") or [])
    declared = list(dict.fromkeys(declared))
    labels = {lbl for lbl, _, _ in model.ports}
    missing = [p for p in declared if p not in labels]
    if not declared or missing:
        raise ValueError(
            "invalid switch-residual Cin basis: residual gauge port missing; "
            f"declared={declared or []} missing={missing or []}")
    extra = sorted(labels.difference(declared))
    if extra:
        raise ValueError(
            "invalid switch-residual Cin basis: expected only residual gauge "
            f"port(s) {declared}, got extra solved port(s) {extra}")


def _required_gate_labels(topo, role):
    if topo.get("parallel_fets") != "per-device":
        return []
    d = topo.get(role, {})
    return [dev.get("gate_label") for dev in d.get("_devices", [])
            if dev.get("gate_label")]


# --------------------------------------------------------------------------- #
# top-level build
# --------------------------------------------------------------------------- #
def _roi(board, topo, margin=8.0):
    """Bounding box of the FETs + Cin footprints, expanded by `margin` mm."""
    refs = set(topo["hs"]["refs"] + topo["ls"]["refs"] + topo["cin"])
    xs, ys = [], []
    for fp in board.GetFootprints():
        if fp.GetReference() in refs:
            bb = fp.GetBoundingBox()
            xs += [mm(bb.GetLeft()), mm(bb.GetRight())]
            ys += [mm(bb.GetTop()), mm(bb.GetBottom())]
    if not xs:
        return None
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def build(board, topo, pitch=1.0, lead_mm=3.0, margin=8.0, cin_parallel=1,
          cu_thickness=CU_T,
          cin_refs=None, cin_loop_refs=None, cin_network_refs=None,
          include_bulk=False, weld_tol=0.6, emit_cin_network=False,
          cin_network_model="scalar_trunk",
          parallel_fets="lumped", zone_mesh="grid", terminal_mode="padland",
          cin_extraction_basis="full_loop", cin_closure="cell_bridge",
          merge_vias=False, merge_via_radius=1.0,
          allow_missing_gate_ports=False):
    zmap = layer_z_map(board)
    model = Model(cu_thickness=cu_thickness, terminal_mode=terminal_mode)
    model.pitch = pitch
    power_nets = {topo["sw"], topo["vin"], topo["gnd"]}

    def side_gate_nets(role):
        if parallel_fets == "per-device":
            return {dev.get("gate", topo[role]["gate"])
                    for dev in topo[role].get("devices", [])}
        return {topo[role]["gate"]}

    gate_nets = side_gate_nets("hs") | side_gate_nets("ls")
    nets = power_nets | gate_nets
    roi = _roi(board, topo, margin)
    if cin_extraction_basis not in ("full_loop", "cap_only", "switch_residual"):
        raise ValueError("--cin-extraction-basis must be one of: full_loop, cap_only, "
                         "switch_residual")
    if cin_closure not in ("cell_bridge", "per_fet"):
        raise ValueError("--cin-closure must be one of: cell_bridge, per_fet")

    # issue #6: index same-net filled pours so add_tracks can skip tracks routed
    # inside their own pour (redundant with the mesh add_zones builds below).
    # Index ONLY power_nets — the exact set add_zones meshes — so a track is
    # dropped only where a substitute pour mesh exists; gate nets (even if a gate
    # net happens to have a pour) are never dropped, as they get no mesh.
    pour_index = build_pour_index(board, zmap, power_nets)
    add_tracks(board, model, zmap, nets, pour_index=pour_index, roi=roi)
    # add_zones BEFORE add_vias: the via-merge reachability gate needs the pour
    # mesh nodes to already exist so it can verify a merged centroid barrel will
    # actually bond (stitch_zones) rather than float. Node CREATION order does not
    # affect the solved mesh (nodes are interned by position; segs are added
    # regardless), only the N-numbering in the .inp — which nothing depends on.
    add_zones(board, model, zmap, power_nets, pitch, roi=roi, mode=zone_mesh)
    add_vias(board, model, zmap, nets,  # gate traces can change layers too — model their vias
             merge_vias=merge_vias, merge_radius=merge_via_radius,
             merge_nets=power_nets,  # only merge power-net via fields; gate vias stay per-via
             pour_index=pour_index,  # ...only where the via sits in same-net filled pour...
             roi=roi, pitch=pitch)   # ...and only where the merged centroid can bond to the mesh

    # FET leads + die shorts, or the cap-only/switch-residual plane-P variants.
    if cin_extraction_basis == "cap_only" and cin_closure == "per_fet":
        fet_closure = "cap_only_per_fet"
    elif cin_extraction_basis == "cap_only" and cin_closure == "cell_bridge":
        fet_closure = "cap_only_cell_bridge"
    else:
        fet_closure = "full_loop"
    build_fet(board, model, zmap, topo, "hs", lead_mm, parallel_mode=parallel_fets,
              closure_mode=fet_closure)
    build_fet(board, model, zmap, topo, "ls", lead_mm, parallel_mode=parallel_fets,
              closure_mode=fet_closure)
    setup_demarcation_plane(model, topo, cin_extraction_basis, closure=cin_closure)

    # Cin pad stacks (create nodes now so the global stitch bonds them to the pours).
    # With cin_parallel>1 the N nearest ceramics each get their own port, so the
    # solve captures their mutual coupling and the reduce step forms the true
    # parallel loop L (not the pessimistic single-cap bound).
    if cin_refs is not None and cin_loop_refs is not None:
        raise ValueError("--cin-refs is an alias for --cin-loop-refs; pass only one")
    if cin_network_model not in ("scalar_trunk", "matrix", "matrix_with_sw_coupling", "none"):
        raise ValueError("--cin-network-model must be one of: scalar_trunk, matrix, "
                         "matrix_with_sw_coupling, none")
    topo["cin_network_model"] = cin_network_model
    topo["cin_extraction_basis"] = cin_extraction_basis
    topo["cin_closure"] = cin_closure
    topo["fet_closure"] = "pad_ideal" if lead_mm <= 0 else "lead_stub"
    topo["lead_mm"] = lead_mm
    topo["parallel_fets"] = parallel_fets
    residual_only = cin_extraction_basis == "switch_residual"
    loop_refs = cin_loop_refs if cin_loop_refs is not None else cin_refs
    cins = cin_ports(board, model, zmap, topo, n=cin_parallel,
                     refs=loop_refs, include_bulk=include_bulk)

    # LF conduction anchor: nearest bulk electrolytic (sources the 39 kHz
    # fundamental). Create its pad nodes now so the stitch/weld below bonds them
    # into the pours. Exclude the HF cin-anchor refs so an all-MLCC bank can't pick
    # the same cap as P_pwr (which would duplicate the port -> singular Zc).
    cref = conduction_ref(board, model, zmap, topo, prefer="bulk",
                          exclude={ref for ref, _, _ in cins})

    # --emit-cin-network: port the FULL input bank (bulk+mlcc) individually for the
    # per-cap branch decomposition. Nodes created here (pre-stitch) so they bond in;
    # reuses the HF cin ports (P_pwr...) and adds P_cin_<ref> for the rest.
    if emit_cin_network and not residual_only:
        hf_labels = [(ref, "P_pwr" if i == 0 else f"P_pwr{i}")
                     for i, (ref, _, _) in enumerate(cins)]
        # the LF conduction anchor cap is ported as P_bulk (below) — fold it into
        # cin_net under that label so it isn't double-ported (its P_cin_<ref> would
        # duplicate P_bulk on the same node pair and make FastHenry singular).
        anchors = [(cref[0], "P_bulk", cref[3])] if (
            cref and cin_extraction_basis != "cap_only") else []
        cin_network_ports(board, model, zmap, topo, hf_labels, anchors,
                          refs=cin_network_refs)

    # bond every track/via/pad node into the pour mesh on its net+layer, then weld
    # near-coincident endpoints interning missed (pad-centre vs trace-end, touching
    # fills) — essential for nets with no pour (gate) and multi-fill power planes.
    model.stitch_zones(pitch)
    model.weld(weld_tol)

    # ---- ports ----
    cin_labels = []
    if not residual_only:
        for i, (_ref, vn, gn) in enumerate(cins):
            label = "P_pwr" if i == 0 else f"P_pwr{i}"
            model.port(label, vn, gn)
            cin_labels.append(label)
    model.cin_ports = cin_labels
    if not residual_only:
        topo["cin_used"] = [ref for ref, _, _ in cins]
    if cin_extraction_basis not in ("cap_only", "switch_residual"):
        for role, label in (("hs", "P_ghs"), ("ls", "P_gls")):
            d = topo[role]
            if parallel_fets == "per-device":
                for dev in d.get("_devices", []):
                    drv = gate_driver_node(model, dev["gate"], dev.get("_gate_pad_node"))
                    ret = dev["_die_src"] if d["kelvin"] else dev["_src_pad_node"]
                    if drv and ret:
                        model.port(dev["gate_label"], drv, ret)
            else:
                drv = gate_driver_node(model, d["gate"], d.get("_gate_pad_node"))
                ret = d["_die_src"] if d["kelvin"] else d["_src_pad_node"]
                if drv and ret:
                    model.port(label, drv, ret)

    # ---- conduction ports (LF, per-side R for conduction-loss attribution) ----
    # Anchored on the bulk cap (cref), not the HF MLCC: P_hs drives Vin(bulk)->SW
    # through HS's drain+source leads (die short routes it), P_ls drives SW->GND
    # (bulk) through LS. Their DC self-R is each switch's true conduction copper;
    # P_bulk is the full LF loop over the bulk cap for the reconstruction check.
    if cref and cin_extraction_basis not in ("cap_only", "switch_residual"):
        cond_ref, cvn_dc, cgn_dc, cond_klass = cref
        model.port("P_bulk", cvn_dc, cgn_dc)
        if parallel_fets == "per-device":
            for dev in topo["hs"].get("_devices", []):
                if dev.get("_src_pad_node"):
                    model.port(dev["switch_label"], cvn_dc, dev["_src_pad_node"])
            for dev in topo["ls"].get("_devices", []):
                if dev.get("_drn_pad_node"):
                    model.port(dev["switch_label"], dev["_drn_pad_node"], cgn_dc)
        else:
            hs_sw = topo["hs"].get("_src_pad_node")   # HS source pad (on SW)
            ls_sw = topo["ls"].get("_drn_pad_node")   # LS drain pad (on SW)
            if hs_sw:
                model.port("P_hs", cvn_dc, hs_sw)
            if ls_sw:
                model.port("P_ls", ls_sw, cgn_dc)
        topo["cond_ref"] = dict(ref=cond_ref, cls=cond_klass)
    topo["parallel_fets"] = parallel_fets
    if parallel_fets == "per-device":
        for role in ("hs", "ls"):
            topo[role]["device_ports"] = [
                dict(ref=dev["ref"], gate=dev["gate"],
                     gate_label=dev["gate_label"], switch_label=dev["switch_label"])
                for dev in topo[role].get("_devices", [])
            ]

    # ---- output conduction ports (SW switch pads -> output inductor / load) ----
    # The buck output inductor is usually off the extracted ROI (external, via a connector
    # on the SW net), so the SW pour that carries the load current to it is otherwise
    # un-ported and reads ~0 in the loss-density map. Port the SW switch pads -> the SW-net
    # output terminal so that copper is modeled. The terminal is auto-detected as the SW-net
    # footprint whose refdes starts with 'J' (connector, preferred) or 'L' (on-board
    # inductor); skipped with no port if none is found (backward compatible). Emitted as
    # P_out_hs/P_out_ls; the loss-density driver injects the inductor current here. These
    # do not perturb the L_loop/CSI/conduction reduction (solve_reduce keys ports by label).
    def _switch_nodes(role, key):
        if parallel_fets == "per-device":
            return [d[key] for d in topo[role].get("_devices", []) if d.get(key)]
        n = topo[role].get(key)
        return [n] if n else []

    out_xy = out_ref = None
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref[:1] not in ("J", "L"):
            continue
        for pad in fp.Pads():
            if pad.GetNetname() == topo["sw"]:
                p = pad.GetPosition()
                out_xy, out_ref = (mm(p.x), mm(p.y)), ref
                break
        if out_ref and ref[:1] == "J":          # prefer a connector over an inductor
            break
    if out_xy is not None:
        # Snap to the NEAREST EXISTING SW-pour node — do NOT create a pad-land terminal:
        # adding terminal copper for the output pad perturbs the commutation-loop mesh and
        # shifts the extracted L_loop. This only references an existing node, so the
        # FastHenry reduction is byte-for-byte unchanged.
        best, bd = None, 1e18
        for nm_, (nnet, _lid) in model.meta.items():
            if nnet != topo["sw"]:
                continue
            px, py, _pz = model._pos[nm_]
            dd = (px - out_xy[0]) ** 2 + (py - out_xy[1]) ** 2
            if dd < bd:
                bd, best = dd, nm_
        hs_nodes = _switch_nodes("hs", "_src_pad_node")
        ls_nodes = _switch_nodes("ls", "_drn_pad_node")
        # Emitted as aux_ports (node pairs for the DC loss-density solve), NOT FastHenry
        # .external ports: a lone SW-net terminal added as a solved port makes Zc singular
        # ("Error on factor"), and it plays no part in the L_loop/CSI/conduction reduction.
        if best and (hs_nodes or ls_nodes):
            if hs_nodes:
                model.aux_ports["P_out_hs"] = (hs_nodes[0], best)
            if ls_nodes:
                model.aux_ports["P_out_ls"] = (ls_nodes[0], best)
            topo["out_ref"] = out_ref

    # drop any port disconnected from the commutation loop (e.g. a distant bulk cap
    # whose pad never bonds into the pour at this pitch) — one floating port NaNs the
    # entire FastHenry solve. Keep the topo/cin_net manifests consistent.
    seed_port = "P_sw_residual" if residual_only else "P_pwr"
    dropped = model.drop_floating_ports(seed_port)
    if dropped:
        ds = set(dropped)
        hf_full = list(getattr(model, "cin_ports", []))   # pre-drop HF labels, 1:1 with cin_used
        model.cin_ports = [c for c in hf_full if c not in ds]
        # keep cin_used aligned with cin_ports — reduce zips them positionally to key
        # the per-cap current split; an unfiltered cin_used silently mislabels every
        # cap after a dropped one (and draws a leg for a cap that was dropped).
        topo["cin_used"] = [r for l, r in zip(hf_full, topo.get("cin_used", []))
                            if l not in ds]
        if topo.get("cin_net"):
            topo["cin_net"] = [e for e in topo["cin_net"] if e["label"] not in ds]
        topo["cin_dropped_ports"] = dropped
        if len(dropped) > len(model.ports):
            # more ports dropped than kept -> P_pwr's own copper is the minority
            # island, i.e. the seed (not a distant cap) is the disconnected one.
            sys.stderr.write(
                "WARNING: dropped more ports than kept — the P_pwr seed net may be "
                "the disconnected one; check --sw/--gnd nets and the nearest-Cin pad.\n")
    if cin_extraction_basis == "cap_only":
        validate_cap_only_ports(model)
    elif cin_extraction_basis == "switch_residual":
        validate_switch_residual_ports(model, topo)
    else:
        validate_required_ports(
            model, topo, allow_missing_gate_ports=allow_missing_gate_ports)
    return model


_BULK_MARKERS = ("CP_", "ELEC", "RADIAL", "TANTAL", "_CAN", "EIA-", "SMC_",
                 "ALUMINUM", "POLYMER")


def _cap_class(fp):
    """Classify a cap by FOOTPRINT/PACKAGE, not value ('mlcc' | 'bulk').

    HF ceramics (SMD chip packages) source the tens-of-MHz commutation edge and
    belong in the SW-peak loop; bulk electrolytic/polymer/tantalum (THT cans,
    radial) are above SRF at the edge and don't. Value is a bad proxy in both
    directions — a 10-22uF 1210 MLCC is a legit HF bypass, a small-value
    electrolytic is not — so we go by package/type instead."""
    name = str(fp.GetFPID().GetLibItemName()).upper()
    if any(k in name for k in _BULK_MARKERS):
        return "bulk"
    try:                       # through-hole cap in the input bank = bulk can
        if fp.GetAttributes() & pcbnew.FP_THROUGH_HOLE:
            return "bulk"
    except Exception:
        pass
    return "mlcc"


def _cap_farads(fp):
    """Nominal capacitance (F) parsed from the footprint Value field (e.g.
    '470uF, 100V' -> 470e-6, '220nF' -> 220e-9), or None. Board data, not a parts
    DB — used only to display a ripple-relevance cutoff in the LF schematic."""
    import re
    m = re.match(r"\s*([\d.]+)\s*(p|n|u|µ|m)", str(fp.GetValue()))
    if not m:                       # require an explicit unit: a bare number is
        return None                 # ambiguous (100 = 100pF? 100µF?) — don't guess
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
                                "m": 1e-3}[m.group(2).lower()]


def cin_ports(board, model, zmap, topo, n=1, refs=None, include_bulk=False):
    """Return up to `n` (ref, vin_node, gnd_node) for input caps bridging
    Vin<->GND, ordered nearest-first by distance to the FET centroid. Ports are
    ALWAYS (Vin-pad -> GND-pad) so every cap port has identical polarity (a
    reversed port would silently corrupt the mutuals).

    - `refs` (explicit refdes list) overrides the nearest-N heuristic and the
      bulk filter entirely.
    - otherwise bulk electrolytics are excluded by package/type (see _cap_class):
      above SRF they can't source the commutation edge and only add mesh cost.
      Pass include_bulk=True to keep them (e.g. a low-freq ripple-path study).
    The per-refdes classification is recorded in topo['cin_class'] so any
    exclusion is visible in the manifest. n==1 (no refs) reproduces the old
    single-cap port."""
    fps = [fp for fp in board.GetFootprints()
           if fp.GetReference() in (topo["hs"]["refs"] + topo["ls"]["refs"])]
    cx = sum(mm(fp.GetPosition().x) for fp in fps) / len(fps)
    cy = sum(mm(fp.GetPosition().y) for fp in fps) / len(fps)
    want = set(refs) if refs else None
    cand, excluded_bulk, cls = [], [], {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if want is not None:
            if ref not in want:
                continue
        elif ref not in topo["cin"]:
            continue
        nets = {p.GetNetname() for p in fp.Pads()}
        if not ({topo["vin"], topo["gnd"]} <= nets):
            continue
        klass = _cap_class(fp)
        cls[ref] = klass
        if want is None and not include_bulk and klass == "bulk":
            excluded_bulk.append(ref)
            continue
        p = fp.GetPosition()
        dsq = (mm(p.x) - cx) ** 2 + (mm(p.y) - cy) ** 2
        cand.append((dsq, ref, fp))
    cand.sort(key=lambda t: t[0])
    take = cand if refs else cand[:max(1, n)]
    out = []
    for _, ref, fp in take:
        vn, _ = _pad_node_stack(board, model, zmap, fp, topo["vin"])
        gn, _ = _pad_node_stack(board, model, zmap, fp, topo["gnd"])
        if vn and gn:
            out.append((ref, vn, gn))
    topo["cin_excluded_bulk"] = excluded_bulk
    topo["cin_class"] = cls
    topo["cin_select"] = dict(
        metric=("explicit-refs" if refs else "centroid-distance"),
        cap_filter=("all (bulk included)" if include_bulk or refs
                    else "package/type: MLCC only, electrolytic excluded"))
    return out


def conduction_ref(board, model, zmap, topo, prefer="bulk", exclude=None):
    """Anchor node pair for the LOW-frequency conduction loop -> (ref, vin_node,
    gnd_node, klass), nearest cap of the preferred class to the FET centroid.

    The 39 kHz fundamental switch current is sourced/returned by the **bulk
    electrolytics** — the MLCCs are near-open at the fundamental and carry only the
    HF commutation edge — so conduction-copper R must be referenced to the bulk
    caps, NOT the nearest ceramic (which anchors the HF commutation loop). Prefers
    `klass==prefer` (bulk); falls back to the nearest ceramic for all-ceramic input
    banks (where the ceramics do carry the fundamental). Creates the cap's pad-node
    stacks so a later stitch/weld bonds them into the pours — call this BEFORE
    stitch_zones().

    `exclude` (the HF cin-anchor refs) is skipped: on an all-MLCC bank the bulk
    fallback would otherwise pick the SAME nearest cap as P_pwr, and P_bulk would
    duplicate P_pwr's node pair -> singular Zc ("Error on factor")."""
    skip = set(exclude or ())
    fps = [fp for fp in board.GetFootprints()
           if fp.GetReference() in (topo["hs"]["refs"] + topo["ls"]["refs"])]
    cx = sum(mm(fp.GetPosition().x) for fp in fps) / len(fps)
    cy = sum(mm(fp.GetPosition().y) for fp in fps) / len(fps)
    cand = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in topo["cin"] or ref in skip:
            continue
        nets = {p.GetNetname() for p in fp.Pads()}
        if not ({topo["vin"], topo["gnd"]} <= nets):
            continue
        klass = _cap_class(fp)
        p = fp.GetPosition()
        dsq = (mm(p.x) - cx) ** 2 + (mm(p.y) - cy) ** 2
        cand.append((klass != prefer, dsq, ref, fp, klass))  # preferred class first, then nearest
    if not cand:
        return None
    cand.sort(key=lambda t: (t[0], t[1]))
    _, _, ref, fp, klass = cand[0]
    vn, _ = _pad_node_stack(board, model, zmap, fp, topo["vin"])
    gn, _ = _pad_node_stack(board, model, zmap, fp, topo["gnd"])
    if not (vn and gn):
        return None
    return ref, vn, gn, klass


def cin_network_ports(board, model, zmap, topo, hf_labels, anchors=None, refs=None):
    """Port EVERY input cap (bulk + mlcc) individually as `P_cin_<ref>`, for the
    --emit-cin-network per-cap branch decomposition. Records the ordered port set
    in topo['cin_net'] = [{ref, cls, label}] for solve_reduce to decompose into
    shared-trunk + per-cap branch.

    Caps already ported ELSEWHERE must be reused, not re-ported — a second
    `.external` on the same node pair makes FastHenry's Zc singular ("Error on
    factor"). Two such sets are folded in under their existing labels:
      - hf_labels [(ref, label)]  : the HF commutation ports (P_pwr...)
      - anchors  [(ref, label, cls)] : the LF conduction anchor (P_bulk), whose cap
        (the nearest bulk electrolytic) would otherwise collide with its P_cin_<ref>.
    Both are added to cin_net under their existing label and skipped in the port loop.

    Must run BEFORE stitch_zones so the new cap pad nodes bond into the pours."""
    cls_map = topo.get("cin_class", {})
    want = set(refs) if refs else None
    fp_by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()
                 if fp.GetReference() in topo["cin"]}

    def _entry(ref, cls, lbl):
        fp = fp_by_ref.get(ref)
        return dict(ref=ref, cls=cls, label=lbl,
                    C=(_cap_farads(fp) if fp else None))

    net = [_entry(ref, cls_map.get(ref, "mlcc"), lbl)
           for ref, lbl in hf_labels
           if want is None or ref in want]
    have = {e["ref"] for e in net}
    for ref, lbl, cls in (anchors or []):
        if ref not in have and (want is None or ref in want):
            net.append(_entry(ref, cls, lbl))
            have.add(ref)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in topo["cin"] or ref in have:
            continue
        if want is not None and ref not in want:
            continue
        nets = {p.GetNetname() for p in fp.Pads()}
        if not ({topo["vin"], topo["gnd"]} <= nets):
            continue
        vn, _ = _pad_node_stack(board, model, zmap, fp, topo["vin"])
        gn, _ = _pad_node_stack(board, model, zmap, fp, topo["gnd"])
        if not (vn and gn):
            continue
        label = f"P_cin_{ref}"
        model.port(label, vn, gn)
        net.append(_entry(ref, cls_map.get(ref, _cap_class(fp)), label))
        have.add(ref)
    topo["cin_net"] = net
    if want is not None:
        topo["cin_network_requested"] = list(refs)
        topo["cin_network_missing"] = sorted(want - have)
    return net


def main():
    ap = argparse.ArgumentParser(description="KiCad PCB -> multiport FastHenry .inp")
    ap.add_argument("pcb")
    ap.add_argument("--sw", required=True)
    ap.add_argument("--gnd", required=True)
    ap.add_argument("--vin")
    ap.add_argument("--hs-ref", nargs="*")
    ap.add_argument("--ls-ref", nargs="*")
    ap.add_argument("--hs-gate")
    ap.add_argument("--ls-gate")
    ap.add_argument("--hs-kelvin", action="store_true")
    ap.add_argument("--ls-kelvin", action="store_true")
    ap.add_argument("--pitch", type=float, default=1.0, help="pour mesh pitch (mm)")
    ap.add_argument("--cin-parallel", type=int, default=1,
                    help="port the N nearest input caps in parallel (effective loop L)")
    ap.add_argument("--cin-refs", nargs="*",
                    help="deprecated alias for --cin-loop-refs")
    ap.add_argument("--cin-loop-refs", nargs="*",
                    help="explicit input-cap refdes for the HF commutation-loop "
                         "reduction (overrides nearest-N)")
    ap.add_argument("--cin-network-refs", nargs="*",
                    help="explicit input-cap refdes to include in --emit-cin-network; "
                         "default is every discovered input cap")
    ap.add_argument("--include-bulk-cin", action="store_true",
                    help="also port bulk electrolytics (>=10uF); default excludes them")
    ap.add_argument("--emit-cin-network", action="store_true",
                    help="port the full input-cap bank individually (P_cin_<ref>) for the "
                         "per-cap branch decomposition consumed by the loss tool's cin_network")
    ap.add_argument("--cin-network-model",
                    choices=("scalar_trunk", "matrix", "matrix_with_sw_coupling", "none"),
                    default="scalar_trunk",
                    help="Cin copper contract for --emit-cin-network; matrix modes are "
                         "reserved for a future cap-branch-only extraction basis")
    ap.add_argument("--cin-extraction-basis",
                    choices=("full_loop", "cap_only", "switch_residual"),
                    default="full_loop",
                    help="FET closure basis for Cin extraction: full_loop is the "
                         "legacy die-short model; cap_only applies --cin-closure; "
                         "switch_residual adds the matching plane-P residual port")
    ap.add_argument("--cin-closure",
                    choices=("cell_bridge", "per_fet"),
                    default="cell_bridge",
                    help="Plane-P closure gauge for cap_only/switch_residual comparison runs")
    ap.add_argument("--parallel-fets", choices=("lumped", "per-device"), default="lumped",
                    help="parallel switch model: lumped (legacy) or per-device gates/leads")
    ap.add_argument("--lead-mm", type=float, default=3.0, help="FET exposed-lead length (mm)")
    ap.add_argument("--weld-tol", type=float, default=0.6,
                    help="fuse same-net nodes within this many mm (fixes pad/trace and "
                         "touching-fill fragmentation; 0 disables)")
    ap.add_argument("--zone-mesh", choices=("grid", "polygon"), default="grid",
                    help="power-pour mesher: grid is validated/default; polygon is "
                         "experimental cell-edge clipping for KiPEX-style cross-checks")
    ap.add_argument("--terminal-mode", choices=("padland", "single", "finite", "point"),
                    default="padland",
                    help="pad-to-pour terminal model: padland is validated/default; "
                         "single is KiPEX-like nearest mesh node; finite uses finite "
                         "pad-copper spokes to mesh nodes; point is legacy/debug "
                         "pad-center stitch")
    ap.add_argument("--allow-missing-gate-ports", action="store_true",
                    help="downgrade missing gate-loop ports to warnings. This emits "
                         "no CSI for the missing side(s); default is a hard failure "
                         "to avoid silently reporting 0 nH CSI")
    ap.add_argument("--margin", type=float, default=8.0,
                    help="ROI margin (mm) around FETs/Cin for pour meshing")
    ap.add_argument("--merge-vias", action="store_true",
                    help="collapse dense same-net via fields into equivalent barrels "
                         "to shrink the FastHenry mesh (parallel R exact; loop-path "
                         "vias, being spread out, stay individual)")
    ap.add_argument("--merge-via-radius", type=float, default=1.0,
                    help="cluster radius (mm) for --merge-vias; vias within this of a "
                         "cluster leader merge (default 1.0)")
    ap.add_argument("--nwinc", type=int, default=1, help="filament width sub-mesh (skin; >1 slower, more HF-accurate)")
    ap.add_argument("--nhinc", type=int, default=1, help="filament height sub-mesh (skin)")
    ap.add_argument("--cu-temp", type=float, default=20.0,
                    help="copper temperature (C) for R; scales sigma (R ~ +0.39%/K). "
                         "Isothermal — no self-heating. L is unaffected.")
    ap.add_argument("--cu-thickness", type=float, default=CU_T,
                    help="copper thickness in mm for FastHenry segment height")
    ap.add_argument("--lf-freq", type=float, default=1e3,
                    help="lowest sweep frequency Hz for near-DC conduction R")
    ap.add_argument("--hf-freq", type=float, default=1e8,
                    help="highest sweep frequency Hz; default 100 MHz. Lower it "
                         "(e.g. 1e7) to drop the slow, worst-conditioned top-decade "
                         "solves — harmless when it stays above --plateau")
    ap.add_argument("--ndec", type=int, default=3,
                    help="FastHenry frequency points per decade; default 3")
    ap.add_argument("-o", "--out", required=True, help="output .inp path")
    args = ap.parse_args()
    if args.cu_thickness <= 0:
        raise SystemExit("--cu-thickness must be > 0 mm")
    if args.lf_freq <= 0:
        raise SystemExit("--lf-freq must be > 0 Hz")
    if args.hf_freq < args.lf_freq:
        raise SystemExit("--hf-freq must be >= --lf-freq")
    if args.ndec <= 0:
        raise SystemExit("--ndec must be > 0")
    if args.merge_vias and args.merge_via_radius <= 0:
        raise SystemExit("--merge-via-radius must be > 0 mm")

    board = pcbnew.LoadBoard(args.pcb)
    try:
        topo = fet_discovery.discover(
            board, args.sw, args.gnd, vin=args.vin,
            hs_ref=args.hs_ref, ls_ref=args.ls_ref,
            hs_gate=args.hs_gate, ls_gate=args.ls_gate,
            hs_kelvin=args.hs_kelvin, ls_kelvin=args.ls_kelvin)
        model = build(board, topo, pitch=args.pitch, lead_mm=args.lead_mm, margin=args.margin,
                      cu_thickness=args.cu_thickness,
                      cin_parallel=args.cin_parallel, cin_refs=args.cin_refs,
                      cin_loop_refs=args.cin_loop_refs,
                      cin_network_refs=args.cin_network_refs,
                      include_bulk=args.include_bulk_cin, weld_tol=args.weld_tol,
                      emit_cin_network=args.emit_cin_network,
                      cin_network_model=args.cin_network_model,
                      cin_extraction_basis=args.cin_extraction_basis,
                      cin_closure=args.cin_closure,
                      parallel_fets=args.parallel_fets,
                      zone_mesh=args.zone_mesh,
                      terminal_mode=args.terminal_mode,
                      merge_vias=args.merge_vias,
                      merge_via_radius=args.merge_via_radius,
                      allow_missing_gate_ports=args.allow_missing_gate_ports)
    except ValueError as e:
        raise SystemExit(str(e))
    dropped = topo.get("cin_dropped_ports")
    if dropped:
        sys.stderr.write(
            f"WARNING: dropped {len(dropped)} port(s) disconnected from the loop at "
            f"pitch {args.pitch} mm: {', '.join(dropped)} — their copper never bonded "
            f"into the meshed pour (distant bulk cap?). Lower --pitch / raise "
            f"--weld-tol / --margin to include them.\n")
    stats = model.write(args.out, fmin=args.lf_freq, fmax=args.hf_freq,
                        ndec=args.ndec, nwinc=args.nwinc, nhinc=args.nhinc,
                        sigma=sigma_at(args.cu_temp))
    complexity = mesh_complexity(stats, nwinc=args.nwinc, nhinc=args.nhinc,
                                 fmin=args.lf_freq, fmax=args.hf_freq,
                                 ndec=args.ndec)
    # sidecar: port order + topology for the reduce step
    ports = [lbl for lbl, _, _ in model.ports]
    cin_used = topo.get("cin_used", [])
    cin_warn = None
    if len(cin_used) < args.cin_parallel:
        cin_warn = (f"requested --cin-parallel {args.cin_parallel} but only "
                    f"{len(cin_used)} eligible Cin ported ({', '.join(cin_used) or 'none'})")
        sys.stderr.write("WARNING: " + cin_warn + "\n")
    loud_fallbacks = [
        f for f in getattr(model, "terminal_fallbacks", [])
        if f.get("reason") in ("polygon_unavailable", "no_mesh_node_inside_pad")
    ]
    if loud_fallbacks:
        sys.stderr.write(
            f"WARNING: {len(loud_fallbacks)} pad-land terminal fallback(s) used "
            f"point-style pad nodes at pitch {args.pitch} mm; see "
            f"{args.out}.ports.json terminal_fallbacks.\n")
    if getattr(model, "zone_mesh_notes", []):
        sys.stderr.write(
            f"WARNING: {len(model.zone_mesh_notes)} zone polygon-mesh note(s); "
            f"see {args.out}.ports.json zone_mesh_notes.\n")
    # DCDC_ONLY_FB deletes the inner copper from the model. It is a diagnostic,
    # not a product mode, so it must be LOUD and must reach the artifacts —
    # otherwise a 2-layer-view run is indistinguishable from a full-stack one in
    # parasitics.json / report.md (ReboostV2: ~2.1 nH vs ~2.6 nH).
    cu_layers = [board.GetLayerName(l) for l in _cu_stack(board)]
    if _ONLY_FB:
        sys.stderr.write(
            "WARNING: DCDC_ONLY_FB=1 — copper stack restricted to F.Cu+B.Cu; "
            "inner-layer copper is NOT modelled. This is a mesher-comparison "
            "diagnostic; L_loop is NOT a full-stack extraction.\n")
    side = dict(ports=ports, cin_ports=getattr(model, "cin_ports", ["P_pwr"]),
                aux_ports={k: list(v) for k, v in getattr(model, "aux_ports", {}).items()},
                cin_used=cin_used, cin_requested=args.cin_parallel, cin_warn=cin_warn,
                topo={k: (v if not isinstance(v, dict) else
                          {kk: vv for kk, vv in v.items() if not kk.startswith("_") and kk != "pads"})
                      for k, v in topo.items()},
                pitch=args.pitch, lead_mm=args.lead_mm, cu_temp=args.cu_temp,
                cu_thickness=args.cu_thickness, lf_freq=args.lf_freq,
                hf_freq=args.hf_freq, ndec=args.ndec,
                cu_layers=cu_layers, only_fb=_ONLY_FB,
                cin_extraction_basis=args.cin_extraction_basis,
                cin_closure=args.cin_closure,
                mesh=complexity,
                via_merge=getattr(model, "via_merge", None),
                zone_mesh=args.zone_mesh,
                terminal_mode=args.terminal_mode,
                zone_mesh_notes=getattr(model, "zone_mesh_notes", []),
                terminal_regions=getattr(model, "terminal_regions", []),
                terminal_fallbacks=getattr(model, "terminal_fallbacks", []))
    with open(args.out + ".ports.json", "w") as f:
        json.dump(side, f, indent=2)
    vm = getattr(model, "via_merge", None)
    if vm and vm.get("clusters_merged"):
        print(f"via-merge: {vm['vias_eligible']}/{vm['powernet_vias']} in-pour in-ROI "
              f"power-net vias -> {vm['barrels_after']} barrels ({vm['clusters_merged']} "
              f"clusters merged, {vm['excluded_off_pour_or_roi']} kept per-via off-pour/ROI, "
              f"radius {vm['radius_mm']:g} mm, widest cluster {vm['max_cluster_extent_mm']:g} mm)")
    print(f"wrote {args.out}: {stats['nodes']} nodes, {stats['segs']} segs, "
          f"~{complexity['filaments_est']} filaments, "
          f"{complexity['freq_points']} freqs, work~{complexity['work_units']:.3g}; "
          f"ports={ports}  Cin(in order)={cin_used}")


if __name__ == "__main__":
    main()
