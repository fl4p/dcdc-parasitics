#!/usr/bin/env python3
"""Emit a standalone HTML viewer for PCB parasitic-current paths.

First target: gate-drive loops. The script runs under KiCad's bundled Python
because it needs pcbnew. If launched with a normal Python that cannot import
pcbnew, it re-execs itself under $KICAD_PY.
"""
import argparse
import html
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "lib")
sys.path.insert(0, LIB)

KICAD_PY = os.environ.get(
    "KICAD_PY",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")

REEXEC_MARKER = "DCDC_TOOLS_KICAD_PY_REEXECED"
if os.path.exists(KICAD_PY) and os.environ.get(REEXEC_MARKER) != "1":
    os.environ[REEXEC_MARKER] = "1"
    os.execv(KICAD_PY, [KICAD_PY, __file__] + sys.argv[1:])

try:
    import pcbnew  # type: ignore
except ImportError:
    if os.path.exists(KICAD_PY) and os.environ.get(REEXEC_MARKER) != "1":
        os.environ[REEXEC_MARKER] = "1"
        os.execv(KICAD_PY, [KICAD_PY, __file__] + sys.argv[1:])
    raise

import fet_discovery  # noqa: E402
import pcb_source  # noqa: E402

NM = 1e6
# Outer copper layer ids from pcbnew, NOT hardcoded: KiCad 8/9 renumbered the
# stack (F.Cu=0, B.Cu=2, inner layers 4,6,...), so the legacy B.Cu=31 dropped
# every bottom-layer pour and mis-classed bottom tracks/pads as "other".
TOP = pcbnew.F_Cu
BOTTOM = pcbnew.B_Cu

DEFAULTS = {
    "vin": None,
    "hs_ref": None,
    "ls_ref": None,
    "hs_gate": None,
    "ls_gate": None,
    "hs_kelvin": False,
    "ls_kelvin": False,
    "margin": 10.0,
    "config": None,
}

REQUIRED_ARGS = ("pcb", "sw", "gnd", "out")
LIST_TYPES = {
    "hs_ref": str,
    "ls_ref": str,
    "pitch": float,       # accepted for shared extractor YAML, ignored here
    "cin_refs": str,      # accepted for shared extractor YAML, ignored here
}
SCALAR_TYPES = {
    "pcb": str,
    "sw": str,
    "gnd": str,
    "vin": str,
    "hs_gate": str,
    "ls_gate": str,
    "margin": float,
    "out": str,
    "config": str,
    # Accepted so an extraction config can be reused directly.
    "cin_parallel": int,
    "cin_esl": float,
    "cin_esr": float,
    "lead_mm": float,
    "weld_tol": float,
    "nwinc": int,
    "nhinc": int,
    "cu_temp": float,
    "cu_thickness": float,
    "plateau": float,
}
BOOL_ARGS = {
    "hs_kelvin",
    "ls_kelvin",
    # Accepted so an extraction config can be reused directly.
    "include_bulk_cin",
    "emit_cin_network",
    "svg",
}


def mm(v):
    return v / NM


def esc(s):
    return html.escape(str(s), quote=True)


def leaf(net):
    s = str(net or "")
    return s.split("/")[-1] if "/" in s else s


def bbox_union(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def bbox_expand(b, margin):
    return (b[0] - margin, b[1] - margin, b[2] + margin, b[3] + margin)


def bbox_intersects(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def point_bbox(p, r=0.0):
    return (p[0] - r, p[1] - r, p[0] + r, p[1] + r)


def _isect(a, b, coord, axis):
    """Point where segment a->b crosses the line (axis==0: x=coord, else y=coord)."""
    other = 1 - axis
    da = b[axis] - a[axis]
    t = 0.0 if da == 0 else (coord - a[axis]) / da
    p = [0.0, 0.0]
    p[axis] = coord
    p[other] = a[other] + t * (b[other] - a[other])
    return (p[0], p[1])


def clip_poly_to_rect(pts, rect):
    """Sutherland-Hodgman clip of a polygon (list of (x,y)) to an axis-aligned rect.

    Keeps pour outlines from spilling across the whole board: a ground/switch
    plane is drawn only where it overlaps the loop ROI, not in full.
    """
    x0, y0, x1, y1 = rect
    edges = (  # (axis, coord, inside-test)
        (0, x0, lambda p: p[0] >= x0),
        (0, x1, lambda p: p[0] <= x1),
        (1, y0, lambda p: p[1] >= y0),
        (1, y1, lambda p: p[1] <= y1),
    )
    poly = pts
    for axis, coord, inside in edges:
        if not poly:
            break
        out = []
        for i in range(len(poly)):
            a = poly[i]
            b = poly[(i + 1) % len(poly)]
            ina, inb = inside(a), inside(b)
            if inb:
                if not ina:
                    out.append(_isect(a, b, coord, axis))
                out.append(b)
            elif ina:
                out.append(_isect(a, b, coord, axis))
        poly = out
    return poly


def fp_bbox(fp):
    bb = fp.GetBoundingBox()
    return (mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom()))


def part_box(fp):
    """Footprint extent from its pad hull, not GetBoundingBox() (which includes
    silk/courtyard and grossly over-states large FET packages). Falls back to the
    full box for footprints with no pads."""
    pts = [(mm(p.GetPosition().x), mm(p.GetPosition().y)) for p in fp.Pads()]
    if not pts:
        return fp_bbox(fp)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    r = max((mm(max(p.GetSize().x, p.GetSize().y)) for p in fp.Pads()), default=0.0) / 2
    return (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)


def item_bbox(item):
    bb = item.GetBoundingBox()
    return (mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom()))


def layer_class(layer):
    if layer == TOP:
        return "layer-top"
    if layer == BOTTOM:
        return "layer-bottom"
    return "layer-other"


def class_attr(*parts):
    return " ".join(p for p in parts if p)


def pt(v):
    return f"{mm(v.x):.4f},{mm(v.y):.4f}"


def rect_svg(x, y, w, h, cls, angle=0.0, rx=0.0, extra=""):
    cx = x + w / 2
    cy = y + h / 2
    transform = f" transform='rotate({angle:.3f} {cx:.4f} {cy:.4f})'" if abs(angle) > 0.01 else ""
    return (f"<rect class='{esc(cls)}' x='{x:.4f}' y='{y:.4f}' width='{w:.4f}' "
            f"height='{h:.4f}' rx='{rx:.4f}'{transform}{extra}/>")


def pad_svg(pad, cls):
    pos = pad.GetPosition()
    size = pad.GetSize()
    x = mm(pos.x)
    y = mm(pos.y)
    sx = max(mm(size.x), 0.05)
    sy = max(mm(size.y), 0.05)
    angle = 0.0
    try:
        angle = float(pad.GetOrientationDegrees())
    except Exception:
        pass
    parent = pad.GetParentFootprint()
    ref = parent.GetReference() if parent else "?"
    title = f"<title>{esc(ref)}.{esc(pad.GetName())} {esc(leaf(pad.GetNetname()))}</title>"
    shape = pad.GetShape()
    if shape == pcbnew.PAD_SHAPE_CIRCLE:
        return (f"<circle class='{esc(cls)}' cx='{x:.4f}' cy='{y:.4f}' "
                f"r='{max(sx, sy) / 2:.4f}'>{title}</circle>")
    if shape == pcbnew.PAD_SHAPE_OVAL:
        transform = f" transform='rotate({angle:.3f} {x:.4f} {y:.4f})'" if abs(angle) > 0.01 else ""
        return (f"<ellipse class='{esc(cls)}' cx='{x:.4f}' cy='{y:.4f}' "
                f"rx='{sx / 2:.4f}' ry='{sy / 2:.4f}'{transform}>{title}</ellipse>")
    rx = 0.0
    if shape == pcbnew.PAD_SHAPE_ROUNDRECT:
        try:
            rx = mm(pad.GetRoundRectCornerRadius())
        except Exception:
            rx = min(sx, sy) * 0.18
    x0 = x - sx / 2
    y0 = y - sy / 2
    cx = x
    cy = y
    transform = f" transform='rotate({angle:.3f} {cx:.4f} {cy:.4f})'" if abs(angle) > 0.01 else ""
    return (f"<rect class='{esc(cls)}' x='{x0:.4f}' y='{y0:.4f}' width='{sx:.4f}' "
            f"height='{sy:.4f}' rx='{rx:.4f}'{transform}>{title}</rect>")


def line_svg(a, b, width, cls, title=""):
    t = f"<title>{esc(title)}</title>" if title else ""
    return (f"<line class='{esc(cls)}' x1='{a[0]:.4f}' y1='{a[1]:.4f}' "
            f"x2='{b[0]:.4f}' y2='{b[1]:.4f}' stroke-width='{max(width, 0.08):.4f}'>{t}</line>")


def circle_svg(x, y, r, cls, title=""):
    t = f"<title>{esc(title)}</title>" if title else ""
    return f"<circle class='{esc(cls)}' cx='{x:.4f}' cy='{y:.4f}' r='{r:.4f}'>{t}</circle>"


def zone_path(poly, roi=None):
    if poly is None:
        return ""
    paths = []
    for oi in range(poly.OutlineCount()):
        outline = poly.Outline(oi)
        n = outline.PointCount()
        if n < 3:
            continue
        pts = [(mm(outline.CPoint(i).x), mm(outline.CPoint(i).y)) for i in range(n)]
        if roi:
            pts = clip_poly_to_rect(pts, roi)
            if len(pts) < 3:
                continue
        d = "M " + " L ".join(f"{x:.4f},{y:.4f}" for x, y in pts) + " Z"
        paths.append(d)
    return " ".join(paths)


def layer_ids(board):
    return list(board.GetEnabledLayers().CuStack())


def gate_driver_footprints(board, driver_net, exclude_refs):
    refs = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref in exclude_refs:
            continue
        if any(p.GetNetname() == driver_net for p in fp.Pads()):
            refs.append(ref)
    return refs


def side_gate_nets(topo, side):
    """Every gate net on the side -- one per FET. Paralleled FETs (e.g. HS Q1||Q3)
    frequently sit on separate gate nets (Net-(Q1-G), Net-(Q3-G)) each with its own
    series resistor, i.e. two distinct gate loops. Derived from each FET's pads as
    the net that is neither the shared source nor drain rail."""
    rails = {topo["sw"], topo["gnd"], topo.get("vin"), side["source"], side["drain"]}
    nets = []
    for pads in (side.get("pads") or {}).values():
        for net, _, _ in pads:
            if net and net not in rails and net not in nets:
                nets.append(net)
    if not nets and side.get("gate"):
        nets = [side["gate"]]
    return nets


def side_specs(board, topo):
    specs = {}
    rails = {topo["sw"], topo["gnd"], topo.get("vin")}
    for role in ("hs", "ls"):
        side = topo[role]
        gate_nets = side_gate_nets(topo, side)
        part_refs = set(side["refs"])
        drive_nets = set()
        # Resolve each gate loop's series R/D and driver-output net independently.
        for gn in gate_nets:
            gd = fet_discovery.gate_network(board, gn, side["refs"], exclude_nets=rails)
            if gd.get("driver_net"):
                drive_nets.add(gd["driver_net"])
            for key in ("r", "d"):
                if gd.get(key):
                    part_refs.add(gd[key]["ref"])
        for dn in drive_nets:
            part_refs.update(gate_driver_footprints(board, dn, part_refs))
        specs[role] = {
            "role": role,
            "title": role.upper(),
            "drive_nets": drive_nets,
            "gate_nets": set(gate_nets),
            "return_net": side["source"],
            "fet_refs": list(side["refs"]),
            "part_refs": sorted(part_refs),
            "gate_return": side["gate_return"],
            "kelvin": side["kelvin"],
        }
    return specs


def side_roi(board, spec, margin):
    # Bound the ROI by the pad locations of the loop's parts (driver, gate
    # resistor, FET), not the full footprint boxes -- silk/courtyard inflate
    # GetBoundingBox() and would zoom the view far off the actual loop.
    boxes = []
    want = set(spec["part_refs"])
    for fp in board.GetFootprints():
        if fp.GetReference() in want:
            boxes.append(part_box(fp))
    b = bbox_union(boxes)
    return bbox_expand(b, margin) if b else None


def net_kind(spec, net):
    if net in spec["drive_nets"]:
        return "drive"
    if net in spec["gate_nets"]:
        return "gate"
    if net == spec["return_net"]:
        return "return"
    return None


def collect_side_svg(board, topo, spec, roi):
    """Return the side's SVG grouped by paint category so callers can z-order it
    (pours at the back, then traces, vias, pads, CSI on top). Each entry is a
    (kind, svg) tuple so return-net copper can be painted under the gate/drive
    copper it would otherwise bury."""
    buckets = {"zone": [], "track": [], "via": [], "pad": [], "csi": []}
    side_cls = "side-" + spec["role"]

    for t in board.GetTracks():
        net = t.GetNetname()
        kind = net_kind(spec, net)
        if not kind:
            continue
        title = f"{spec['title']} {kind}: {leaf(net)}"
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            x, y = mm(p.x), mm(p.y)
            try:
                dia = t.GetFrontWidth()
            except Exception:
                dia = t.GetDrillValue() * 2
            r = max(mm(dia) / 2, 0.18)
            if roi and not bbox_intersects(point_bbox((x, y), r), roi):
                continue
            via_cls = class_attr("via", side_cls, "kind-" + kind, "layer-top", "layer-bottom")
            buckets["via"].append((kind, circle_svg(x, y, r, via_cls, title)))
            continue
        a = t.GetStart()
        b = t.GetEnd()
        x1, y1, x2, y2 = mm(a.x), mm(a.y), mm(b.x), mm(b.y)
        w = mm(t.GetWidth())
        if roi and not bbox_intersects((min(x1, x2) - w, min(y1, y2) - w,
                                        max(x1, x2) + w, max(y1, y2) + w), roi):
            continue
        cls = class_attr("copper", side_cls, "kind-" + kind, layer_class(t.GetLayer()))
        buckets["track"].append((kind, line_svg((x1, y1), (x2, y2), w, cls, title)))

    for i in range(board.GetAreaCount()):
        z = board.GetArea(i)
        kind = net_kind(spec, z.GetNetname())
        if not kind:
            continue
        for lid in z.GetLayerSet().Seq():
            if lid not in (TOP, BOTTOM):
                continue
            poly = z.GetFilledPolysList(lid)
            if poly is None or poly.OutlineCount() == 0:
                continue
            bb = poly.BBox()
            zbb = (mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom()))
            if roi and not bbox_intersects(zbb, roi):
                continue
            d = zone_path(poly, roi)
            if d:
                cls = class_attr("zone", side_cls, "kind-" + kind, layer_class(lid))
                buckets["zone"].append((kind, f"<path class='{esc(cls)}' d='{esc(d)}'><title>{esc(spec['title'])} {esc(kind)} pour {esc(leaf(z.GetNetname()))}</title></path>"))

    for fp in board.GetFootprints():
        if roi and not bbox_intersects(fp_bbox(fp), roi):
            continue
        for pad in fp.Pads():
            kind = net_kind(spec, pad.GetNetname())
            if not kind:
                continue
            pbb = item_bbox(pad)
            if roi and not bbox_intersects(pbb, roi):
                continue
            touched = [lid for lid in (TOP, BOTTOM) if pad.IsOnLayer(lid)]
            layer_cls = " ".join(layer_class(lid) for lid in touched) or "layer-other"
            cls = class_attr("pad", side_cls, "kind-" + kind, layer_cls)
            buckets["pad"].append((kind, pad_svg(pad, cls)))

    buckets["csi"].extend(csi_markers(board, topo, spec))
    return buckets


def order_paths(sides):
    """Flatten per-side buckets into a single z-ordered path list: pours, then
    traces, vias, pads, CSI -- and within each, return-net copper first so the
    gate/drive loop and the FET gate pad are never buried by the return plane."""
    out = []
    for cat in ("zone", "track", "via", "pad", "csi"):
        items = [kv for side in sides.values() for kv in side[cat]]
        items.sort(key=lambda kv: 0 if kv[0] == "return" else 1)  # stable: return first
        out.extend(svg for _, svg in items)
    return "\n".join(out)


def csi_markers(board, topo, spec):
    out = []
    side = topo[spec["role"]]
    source = side["source"]
    side_cls = "side-" + spec["role"]
    refs = set(side["refs"])
    for fp in board.GetFootprints():
        if fp.GetReference() not in refs:
            continue
        pads = [p for p in fp.Pads() if p.GetNetname() == source]
        if not pads:
            continue
        xs = [mm(p.GetPosition().x) for p in pads]
        ys = [mm(p.GetPosition().y) for p in pads]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        cls = class_attr("csi", side_cls, "kind-csi", "layer-top", "layer-bottom")
        out.append(("csi", circle_svg(cx, cy, 1.25, cls, f"{spec['title']} source-lead CSI")))
        out.append(("csi", f"<text class='{esc(cls)} csi-label' x='{cx + 1.7:.4f}' y='{cy - 1.0:.4f}'>{esc(spec['title'])} CSI</text>"))
    return out


def collect_parts_svg(board, specs, rois):
    out = []
    for role, spec in specs.items():
        roi = rois[role]
        side_cls = "side-" + role
        for fp in board.GetFootprints():
            ref = fp.GetReference()
            if ref not in spec["part_refs"]:
                continue
            b = part_box(fp)
            if roi and not bbox_intersects(b, roi):
                continue
            cls = class_attr("part", side_cls, "kind-parts")
            out.append(rect_svg(b[0], b[1], b[2] - b[0], b[3] - b[1], cls, 0.0, 0.15))
            x = (b[0] + b[2]) / 2
            y = b[1] - 0.45
            out.append(f"<text class='{esc(cls)} part-label' x='{x:.4f}' y='{y:.4f}'>{esc(ref)}</text>")
    return "\n".join(out)


def collect_edge_svg(board):
    out = []
    boxes = []
    for d in board.GetDrawings():
        try:
            if d.GetLayerName() != "Edge.Cuts":
                continue
        except Exception:
            continue
        boxes.append(item_bbox(d))
        if hasattr(d, "GetStart") and hasattr(d, "GetEnd"):
            a = d.GetStart()
            b = d.GetEnd()
            out.append(line_svg((mm(a.x), mm(a.y)), (mm(b.x), mm(b.y)),
                                max(mm(d.GetWidth()), 0.12), "board-edge kind-board", "Edge.Cuts"))
    return "\n".join(out), bbox_union(boxes)


def topology_summary(topo, specs):
    rows = []
    for role in ("hs", "ls"):
        spec = specs[role]
        rows.append({
            "side": role.upper(),
            "fet": ", ".join(topo[role]["refs"]),
            "drive": ", ".join(sorted(leaf(n) for n in spec["drive_nets"])) or "-",
            "gate": ", ".join(sorted(leaf(n) for n in spec["gate_nets"])) or "-",
            "loops": len(spec["gate_nets"]),
            "return": leaf(spec.get("return_net")),
            "returnMode": "Kelvin" if spec.get("kelvin") else "source",
        })
    return rows


def html_doc(board, topo, specs, rois, viewbox, edge_svg, path_svg, part_svg):
    summary = topology_summary(topo, specs)
    data = json.dumps({"summary": summary}, sort_keys=True)
    vb = " ".join(f"{v:.3f}" for v in viewbox)
    title = f"Gate-drive parasitic paths - {os.path.basename(board.GetFileName())}"
    css = """
:root {
  color-scheme: light;
  --ink: #202124;
  --muted: #6b6f76;
  --line: #c7ccd2;
  --panel: #f5f6f7;
  --top: #c65f00;
  --bottom: #047b83;
  --drive: #2563eb;
  --gate: #c58a00;
  --return: #bf2f61;
  --csi: #111827;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  height: 100vh;
  display: grid;
  grid-template-columns: 300px 1fr;
  font: 13px/1.35 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
  background: #ffffff;
}
aside {
  background: var(--panel);
  border-right: 1px solid var(--line);
  padding: 14px;
  overflow: auto;
}
main { min-width: 0; display: grid; grid-template-rows: 42px 1fr; }
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  border-bottom: 1px solid var(--line);
  padding: 6px 10px;
  background: #ffffff;
}
h1 { font-size: 15px; margin: 0 0 12px; font-weight: 650; }
h2 { font-size: 12px; margin: 18px 0 8px; color: var(--muted); text-transform: uppercase; }
label { display: flex; align-items: center; gap: 8px; min-height: 28px; }
input[type="checkbox"] { width: 16px; height: 16px; accent-color: #2563eb; }
button {
  min-width: 34px;
  height: 28px;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--ink);
  border-radius: 4px;
}
button.active { border-color: #2563eb; box-shadow: inset 0 0 0 1px #2563eb; }
.spacer { flex: 1; }
.meta { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
td { padding: 4px 0; border-bottom: 1px solid #e4e7eb; vertical-align: top; }
td:first-child { color: var(--muted); width: 58px; }
.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 2px; border: 1px solid rgba(0,0,0,.18); }
.viewer { min-height: 0; overflow: hidden; background: #fafafa; cursor: grab; }
.viewer.dragging { cursor: grabbing; }
svg { width: 100%; height: 100%; display: block; background: #fbfbfb; }
.board-edge { stroke: #30343a; fill: none; stroke-linecap: round; vector-effect: non-scaling-stroke; }
.copper { fill: none; stroke-linecap: round; stroke-linejoin: round; opacity: .92; }
.via { stroke: none; opacity: .95; }
.zone { stroke: none; opacity: .10; }
.pad { opacity: .82; stroke: rgba(0,0,0,.18); stroke-width: .08; vector-effect: non-scaling-stroke; }
.part { fill: rgba(255,255,255,.18); stroke: #3f454d; stroke-width: .16; vector-effect: non-scaling-stroke; }
.part-label, .csi-label { font-size: 1.6px; paint-order: stroke; stroke: white; stroke-width: .55px; fill: #1f242b; pointer-events: none; }
.csi { fill: none; stroke: var(--csi); stroke-width: .42; stroke-dasharray: .9 .55; opacity: .98; vector-effect: non-scaling-stroke; }
.kind-drive { stroke: var(--drive); fill: var(--drive); }
.kind-gate { stroke: var(--gate); fill: var(--gate); }
.kind-return { stroke: var(--return); fill: var(--return); }
.layer-bottom { stroke-dasharray: 1.2 .55; }
.zone.layer-bottom { opacity: .11; }
.hidden { display: none !important; }
"""
    js = """
const svg = document.querySelector("svg");
const viewport = document.querySelector("#viewport");
const viewer = document.querySelector(".viewer");
const initial = svg.viewBox.baseVal;
let vb = {x: initial.x, y: initial.y, w: initial.width, h: initial.height};
let drag = null;
function applyViewBox() { svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); }
function zoom(factor, cx = vb.x + vb.w / 2, cy = vb.y + vb.h / 2) {
  const nw = vb.w * factor, nh = vb.h * factor;
  vb.x = cx - (cx - vb.x) * factor;
  vb.y = cy - (cy - vb.y) * factor;
  vb.w = nw; vb.h = nh; applyViewBox();
}
function point(evt) {
  const p = svg.createSVGPoint();
  p.x = evt.clientX; p.y = evt.clientY;
  return p.matrixTransform(svg.getScreenCTM().inverse());
}
svg.addEventListener("wheel", evt => {
  evt.preventDefault();
  const p = point(evt);
  zoom(evt.deltaY < 0 ? 0.88 : 1.14, p.x, p.y);
}, {passive: false});
svg.addEventListener("pointerdown", evt => {
  drag = {x: evt.clientX, y: evt.clientY, vb: {...vb}};
  viewer.classList.add("dragging");
  svg.setPointerCapture(evt.pointerId);
});
svg.addEventListener("pointermove", evt => {
  if (!drag) return;
  const sx = vb.w / svg.clientWidth;
  const sy = vb.h / svg.clientHeight;
  vb.x = drag.vb.x - (evt.clientX - drag.x) * sx;
  vb.y = drag.vb.y - (evt.clientY - drag.y) * sy;
  applyViewBox();
});
svg.addEventListener("pointerup", evt => {
  drag = null;
  viewer.classList.remove("dragging");
  svg.releasePointerCapture(evt.pointerId);
});
document.querySelector("[data-zoom='in']").onclick = () => zoom(0.82);
document.querySelector("[data-zoom='out']").onclick = () => zoom(1.22);
document.querySelector("[data-zoom='reset']").onclick = () => {
  vb = {x: initial.x, y: initial.y, w: initial.width, h: initial.height};
  applyViewBox();
};
// Each element carries a class from several independent categories (side, kind,
// layer). It should be visible only when every category it participates in has
// at least one selected class -- i.e. AND across categories, OR within a
// category. A naive per-class toggle re-shows elements via their still-checked
// side/layer classes, so filtering must be computed jointly.
const GROUPS = [
  ["side-hs", "side-ls"],
  ["kind-drive", "kind-gate", "kind-return", "kind-csi", "kind-parts"],
  ["layer-top", "layer-bottom"],
];
function sync() {
  const on = {};
  document.querySelectorAll("[data-class]").forEach(i => { on[i.dataset.class] = i.checked; });
  document.querySelectorAll("#viewport [class]").forEach(el => {
    let show = true;
    for (const g of GROUPS) {
      const present = g.filter(c => el.classList.contains(c));
      if (present.length && !present.some(c => on[c])) { show = false; break; }
    }
    el.classList.toggle("hidden", !show);
  });
}
document.querySelectorAll("[data-class]").forEach(input => input.addEventListener("change", sync));
sync();
"""
    controls = """
<h2>Sides</h2>
<label><input type="checkbox" data-class="side-hs" checked>HS gate loop</label>
<label><input type="checkbox" data-class="side-ls" checked>LS gate loop</label>
<h2>Paths</h2>
<label><input type="checkbox" data-class="kind-drive" checked><span class="swatch" style="background:#2563eb"></span>Driver output</label>
<label><input type="checkbox" data-class="kind-gate" checked><span class="swatch" style="background:#c58a00"></span>FET gate</label>
<label><input type="checkbox" data-class="kind-return" checked><span class="swatch" style="background:#bf2f61"></span>Source return</label>
<label><input type="checkbox" data-class="kind-csi" checked>Source-lead CSI</label>
<label><input type="checkbox" data-class="kind-parts" checked>Parts</label>
<h2>Layers</h2>
<label><input type="checkbox" data-class="layer-top" checked><span class="swatch" style="background:#c65f00"></span>Top copper</label>
<label><input type="checkbox" data-class="layer-bottom" checked><span class="swatch" style="background:#047b83"></span>Bottom copper</label>
"""
    rows = []
    for r in summary:
        rows.extend([
            f"<tr><td>{esc(r['side'])}</td><td>{esc(r['fet'])} "
            f"({r['loops']} gate loop{'s' if r['loops'] != 1 else ''})</td></tr>",
            f"<tr><td>drive</td><td>{esc(r['drive'])}</td></tr>",
            f"<tr><td>gate</td><td>{esc(r['gate'])}</td></tr>",
            f"<tr><td>return</td><td>{esc(r['return'])} ({esc(r['returnMode'])})</td></tr>",
        ])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{css}</style>
</head>
<body>
<aside>
  <h1>{esc(title)}</h1>
  {controls}
  <h2>Topology</h2>
  <table>{"".join(rows)}</table>
</aside>
<main>
  <div class="toolbar">
    <button data-zoom="in" title="Zoom in">+</button>
    <button data-zoom="out" title="Zoom out">-</button>
    <button data-zoom="reset" title="Reset view">1:1</button>
    <div class="spacer"></div>
    <div class="meta">{esc(board.GetFileName())}</div>
  </div>
  <div class="viewer">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}">
      <g id="viewport">
        <g id="board">{edge_svg}</g>
        <g id="paths">{path_svg}</g>
        <g id="parts">{part_svg}</g>
      </g>
    </svg>
  </div>
</main>
<script type="application/json" id="path-data">{esc(data)}</script>
<script>{js}</script>
</body>
</html>
"""


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pcb", nargs="?", default=argparse.SUPPRESS)
    ap.add_argument("--config", default=argparse.SUPPRESS,
                    help="YAML file containing CLI args as argparse dest names")
    ap.add_argument("--sw", default=argparse.SUPPRESS, help="switch-node net name")
    ap.add_argument("--gnd", default=argparse.SUPPRESS, help="ground net name")
    ap.add_argument("--vin", default=argparse.SUPPRESS, help="input rail net")
    ap.add_argument("--hs-ref", nargs="*", default=argparse.SUPPRESS)
    ap.add_argument("--ls-ref", nargs="*", default=argparse.SUPPRESS)
    ap.add_argument("--hs-gate", default=argparse.SUPPRESS)
    ap.add_argument("--ls-gate", default=argparse.SUPPRESS)
    ap.add_argument("--hs-kelvin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS)
    ap.add_argument("--ls-kelvin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS)
    ap.add_argument("--margin", type=float, default=argparse.SUPPRESS,
                    help="path ROI margin in mm")
    ap.add_argument("-o", "--out", default=argparse.SUPPRESS,
                    help="output .html path, or a directory")
    return ap


def _load_config(path):
    try:
        import yaml
    except ImportError:
        raise SystemExit("visualize_paths: PyYAML required for --config; "
                         "install with `pip install pyyaml`")
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as e:
        raise SystemExit(f"{path}: failed to load YAML config: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a YAML mapping at top level")
    return data


def _coerce_scalar(name, value, typ):
    if value is None:
        return None
    if typ is str:
        if not isinstance(value, str):
            raise TypeError("expected string")
        return value
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("expected integer")
        return value
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("expected number")
        return float(value)
    raise AssertionError(f"unhandled scalar type for {name}")


def _validate_config(config, path):
    allowed = set(REQUIRED_ARGS) | set(DEFAULTS) | set(LIST_TYPES) | set(SCALAR_TYPES) | BOOL_ARGS
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise SystemExit(f"{path}: unknown config key(s): {', '.join(unknown)}")

    out = {}
    for key, value in config.items():
        try:
            if key in BOOL_ARGS:
                if not isinstance(value, bool):
                    raise TypeError("expected boolean")
                out[key] = value
            elif key in LIST_TYPES:
                if value is None:
                    out[key] = None
                elif isinstance(value, list):
                    out[key] = [_coerce_scalar(key, item, LIST_TYPES[key]) for item in value]
                else:
                    raise TypeError("expected list")
            elif key in SCALAR_TYPES:
                out[key] = _coerce_scalar(key, value, SCALAR_TYPES[key])
            else:
                raise AssertionError(f"unhandled config key {key}")
        except TypeError as e:
            raise SystemExit(f"{path}: {key}: {e}")
    return out


def _normalize_out(path):
    if path.endswith(os.sep) or os.path.isdir(path) or os.path.splitext(path)[1].lower() != ".html":
        return os.path.join(path, "gate-loop-viewer.html")
    return path


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    pre_args, _ = pre.parse_known_args(argv)

    yaml_args = {}
    if pre_args.config:
        yaml_args = _validate_config(_load_config(pre_args.config), pre_args.config)

    ap = build_parser()
    cli_args = vars(ap.parse_args(argv))
    merged = {}
    merged.update(DEFAULTS)
    merged.update(yaml_args)
    merged.update(cli_args)

    missing = [name for name in REQUIRED_ARGS if not merged.get(name)]
    if missing:
        ap.error("missing required argument(s): " + ", ".join(missing))

    # Keep only the values this viewer consumes. Extra extraction-only YAML keys
    # are accepted above so users can point both tools at the same config.
    keep = set(DEFAULTS) | set(REQUIRED_ARGS)
    args = argparse.Namespace(**{k: v for k, v in merged.items() if k in keep})
    args.out = _normalize_out(args.out)
    return args


def main(argv=None):
    args = parse_args(argv)
    workdir = tempfile.mkdtemp(prefix="dcdc_view_")
    args.pcb = pcb_source.resolve_pcb_path(args.pcb, workdir)
    board = pcbnew.LoadBoard(args.pcb)
    if board is None:
        raise SystemExit(f"{args.pcb}: KiCad failed to load PCB")
    topo = fet_discovery.discover(board, args.sw, args.gnd, vin=args.vin,
                                  hs_ref=args.hs_ref, ls_ref=args.ls_ref,
                                  hs_gate=args.hs_gate, ls_gate=args.ls_gate,
                                  hs_kelvin=args.hs_kelvin, ls_kelvin=args.ls_kelvin)
    specs = side_specs(board, topo)
    rois = {role: side_roi(board, spec, args.margin) for role, spec in specs.items()}
    edge_svg, edge_box = collect_edge_svg(board)
    sides = {role: collect_side_svg(board, topo, specs[role], rois[role])
             for role in ("hs", "ls")}
    path_svg = order_paths(sides)
    part_svg = collect_parts_svg(board, specs, rois)
    focus = bbox_union([rois[r] for r in rois])
    if focus is None:
        focus = edge_box or (0, 0, 100, 100)
    focus = bbox_expand(focus, 2.0)
    viewbox = (focus[0], focus[1], max(focus[2] - focus[0], 1.0), max(focus[3] - focus[1], 1.0))
    doc = html_doc(board, topo, specs, rois, viewbox, edge_svg, path_svg, part_svg)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
