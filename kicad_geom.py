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
    # parallel FETs share drain/source/gate rails; model each, equiv their dies
    die_src = die_drn = die_gate = None
    src_pad_node = None
    for ref in refs:
        fp = fps[ref]
        dn, dpos = _pad_node_stack(board, model, zmap, fp, d["drain"])
        sn, spos = _pad_node_stack(board, model, zmap, fp, d["source"])
        gn, gpos = _pad_node_stack(board, model, zmap, fp, d["gate"])
        if not (dn and sn and gn):
            raise ValueError(f"{ref}: could not resolve drain/source/gate pads")
        # vertical lead stubs up to a die plane at z = +lead_mm
        dref = model.node(d["drain"], "DIE", *dpos, lead_mm)
        sref = model.node(d["source"], "DIE", *spos, lead_mm)
        gref = model.node(d["gate"], "DIE", *gpos, lead_mm)
        model.seg(dn, dref, 1.0)
        model.seg(sn, sref, 1.0)
        model.seg(gn, gref, 0.5)
        # channel + gate-source close at the die
        model.equiv(dref, sref)
        model.equiv(gref, sref)
        if die_src is None:
            die_src, die_drn, die_gate = sref, dref, gref
            src_pad_node = sn
        else:
            model.equiv(die_src, sref)
            model.equiv(die_drn, dref)
            model.equiv(die_gate, gref)
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


def build(board, topo, pitch=1.0, lead_mm=3.0, margin=8.0):
    zmap = layer_z_map(board)
    model = Model()
    power_nets = {topo["sw"], topo["vin"], topo["gnd"]}
    gate_nets = {topo["hs"]["gate"], topo["ls"]["gate"]}
    nets = power_nets | gate_nets
    roi = _roi(board, topo, margin)

    add_tracks(board, model, zmap, nets)
    add_vias(board, model, zmap, power_nets)
    add_zones(board, model, zmap, power_nets, pitch, roi=roi)

    # FET leads + die shorts
    build_fet(board, model, zmap, topo, "hs", lead_mm)
    build_fet(board, model, zmap, topo, "ls", lead_mm)

    # Cin pad stacks (create nodes now so the global stitch bonds them to the pours)
    cinp, cinm = cin_port_nodes(board, model, zmap, topo)

    # bond every track/via/pad node into the pour mesh on its net+layer
    model.stitch_zones(pitch)

    # ---- ports ----
    if cinp and cinm:
        model.port("P_pwr", cinp, cinm)
    for role, label in (("hs", "P_ghs"), ("ls", "P_gls")):
        d = topo[role]
        drv = gate_driver_node(board, model, zmap, d["gate"])
        ret = d["_die_src"] if d["kelvin"] else d["_src_pad_node"]
        if drv and ret:
            model.port(label, drv, ret)
    return model


def cin_port_nodes(board, model, zmap, topo):
    """Pick the Cin nearest the FETs; return its Vin-pad and GND-pad nodes."""
    # FET centroid
    fps = [fp for fp in board.GetFootprints()
           if fp.GetReference() in (topo["hs"]["refs"] + topo["ls"]["refs"])]
    cx = sum(mm(fp.GetPosition().x) for fp in fps) / len(fps)
    cy = sum(mm(fp.GetPosition().y) for fp in fps) / len(fps)
    best = None
    for fp in board.GetFootprints():
        if fp.GetReference() not in topo["cin"]:
            continue
        nets = {p.GetNetname() for p in fp.Pads()}
        if not ({topo["vin"], topo["gnd"]} <= nets):
            continue
        p = fp.GetPosition()
        dsq = (mm(p.x) - cx) ** 2 + (mm(p.y) - cy) ** 2
        if best is None or dsq < best[0]:
            best = (dsq, fp)
    if best is None:
        return None, None
    fp = best[1]
    vn, _ = _pad_node_stack(board, model, zmap, fp, topo["vin"])
    gn, _ = _pad_node_stack(board, model, zmap, fp, topo["gnd"])
    return vn, gn


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
    ap.add_argument("--lead-mm", type=float, default=3.0, help="FET exposed-lead length (mm)")
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
    model = build(board, topo, pitch=args.pitch, lead_mm=args.lead_mm)
    stats = model.write(args.out, nwinc=args.nwinc, nhinc=args.nhinc)
    # sidecar: port order + topology for the reduce step
    ports = [lbl for lbl, _, _ in model.ports]
    side = dict(ports=ports, topo={k: (v if not isinstance(v, dict) else
                                        {kk: vv for kk, vv in v.items() if not kk.startswith("_") and kk != "pads"})
                                    for k, v in topo.items()},
                pitch=args.pitch, lead_mm=args.lead_mm)
    with open(args.out + ".ports.json", "w") as f:
        json.dump(side, f, indent=2)
    print(f"wrote {args.out}: {stats['nodes']} nodes, {stats['segs']} segs, "
          f"ports={ports}")


if __name__ == "__main__":
    main()
