#!/usr/bin/env python3
"""Auto-discover the half-bridge power-stage topology from a KiCad board.

Runs under KiCad's bundled python (pcbnew). Given the switch-node net and the
GND net, it identifies the high-side (HS) and low-side (LS) MOSFETs, the input
rail (Vin), each FET's gate net, the input capacitors that close the commutation
loop, and each gate driver's return reference (Kelvin-source vs power-source,
which decides whether the common-source inductance appears in the gate loop).

Identification is by NET CONNECTIVITY, not pad-name strings (pad names like
G/D/S are not reliable across footprints):
  * HS FET  = a FET with a pad on SW whose other power pad is NOT GND (that net
              is Vin).  Source=SW, Drain=Vin.
  * LS FET  = a FET with a pad on SW and a pad on GND.  Drain=SW, Source=GND.
  * gate    = the remaining FET pad net (the small-signal net: fewest board-wide
              pad connections of the FET's non-SW nets).

Everything is overridable from the CLI. Returns a plain dict (JSON-friendly) so
the geometry step can consume it without importing pcbnew types.
"""
import argparse
import json
import os

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))


def _nm_to_mm(v):
    return v / 1e6


def _pad_count_by_net(board):
    """Board-wide pad count per net name -> used to tell a bulk rail from a gate."""
    counts = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            n = pad.GetNetname()
            if n:
                counts[n] = counts.get(n, 0) + 1
    return counts


def _fet_pads(fp):
    """Return [(netname, (x_mm, y_mm), layer_id)] for the electrically-live pads."""
    out = []
    for pad in fp.Pads():
        net = pad.GetNetname()
        if not net:
            continue
        pos = pad.GetPosition()
        out.append((net, (_nm_to_mm(pos.x), _nm_to_mm(pos.y)), pad.GetLayer()))
    return out


def _is_fet(fp):
    ref = fp.GetReference().upper()
    fpid = str(fp.GetFPID().GetLibItemName()).upper()
    if ref.startswith("Q"):
        return True
    return any(k in fpid for k in ("MOSFET", "TO-220", "TO-247", "DPAK", "D2PAK",
                                   "SOT-23", "TO-252", "TO-263", "POWERPAK"))


def _unique_nets(pads):
    seen, out = set(), []
    for net, _, _ in pads:
        if net not in seen:
            seen.add(net)
            out.append(net)
    return out


def gate_network(board, gate_net, owner_refs, exclude_nets=()):
    """Discrete gate-drive parts in series between the gate net and the driver net.

    Detects a series gate resistor and an anti-parallel diode by connectivity: any
    part (not the FET itself) with a pad on the FET-side gate net; its other pad is
    the driver-side net. Parts whose other pad lands on a power/reference net in
    `exclude_nets` (SW/GND/Vin) are gate-source **pulldowns/clamps**, not series
    drive elements, and are skipped. Returns dict(r=..., d=..., driver_net=...) where
    r/d are each dict(ref, value, driver_net) or None. Annotated on the schematic for
    context — FastHenry meshes only copper, so R/D are not part of the extraction.
    """
    r = d = driver_net = None
    owners = set(owner_refs)
    excl = set(exclude_nets)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref in owners:
            continue
        nets = [p.GetNetname() for p in fp.Pads() if p.GetNetname()]
        if gate_net not in nets:
            continue
        others = [n for n in nets if n != gate_net]
        if not others or others[0] in excl:
            continue  # dangling, or a gate-source pulldown/clamp — not series drive
        drv = others[0]
        entry = dict(ref=ref, value=fp.GetValue(), driver_net=drv)
        up = ref.upper()
        if up.startswith("R") and r is None:
            r = entry
            driver_net = driver_net or drv
        elif up.startswith("D") and d is None:
            d = entry
            driver_net = driver_net or drv
    return dict(r=r, d=d, driver_net=driver_net)


def discover(board, sw, gnd, vin=None, hs_ref=None, ls_ref=None,
             hs_gate=None, ls_gate=None, hs_kelvin=None, ls_kelvin=None):
    """Return a topology dict. Raises ValueError if the half-bridge is ambiguous."""
    padcount = _pad_count_by_net(board)
    if sw not in padcount:
        raise ValueError(f"switch net {sw!r} not found on any pad")
    if gnd not in padcount:
        raise ValueError(f"gnd net {gnd!r} not found on any pad")

    hs, ls = [], []
    for fp in board.GetFootprints():
        if not _is_fet(fp):
            continue
        ref = fp.GetReference()
        pads = _fet_pads(fp)
        nets = _unique_nets(pads)
        if sw not in nets:
            continue  # only FETs touching the switch node are half-bridge switches
        rec = dict(ref=ref, pads=pads, nets=nets)
        if gnd in nets:
            ls.append(rec)
        else:
            hs.append(rec)

    if hs_ref:
        hs = [r for r in hs if r["ref"] in set(hs_ref)]
    if ls_ref:
        ls = [r for r in ls if r["ref"] in set(ls_ref)]
    if not hs:
        raise ValueError(f"no high-side FET found on {sw!r} (drain to a non-GND rail)")
    if not ls:
        raise ValueError(f"no low-side FET found bridging {sw!r} and {gnd!r}")

    def classify_gate_and_rail(rec, other_power_net, gate_override):
        """other_power_net is the known bulk rail besides SW (GND for LS, Vin for HS
        when known). Returns (gate_net, rail_net)."""
        cand = [n for n in rec["nets"] if n != sw and n != other_power_net]
        if gate_override:
            gate = gate_override
        elif len(cand) == 1:
            gate = cand[0]
        elif not cand:
            raise ValueError(
                f"{rec['ref']}: invalid FET topology on {sw!r}: no separate gate net "
                f"found (D/S/G may be shorted or the selected nets are wrong)")
        else:
            # gate = smallest board-wide pad count (signal net, not a bulk rail)
            gate = min(cand, key=lambda n: padcount.get(n, 0))
        rail = next((n for n in rec["nets"] if n != sw and n != gate), other_power_net)
        return gate, rail

    # HS: rail is Vin. If --vin not given, infer as the non-gate, non-SW net.
    hs_gate_net, vin_net = classify_gate_and_rail(hs[0], vin, hs_gate)
    if vin:
        vin_net = vin
    # LS: rail is GND (known); gate is the remaining net.
    ls_gate_net, _ = classify_gate_and_rail(ls[0], gnd, ls_gate)

    def validate_switch(role, recs, gate, drain, source):
        shorts = []
        if drain == source:
            shorts.append("D-S")
        if gate == source:
            shorts.append("G-S")
        if gate == drain:
            shorts.append("G-D")
        if shorts:
            refs = ",".join(r["ref"] for r in recs)
            raise ValueError(
                f"{role.upper()} FET topology invalid for {refs}: "
                f"{'/'.join(shorts)} shorted (gate={gate!r}, drain={drain!r}, "
                f"source={source!r}); not a valid half-bridge switch")
        first = recs[0]
        missing = [n for n in (gate, drain, source) if n not in first["nets"]]
        if missing:
            raise ValueError(
                f"{role.upper()} FET topology invalid for {first['ref']}: missing "
                f"expected net(s) {missing}; gate={gate!r}, drain={drain!r}, "
                f"source={source!r}")

    validate_switch("hs", hs, hs_gate_net, vin_net, sw)
    validate_switch("ls", ls, ls_gate_net, sw, gnd)

    # Cin: caps with one pad on Vin and one on GND.
    cin = []
    for fp in board.GetFootprints():
        if not fp.GetReference().upper().startswith("C"):
            continue
        nets = {p.GetNetname() for p in fp.Pads()}
        if vin_net in nets and gnd in nets:
            cin.append(fp.GetReference())

    # Gate-return reference. Default (worst case): non-Kelvin -> gate return shares
    # the FET's power-source net, so the full source lead is common-source. A Kelvin
    # layout returns to the die-source; caller can force it with --*-kelvin.
    hs_kv = bool(hs_kelvin)
    ls_kv = bool(ls_kelvin)

    def source_net(role):
        return sw if role == "hs" else gnd

    topo = dict(
        pcb=board.GetFileName(),
        copper_layers=board.GetCopperLayerCount(),
        sw=sw, gnd=gnd, vin=vin_net,
        hs=dict(refs=[r["ref"] for r in hs], gate=hs_gate_net,
                drain=vin_net, source=sw, kelvin=hs_kv,
                gate_return=source_net("hs") if not hs_kv else "KELVIN",
                gate_drive=gate_network(board, hs_gate_net, [r["ref"] for r in hs],
                                        exclude_nets={sw, gnd, vin_net}),
                pads={r["ref"]: r["pads"] for r in hs}),
        ls=dict(refs=[r["ref"] for r in ls], gate=ls_gate_net,
                drain=sw, source=gnd, kelvin=ls_kv,
                gate_return=source_net("ls") if not ls_kv else "KELVIN",
                gate_drive=gate_network(board, ls_gate_net, [r["ref"] for r in ls],
                                        exclude_nets={sw, gnd, vin_net}),
                pads={r["ref"]: r["pads"] for r in ls}),
        cin=cin,
    )
    return topo


def _report(topo):
    print(f"board            : {os.path.basename(topo['pcb'])} "
          f"({topo['copper_layers']} cu layers)")
    print(f"SW  net          : {topo['sw']}")
    print(f"GND net          : {topo['gnd']}")
    print(f"Vin net          : {topo['vin']}")
    for role in ("hs", "ls"):
        d = topo[role]
        print(f"{role.upper()} FET(s)       : {', '.join(d['refs'])}")
        print(f"    gate net     : {d['gate']}")
        print(f"    drain/source : {d['drain']} / {d['source']}")
        print(f"    gate return  : {d['gate_return']}"
              f"  ({'Kelvin -> CSI excluded' if d['kelvin'] else 'shared source -> CSI in gate loop'})")
        gdw = d.get("gate_drive") or {}
        gr, gd = gdw.get("r"), gdw.get("d")
        parts = []
        if gr:
            parts.append(f"Rg {gr['ref']}={gr['value']}")
        if gd:
            parts.append(f"anti-parallel D {gd['ref']}={gd['value']}")
        print(f"    gate drive   : {' , '.join(parts) or '(no series R/D found — direct drive)'}"
              + (f"  from {gdw.get('driver_net')}" if gdw.get("driver_net") else ""))
    print(f"Cin (Vin<->GND)  : {', '.join(topo['cin']) or '(none found)'}")


def main():
    ap = argparse.ArgumentParser(description="Discover half-bridge topology from a KiCad PCB")
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
    ap.add_argument("--json", action="store_true", help="dump topology as JSON")
    args = ap.parse_args()

    board = pcbnew.LoadBoard(args.pcb)
    topo = discover(board, args.sw, args.gnd, vin=args.vin,
                    hs_ref=args.hs_ref, ls_ref=args.ls_ref,
                    hs_gate=args.hs_gate, ls_gate=args.ls_gate,
                    hs_kelvin=args.hs_kelvin, ls_kelvin=args.ls_kelvin)
    if args.json:
        print(json.dumps(topo, indent=2))
    else:
        _report(topo)


if __name__ == "__main__":
    main()
