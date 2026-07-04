#!/usr/bin/env python3
"""KiCad PCB -> multiport FastHenry input for power-stage parasitic extraction.

Runs under KiCad's bundled python (pcbnew). Builds ONE FastHenry model of the
half-bridge power copper with three ports so a single solve yields the full
mutual-inductance matrix:

    Port 1  P_pwr  : across the nearest Cin (Vin+ <-> GND-)   -> commutation loop
    Port 2  P_ghs  : HS gate driver-end <-> HS gate return    -> HS gate loop
    Port 3  P_gls  : LS gate driver-end <-> LS gate return    -> LS gate loop

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
import os
import sys

import pcbnew

import fet_discovery

HERE = os.path.dirname(os.path.abspath(__file__))
NM = 1e6                       # KiCad internal units (nm) per mm
CU_T = 0.035                   # copper thickness, mm (1 oz)
SIGMA = 5.8e4                  # copper conductivity, S/mm  (5.8e7 S/m)
SNAP = 0.02                    # node-identity grid, mm (merge coincident nodes)


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def mm(v):
    return v / NM


def layer_z_map(board):
    """Return {layer_id: z_mm} for the copper stack (top = 0, going down)."""
    cu = list(board.GetEnabledLayers().CuStack())
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

    def __init__(self):
        self._nodes = {}          # key -> node name
        self._pos = {}            # node name -> (x,y,z)
        self.meta = {}            # node name -> (net, layer)
        self.zone_nodes = set()   # names created as pour-mesh nodes
        self.segs = []            # (name, na, nb, w, h)
        self.equivs = []          # (na, nb)
        self.ports = []           # (label, na, nb)
        self.cin_ports = ["P_pwr"]  # labels of the commutation (Cin) ports
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
        for nm_, (net, lid) in list(self.meta.items()):
            if nm_ in self.zone_nodes:
                continue
            cand = buckets.get((net, lid))
            if not cand:
                continue
            x, y, _ = self._pos[nm_]
            bx, by, bn = min(cand, key=lambda t: (t[0] - x) ** 2 + (t[1] - y) ** 2)
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
        for nm_, (net, lid) in self.meta.items():
            x, y, _ = self._pos[nm_]
            cx, cy = round(x / cell), round(y / cell)
            best, bd = None, tol2
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for b in grid.get((net, lid, cx + dx, cy + dy), ()):
                        if b == nm_:
                            continue
                        xb, yb, _ = self._pos[b]
                        dd = (x - xb) ** 2 + (y - yb) ** 2
                        if dd <= bd:
                            bd, best = dd, b
            if best is not None:
                key = frozenset((nm_, best))
                if key not in done:
                    done.add(key)
                    self.seg(nm_, best, tol)
                    welded += 1
        return welded

    def seg(self, na, nb, w, h=CU_T):
        if na == nb:
            return
        self._si += 1
        self.segs.append((f"E{self._si}", na, nb, max(w, 0.05), h))

    def equiv(self, na, nb):
        if na != nb:
            self.equivs.append((na, nb))

    def port(self, label, na, nb):
        self.ports.append((label, na, nb))

    # ----- connectivity prune: keep nodes reachable from any port -----
    def prune(self):
        adj = {}
        def link(a, b):
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        for _, a, b, _, _ in self.segs:
            link(a, b)
        for a, b in self.equivs:
            link(a, b)
        seeds = set()
        for _, a, b in self.ports:
            seeds.add(a); seeds.add(b)
        seen = set()
        stack = list(seeds)
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(adj.get(n, ()))
        self.segs = [s for s in self.segs if s[1] in seen and s[2] in seen]
        self.equivs = [e for e in self.equivs if e[0] in seen and e[1] in seen]
        return seen

    def write(self, path, fmin=1e5, fmax=1e8, ndec=3, nwinc=1, nhinc=1):
        keep = self.prune()
        lines = [
            "* auto-generated by dcdc-tools/parasitics/kicad_geom.py",
            ".units mm",
            f".default sigma={SIGMA:g} nwinc={nwinc} nhinc={nhinc} z=0",
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


# --------------------------------------------------------------------------- #
# copper collection & meshing
# --------------------------------------------------------------------------- #
def add_tracks(board, model, zmap, nets):
    """Straight track segments (skip vias/arcs for now) -> filaments."""
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
        w = mm(t.GetWidth())
        na = model.node(net, lid, mm(a.x), mm(a.y), z)
        nb = model.node(net, lid, mm(b.x), mm(b.y), z)
        model.seg(na, nb, w)


def add_vias(board, model, zmap, nets):
    """Vias -> vertical filaments spanning the copper layers they connect."""
    cu = list(board.GetEnabledLayers().CuStack())
    for t in board.GetTracks():
        if t.Type() != pcbnew.PCB_VIA_T:
            continue
        net = t.GetNetname()
        if net not in nets:
            continue
        p = t.GetPosition()
        x, y = mm(p.x), mm(p.y)
        d = mm(t.GetWidth())
        # nodes on every copper layer between top and bottom of the via span
        top = t.TopLayer(); bot = t.BottomLayer()
        span = [l for l in cu if _between(l, top, bot, cu)]
        prev = None
        for lid in span:
            z = zmap[lid]
            n = model.node(net, lid, x, y, z)
            if prev is not None:
                model.seg(prev, n, d)     # vertical barrel
            prev = n


def _between(lid, top, bot, cu):
    i = cu.index(lid) if lid in cu else None
    it = cu.index(top) if top in cu else 0
    ib = cu.index(bot) if bot in cu else len(cu) - 1
    return i is not None and min(it, ib) <= i <= max(it, ib)


def add_zones(board, model, zmap, nets, pitch, roi=None):
    """Copper pours -> gridded filament mesh clipped to the filled polygon and to
    an optional ROI box (rx0,ry0,rx1,ry1) so only loop-relevant copper is meshed.

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


# --------------------------------------------------------------------------- #
# pads, THT stacks, FET leads, ports
# --------------------------------------------------------------------------- #
def _pad_node_stack(board, model, zmap, fp, want_net):
    """For the pad(s) of `fp` on `want_net`, return the node on the TOP copper
    layer and connect the THT stack vertically across all layers the pad touches."""
    cu = list(board.GetEnabledLayers().CuStack())
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
            n = model.node(want_net, lid, x, y, zmap[lid])
            if prev is not None:
                model.seg(prev, n, mm(pad.GetSizeX()) or 1.0)
            prev = n
            if lid == cu[0] or top_node is None:
                top_node = n
        return top_node, (x, y)
    return None, None


def build_fet(board, model, zmap, topo, role, lead_mm):
    """Add lead stubs + die shorts for one FET role; return port endpoints."""
    d = topo[role]
    refs = d["refs"]
    fps = {fp.GetReference(): fp for fp in board.GetFootprints() if fp.GetReference() in refs}
    # Parallel FETs share the drain/source rails: model each one's drain+source
    # lead stubs (they parallel in the power loop, lowering the effective lead L)
    # and equiv their dies. Only the FIRST FET's gate is ported (d["gate"]);
    # paralleled FETs may sit on separate gate nets (Net-(Q1-G) vs Net-(Q3-G)), so
    # their gates are not required or modeled here.
    die_src = die_drn = die_gate = None
    src_pad_node = None
    for ref in refs:
        fp = fps[ref]
        dn, dpos = _pad_node_stack(board, model, zmap, fp, d["drain"])
        sn, spos = _pad_node_stack(board, model, zmap, fp, d["source"])
        if not (dn and sn):
            raise ValueError(f"{ref}: could not resolve drain/source pads "
                             f"(drain={d['drain']}, source={d['source']})")
        # vertical lead stubs up to a die plane at z = +lead_mm
        dref = model.node(d["drain"], "DIE", *dpos, lead_mm)
        sref = model.node(d["source"], "DIE", *spos, lead_mm)
        model.seg(dn, dref, 1.0)
        model.seg(sn, sref, 1.0)
        model.equiv(dref, sref)  # channel short at the die
        if die_src is None:
            # first FET: also model + port its gate loop
            gn, gpos = _pad_node_stack(board, model, zmap, fp, d["gate"])
            if not gn:
                raise ValueError(f"{ref}: could not resolve gate pad on {d['gate']}")
            gref = model.node(d["gate"], "DIE", *gpos, lead_mm)
            model.seg(gn, gref, 0.5)
            model.equiv(gref, sref)
            die_src, die_drn, die_gate = sref, dref, gref
            src_pad_node = sn
        else:
            model.equiv(die_src, sref)
            model.equiv(die_drn, dref)
    d["_die_src"] = die_src
    d["_die_gate"] = die_gate
    d["_src_pad_node"] = src_pad_node
    return d


def gate_driver_node(board, model, zmap, net):
    """Endpoint of the gate net copper farthest from the FET gate pad = driver/Rg end.
    Returns the node at that track endpoint (interned on its layer)."""
    endpoints = []
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T or t.GetNetname() != net:
            continue
        lid = t.GetLayer(); z = zmap.get(lid)
        if z is None:
            continue
        for pt in (t.GetStart(), t.GetEnd()):
            endpoints.append((lid, mm(pt.x), mm(pt.y)))
    if not endpoints:
        return None
    # pick the endpoint with the fewest coincidences (a dangling trace end = driver side)
    from collections import Counter
    key = lambda e: (round(e[1] / SNAP), round(e[2] / SNAP))
    cnt = Counter(key(e) for e in endpoints)
    dangling = [e for e in endpoints if cnt[key(e)] == 1]
    pick = dangling[0] if dangling else endpoints[0]
    lid, x, y = pick
    return model.node(net, lid, x, y, zmap[lid])


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
          cin_refs=None, include_bulk=False, weld_tol=0.6):
    zmap = layer_z_map(board)
    model = Model()
    power_nets = {topo["sw"], topo["vin"], topo["gnd"]}
    gate_nets = {topo["hs"]["gate"], topo["ls"]["gate"]}
    nets = power_nets | gate_nets
    roi = _roi(board, topo, margin)

    add_tracks(board, model, zmap, nets)
    add_vias(board, model, zmap, nets)  # gate traces can change layers too — model their vias
    add_zones(board, model, zmap, power_nets, pitch, roi=roi)

    # FET leads + die shorts
    build_fet(board, model, zmap, topo, "hs", lead_mm)
    build_fet(board, model, zmap, topo, "ls", lead_mm)

    # Cin pad stacks (create nodes now so the global stitch bonds them to the pours).
    # With cin_parallel>1 the N nearest ceramics each get their own port, so the
    # solve captures their mutual coupling and the reduce step forms the true
    # parallel loop L (not the pessimistic single-cap bound).
    cins = cin_ports(board, model, zmap, topo, n=cin_parallel,
                     refs=cin_refs, include_bulk=include_bulk)

    # bond every track/via/pad node into the pour mesh on its net+layer, then weld
    # near-coincident endpoints interning missed (pad-centre vs trace-end, touching
    # fills) — essential for nets with no pour (gate) and multi-fill power planes.
    model.stitch_zones(pitch)
    model.weld(weld_tol)

    # ---- ports ----
    cin_labels = []
    for i, (_ref, vn, gn) in enumerate(cins):
        label = "P_pwr" if i == 0 else f"P_pwr{i}"
        model.port(label, vn, gn)
        cin_labels.append(label)
    model.cin_ports = cin_labels
    topo["cin_used"] = [ref for ref, _, _ in cins]
    for role, label in (("hs", "P_ghs"), ("ls", "P_gls")):
        d = topo[role]
        drv = gate_driver_node(board, model, zmap, d["gate"])
        ret = d["_die_src"] if d["kelvin"] else d["_src_pad_node"]
        if drv and ret:
            model.port(label, drv, ret)
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
                    help="explicit input-cap refdes to port (overrides nearest-N)")
    ap.add_argument("--include-bulk-cin", action="store_true",
                    help="also port bulk electrolytics (>=10uF); default excludes them")
    ap.add_argument("--lead-mm", type=float, default=3.0, help="FET exposed-lead length (mm)")
    ap.add_argument("--weld-tol", type=float, default=0.6,
                    help="fuse same-net nodes within this many mm (fixes pad/trace and "
                         "touching-fill fragmentation; 0 disables)")
    ap.add_argument("--margin", type=float, default=8.0,
                    help="ROI margin (mm) around FETs/Cin for pour meshing")
    ap.add_argument("--nwinc", type=int, default=1, help="filament width sub-mesh (skin; >1 slower, more HF-accurate)")
    ap.add_argument("--nhinc", type=int, default=1, help="filament height sub-mesh (skin)")
    ap.add_argument("-o", "--out", required=True, help="output .inp path")
    args = ap.parse_args()

    board = pcbnew.LoadBoard(args.pcb)
    topo = fet_discovery.discover(
        board, args.sw, args.gnd, vin=args.vin,
        hs_ref=args.hs_ref, ls_ref=args.ls_ref,
        hs_gate=args.hs_gate, ls_gate=args.ls_gate,
        hs_kelvin=args.hs_kelvin, ls_kelvin=args.ls_kelvin)
    model = build(board, topo, pitch=args.pitch, lead_mm=args.lead_mm, margin=args.margin,
                  cin_parallel=args.cin_parallel, cin_refs=args.cin_refs,
                  include_bulk=args.include_bulk_cin, weld_tol=args.weld_tol)
    stats = model.write(args.out, nwinc=args.nwinc, nhinc=args.nhinc)
    # sidecar: port order + topology for the reduce step
    ports = [lbl for lbl, _, _ in model.ports]
    cin_used = topo.get("cin_used", [])
    cin_warn = None
    if len(cin_used) < args.cin_parallel:
        cin_warn = (f"requested --cin-parallel {args.cin_parallel} but only "
                    f"{len(cin_used)} eligible Cin ported ({', '.join(cin_used) or 'none'})")
        sys.stderr.write("WARNING: " + cin_warn + "\n")
    side = dict(ports=ports, cin_ports=getattr(model, "cin_ports", ["P_pwr"]),
                cin_used=cin_used, cin_requested=args.cin_parallel, cin_warn=cin_warn,
                topo={k: (v if not isinstance(v, dict) else
                          {kk: vv for kk, vv in v.items() if not kk.startswith("_") and kk != "pads"})
                      for k, v in topo.items()},
                pitch=args.pitch, lead_mm=args.lead_mm)
    with open(args.out + ".ports.json", "w") as f:
        json.dump(side, f, indent=2)
    print(f"wrote {args.out}: {stats['nodes']} nodes, {stats['segs']} segs, "
          f"ports={ports}  Cin(in order)={cin_used}")


if __name__ == "__main__":
    main()
