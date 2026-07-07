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

    def __init__(self, cu_thickness=CU_T):
        self._nodes = {}          # key -> node name
        self._pos = {}            # node name -> (x,y,z)
        self.meta = {}            # node name -> (net, layer)
        self.zone_nodes = set()   # names created as pour-mesh nodes
        self.segs = []            # (name, na, nb, w, h)
        self.equivs = []          # (na, nb)
        self.ports = []           # (label, na, nb)
        self.cin_ports = ["P_pwr"]  # labels of the commutation (Cin) ports
        self.cu_thickness = cu_thickness
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

    def seg(self, na, nb, w, h=None):
        if na == nb:
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

    def write(self, path, fmin=1e5, fmax=1e8, ndec=3, nwinc=1, nhinc=1, sigma=SIGMA):
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


def build_fet(board, model, zmap, topo, role, lead_mm, parallel_mode="lumped"):
    """Add lead stubs + die shorts for one FET role; return port endpoints."""
    d = topo[role]
    refs = d["refs"]
    _require_unique_device_labels(role, refs, parallel_mode)
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
        dn, dpos = _pad_node_stack(board, model, zmap, fp, drain)
        sn, spos = _pad_node_stack(board, model, zmap, fp, source)
        if not (dn and sn):
            raise ValueError(f"{ref}: could not resolve drain/source pads "
                             f"(drain={drain}, source={source})")
        # vertical lead stubs up to a die plane at z = +lead_mm
        die_layer = "DIE" if parallel_mode == "lumped" else f"DIE_{ref}"
        dref = model.node(drain, die_layer, *dpos, lead_mm)
        sref = model.node(source, die_layer, *spos, lead_mm)
        model.seg(dn, dref, 1.0)
        model.seg(sn, sref, 1.0)
        model.equiv(dref, sref)  # channel short at the die
        if parallel_mode == "per-device" or die_src is None:
            gn, gpos = _pad_node_stack(board, model, zmap, fp, gate)
            if not gn:
                raise ValueError(f"{ref}: could not resolve gate pad on {gate}")
            gref = model.node(gate, die_layer, *gpos, lead_mm)
            model.seg(gn, gref, 0.5)
            model.equiv(gref, sref)
        else:
            gn = gref = None
        if die_src is None:
            die_src, die_drn, die_gate = sref, dref, gref
            gate_pad_node, src_pad_node, drn_pad_node = gn, sn, dn
        elif parallel_mode == "lumped":
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


def validate_required_ports(model, topo):
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
        raise ValueError("invalid half-bridge topology for parasitic extraction:\n  - "
                         + "\n  - ".join(missing))


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
          cin_refs=None, include_bulk=False, weld_tol=0.6, emit_cin_network=False,
          parallel_fets="lumped"):
    zmap = layer_z_map(board)
    model = Model(cu_thickness=cu_thickness)
    power_nets = {topo["sw"], topo["vin"], topo["gnd"]}

    def side_gate_nets(role):
        if parallel_fets == "per-device":
            return {dev.get("gate", topo[role]["gate"])
                    for dev in topo[role].get("devices", [])}
        return {topo[role]["gate"]}

    gate_nets = side_gate_nets("hs") | side_gate_nets("ls")
    nets = power_nets | gate_nets
    roi = _roi(board, topo, margin)

    add_tracks(board, model, zmap, nets)
    add_vias(board, model, zmap, nets)  # gate traces can change layers too — model their vias
    add_zones(board, model, zmap, power_nets, pitch, roi=roi)

    # FET leads + die shorts
    build_fet(board, model, zmap, topo, "hs", lead_mm, parallel_mode=parallel_fets)
    build_fet(board, model, zmap, topo, "ls", lead_mm, parallel_mode=parallel_fets)

    # Cin pad stacks (create nodes now so the global stitch bonds them to the pours).
    # With cin_parallel>1 the N nearest ceramics each get their own port, so the
    # solve captures their mutual coupling and the reduce step forms the true
    # parallel loop L (not the pessimistic single-cap bound).
    cins = cin_ports(board, model, zmap, topo, n=cin_parallel,
                     refs=cin_refs, include_bulk=include_bulk)

    # LF conduction anchor: nearest bulk electrolytic (sources the 39 kHz
    # fundamental). Create its pad nodes now so the stitch/weld below bonds them
    # into the pours. Exclude the HF cin-anchor refs so an all-MLCC bank can't pick
    # the same cap as P_pwr (which would duplicate the port -> singular Zc).
    cref = conduction_ref(board, model, zmap, topo, prefer="bulk",
                          exclude={ref for ref, _, _ in cins})

    # --emit-cin-network: port the FULL input bank (bulk+mlcc) individually for the
    # per-cap branch decomposition. Nodes created here (pre-stitch) so they bond in;
    # reuses the HF cin ports (P_pwr...) and adds P_cin_<ref> for the rest.
    if emit_cin_network:
        hf_labels = [(ref, "P_pwr" if i == 0 else f"P_pwr{i}")
                     for i, (ref, _, _) in enumerate(cins)]
        # the LF conduction anchor cap is ported as P_bulk (below) — fold it into
        # cin_net under that label so it isn't double-ported (its P_cin_<ref> would
        # duplicate P_bulk on the same node pair and make FastHenry singular).
        anchors = [(cref[0], "P_bulk", cref[3])] if cref else []
        cin_network_ports(board, model, zmap, topo, hf_labels, anchors)

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
    if cref:
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

    # drop any port disconnected from the commutation loop (e.g. a distant bulk cap
    # whose pad never bonds into the pour at this pitch) — one floating port NaNs the
    # entire FastHenry solve. Keep the topo/cin_net manifests consistent.
    dropped = model.drop_floating_ports("P_pwr")
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
    validate_required_ports(model, topo)
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


def cin_network_ports(board, model, zmap, topo, hf_labels, anchors=None):
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
    fp_by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()
                 if fp.GetReference() in topo["cin"]}

    def _entry(ref, cls, lbl):
        fp = fp_by_ref.get(ref)
        return dict(ref=ref, cls=cls, label=lbl,
                    C=(_cap_farads(fp) if fp else None))

    net = [_entry(ref, cls_map.get(ref, "mlcc"), lbl) for ref, lbl in hf_labels]
    have = {ref for ref, _ in hf_labels}
    for ref, lbl, cls in (anchors or []):
        if ref not in have:
            net.append(_entry(ref, cls, lbl))
            have.add(ref)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in topo["cin"] or ref in have:
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
                    help="explicit input-cap refdes to port (overrides nearest-N)")
    ap.add_argument("--include-bulk-cin", action="store_true",
                    help="also port bulk electrolytics (>=10uF); default excludes them")
    ap.add_argument("--emit-cin-network", action="store_true",
                    help="port the full input-cap bank individually (P_cin_<ref>) for the "
                         "per-cap branch decomposition consumed by the loss tool's cin_network")
    ap.add_argument("--parallel-fets", choices=("lumped", "per-device"), default="lumped",
                    help="parallel switch model: lumped (legacy) or per-device gates/leads")
    ap.add_argument("--lead-mm", type=float, default=3.0, help="FET exposed-lead length (mm)")
    ap.add_argument("--weld-tol", type=float, default=0.6,
                    help="fuse same-net nodes within this many mm (fixes pad/trace and "
                         "touching-fill fragmentation; 0 disables)")
    ap.add_argument("--margin", type=float, default=8.0,
                    help="ROI margin (mm) around FETs/Cin for pour meshing")
    ap.add_argument("--nwinc", type=int, default=1, help="filament width sub-mesh (skin; >1 slower, more HF-accurate)")
    ap.add_argument("--nhinc", type=int, default=1, help="filament height sub-mesh (skin)")
    ap.add_argument("--cu-temp", type=float, default=20.0,
                    help="copper temperature (C) for R; scales sigma (R ~ +0.39%/K). "
                         "Isothermal — no self-heating. L is unaffected.")
    ap.add_argument("--cu-thickness", type=float, default=CU_T,
                    help="copper thickness in mm for FastHenry segment height")
    ap.add_argument("--lf-freq", type=float, default=1e5,
                    help="lowest sweep frequency Hz for LF conduction / Cin ripple R,L")
    ap.add_argument("-o", "--out", required=True, help="output .inp path")
    args = ap.parse_args()
    if args.cu_thickness <= 0:
        raise SystemExit("--cu-thickness must be > 0 mm")
    if args.lf_freq <= 0:
        raise SystemExit("--lf-freq must be > 0 Hz")

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
                      include_bulk=args.include_bulk_cin, weld_tol=args.weld_tol,
                      emit_cin_network=args.emit_cin_network,
                      parallel_fets=args.parallel_fets)
    except ValueError as e:
        raise SystemExit(str(e))
    dropped = topo.get("cin_dropped_ports")
    if dropped:
        sys.stderr.write(
            f"WARNING: dropped {len(dropped)} port(s) disconnected from the loop at "
            f"pitch {args.pitch} mm: {', '.join(dropped)} — their copper never bonded "
            f"into the meshed pour (distant bulk cap?). Lower --pitch / raise "
            f"--weld-tol / --margin to include them.\n")
    stats = model.write(args.out, fmin=args.lf_freq,
                        nwinc=args.nwinc, nhinc=args.nhinc,
                        sigma=sigma_at(args.cu_temp))
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
                pitch=args.pitch, lead_mm=args.lead_mm, cu_temp=args.cu_temp,
                cu_thickness=args.cu_thickness, lf_freq=args.lf_freq)
    with open(args.out + ".ports.json", "w") as f:
        json.dump(side, f, indent=2)
    print(f"wrote {args.out}: {stats['nodes']} nodes, {stats['segs']} segs, "
          f"ports={ports}  Cin(in order)={cin_used}")


if __name__ == "__main__":
    main()
