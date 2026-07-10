#!/usr/bin/env python3
"""Ground-truth Altium→KiCad zone rebuild using altium_monkey.

Runs under /tmp/altium-venv/bin/python3 (NOT pcbnew Python). Reads real
zone geometry from .PcbDoc via altium_monkey, applies a self-calibrating
Y-mirror transform, and injects clean filled zones into a base .kicad_pcb
(produced by the partial relayer). No synth bridge, no double-facing,
no ZONE_FILLER — regions are pre-poured outlines.

Layer flip (consistent global):
  Altium BOTTOM(32) → F.Cu
  Altium TOP(1)     → B.Cu
  Altium MID1(2)    → In2.Cu
  Altium MID2(3)    → In1.Cu

The transform self-calibrates per board by matching pad positions
between the altium_monkey .PcbDoc and the converted .kicad_pcb.
"""
import json
import os
import re
import sys

from altium_monkey import AltiumPcbDoc

ALTium_PY = os.environ.get("ALTIUM_PY", "/tmp/altium-venv/bin/python3")

# Altium layer → KiCad layer (consistent global flip)
LMAP = {32: "F.Cu", 1: "B.Cu", 2: "In2.Cu", 3: "In1.Cu"}


def _net_name(doc, net_index):
    try:
        n = doc.nets[net_index]
        for a in ("name", "net_name", "Name"):
            if hasattr(n, a):
                return getattr(n, a)
    except Exception:
        return None


def _resolve_region_net(doc, region):
    n = _net_name(doc, region.net_index)
    if n is None and region.polygon_index not in (65535, None):
        p = doc.polygons[region.polygon_index]
        n = p.net if isinstance(p.net, str) else _net_name(doc, p.net)
    return n


def _calibrate_transform(doc, base_pcb_path):
    """Self-calibrate Y-mirror transform by matching pad positions
    between altium_monkey .PcbDoc and the converted .kicad_pcb.

    Returns (tx, ty) such that:
      x_kicad = x_altium_mils * 0.0254 + tx
      y_kicad = ty - y_altium_mils * 0.0254
    """
    import pcbnew
    board = pcbnew.LoadBoard(base_pcb_path)
    kicad_pads = {}
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        for p in fp.Pads():
            pn = str(p.GetNumber())
            pos = p.GetPosition()
            kicad_pads[(ref, pn)] = (pos.x / 1e6, pos.y / 1e6)

    altium_pads = {}
    for comp in doc.components:
        ref = getattr(comp, "name", None) or getattr(comp, "reference", None)
        if not ref:
            continue
        for pad in getattr(comp, "pads", []) or []:
            pn = getattr(pad, "name", None) or getattr(pad, "number", None)
            if pn is None:
                continue
            x = getattr(pad, "x_mils", None) or getattr(pad, "x", None)
            y = getattr(pad, "y_mils", None) or getattr(pad, "y", None)
            if x is not None and y is not None:
                altium_pads[(ref, str(pn))] = (float(x), float(y))

    dxs, dys = [], []
    for key, (kx, ky) in kicad_pads.items():
        if key in altium_pads:
            ax, ay = altium_pads[key]
            ax_mm = ax * 0.0254
            ay_mm = ay * 0.0254
            dxs.append(kx - ax_mm)
            dys.append(ky + ay_mm)

    if not dxs:
        raise ValueError("cannot calibrate transform: no matching pads")

    dxs.sort()
    dys.sort()
    tx = dxs[len(dxs) // 2]
    ty = dys[len(dys) // 2]
    return tx, ty


def _dedupe_pts(pts, eps=1e-4):
    out = []
    for p in pts:
        if not out or (abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps):
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) < eps and abs(out[0][1] - out[-1][1]) < eps:
        out.pop()
    return out


def _uuid():
    import uuid
    return str(uuid.uuid4())


def build_groundtruth(src_pcbdoc, base_kicad_pcb, dst_kicad_pcb,
                      power_nets=("Vb", "HSS", "GND")):
    """Rebuild power-stage copper from altium_monkey ground truth.

    Parameters
    ----------
    src_pcbdoc : str
        Path to the source Altium .PcbDoc.
    base_kicad_pcb : str
        Path to the partial-relayer converted .kicad_pcb (provides
        footprints, pads, vias, non-power tracks/zones).
    dst_kicad_pcb : str
        Path for the output ground-truth .kicad_pcb.
    power_nets : tuple[str]
        Net names whose zones should be rebuilt from ground truth.

    Returns
    -------
    dict
        Provenance metadata.
    """
    meta = {
        "source": src_pcbdoc,
        "base": base_kicad_pcb,
        "output": dst_kicad_pcb,
        "relayer": "groundtruth",
        "regions_injected": 0,
        "tracks_removed": 0,
        "zones_removed": 0,
        "transform": None,
        "warnings": [],
    }

    doc = AltiumPcbDoc.from_file(src_pcbdoc)
    power_set = set(power_nets)

    tx, ty = _calibrate_transform(doc, base_kicad_pcb)
    meta["transform"] = {"tx": round(tx, 4), "ty": round(ty, 4), "type": "Y-mirror"}

    def T(xm, ym):
        return (round(xm * 0.0254 + tx, 6), round(ty - ym * 0.0254, 6))

    net_nums = {}
    for i, n in enumerate(doc.nets):
        name = _net_name(doc, i)
        if name:
            net_nums[name] = i

    zones_out = []
    for r in doc.shapebased_regions:
        n = _resolve_region_net(doc, r)
        if n not in power_set or r.layer not in LMAP:
            continue
        layer = LMAP[r.layer]
        raw = [T(v.x_mils, v.y_mils) for v in r.outline]
        pts = _dedupe_pts(raw)
        if len(pts) < 3:
            continue
        net_code = net_nums.get(n, 0)
        ptsxy = " ".join(f"(xy {x} {y})" for x, y in pts)
        zones_out.append(
            f'\t(zone\n'
            f'\t\t(net {net_code})\n'
            f'\t\t(net_name "{n}")\n'
            f'\t\t(layer "{layer}")\n'
            f'\t\t(uuid "{_uuid()}")\n'
            f'\t\t(hatch edge 0.5)\n'
            f'\t\t(connect_pads yes (clearance 0.2))\n'
            f'\t\t(min_thickness 0.1)\n'
            f'\t\t(filled_areas_thickness no)\n'
            f'\t\t(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.4))\n'
            f'\t\t(polygon (pts {ptsxy}))\n'
            f'\t\t(filled_polygon (layer "{layer}") (pts {ptsxy}))\n'
            f'\t)'
        )
        meta["regions_injected"] += 1

    src = open(base_kicad_pcb).read()

    def spans(kw, s):
        out = []
        for m in re.finditer(r'\(' + kw + r'\b', s):
            j = m.start()
            d = 0
            k = j
            while k < len(s):
                c = s[k]
                if c == '(':
                    d += 1
                elif c == ')':
                    d -= 1
                    if d == 0:
                        break
                k += 1
            out.append((j, k + 1))
        return out

    kill = []
    for a, b in spans('zone', src):
        z = src[a:b]
        m = re.search(r'\(net_name "([^"]*)"', z)
        if m and m.group(1) in power_set:
            kill.append((a, b))
            meta["zones_removed"] += 1
    for a, b in spans('segment', src):
        z = src[a:b]
        m = re.search(r'\(net (\d+)\)', z)
        if m and int(m.group(1)) in {net_nums.get(n, -1) for n in power_set}:
            kill.append((a, b))
            meta["tracks_removed"] += 1
    kill.sort(reverse=True)
    for a, b in kill:
        e = b
        while e < len(src) and src[e] in ' \t\r\n':
            e += 1
        src = src[:a] + src[e:]

    inject = "\n" + "\n".join(zones_out) + "\n"
    last = src.rstrip()
    assert last.endswith(')'), "board doesn't end with )"
    src = last[:-1] + inject + ")\n"
    open(dst_kicad_pcb, "w").write(src)

    meta["warnings"].append(
        "Ground-truth zones from altium_monkey (no synth, no ZONE_FILLER). "
        "Power tracks removed (loop is pour+via dominated)."
    )
    return meta


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", help="Altium .PcbDoc path")
    ap.add_argument("--base", required=True, help="base .kicad_pcb (partial relayer output)")
    ap.add_argument("-o", "--out", required=True, help="output ground-truth .kicad_pcb")
    ap.add_argument("--meta-out", default=None, help="metadata JSON path")
    ap.add_argument("--power-nets", nargs="*", default=["Vb", "HSS", "GND"],
                    help="power net names to rebuild from ground truth")
    args = ap.parse_args()

    meta = build_groundtruth(args.src, args.base, args.out, tuple(args.power_nets))
    meta_out = args.meta_out or (args.out + ".altium.json")
    with open(meta_out, "w") as f:
        json.dump(meta, f, indent=2)
    print(json.dumps(meta, indent=2))
