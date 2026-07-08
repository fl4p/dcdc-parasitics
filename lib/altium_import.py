"""Convert Altium .PcbDoc -> .kicad_pcb for the dcdc-tools parasitics extractor.

The KiCad 9 programmatic Altium importer (PCB_IO_MGR.ALTIUM_DESIGNER) has three
known bugs that break the parasitics extractor's FastHenry mesh:

1. **Pad layer-set inversion**: SMD footprints are placed "Bottom" by the
   importer, so pad GetLayerSet() contains B_Cu instead of F_Cu, even though
   GetLayer() reports 0 (F.Cu).  Pads and zones end up on different layers,
   severing pad-to-pour connectivity.

2. **Power-track mis-layering**: wide power-bus tracks (e.g. 3 mm Vb rail)
   land on B.Cu instead of F.Cu, disconnected from the F.Cu FET pads.

3. **Dropped zone objects**: some copper pours are simply lost — not
   mis-layered, but absent from the output entirely.  On ReboostV2 the
   top-layer Vb pour covering the FET/Cin area is gone, leaving a 2.8 mm
   gap between the Q1 drain pad and the nearest surviving Vb track.

This module applies a three-tier fix:

* **Uniform pad relayer** — swap every B_Cu-only SMD pad layer set to F_Cu
  (also mask/paste).  This is a uniform correction of the importer's
  footprint-flip bug.

* **Targeted power-track relayer** — move only power-net (vin/gnd/sw) tracks
  from B.Cu to F.Cu.  Signal tracks are left on their imported layers to
  avoid creating a degenerate single-layer mesh (moving ALL 1184 tracks
  produces an all-NaN Zc matrix).

* **Power-zone relayer + Vb-pour fallback** — add F.Cu to existing GND/Vb
  zone layer sets so they fill on the same layer as the pads.  If no Vb
  zone covers the power-stage bounding box (computed from FET drain pads
  + Cin Vin pads), insert a minimal bridging Vb zone on F.Cu via
  S-expression text insertion (the pcbnew.ZONE() Python constructor hangs).

Zone fill MUST be performed after a Save+reopen cycle: calling
ZONE_FILLER.Fill() on the freshly-Loaded Altium board hangs/SIGSEGVs
because the imported board object has incomplete internal connectivity state.
The fix is: Load(ALTIUM) -> Save(KICAD) -> LoadBoard() -> Fill -> Save.

The function returns a provenance dict so the caller can embed it in
parasitics.json and report.md.
"""

import json
import os
import re

import pcbnew

KICAD_PY_DEFAULT = (
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
    "Python.framework/Versions/Current/bin/python3"
)

MM = pcbnew.FromMM
NM = pcbnew.FromMM


def _board_bbox_mm(board, nets, refs):
    """Bounding box (x0, y0, x1, y1) in mm of pads on the given nets
    that ALSO belong to the given refs.  This intersection (not union)
    keeps the bbox tight around the power-stage components."""
    xs, ys = [], []
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        if ref not in refs:
            continue
        for p in fp.Pads():
            pn = str(p.GetNetname())
            if pn in nets:
                pos = p.GetPosition()
                xs.append(pos.x / 1e6)
                ys.append(pos.y / 1e6)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _power_stage_bbox(board, vin_net, fet_refs, cin_refs, max_dist=8.0):
    """Tight bbox for the Vb-pour bridge: FET vin pads + Cin vin pads
    that are within *max_dist* mm of a FET vin pad.  Excludes far bulk
    caps and gate-driver-area caps that would inflate the pour."""
    fet_pads = []
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        if ref not in fet_refs:
            continue
        for p in fp.Pads():
            if str(p.GetNetname()) == vin_net:
                pos = p.GetPosition()
                fet_pads.append((pos.x / 1e6, pos.y / 1e6))
    if not fet_pads:
        return None

    xs, ys = [c for c in zip(*fet_pads)] if fet_pads else ([], [])
    xs, ys = list(xs), list(ys)
    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        if ref not in cin_refs:
            continue
        for p in fp.Pads():
            if str(p.GetNetname()) != vin_net:
                continue
            pos = p.GetPosition()
            px, py = pos.x / 1e6, pos.y / 1e6
            if any(((px - fx) ** 2 + (py - fy) ** 2) ** 0.5 < max_dist
                   for fx, fy in fet_pads):
                xs.append(px)
                ys.append(py)
    return min(xs), min(ys), max(xs), max(ys)


def _zone_covers_bbox(board, netname, bbox, layer_id=0):
    """True if any filled zone of *netname* on *layer_id* overlaps *bbox*."""
    x0, y0, x1, y1 = bbox
    for i in range(board.GetAreaCount()):
        a = board.GetArea(i)
        if str(a.GetNetname()) != netname:
            continue
        ls = a.GetLayerSet()
        if not ls.Contains(layer_id):
            continue
        bb = a.GetBoundingBox()
        bx0, by0 = bb.GetX() / 1e6, bb.GetY() / 1e6
        bx1 = (bb.GetX() + bb.GetWidth()) / 1e6
        by1 = (bb.GetY() + bb.GetHeight()) / 1e6
        if bx0 < x1 and bx1 > x0 and by0 < y1 and by1 > y0:
            return True
    return False


def _insert_vb_zone(pcb_path, net_code, net_name, bbox, margin=1.0,
                     layer="F.Cu"):
    """Insert a Vb zone into the .kicad_pcb S-expression text.

    The pcbnew.ZONE(board) Python constructor hangs (swiginit deadlock), so
    we edit the file text directly.  The zone is inserted before the first
    existing zone with the same net name, or appended before the closing
    ``)`` of the kicad_pcb element if none is found.

    Returns the inserted polygon area in mm^2 (for provenance).
    """
    x0, y0, x1, y1 = bbox
    x0 -= margin
    y0 -= margin
    x1 += margin
    y1 += margin
    area = (x1 - x0) * (y1 - y0)

    zone_block = (
        '\t(zone\n'
        f'\t\t(net {net_code})\n'
        f'\t\t(net_name "{net_name}")\n'
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "a1b2c3d4-e5f6-7890-abcd-ef1234567890")\n'
        f'\t\t(hatch edge 0.5)\n'
        f'\t\t(priority 4)\n'
        f'\t\t(connect_pads yes\n'
        f'\t\t\t(clearance 0.5)\n'
        f'\t\t)\n'
        f'\t\t(min_thickness 0.25)\n'
        f'\t\t(filled_areas_thickness no)\n'
        f'\t\t(fill yes\n'
        f'\t\t\t(thermal_gap 0.2)\n'
        f'\t\t\t(thermal_bridge_width 0.4)\n'
        f'\t\t)\n'
        f'\t\t(polygon\n'
        f'\t\t\t(pts\n'
        f'\t\t\t\t(xy {x0:.4f} {y0:.4f}) (xy {x1:.4f} {y0:.4f}) '
        f'(xy {x1:.4f} {y1:.4f}) (xy {x0:.4f} {y1:.4f})\n'
        f'\t\t\t)\n'
        f'\t\t)\n'
        f'\t)\n'
    )

    with open(pcb_path, "r") as f:
        text = f.read()

    # Insert before the first existing zone with this net_name
    pattern = re.compile(
        r'(\t\(zone\n\t\t\(net \d+\)\n\t\t\(net_name "' +
        re.escape(net_name) + r'"\))',
        re.MULTILINE,
    )
    match = pattern.search(text)
    if match:
        text = text[: match.start()] + zone_block + text[match.start():]
    else:
        # Append before the final closing paren of the kicad_pcb element
        text = text.rstrip() + "\n" + zone_block + ")\n"

    with open(pcb_path, "w") as f:
        f.write(text)

    return round(area, 2)


def convert(
    src_pcbdoc,
    dst_kicad_pcb,
    vin_net="Vb",
    gnd_net="GND",
    sw_net=None,
    hs_refs=None,
    ls_refs=None,
    cin_refs=None,
    relayer="partial",
):
    """Convert an Altium .PcbDoc to a parasitics-ready .kicad_pcb.

    Parameters
    ----------
    src_pcbdoc : str
        Path to the source Altium .PcbDoc file.
    dst_kicad_pcb : str
        Path for the output .kicad_pcb file.
    vin_net, gnd_net, sw_net : str
        Net names for the input rail, ground, and switch node.  Used for
        targeted track/zone relayering and the Vb-pour fallback bbox.
    hs_refs, ls_refs : list[str]
        Refdes of the HS/LS FETs.  Used to compute the power-stage bbox.
    cin_refs : list[str]
        Refdes of input capacitors.  Their Vin pads near the FETs define
        the Vb-pour bridge bbox.
    relayer : str
        Relayer strategy: "partial" flips pads + power-net tracks + zones
        from B.Cu to F.Cu (single-layer approximation); "none" keeps the
        raw import as-is on B.Cu (preserves multilayer via-stitched loop).
        The Vb-pour fallback is inserted on F.Cu for "partial" or B.Cu
        for "none".

    Returns
    -------
    dict
        Provenance metadata: pads_fixed, tracks_relayered, zones_relayered,
        vb_pour_synthesized (or None), warnings, relayer.
    """
    meta = {
        "source": src_pcbdoc,
        "output": dst_kicad_pcb,
        "relayer": relayer,
        "pads_fixed": 0,
        "tracks_relayered": 0,
        "zones_relayered": 0,
        "vb_pour_synthesized": None,
        "warnings": [],
    }

    power_nets = {n for n in (vin_net, gnd_net, sw_net) if n}
    fet_refs = set((hs_refs or []) + (ls_refs or []))
    cin_ref_set = set(cin_refs or [])
    do_relayer = relayer != "none"
    target_layer = pcbnew.F_Cu if do_relayer else pcbnew.B_Cu
    target_layer_name = "F.Cu" if do_relayer else "B.Cu"

    # --- Step 1: Altium -> KiCad (no fill — fill hangs on the raw import) ---
    board = pcbnew.PCB_IO_MGR.Load(
        pcbnew.PCB_IO_MGR.ALTIUM_DESIGNER, src_pcbdoc
    )
    pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, dst_kicad_pcb, board)

    # --- Step 2: Re-open as native KiCad (rebuilds connectivity state) ---
    board = pcbnew.LoadBoard(dst_kicad_pcb)

    if do_relayer:
        # --- Step 3: Fix pad layer sets (B.Cu -> F.Cu) ---
        for fp in board.GetFootprints():
            for p in fp.Pads():
                ls = p.GetLayerSet()
                if ls.Contains(pcbnew.B_Cu) and not ls.Contains(pcbnew.F_Cu):
                    ls.RemoveLayer(pcbnew.B_Cu)
                    ls.AddLayer(pcbnew.F_Cu)
                    if ls.Contains(pcbnew.B_Mask):
                        ls.RemoveLayer(pcbnew.B_Mask)
                        ls.AddLayer(pcbnew.F_Mask)
                    if ls.Contains(pcbnew.B_Paste):
                        ls.RemoveLayer(pcbnew.B_Paste)
                        ls.AddLayer(pcbnew.F_Paste)
                    p.SetLayerSet(ls)
                    meta["pads_fixed"] += 1

        # --- Step 4: Relayer power-net tracks (B.Cu -> F.Cu only) ---
        for t in board.GetTracks():
            if t.Type() != pcbnew.PCB_TRACE_T:
                continue
            if t.GetLayer() != 2:  # B.Cu
                continue
            if str(t.GetNetname()) in power_nets:
                t.SetLayer(0)  # F.Cu
                meta["tracks_relayered"] += 1

        # --- Step 5: Add F.Cu to power-net zone layer sets ---
        for i in range(board.GetAreaCount()):
            a = board.GetArea(i)
            net = str(a.GetNetname())
            if net not in power_nets:
                continue
            ls = a.GetLayerSet()
            if ls.Contains(pcbnew.B_Cu) and not ls.Contains(pcbnew.F_Cu):
                ls.AddLayer(pcbnew.F_Cu)
                a.SetLayerSet(ls)
                meta["zones_relayered"] += 1

    # Save before the text-edit step (so the file on disk matches the board)
    pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, dst_kicad_pcb, board)

    # --- Step 6: Vb-pour fallback (if no Vb zone covers the FET area) ---
    if vin_net:
        # Compute power-stage bbox from FET drain pads + LOCAL Cin Vin pads
        # only (filter Cin to those within 5mm of a FET drain pad)
        bbox = _power_stage_bbox(board, vin_net, fet_refs, cin_ref_set)
        if bbox and not _zone_covers_bbox(board, vin_net, bbox, target_layer):
            net_code = 0
            nl = board.GetNetInfo()
            for code in nl.NetsByNetcode():
                net = nl.GetNetItem(code)
                if net and str(net.GetNetname()) == vin_net:
                    net_code = code
                    break
            area = _insert_vb_zone(
                dst_kicad_pcb, net_code, vin_net, bbox, margin=1.0,
                layer=target_layer_name,
            )
            meta["vb_pour_synthesized"] = {
                "bbox": [round(v, 2) for v in bbox],
                "area_mm2": area,
                "layer": target_layer_name,
                "reason": (
                    "Altium importer dropped the FET-area Vb copper pour; "
                    "a minimal bridging rectangle was inserted to connect "
                    "FET drain pads to Cin Vin pads and surviving Vb tracks."
                ),
            }
            meta["warnings"].append(
                f"Vb pour SYNTHESIZED ({area} mm^2 on {target_layer_name}) "
                "— L_loop carries geometry uncertainty from the invented "
                "copper shape"
            )

    # --- Step 7: Re-open, fill zones, save ---
    board = pcbnew.LoadBoard(dst_kicad_pcb)
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, dst_kicad_pcb, board)

    return meta


def write_meta(path, meta):
    """Write conversion metadata beside the converted board."""
    with open(path, "w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")


def is_altium(path):
    """True if the path looks like an Altium PCB file."""
    return path.lower().endswith(".pcbdoc")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", help="Altium .PcbDoc path")
    ap.add_argument("-o", "--out", required=True, help="output .kicad_pcb path")
    ap.add_argument("--meta-out", default=None,
                    help="conversion metadata JSON path; default: OUT.altium.json")
    ap.add_argument("--vin", default="Vb", help="input rail net name")
    ap.add_argument("--gnd", default="GND", help="ground net name")
    ap.add_argument("--sw", default=None, help="switch node net name")
    ap.add_argument("--hs-ref", nargs="*", default=None, help="HS FET refdes")
    ap.add_argument("--ls-ref", nargs="*", default=None, help="LS FET refdes")
    ap.add_argument("--cin-refs", nargs="*", default=None, help="Cin refdes")
    ap.add_argument("--relayer", choices=("partial", "none"), default="partial",
                    help="relayer strategy: partial (flip pads+tracks to F.Cu) "
                         "or none (keep raw B.Cu, preserves multilayer loop)")
    args = ap.parse_args()

    meta = convert(
        args.src,
        args.out,
        vin_net=args.vin,
        gnd_net=args.gnd,
        sw_net=args.sw,
        hs_refs=args.hs_ref,
        ls_refs=args.ls_ref,
        cin_refs=args.cin_refs,
        relayer=args.relayer,
    )
    meta_out = args.meta_out or (args.out + ".altium.json")
    write_meta(meta_out, meta)
    print(json.dumps(meta, indent=2))
