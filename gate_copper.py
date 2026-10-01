#!/usr/bin/env python3
"""Emit one SVG per gate-drive loop showing all loop copper, colored by layer.

For each side (HS, LS) the gate-loop nets — driver output, FET gate, and source
return — are collected (every track, via, copper-pour fill, and pad on those
nets within an ROI around the FETs and gate-drive parts) and rendered as a
standalone SVG with a distinct color per copper layer.  One file per path.

Use it to verify the extraction tool meshes the correct geometry and to spot
routing flaws (missing vias, thin necks, detours) at a glance.

Runs under KiCad's bundled Python (needs pcbnew); re-execs under $KICAD_PY if
launched with a normal Python that cannot import pcbnew.

Usage:
    python3 gate_copper.py PCB --sw SW --gnd GND [opts] -o OUTDIR
    python3 gate_copper.py --config fugu2.yaml
"""
import argparse
import html
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "lib")
sys.path.insert(0, LIB)

KICAD_PY = os.environ.get(
    "KICAD_PY",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")

try:
    import pcbnew  # type: ignore
except ImportError:
    if os.path.exists(KICAD_PY) and os.path.abspath(sys.executable) != os.path.abspath(KICAD_PY):
        os.execv(KICAD_PY, [KICAD_PY, __file__] + sys.argv[1:])
    raise

import fet_discovery  # noqa: E402

NM = 1e6

# Colors by layer role (top / inner / bottom).  Assigned by layer ID, not
# position, so a 2-layer board's B.Cu gets teal (not the In1 blue).
TOP_COLOR = "#c65f00"       # F.Cu  — orange/brown
BOTTOM_COLOR = "#047b83"    # B.Cu  — teal
INNER_COLORS = [            # In*.Cu — blue, purple, pink, green …
    "#1d4ed8", "#7c3aed", "#be185d", "#15803d",
]

# Net dash patterns (none — solid lines only, like gerber files)

# -- config (accepts the same YAML as extract_parasitics.py / visualize_paths.py) --
DEFAULTS = {
    "vin": None, "hs_ref": None, "ls_ref": None,
    "hs_gate": None, "ls_gate": None,
    "hs_kelvin": False, "ls_kelvin": False,
    "margin": 10.0, "config": None, "separate": False, "split_fets": False,
}
REQUIRED_ARGS = ("pcb", "sw", "gnd", "out")
LIST_TYPES = {"hs_ref": str, "ls_ref": str, "pitch": float, "cin_refs": str}
SCALAR_TYPES = {
    "pcb": str, "sw": str, "gnd": str, "vin": str,
    "hs_gate": str, "ls_gate": str, "margin": float, "out": str, "config": str,
    "cin_parallel": int, "cin_esl": float, "cin_esr": float,
    "lead_mm": float, "weld_tol": float, "nwinc": int, "nhinc": int,
    "cu_temp": float, "plateau": float,
}
BOOL_ARGS = {"hs_kelvin", "ls_kelvin", "include_bulk_cin", "emit_cin_network", "svg", "separate", "split_fets"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def mm(v):
    return v / NM


def esc(s):
    return html.escape(str(s), quote=True)


def leaf(net):
    s = str(net or "")
    return s.split("/")[-1] if "/" in s else s


def pt(v):
    return f"{mm(v.x):.4f},{mm(v.y):.4f}"


def _bb(item):
    b = item.GetBoundingBox()
    return (mm(b.GetLeft()), mm(b.GetTop()), mm(b.GetRight()), mm(b.GetBottom()))


def _bb_union(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _bb_expand(b, m):
    return (b[0] - m, b[1] - m, b[2] + m, b[3] + m)


def _bb_hit(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _pt_bb(p, r=0.0):
    return (p[0] - r, p[1] - r, p[0] + r, p[1] + r)


def layer_table(board):
    """[(layer_id, index, color, name)] top-to-bottom for every enabled Cu layer.

    Color is by layer role (F.Cu=orange, B.Cu=teal, inner=blue/purple/…), not by
    position — so a 2-layer board's bottom gets teal, not the In1 blue.
    """
    cu = list(board.GetEnabledLayers().CuStack())
    inner_i = 0
    out = []
    for i, lid in enumerate(cu):
        if lid == pcbnew.F_Cu:
            color = TOP_COLOR
        elif lid == pcbnew.B_Cu:
            color = BOTTOM_COLOR
        else:
            color = INNER_COLORS[inner_i % len(INNER_COLORS)]
            inner_i += 1
        try:
            name = board.GetLayerName(lid)
        except Exception:
            name = f"L{i}"
        out.append((lid, i, color, name))
    return out


# --------------------------------------------------------------------------- #
# SVG primitives
# --------------------------------------------------------------------------- #
def zone_path_data(poly):
    if poly is None:
        return ""
    paths = []
    for oi in range(poly.OutlineCount()):
        ol = poly.Outline(oi)
        n = ol.PointCount()
        if n < 3:
            continue
        pts = [ol.CPoint(i) for i in range(n)]
        paths.append("M " + " L ".join(pt(p) for p in pts) + " Z")
    return " ".join(paths)


def pad_svg(pad, fill):
    """Outlined pad (separate mode)."""
    return _pad_svg(pad, fill, stroke="rgba(0,0,0,.22)", sw="0.08", op="0.72")


def _pad_merged_svg(pad, fill):
    """Solid fill, no outline (merged/gerber mode)."""
    return _pad_svg(pad, fill, stroke="none", sw="0", op="0.72")


def _pad_svg(pad, fill, stroke, sw, op):
    pos = pad.GetPosition()
    size = pad.GetSize()
    x, y = mm(pos.x), mm(pos.y)
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
    st_attr = f" stroke='{stroke}' stroke-width='{sw}'" if stroke != "none" else ""
    if shape == pcbnew.PAD_SHAPE_CIRCLE:
        return (f"<circle cx='{x:.4f}' cy='{y:.4f}' r='{max(sx, sy) / 2:.4f}' "
                f"fill='{fill}' fill-opacity='{op}'{st_attr}>{title}</circle>")
    if shape == pcbnew.PAD_SHAPE_OVAL:
        tr = f" transform='rotate({angle:.3f} {x:.4f} {y:.4f})'" if abs(angle) > 0.01 else ""
        return (f"<ellipse cx='{x:.4f}' cy='{y:.4f}' rx='{sx / 2:.4f}' ry='{sy / 2:.4f}' "
                f"fill='{fill}' fill-opacity='{op}'{st_attr}{tr}>{title}</ellipse>")
    rx = 0.0
    if shape == pcbnew.PAD_SHAPE_ROUNDRECT:
        try:
            rx = mm(pad.GetRoundRectCornerRadius())
        except Exception:
            rx = min(sx, sy) * 0.18
    x0, y0 = x - sx / 2, y - sy / 2
    tr = f" transform='rotate({angle:.3f} {x:.4f} {y:.4f})'" if abs(angle) > 0.01 else ""
    return (f"<rect x='{x0:.4f}' y='{y0:.4f}' width='{sx:.4f}' height='{sy:.4f}' "
            f"rx='{rx:.4f}' fill='{fill}' fill-opacity='{op}'{st_attr}{tr}>{title}</rect>")


# --------------------------------------------------------------------------- #
# topology — per-FET gate-loop discovery
# --------------------------------------------------------------------------- #
def _gate_driver_refs(board, driver_net, exclude):
    refs = []
    for fp in board.GetFootprints():
        if fp.GetReference() in exclude:
            continue
        if any(p.GetNetname() == driver_net for p in fp.Pads()):
            refs.append(fp.GetReference())
    return refs


def fet_gate_net(topo, role, ref, padcount, override):
    """Gate net for one specific FET, from its pad nets.

    Paralleled FETs may sit on separate gate nets (Net-(Q1-G) vs Net-(Q3-G)),
    so we discover per-ref instead of using the side-level gate.  Override
    (--hs-gate/--ls-gate) applies to all FETs on that side, matching the
    extractor."""
    if override:
        return override
    side = topo[role]
    pads = side["pads"].get(ref, [])
    seen, nets = set(), []
    for net, _, _ in pads:
        if net not in seen:
            seen.add(net)
            nets.append(net)
    cand = [n for n in nets if n != side["drain"] and n != side["source"]]
    if len(cand) == 1:
        return cand[0]
    if not cand:
        return None
    return min(cand, key=lambda n: padcount.get(n, 0))


def fet_loop_nets(board, topo, role, ref, padcount, gate_override):
    """{kind: net_name} for one FET's gate loop (drive/gate/return)."""
    side = topo[role]
    gate = fet_gate_net(topo, role, ref, padcount, gate_override)
    if not gate:
        return None
    gd = fet_discovery.gate_network(
        board, gate, [ref],
        exclude_nets={topo["sw"], topo["gnd"], topo["vin"]})
    nets = {}
    drive = gd.get("driver_net")
    if drive and drive != gate:
        nets["drive"] = drive
    nets["gate"] = gate
    nets["return"] = side["source"]
    return nets


def fet_part_refs(board, topo, role, ref, nets):
    """All footprint refdes relevant to one FET's gate loop."""
    refs = {ref}
    gate = nets.get("gate")
    if gate:
        gd = fet_discovery.gate_network(
            board, gate, [ref],
            exclude_nets={topo["sw"], topo["gnd"], topo["vin"]})
        for key in ("r", "d"):
            if gd.get(key):
                refs.add(gd[key]["ref"])
    drive = nets.get("drive")
    if drive:
        refs.update(_gate_driver_refs(board, drive, refs))
    return sorted(refs)


def side_nets(topo, role):
    """{kind: net_name} for a side's gate loop (drive/gate/return).

    Uses the side-level gate net from topology discovery (the first FET's gate).
    For paralleled FETs on separate gate nets, use --split-fets instead."""
    side = topo[role]
    gd = side.get("gate_drive") or {}
    nets = {}
    drive = gd.get("driver_net")
    gate = side["gate"]
    if drive and drive != gate:
        nets["drive"] = drive
    nets["gate"] = gate
    nets["return"] = side["source"]
    return nets


def side_part_refs(board, topo, role, nets):
    """All footprint refdes relevant to a side's gate loop (all paralleled FETs)."""
    refs = set(topo[role]["refs"])
    gd = topo[role].get("gate_drive") or {}
    for key in ("r", "d"):
        if gd.get(key):
            refs.add(gd[key]["ref"])
    drive = nets.get("drive")
    if drive:
        refs.update(_gate_driver_refs(board, drive, refs))
    return sorted(refs)


def side_roi(board, part_refs, margin):
    want = set(part_refs)
    boxes = [_bb(fp) for fp in board.GetFootprints() if fp.GetReference() in want]
    b = _bb_union(boxes)
    return _bb_expand(b, margin) if b else None


# --------------------------------------------------------------------------- #
# copper collection
# --------------------------------------------------------------------------- #
def collect_copper(board, nets, roi, ltable, merged=True):
    """Return ({layer_idx: [svg]}, [via_svg]) for all copper on the gate-loop nets.

    merged=True  — every element is solid fill/stroke in the layer color, no
                   outlines or element-type distinction (gerber-like).
    merged=False — zones faint, pads outlined, vias dark (shows element types).
    """
    net_kind = {net: kind for kind, net in nets.items() if net}
    lid_idx = {lid: idx for lid, idx, _, _ in ltable}
    layers = {}
    vias = []

    zone_op = "0.55" if merged else "0.15"
    pad_st = "none" if merged else "rgba(0,0,0,.22)"
    pad_sw = "0" if merged else "0.08"
    pad_op = "0.72"
    via_fill = None   # set per-via below
    via_op = "0.72" if merged else "0.6"

    # zones (pours)
    for i in range(board.GetAreaCount()):
        z = board.GetArea(i)
        net = z.GetNetname()
        if net not in net_kind:
            continue
        kind = net_kind[net]
        for lid in z.GetLayerSet().Seq():
            idx = lid_idx.get(lid)
            if idx is None:
                continue
            _, _, color, lname = ltable[idx]
            poly = z.GetFilledPolysList(lid)
            if poly is None or poly.OutlineCount() == 0:
                continue
            bb = poly.BBox()
            if roi and not _bb_hit((mm(bb.GetLeft()), mm(bb.GetTop()),
                                    mm(bb.GetRight()), mm(bb.GetBottom())), roi):
                continue
            d = zone_path_data(poly)
            if not d:
                continue
            title = f"{kind}: {leaf(net)} ({lname})"
            layers.setdefault(idx, []).append(
                f"<path d='{esc(d)}' fill='{color}' fill-opacity='{zone_op}' "
                f"fill-rule='evenodd' stroke='none'><title>{esc(title)}</title></path>")

    # tracks + vias
    for t in board.GetTracks():
        net = t.GetNetname()
        if net not in net_kind:
            continue
        kind = net_kind[net]
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            x, y = mm(p.x), mm(p.y)
            try:
                dia = t.GetWidth(t.TopLayer())
            except Exception:
                dia = t.GetDrillValue() * 2
            r = max(mm(dia) / 2, 0.18)
            if roi and not _bb_hit(_pt_bb((x, y), r), roi):
                continue
            vfill = "#3f454d"
            if merged:
                top_lid = t.TopLayer()
                idx = lid_idx.get(top_lid)
                if idx is not None:
                    vfill = ltable[idx][2]
            vias.append(
                f"<circle cx='{x:.4f}' cy='{y:.4f}' r='{r:.4f}' fill='{vfill}' "
                f"fill-opacity='{via_op}' stroke='none'><title>{esc(kind)}: "
                f"{esc(leaf(net))} (via)</title></circle>")
            continue
        lid = t.GetLayer()
        idx = lid_idx.get(lid)
        if idx is None:
            continue
        _, _, color, lname = ltable[idx]
        a, b = t.GetStart(), t.GetEnd()
        x1, y1, x2, y2 = mm(a.x), mm(a.y), mm(b.x), mm(b.y)
        w = mm(t.GetWidth())
        if roi and not _bb_hit((min(x1, x2) - w, min(y1, y2) - w,
                                max(x1, x2) + w, max(y1, y2) + w), roi):
            continue
        title = f"{kind}: {leaf(net)} ({lname})"
        layers.setdefault(idx, []).append(
            f"<line x1='{x1:.4f}' y1='{y1:.4f}' x2='{x2:.4f}' y2='{y2:.4f}' "
            f"stroke='{color}' stroke-width='{max(w, 0.08):.4f}' stroke-linecap='round'>"
            f"<title>{esc(title)}</title></line>")

    # pads
    for fp in board.GetFootprints():
        if roi and not _bb_hit(_bb(fp), roi):
            continue
        for pad in fp.Pads():
            net = pad.GetNetname()
            if net not in net_kind:
                continue
            if roi and not _bb_hit(_bb(pad), roi):
                continue
            touched = [lid_idx[lid] for lid in lid_idx if pad.IsOnLayer(lid)]
            if not touched:
                continue
            best = min(touched)
            _, _, color, _ = ltable[best]
            if merged:
                layers.setdefault(best, []).append(
                    _pad_merged_svg(pad, color))
            else:
                layers.setdefault(best, []).append(pad_svg(pad, color))

    return layers, vias


def collect_footprint_svg(board, part_refs, roi):
    """Thin outlines + refdes labels for the gate-loop parts."""
    out = []
    want = set(part_refs)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in want:
            continue
        b = _bb(fp)
        if roi and not _bb_hit(b, roi):
            continue
        x, y, w, h = b[0], b[1], b[2] - b[0], b[3] - b[1]
        cx = (b[0] + b[2]) / 2
        out.append(
            f"<rect x='{x:.4f}' y='{y:.4f}' width='{w:.4f}' height='{h:.4f}' "
            f"fill='none' stroke='#3f454d' stroke-width='0.15' "
            f"stroke-opacity='0.45' rx='0.2'/>")
        if ref.upper().startswith("TP"):
            continue
        out.append(
            f"<text x='{cx:.4f}' y='{y - 0.5:.4f}' font-size='1.6' "
            f"text-anchor='middle' fill='#1f242b' paint-order='stroke' "
            f"stroke='white' stroke-width='0.5'>{esc(ref)}</text>")
    return "\n".join(out)


def collect_edge_svg(board):
    """Faint board outline for orientation."""
    out, boxes = [], []
    for d in board.GetDrawings():
        try:
            if d.GetLayerName() != "Edge.Cuts":
                continue
        except Exception:
            continue
        boxes.append(_bb(d))
        if hasattr(d, "GetStart") and hasattr(d, "GetEnd"):
            a, b = d.GetStart(), d.GetEnd()
            w = max(mm(d.GetWidth()), 0.12)
            out.append(
                f"<line x1='{mm(a.x):.4f}' y1='{mm(a.y):.4f}' "
                f"x2='{mm(b.x):.4f}' y2='{mm(b.y):.4f}' "
                f"stroke='#30343a' stroke-width='{w:.4f}' stroke-opacity='0.2' "
                f"stroke-linecap='round'/>")
    return "\n".join(out), _bb_union(boxes)


# --------------------------------------------------------------------------- #
# SVG assembly
# --------------------------------------------------------------------------- #
def render_svg(board, role, ref, nets, layers_svg, vias_svg, fp_svg, edge_svg, vb, ltable):
    label = "HS" if role == "hs" else "LS"
    if ref:
        title = f"{label} {ref} gate-drive loop - {os.path.basename(board.GetFileName())}"
    else:
        title = f"{label} gate-drive loop - {os.path.basename(board.GetFileName())}"
    vbs = " ".join(f"{v:.3f}" for v in vb)
    groups = []
    for idx in reversed(range(len(ltable))):  # bottom-to-top so top is on top
        svgs = layers_svg.get(idx, [])
        if not svgs:
            continue
        groups.append(f"<g id='L{idx}'>\n" + "\n".join(svgs) + "\n</g>\n")
    via_join = "\n".join(vias_svg)
    return (
        f"<?xml version='1.0' encoding='UTF-8'?>\n"
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='{vbs}' "
        f"width='100%' height='100%'>\n"
        f"<title>{esc(title)}</title>\n"
        f"<rect x='{vb[0]:.3f}' y='{vb[1]:.3f}' width='{vb[2]:.3f}' height='{vb[3]:.3f}' "
        f"fill='#fafafa'/>\n"
        f"<g id='edge'>\n{edge_svg}\n</g>\n"
        f"{''.join(groups)}"
        f"<g id='vias'>\n{via_join}\n</g>\n"
        f"<g id='parts'>\n{fp_svg}\n</g>\n"
        f"</svg>\n"
    )


# --------------------------------------------------------------------------- #
# config + CLI
# --------------------------------------------------------------------------- #
def _load_config(path):
    try:
        import yaml
    except ImportError:
        raise SystemExit("gate_copper: PyYAML required for --config; "
                         "install with `pip install pyyaml`")
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as e:
        raise SystemExit(f"{path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a YAML mapping")
    return data


def _coerce(name, value, typ):
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
    raise AssertionError(name)


def _validate_config(config, path):
    allowed = (set(REQUIRED_ARGS) | set(DEFAULTS) | set(LIST_TYPES)
               | set(SCALAR_TYPES) | BOOL_ARGS)
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise SystemExit(f"{path}: unknown key(s): {', '.join(unknown)}")
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
                    out[key] = [_coerce(key, v, LIST_TYPES[key]) for v in value]
                else:
                    raise TypeError("expected list")
            elif key in SCALAR_TYPES:
                out[key] = _coerce(key, value, SCALAR_TYPES[key])
        except TypeError as e:
            raise SystemExit(f"{path}: {key}: {e}")
    return out


def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcb", nargs="?", default=argparse.SUPPRESS)
    ap.add_argument("--config", default=argparse.SUPPRESS,
                    help="YAML file containing CLI args (same keys as extract_parasitics.py)")
    ap.add_argument("--sw", default=argparse.SUPPRESS, help="switch-node net name")
    ap.add_argument("--gnd", default=argparse.SUPPRESS, help="ground net name")
    ap.add_argument("--vin", default=argparse.SUPPRESS, help="input rail net (auto if omitted)")
    ap.add_argument("--hs-ref", nargs="*", default=argparse.SUPPRESS,
                    help="force HS FET refdes")
    ap.add_argument("--ls-ref", nargs="*", default=argparse.SUPPRESS,
                    help="force LS FET refdes")
    ap.add_argument("--hs-gate", default=argparse.SUPPRESS, help="force HS gate net")
    ap.add_argument("--ls-gate", default=argparse.SUPPRESS, help="force LS gate net")
    ap.add_argument("--hs-kelvin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS, help="HS gate uses Kelvin source")
    ap.add_argument("--ls-kelvin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS, help="LS gate uses Kelvin source")
    ap.add_argument("--margin", type=float, default=argparse.SUPPRESS,
                    help="ROI margin (mm) around FETs/gate-driver")
    ap.add_argument("--separate", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="show pads/tracks/vias with distinct styling instead of "
                         "a single merged fill per layer (default: merged/gerber-like)")
    ap.add_argument("--split-fets", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="emit one SVG per paralleled FET instead of one per side "
                         "(default: per-side, using the first FET's gate net)")
    ap.add_argument("-o", "--out", default=argparse.SUPPRESS,
                    help="output directory for per-path SVGs")
    return ap


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    pre_args, _ = pre.parse_known_args(argv)
    yaml_args = {}
    if pre_args.config:
        yaml_args = _validate_config(_load_config(pre_args.config), pre_args.config)
    ap = build_parser()
    cli = vars(ap.parse_args(argv))
    merged = {}
    merged.update(DEFAULTS)
    merged.update(yaml_args)
    merged.update(cli)
    missing = [k for k in REQUIRED_ARGS if not merged.get(k)]
    if missing:
        ap.error("missing required argument(s): " + ", ".join(missing))
    keep = set(DEFAULTS) | set(REQUIRED_ARGS)
    return argparse.Namespace(**{k: v for k, v in merged.items() if k in keep})


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    args = parse_args(argv)
    board = pcbnew.LoadBoard(args.pcb)
    try:
        topo = fet_discovery.discover(
            board, args.sw, args.gnd, vin=args.vin,
            hs_ref=args.hs_ref, ls_ref=args.ls_ref,
            hs_gate=args.hs_gate, ls_gate=args.ls_gate,
            hs_kelvin=args.hs_kelvin, ls_kelvin=args.ls_kelvin)
    except ValueError as e:
        raise SystemExit(str(e))

    padcount = fet_discovery._pad_count_by_net(board)
    ltable = layer_table(board)
    edge_svg, edge_bb = collect_edge_svg(board)
    os.makedirs(args.out, exist_ok=True)
    merged = not args.separate

    def emit_svg(role, ref, nets, parts):
        roi = side_roi(board, parts, args.margin)
        layers_svg, vias_svg = collect_copper(board, nets, roi, ltable, merged=merged)
        fp_svg = collect_footprint_svg(board, parts, roi)
        focus = roi or edge_bb or (0, 0, 100, 100)
        vb = _bb_expand(focus, 2.0)
        viewbox = (vb[0], vb[1], max(vb[2] - vb[0], 1.0), max(vb[3] - vb[1], 1.0))
        svg = render_svg(board, role, ref, nets, layers_svg, vias_svg, fp_svg,
                         edge_svg, viewbox, ltable)
        suffix = f"_{ref}" if ref else ""
        path = os.path.join(args.out, f"gate_copper_{role}{suffix}.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        n_lyr = sum(1 for idx in range(len(ltable)) if layers_svg.get(idx))
        mode = "merged" if merged else "separate"
        print(f"wrote {path}  ({len(nets)} nets, {n_lyr} layers, {mode})")

    for role in ("hs", "ls"):
        gate_override = args.hs_gate if role == "hs" else args.ls_gate
        if args.split_fets:
            for ref in topo[role]["refs"]:
                nets = fet_loop_nets(board, topo, role, ref, padcount, gate_override)
                if not nets:
                    print(f"SKIP {role.upper()} {ref}: no gate net found")
                    continue
                parts = fet_part_refs(board, topo, role, ref, nets)
                emit_svg(role, ref, nets, parts)
        else:
            nets = side_nets(topo, role)
            parts = side_part_refs(board, topo, role, nets)
            emit_svg(role, None, nets, parts)


if __name__ == "__main__":
    main()
