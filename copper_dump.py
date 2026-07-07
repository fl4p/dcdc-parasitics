#!/usr/bin/env python3
"""Dump real PCB copper (zones / tracks / pads / vias / board edge) to JSON in mm,
in the SAME pcbnew coordinate frame kicad_geom.py uses for the FastHenry mesh — so
`power_copper.py --copper <json>` overlays the extracted mesh on the real copper
with zero coordinate transform.

Runs under KiCad's bundled Python (needs pcbnew); re-execs under $KICAD_PY otherwise.

Usage:
    copper_dump.py PCB [-o copper.json]
"""
import json, os, sys, argparse

KICAD_PY = os.environ.get(
    "KICAD_PY",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")
try:
    import pcbnew  # type: ignore
except ImportError:
    if os.path.exists(KICAD_PY) and os.path.abspath(sys.executable) != os.path.abspath(KICAD_PY):
        os.execv(KICAD_PY, [KICAD_PY, __file__] + sys.argv[1:])
    raise

NM = 1e6


def mm(v):
    return v / NM


def poly_to_rings(sps):
    rings = []
    for oi in range(sps.OutlineCount()):
        ol = sps.Outline(oi)
        pts = [(mm(ol.CPoint(i).x), mm(ol.CPoint(i).y)) for i in range(ol.PointCount())]
        if len(pts) >= 3:
            rings.append(pts)
    return rings


def pad_rings(p, lyr):
    for args in ((lyr, pcbnew.ERROR_INSIDE), (lyr,), ()):
        try:
            r = poly_to_rings(p.GetEffectivePolygon(*args))
            if r:
                return r
        except Exception:
            continue
    return []


def dump(pcb_path):
    b = pcbnew.LoadBoard(pcb_path)
    FCU, BCU = pcbnew.F_Cu, pcbnew.B_Cu
    out = {"zones": {"F": [], "B": []}, "tracks": {"F": [], "B": []},
           "vias": [], "pads": {"F": [], "B": []}, "edge": []}
    for z in b.Zones():
        for lyr, key in ((FCU, "F"), (BCU, "B")):
            if z.IsOnLayer(lyr):
                try:
                    sps = z.GetFilledPolysList(lyr)
                except TypeError:
                    sps = z.GetFilledPolysList()
                out["zones"][key].extend(poly_to_rings(sps))
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            c = t.GetPosition()
            out["vias"].append([mm(c.x), mm(c.y), mm(t.GetWidth()) / 2])
        else:
            lyr = t.GetLayer()
            key = "F" if lyr == FCU else ("B" if lyr == BCU else None)
            if key:
                s, e = t.GetStart(), t.GetEnd()
                out["tracks"][key].append([mm(s.x), mm(s.y), mm(e.x), mm(e.y), mm(t.GetWidth())])
    for fp in b.GetFootprints():
        for p in fp.Pads():
            for lyr, key in ((FCU, "F"), (BCU, "B")):
                if p.IsOnLayer(lyr):
                    out["pads"][key].extend(pad_rings(p, lyr))
    try:
        for d in b.GetDrawings():
            if d.GetLayer() == pcbnew.Edge_Cuts and d.GetClass() == "PCB_SHAPE":
                out["edge"].append([mm(d.GetStart().x), mm(d.GetStart().y),
                                    mm(d.GetEnd().x), mm(d.GetEnd().y)])
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcb")
    ap.add_argument("-o", "--out", default="copper.json")
    a = ap.parse_args()
    out = dump(a.pcb)
    json.dump(out, open(a.out, "w"))
    print(f"wrote {a.out}: zones F/B {len(out['zones']['F'])}/{len(out['zones']['B'])}, "
          f"tracks F/B {len(out['tracks']['F'])}/{len(out['tracks']['B'])}, "
          f"vias {len(out['vias'])}, pads F/B {len(out['pads']['F'])}/{len(out['pads']['B'])}")


if __name__ == "__main__":
    main()
