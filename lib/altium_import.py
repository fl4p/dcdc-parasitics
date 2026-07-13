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


def _net_pad_points(board, netname, refs, near=None, max_dist=8.0):
    """(x, y) of every *netname* pad on the footprints in *refs*, in IU.

    With *near* (a list of reference points), only pads within *max_dist* mm of
    one of them are returned. The pour check MUST use this for the Cin side: a
    distant bulk cap that happens to share a pour outline with the FET would
    otherwise satisfy the bridge test, and the guard would pass on exactly the
    geometry it exists to catch — the dropped FET-to-LOCAL-Cin pour. Same
    locality filter _power_stage_bbox() applies."""
    lim = MM(max_dist)
    pts = []
    for fp in board.GetFootprints():
        if str(fp.GetReference()) not in refs:
            continue
        for p in fp.Pads():
            if str(p.GetNetname()) != netname:
                continue
            pos = p.GetPosition()
            if near is not None and not any(
                    ((pos.x - nx) ** 2 + (pos.y - ny) ** 2) ** 0.5 < lim
                    for nx, ny in near):
                continue
            pts.append((pos.x, pos.y))
    return pts


def _pad_cu_layer(board, netname, refs, default=None):
    """The copper layer the SMD *netname* pads of *refs* actually sit on.

    The pour check must probe where the pads ENDED UP, not where the relayer
    mode says they should be — deriving the layer from the mode makes the guard
    test a different layer than the one carrying the FETs.

    Only an SMD pad names a side. A THROUGH-HOLE pad spans the whole stack, so
    its layer set always starts at F.Cu and would drag the probe onto F.Cu even
    for a `--relayer none` board whose power copper is entirely on B.Cu — turning
    a healthy board into a loud false MISSING (and, in partial mode, an invented
    bridge on the wrong layer). For a THT FET the mode-derived *default* is the
    only meaningful answer, so fall back to it.
    """
    for fp in board.GetFootprints():
        if str(fp.GetReference()) not in refs:
            continue
        for p in fp.Pads():
            if str(p.GetNetname()) != netname:
                continue
            cu = list(p.GetLayerSet().CuStack())
            if len(cu) == 1:          # SMD: sits on exactly one copper layer
                return cu[0]
    return default


def _pour_bridges_pads(board, netname, layer, fet_pts, cin_pts):
    """True iff ONE FILLED polygon of *netname* on *layer* contains at least one
    FET Vin pad AND at least one local Cin Vin pad.

    This is the physical question the guard exists to answer: does real copper
    bridge the FET drain to the local Cin?  The previous bounding-box test
    answered a much weaker one — a ring/L-shaped pour elsewhere on the net, or a
    zone whose fill came out EMPTY, has a bbox that spans the FET area and
    passed, so the dropped-pour condition read as verified-clean.

    Zones must already be FILLED when this runs.  A pad centre sitting in a
    thermal-relief void would read as not-covered; that direction is safe (a
    loud false warning), the reverse is not.
    """
    for i in range(board.GetAreaCount()):
        a = board.GetArea(i)
        if str(a.GetNetname()) != netname:
            continue
        if not a.GetLayerSet().Contains(layer):
            continue
        try:
            ps = a.GetFilledPolysList(layer)
        except Exception:
            continue
        if ps is None or ps.IsEmpty():
            continue  # zone exists but fills to nothing: NOT coverage
        for oi in range(ps.OutlineCount()):
            poly = pcbnew.SHAPE_POLY_SET()
            poly.AddOutline(ps.Outline(oi))
            hit_fet = any(poly.Contains(pcbnew.VECTOR2I(x, y)) for x, y in fet_pts)
            if not hit_fet:
                continue
            if any(poly.Contains(pcbnew.VECTOR2I(x, y)) for x, y in cin_pts):
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
        # Insert before the final closing paren of the kicad_pcb element
        stripped = text.rstrip()
        idx = stripped.rfind(")")
        if idx < 0:
            raise ValueError("cannot find closing paren in .kicad_pcb")
        text = stripped[:idx] + zone_block + stripped[idx:] + "\n"

    with open(pcb_path, "w") as f:
        f.write(text)

    return round(area, 2)


def _swap_cu(layer):
    """F.Cu<->B.Cu and In1.Cu<->In2.Cu; other layers unchanged.

    The KiCad Altium importer maps layers DIRECT (Altium TOP->F.Cu,
    BOTTOM->B.Cu, MID1->In1, MID2->In2). The dcdc/ground-truth convention is
    the consistent global FLIP (Altium BOTTOM->F.Cu, TOP->B.Cu, MID1->In2,
    MID2->In1) so the bottom-mounted power stage lands on F.Cu. That flip is
    exactly this uniform swap applied to the raw import."""
    return {
        pcbnew.F_Cu: pcbnew.B_Cu, pcbnew.B_Cu: pcbnew.F_Cu,
        pcbnew.In1_Cu: pcbnew.In2_Cu, pcbnew.In2_Cu: pcbnew.In1_Cu,
    }.get(layer, layer)


def _swap_tech_pairs():
    """{layer: opposite-side layer} for the F/B technical-layer pairs."""
    pairs = {}
    for f, b in (("F_Mask", "B_Mask"), ("F_Paste", "B_Paste"),
                 ("F_SilkS", "B_SilkS"), ("F_Adhes", "B_Adhes"),
                 ("F_CrtYd", "B_CrtYd"), ("F_Fab", "B_Fab")):
        lf, lb = getattr(pcbnew, f, None), getattr(pcbnew, b, None)
        if lf is not None and lb is not None:
            pairs[lf], pairs[lb] = lb, lf
    return pairs


_TECH_SWAP = _swap_tech_pairs()


def _swap_any(layer):
    """Opposite-side layer for copper AND F/B technical layers."""
    sw = _swap_cu(layer)
    return sw if sw != layer else _TECH_SWAP.get(layer, layer)


def _swap_lset(ls):
    """Layer set with every layer mapped to its opposite side."""
    new = pcbnew.LSET()
    for lid in ls.Seq():
        new.AddLayer(_swap_any(lid))
    return new


def _cu_index(layer):
    """Position of a copper layer in the physical stack (top -> bottom)."""
    order = [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu]
    return order.index(layer) if layer in order else len(order)


def _relayer_faithful(board, meta):
    """Faithful full-board relayer: uniform global RELABEL of the raw import.

    This is a pure layer remap — F<->B, In1<->In2 — applied to every pad layer
    set, footprint side flag, track and zone.  NO geometry moves.

    It must NOT use FOOTPRINT.Flip(): Flip() mirrors each footprint's pad
    offsets about its own anchor (verified on ReboostV2: Q2's G1/G2 gate pads
    exchange places, ±6.1 mm) while standalone tracks and zones are only
    relabelled, never mirrored.  That leaves pads sitting on copper they are
    not connected to on the real board.  We are re-interpreting which physical
    layer each object lives on, not physically moving parts to the other side
    of the board, so the pad XY must stay exactly as imported.

    Because it starts from the RAW import (where the importer put pads AND
    their tracks on the same direct-mapped layer), the uniform relabel
    preserves pad<->track consistency while reaching the flipped convention —
    unlike the 'partial' relayer, which moves pads but leaves signal tracks,
    stranding them on the opposite layer."""
    # _swap_cu only knows F<->B and In1<->In2. On a board with copper beyond In2
    # the extra inner layers would be left where the importer put them while
    # everything else flips — a mixed, inconsistent stack that cuts the
    # via-stitched return and still reports clean relayer counts. REFUSE it
    # rather than mis-relabel it silently. (The KiPEX adapter refuses the same
    # boards for the same reason.)
    known = {pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu}
    extra = [l for l in board.GetEnabledLayers().CuStack() if l not in known]
    if extra:
        names = ", ".join(board.GetStandardLayerName(l) for l in extra)
        raise NotImplementedError(
            f"the 'faithful' relayer can only relabel F/B/In1/In2 copper; this "
            f"board also enables {names}. Relabelling only the layers it knows "
            f"would leave that copper on the un-flipped layer and cut the "
            f"via-stitched return. Extend _swap_cu() for this stack first.")

    for fp in board.GetFootprints():
        for p in fp.Pads():
            ls = p.GetLayerSet()
            new = _swap_lset(ls)
            if list(new.Seq()) != list(ls.Seq()):
                p.SetLayerSet(new)
                meta["pads_relayered"] += 1
        # zones can be owned by a FOOTPRINT (fp.Zones()); board.GetArea() does not
        # enumerate those, so they would keep the importer's layers
        for z in fp.Zones():
            ls = z.GetLayerSet()
            new = _swap_lset(ls)
            if list(new.Seq()) != list(ls.Seq()):
                z.SetLayerSet(new)
                meta["zones_relayered"] += 1
        for it in fp.GraphicalItems():
            it.SetLayer(_swap_any(it.GetLayer()))
        nl = _swap_any(fp.GetLayer())
        if nl != fp.GetLayer():
            # side flag only; SetLayer() does not mirror geometry (Flip() does)
            fp.SetLayer(nl)
            meta["footprints_relayered"] += 1
    for t in board.GetTracks():
        # Straight segments AND arcs are copper: relabelling only PCB_TRACE_T
        # strands every arc on the un-relabelled layer, cutting the traces it
        # joins. (ReboostV2 happens to have zero arcs — do not rely on that.)
        if t.Type() in (pcbnew.PCB_TRACE_T, pcbnew.PCB_ARC_T):
            nl = _swap_cu(t.GetLayer())
            if nl != t.GetLayer():
                t.SetLayer(nl)
                meta["tracks_relayered"] += 1
        elif t.Type() == pcbnew.PCB_VIA_T:
            # A through via spans the whole stack, so F<->B is a no-op for it;
            # a blind/buried via's span must be relabelled with everything else
            # or it lands on the wrong layers.
            if t.GetViaType() != pcbnew.VIATYPE_THROUGH:
                top, bot = _swap_cu(t.TopLayer()), _swap_cu(t.BottomLayer())
                # SetLayerPair wants (top, bottom) in stack order
                if _cu_index(top) > _cu_index(bot):
                    top, bot = bot, top
                t.SetLayerPair(top, bot)
                meta["vias_relayered"] += 1
    for i in range(board.GetAreaCount()):
        a = board.GetArea(i)
        ls = a.GetLayerSet()
        new = _swap_lset(ls)
        if list(new.Seq()) != list(ls.Seq()):
            a.SetLayerSet(new)
            meta["zones_relayered"] += 1
    # Board-level graphics (gr_poly / gr_rect / gr_line) can be COPPER — Altium
    # regions and polygon pours often import as PCB_SHAPE on a copper layer.
    # Leaving them un-relabelled strands that copper on the opposite layer.
    for d in board.GetDrawings():
        nl = _swap_any(d.GetLayer())
        if nl != d.GetLayer():
            d.SetLayer(nl)
            if _swap_cu(d.GetLayer()) != d.GetLayer():  # it is on a copper layer
                meta["shapes_relayered"] += 1


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
        from B.Cu to F.Cu (single-layer approximation); "faithful" relabels
        the copper of the WHOLE board (F<->B, In1<->In2) with no geometry
        change; "none" keeps the raw import as-is on B.Cu (preserves
        multilayer via-stitched loop).  The Vb-pour bridge is only ever
        synthesized for "partial"; the other modes warn instead.

    Returns
    -------
    dict
        Provenance metadata: pads_fixed, tracks_relayered, zones_relayered,
        pads_relayered, footprints_relayered, vb_pour_check,
        vb_pour_synthesized (or None), warnings, relayer.
    """
    meta = {
        "source": src_pcbdoc,
        "output": dst_kicad_pcb,
        "relayer": relayer,
        # 'partial' counters: pads de-inverted, POWER-net tracks/zones relayered
        "pads_fixed": 0,
        "tracks_relayered": 0,
        "zones_relayered": 0,
        # 'faithful' counters: whole-board relabel (every net, not just power)
        "pads_relayered": 0,
        "footprints_relayered": 0,
        "vias_relayered": 0,
        "shapes_relayered": 0,
        "vb_pour_check": None,
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

    if relayer == "faithful":
        _relayer_faithful(board, meta)
    elif do_relayer:
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
            if t.GetLayer() != pcbnew.B_Cu:
                continue
            if str(t.GetNetname()) in power_nets:
                t.SetLayer(pcbnew.F_Cu)
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

    # --- Step 6: fill zones (fill must precede the pour check: an unfilled zone
    #     has copper only in intent, and the check asks about real copper) ---
    board = _reload_and_fill(dst_kicad_pcb)

    # --- Step 7: Vb-pour check (the importer silently DROPS copper pours) ---
    # The CHECK runs for every relayer — a dropped FET-area Vin pour breaks the
    # commutation loop no matter which relayer produced the board, and a missing
    # pour must never pass silently. Only the SYNTHESIS is partial-relayer-only
    # (faithful keeps the real Altium copper); the other modes get a loud
    # warning instead of invented copper.
    if vin_net:
        # Probe the layer the FET Vin pads ACTUALLY landed on, not the one the
        # relayer mode implies (see _pad_cu_layer).
        probe_layer = _pad_cu_layer(board, vin_net, fet_refs, default=target_layer)
        # CANONICAL name ("F.Cu"), not GetLayerName() — that returns the board's
        # USER layer name, which on an Altium import is "Top Layer". A zone whose
        # (layer ...) is a user name does not resolve, and the board fails to load.
        probe_name = board.GetStandardLayerName(probe_layer)
        fet_pts = _net_pad_points(board, vin_net, fet_refs)
        # LOCAL Cin pads only — a far bulk cap sharing the pour must not satisfy
        # the bridge test (see _net_pad_points).
        cin_pts = _net_pad_points(board, vin_net, cin_ref_set, near=fet_pts)
        # bbox for the synthesized bridge (FET pads + LOCAL Cin pads only)
        bbox = _power_stage_bbox(board, vin_net, fet_refs, cin_ref_set)

        if not fet_pts or not cin_pts or bbox is None:
            missing = []
            if not fet_pts:
                missing.append(f"no {vin_net} pad on FET refs {sorted(fet_refs) or '(none given)'}")
            if not cin_pts:
                missing.append(f"no {vin_net} pad on Cin refs {sorted(cin_ref_set) or '(none given)'}")
            meta["vb_pour_check"] = "UNVERIFIED"
            meta["warnings"].append(
                f"Vb-pour check UNVERIFIED ({'; '.join(missing) or 'no power-stage bbox'}) "
                "— could NOT confirm the FET-area Vin pour survived the import. This is "
                "not a pass: pass --hs-ref/--ls-ref/--cin-refs so the check can run."
            )
        elif _pour_bridges_pads(board, vin_net, probe_layer, fet_pts, cin_pts):
            meta["vb_pour_check"] = "present"
        elif relayer == "partial":
            net_code = 0
            nl = board.GetNetInfo()
            for code in nl.NetsByNetcode():
                net = nl.GetNetItem(code)
                if net and str(net.GetNetname()) == vin_net:
                    net_code = code
                    break
            area = _insert_vb_zone(
                dst_kicad_pcb, net_code, vin_net, bbox, margin=1.0,
                layer=probe_name,
            )
            # re-fill and RE-CHECK: a bridge that does not actually bridge must
            # not be recorded as one (a guard that never re-verifies its own fix
            # is how a false "repaired" verdict gets persisted).
            board = _reload_and_fill(dst_kicad_pcb)
            bridged = _pour_bridges_pads(
                board, vin_net, probe_layer,
                _net_pad_points(board, vin_net, fet_refs),
                _net_pad_points(board, vin_net, cin_ref_set,
                                near=_net_pad_points(board, vin_net, fet_refs)))
            meta["vb_pour_synthesized"] = {
                "bbox": [round(v, 2) for v in bbox],
                "area_mm2": area,
                "layer": probe_name,
                "bridges_fet_to_cin": bridged,
                "reason": (
                    "Altium importer dropped the FET-area Vb copper pour; "
                    "a minimal bridging rectangle was inserted to connect "
                    "FET drain pads to Cin Vin pads and surviving Vb tracks."
                ),
            }
            meta["vb_pour_check"] = (
                "missing -> synthesized" if bridged
                else "missing -> synthesis FAILED to bridge")
            meta["warnings"].append(
                f"Vb pour SYNTHESIZED ({area} mm^2 on {probe_name}) "
                "— L_loop carries geometry uncertainty from the invented "
                "copper shape"
            )
            if not bridged:
                meta["warnings"].append(
                    f"The synthesized {vin_net} pour STILL does not bridge the FET "
                    f"drain to the local Cin on {probe_name} — the Vin path is not "
                    "repaired. Do not trust L_loop; restore the pour in the KiCad GUI."
                )
        else:
            meta["vb_pour_check"] = "MISSING (not synthesized)"
            meta["vb_pour_missing"] = {
                "bbox": [round(v, 2) for v in bbox],
                "layer": probe_name,
            }
            meta["warnings"].append(
                f"No filled {vin_net} pour bridges the FET drain to the local Cin "
                f"on {probe_name} — the Altium importer most likely DROPPED it "
                f"(importer bug 3). relayer={relayer} does not synthesize a bridging "
                "pour, so the FET drain reaches the local Cin over TRACK copper only "
                "(or not at all): L_loop is extracted through a Vin path narrower "
                "than the real board's. Cross-check against a KiCad GUI import, use "
                "--relayer partial to bridge it, or restore the pour in the GUI."
            )

    # Record what the board actually is, so a stale sidecar cannot be pinned to a
    # board it does not describe (the consumer compares this to the board's hash).
    meta["output_sha256"] = _sha256(dst_kicad_pcb)
    # ...and WHICH CONVERTER produced it. Hashing only the output board would let a
    # committed sidecar keep serving a plausible provenance verdict after this
    # file's relayer/pour logic changed underneath it — the signature must cover
    # the code that DERIVES the value, not just the data it describes.
    meta["converter_sha256"] = _sha256(os.path.abspath(__file__))
    return meta


def _reload_and_fill(path):
    """Re-open the saved board and fill its zones, then save.

    Fill MUST be performed after a Save+reopen cycle: ZONE_FILLER.Fill() on a
    freshly-Loaded Altium board hangs/SIGSEGVs (incomplete connectivity state)."""
    board = pcbnew.LoadBoard(path)
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, path, board)
    return pcbnew.LoadBoard(path)


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
    ap.add_argument("--relayer", choices=("partial", "none", "groundtruth", "faithful"),
                    default="partial",
                    help="relayer strategy: partial (flip pads+power tracks to F.Cu, "
                         "for power-loop extraction), faithful (uniform global RELABEL "
                         "of the whole board's copper -> correct full-board layers, no "
                         "geometry change), none (keep raw), or groundtruth (rebuild "
                         "power zones from .PcbDoc via altium_monkey)")
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
