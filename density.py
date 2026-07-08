#!/usr/bin/env python3
"""Per-layer copper power-loss density from a FastHenry mesh + injected port currents.

Loss-agnostic: this solves the extracted mesh as a **DC resistive network**, injects
the operating currents named in a small currents-spec JSON, and renders per-copper-layer
W/mm² heatmaps. It knows nothing about SPICE, `.raw`, or the loss tool — the currents (and
optional per-phase total-normalization targets) are just parameters. The loss-side driver
(`loss/loss_density.py`) sources those numbers from a real run and calls this via subprocess;
a hand-written spec drives it directly (used by the unit tests).

Why a self-solve: the extractor keeps only lumped port L/R, and FastHenry's current dump
covers ground planes only — not our filament-grid pour mesh. A DC nodal solve on the mesh
(`R_seg = length/(σ·w·h)`, `.equiv` = short) recovers the current *spreading* through the
pours, which is exactly what a density map needs; a lumped R cannot be distributed.

Currents-spec JSON:
    {
      "sigma": 58000.0,          # optional S/mm; else from the .inp `.default`, else 58e3
      "t_copper": 100.0,         # optional copper temp (C) to scale R (+0.39%/K)
      "cu_temp_ref": 20.0,       # optional temp the .inp sigma corresponds to (default 20)
      "phases": [
        {"name":"hs",  "port":"P_hs", "i_rms":41.2, "norm_W":0.166},
        {"name":"ls",  "port":"P_ls", "i_rms":52.1, "norm_W":0.164},
        {"name":"cin", "tap":"P_pwr", "cap_currents":{"C11":6.1,"C12":5.7},
         "norm_W":0.588}
      ]
    }

A 2-terminal phase (`port`,`i_rms`) drives current between that port's `.external` node
pair. A Cin phase (`tap`,`cap_currents`) sources each cap's branch current at that cap's
Vin/GND pads and sinks the sum at the tap port (nearest-cap commutation entry), so the
Vin/GND trunk carries the summed ripple and each cap only its own branch — currents add
*before* squaring inside the single solve. `norm_W` (optional) scales a phase so its
`Σ P_seg` equals a reference bucket (e.g. the loss tool's `loop_r`/`cin_copper`).

    density.py MESH.inp --currents SPEC.json [--ports P.json] [-o OUTDIR]
                        [--copper copper.json] [--cmap inferno] [--clip-pct 99]
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import spsolve

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mesh_geom import (parse_mesh, plane, track_rects, cap_names,  # noqa: E402
                       draw_copper_underlay, F_CU, B_CU)

SIGMA_20C = 5.8e4           # S/mm, copper at 20 C
ALPHA_CU = 0.0039           # +0.39 %/K
CU_T = 0.035                # default copper thickness mm (1 oz)
Z_EPS = 0.01                # mm: |za-zb| below this = planar (same layer)


# --------------------------------------------------------------------------- #
# union-find for .equiv shorts
# --------------------------------------------------------------------------- #
class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:      # path compression
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# --------------------------------------------------------------------------- #
# resistive network
# --------------------------------------------------------------------------- #
class Network:
    """DC resistive network built from a parsed mesh. Nodes merged across `.equiv`."""

    def __init__(self, mesh, sigma):
        self.pos = mesh["nodes"]
        self.sigma = sigma
        uf = _UF()
        for n in self.pos:
            uf.find(n)
        for a, b in mesh["equivs"]:
            if a in self.pos and b in self.pos:
                uf.union(a, b)
        self.rep = {n: uf.find(n) for n in self.pos}          # node -> supernode
        reps = sorted(set(self.rep.values()))
        self.idx = {r: i for i, r in enumerate(reps)}          # supernode -> matrix row
        self.n = len(reps)

        # resistive segments (drop zero-length / self-shorted after equiv merge)
        self.segs = []          # (i, j, R, w, length, midx, midy, layer, na, nb)
        rows, cols, data = [], [], []
        for s in mesh["segs"]:
            na, nb = s["na"], s["nb"]
            if na not in self.pos or nb not in self.pos:
                continue
            i, j = self.idx[self.rep[na]], self.idx[self.rep[nb]]
            if i == j:
                continue                                       # shorted by .equiv
            xa, ya, za = self.pos[na]
            xb, yb, zb = self.pos[nb]
            length = math.dist((xa, ya, za), (xb, yb, zb))
            w = s["w"] if s["w"] else 0.05
            h = s["h"] if s["h"] else CU_T
            if length < 1e-9:
                continue
            R = length / (sigma * w * h)
            g = 1.0 / R
            rows += [i, j, i, j]
            cols += [i, j, j, i]
            data += [g, g, -g, -g]
            layer = self._layer(za, zb)
            self.segs.append((i, j, R, w, length,
                              (xa + xb) / 2, (ya + yb) / 2, layer,
                              (xa, ya, xb, yb)))
        self.G = sp.csr_matrix((data, (rows, cols)), shape=(self.n, self.n))

    @staticmethod
    def _layer(za, zb):
        """Layer/kind of a segment from its endpoint z. Planar -> the copper layer;
        inter-layer -> via or FET-lead riser."""
        if abs(za - zb) < Z_EPS:
            p = plane(za)
            return {"top": "F.Cu", "bot": "B.Cu", "lead": "lead"}.get(p, f"z{za:.2f}")
        return "leads" if "lead" in (plane(za), plane(zb)) else "vias"

    def node_row(self, node):
        return self.idx[self.rep[node]]

    def solve(self, inject):
        """inject: {supernode_row: current_A} (must sum ~0). Returns node voltages v
        (len n, reference node pinned to 0)."""
        b = np.zeros(self.n)
        for r, cur in inject.items():
            b[r] += cur
        if abs(b.sum()) > 1e-6 * (np.abs(b).sum() + 1e-30):
            raise ValueError(f"injection not balanced (Σ={b.sum():.3e} A)")
        # pin a reference node (one with injection) to 0 by dropping its row/col
        ref = max(inject, key=lambda r: abs(inject[r]))
        keep = np.ones(self.n, dtype=bool)
        keep[ref] = False
        Gr = self.G[keep][:, keep]
        br = b[keep]
        vr = spsolve(Gr.tocsc(), br)
        v = np.zeros(self.n)
        v[keep] = np.asarray(vr)
        return v

    def seg_power(self, v):
        """Per-segment dissipation array P_seg = (v_i - v_j)^2 / R (W)."""
        return np.array([(v[i] - v[j]) ** 2 / R for (i, j, R, *_) in self.segs])


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #
def _port_nodes(ports_json, mesh):
    """{label: (nodeA, nodeB)} — .external order matches the ports list order, plus any
    `aux_ports` (node pairs the extractor emits for DC tools but does NOT solve in FastHenry,
    e.g. the output-inductor terminal P_out_hs/P_out_ls)."""
    pj = json.load(open(ports_json))
    pmap = dict(zip(pj["ports"], mesh["external"]))
    for lbl, pair in (pj.get("aux_ports") or {}).items():
        pmap[lbl] = tuple(pair)
    return pmap, pj


def phase_power(net, ph, pmap, refdes2port):
    """Return per-segment P (W) for one phase, before normalization."""
    if "cap_currents" in ph:                       # Cin ripple
        tap = ph.get("tap", "P_pwr")
        if tap not in pmap:
            raise SystemExit(f"cin phase: tap port {tap} not in mesh")
        tap_vin, tap_gnd = pmap[tap]
        inj = {}

        def add(row, cur):
            inj[row] = inj.get(row, 0.0) + cur

        tot = 0.0
        # `cap_ports` lets the caller pin a refdes to a specific port when the mesh names
        # it differently (e.g. the bulk anchor cap is ported as P_bulk, not P_cin_<ref>).
        cap_ports = ph.get("cap_ports", {})
        for ref, cur in ph["cap_currents"].items():
            port = cap_ports.get(ref) or refdes2port.get(ref)
            if port is None or port not in pmap:
                continue
            vin, gnd = pmap[port]
            add(net.node_row(vin), +cur)           # cap sources into Vin trunk
            add(net.node_row(gnd), -cur)           # returns on GND trunk
            tot += cur
        add(net.node_row(tap_vin), -tot)           # switch tap sinks the sum (Vin)
        add(net.node_row(tap_gnd), +tot)           # and sources it back (GND)
        v = net.solve(inj)
        return net.seg_power(v)

    # 2-terminal conduction phase
    port = ph["port"]
    if port not in pmap:
        raise SystemExit(f"phase {ph.get('name')}: port {port} not in mesh")
    a, b = pmap[port]
    i = ph["i_rms"]
    inj = {net.node_row(a): +i, net.node_row(b): -i}
    v = net.solve(inj)
    return net.seg_power(v)


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def _pitch_estimate(net):
    """Median planar-segment length ≈ the pour mesh pitch (mm), for field binning."""
    import statistics
    ls = [net.segs[k][4] for k in range(len(net.segs))
          if net.segs[k][7] in ("F.Cu", "B.Cu") or net.segs[k][7].startswith("z")]
    return statistics.median(ls) if ls else 1.0


def render(net, dens, out_stem, cmap="inferno", clip_pct=99.0, copper=None,
           title="copper loss density", style="field"):
    """Per-layer heatmap PNGs + a single-file HTML viewer. dens = W/mm² per segment.

    style='field' (default): pour layers rendered as a node-binned density field
    (imshow) — each ~pitch cell is the mean of the filament densities whose midpoints
    fall in it, so the horizontal+vertical edges at a location merge and the directional
    per-filament checkerboard disappears. style='filaments': draw each segment rectangle
    (the raw mesh; useful for debugging). Vias/FET-leads are always discrete rectangles."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from matplotlib.colorbar import ColorbarBase
    from matplotlib.colors import Normalize
    import base64

    order = ["F.Cu", "B.Cu"] + sorted(
        {net.segs[k][7] for k in range(len(net.segs))} - {"F.Cu", "B.Cu"})
    pos_d = dens[dens > 0]
    vmax = float(np.percentile(pos_d, clip_pct)) if pos_d.size else 1.0
    if vmax <= 0:
        vmax = 1.0
    norm = Normalize(0, vmax)

    xs = [p[0] for p in net.pos.values()]
    ys = [p[1] for p in net.pos.values()]
    m = 2.0
    bbox = (min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m)
    W = bbox[1] - bbox[0]
    H = bbox[3] - bbox[2]
    dpi = 300
    figw = 9.0
    figsize = (figw, figw * H / W)
    pxw, pxh = int(figw * dpi), int(figw * H / W * dpi)
    pitch = _pitch_estimate(net)
    fcmap = plt.get_cmap(cmap).copy()
    fcmap.set_bad(alpha=0.0)                 # empty (no-copper) cells -> transparent

    layers = {}
    for k, seg in enumerate(net.segs):
        layers.setdefault(seg[7], []).append(k)

    def field(ks):
        """Node-binned mean density (masked where no filament) for imshow."""
        mx = np.array([net.segs[k][5] for k in ks])
        my = np.array([net.segs[k][6] for k in ks])
        d = np.array([dens[k] for k in ks])
        nx = max(1, int(round(W / pitch)))
        ny = max(1, int(round(H / pitch)))
        xe = np.linspace(bbox[0], bbox[1], nx + 1)
        ye = np.linspace(bbox[2], bbox[3], ny + 1)
        ssum, _, _ = np.histogram2d(mx, my, bins=[xe, ye], weights=d)
        cnt, _, _ = np.histogram2d(mx, my, bins=[xe, ye])
        fld = np.divide(ssum, cnt, out=np.full_like(ssum, np.nan), where=cnt > 0)
        return np.ma.masked_invalid(fld.T)   # transpose -> rows indexed by y

    out_layers = []          # (id, label, filename)
    for layer in order:
        if layer not in layers:
            continue
        ks = layers[layer]
        fig = plt.figure(figsize=figsize, dpi=dpi)
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_xlim(bbox[0], bbox[1])
        ax.set_ylim(bbox[3], bbox[2])       # y inverted (PCB convention)
        ax.set_aspect("equal")
        ax.axis("off")
        if copper:
            key = "F" if layer == "F.Cu" else ("B" if layer == "B.Cu" else None)
            if key:
                draw_copper_underlay(ax, copper, key,
                                     F_CU if key == "F" else B_CU, zbase=0)
        planar = layer in ("F.Cu", "B.Cu") or layer.startswith("z")
        if style == "field" and planar:
            fld = field(ks)
            normed = np.clip(np.asarray(norm(fld)), 0, 1)
            rgba = fcmap(normed)                 # value -> color
            # alpha ramps with density so low-loss copper stays transparent (the board
            # overlay glows through) and hotspots read bright — a heat-glow over the PCB.
            alpha = normed ** 0.7
            alpha[np.ma.getmaskarray(fld)] = 0.0
            rgba[..., 3] = alpha
            ax.imshow(rgba, extent=(bbox[0], bbox[1], bbox[2], bbox[3]),
                      origin="lower", interpolation="nearest", zorder=5)
        else:                                # filaments, or discrete vias/leads
            rects = track_rects([net.segs[k][8] + (net.segs[k][3],) for k in ks])
            ax.add_collection(PolyCollection(rects, array=np.array([dens[k] for k in ks]),
                                             cmap=cmap, norm=norm, edgecolors="none",
                                             zorder=5))
        fn = f"{out_stem}_{layer.replace('.', '')}.png"
        fig.savefig(fn, transparent=True, dpi=dpi)
        plt.close(fig)
        out_layers.append((layer.replace(".", ""), layer, fn))

    # colorbar strip
    cbar_fn = f"{out_stem}_colorbar.png"
    fig, ax = plt.subplots(figsize=(figw, 0.9))
    fig.subplots_adjust(bottom=0.55, top=0.9, left=0.03, right=0.97)
    cb = ColorbarBase(ax, cmap=plt.get_cmap(cmap), norm=norm,
                      orientation="horizontal")
    cb.set_label(f"copper loss density (W/mm²)  ·  scale clipped at P{clip_pct:g}")
    fig.savefig(cbar_fn, dpi=dpi)
    plt.close(fig)

    def b64(fn):
        return "data:image/png;base64," + base64.b64encode(open(fn, "rb").read()).decode()

    imgs = "\n".join(
        f'<img id="{lid}" class="ly" src="{b64(fn)}">' for lid, _, fn in out_layers)
    toggles = "\n".join(
        f'<label><input type=checkbox checked '
        f'onchange="document.getElementById(\'{lid}\').style.display=this.checked?\'block\':\'none\'">'
        f'{lab}</label>' for lid, lab, _ in out_layers)
    html = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}
body{{margin:0;height:100vh;display:grid;grid-template-columns:230px 1fr;
 font:13px/1.35 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;color:#e8e8e8;background:#0a0a0a}}
aside{{background:#141414;border-right:1px solid #2a2a2a;padding:14px;overflow:auto}}
h1{{font-size:14px;margin:0 0 12px}}label{{display:flex;gap:8px;min-height:26px;cursor:pointer}}
.viewer{{overflow:hidden;background:#0a0a0a;cursor:grab;position:relative}}
#stage{{position:absolute;transform-origin:0 0}}
.ly{{position:absolute;top:0;left:0;width:{pxw}px;height:{pxh}px;pointer-events:none}}
.cbar{{margin-top:14px;width:100%}}
</style></head><body>
<aside><h1>{title}</h1>{toggles}
<img class=cbar src="{b64(cbar_fn)}">
<div style="color:#8a8a8a;font-size:11px;margin-top:12px">scroll=zoom, drag=pan ·
 <a href=# onclick="reset();return false" style=color:#6af>reset</a></div></aside>
<div class=viewer id=vp><div id=stage style="width:{pxw}px;height:{pxh}px">{imgs}</div></div>
<script>
let s=1,tx=0,ty=0,st=document.getElementById('stage'),vp=document.getElementById('vp');
function ap(){{st.style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{s}})`}}
function reset(){{let r=vp.getBoundingClientRect();s=Math.min(r.width/{pxw},r.height/{pxh});
 tx=(r.width-{pxw}*s)/2;ty=(r.height-{pxh}*s)/2;ap()}}
addEventListener('load',reset);addEventListener('resize',reset);
vp.addEventListener('wheel',e=>{{e.preventDefault();let r=vp.getBoundingClientRect(),
 mx=e.clientX-r.left,my=e.clientY-r.top,k=Math.exp(-e.deltaY*0.0015);
 tx=mx-(mx-tx)*k;ty=my-(my-ty)*k;s*=k;ap()}},{{passive:false}});
let dr=0,px,py;vp.addEventListener('mousedown',e=>{{dr=1;px=e.clientX;py=e.clientY}});
addEventListener('mousemove',e=>{{if(!dr)return;tx+=e.clientX-px;ty+=e.clientY-py;px=e.clientX;py=e.clientY;ap()}});
addEventListener('mouseup',()=>dr=0);
</script></body></html>"""
    html_fn = f"{out_stem}.html"
    open(html_fn, "w").write(html)
    return html_fn, vmax


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def compute(mesh, spec, ports_json):
    sigma = spec.get("sigma") or mesh["sigma"] or SIGMA_20C
    t_cu = spec.get("t_copper")
    if t_cu is not None:
        sigma = sigma / (1.0 + ALPHA_CU * (t_cu - spec.get("cu_temp_ref", 20.0)))
    net = Network(mesh, sigma)
    pmap, pj = _port_nodes(ports_json, mesh)
    refdes2port = {v: k for k, v in cap_names(pj).items()}

    total = np.zeros(len(net.segs))
    phase_rows = []
    for ph in spec["phases"]:
        P = phase_power(net, ph, pmap, refdes2port)
        raw_W = float(P.sum())
        norm_W = ph.get("norm_W")
        if norm_W and raw_W > 0:
            P = P * (norm_W / raw_W)
        total += P
        phase_rows.append(dict(name=ph.get("name", ph.get("port", "?")),
                               raw_W=raw_W, norm_W=norm_W,
                               W=float(P.sum())))
    return net, total, sigma, phase_rows


def layer_summary(net, total):
    areas, powers, peaks = {}, {}, {}
    for k, seg in enumerate(net.segs):
        layer, w, L = seg[7], seg[3], seg[4]
        a = w * L
        areas[layer] = areas.get(layer, 0.0) + a
        powers[layer] = powers.get(layer, 0.0) + total[k]
        d = total[k] / a if a > 0 else 0.0
        if d > peaks.get(layer, (0.0, None))[0]:
            peaks[layer] = (d, (seg[5], seg[6]))
    rows = []
    for layer in sorted(powers, key=lambda l: -powers[l]):
        a = areas[layer]
        pk, pkxy = peaks.get(layer, (0.0, None))
        rows.append(dict(layer=layer, W=powers[layer], area_mm2=a,
                         mean_W_mm2=(powers[layer] / a if a > 0 else 0.0),
                         peak_W_mm2=pk, peak_xy=pkxy))
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp", help="FastHenry mesh .inp")
    ap.add_argument("--currents", required=True, help="currents-spec JSON")
    ap.add_argument("--ports", default=None, help="ports.json (default <inp>.ports.json)")
    ap.add_argument("--copper", default=None, help="copper.json underlay (from copper_dump.py)")
    ap.add_argument("-o", "--out", default="density", help="output dir")
    ap.add_argument("--cmap", default="inferno")
    ap.add_argument("--clip-pct", type=float, default=99.0)
    ap.add_argument("--style", choices=("field", "filaments"), default="field",
                    help="field: node-binned smooth heatmap (default); "
                         "filaments: raw per-segment mesh rectangles")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()

    mesh = parse_mesh(args.inp)
    spec = json.load(open(args.currents))
    ports_json = args.ports or (args.inp + ".ports.json")
    net, total, sigma, phase_rows = compute(mesh, spec, ports_json)

    os.makedirs(args.out, exist_ok=True)
    rows = layer_summary(net, total)
    dens = np.array([total[k] / (net.segs[k][3] * net.segs[k][4])
                     if net.segs[k][4] > 0 else 0.0 for k in range(len(net.segs))])

    out = dict(sigma_S_per_mm=sigma, total_W=float(total.sum()),
               n_segs=len(net.segs), n_nodes=net.n, phases=phase_rows, layers=rows)
    json.dump(out, open(os.path.join(args.out, "density.json"), "w"), indent=2, default=list)

    print(f"σ={sigma:.4g} S/mm   segments={len(net.segs)}   supernodes={net.n}")
    print(f"{'layer':<8} {'W':>8} {'area/mm²':>10} {'mean W/mm²':>12} {'peak W/mm²':>12}")
    for r in rows:
        print(f"{r['layer']:<8} {r['W']:>8.4f} {r['area_mm2']:>10.2f} "
              f"{r['mean_W_mm2']:>12.4g} {r['peak_W_mm2']:>12.4g}")
    print(f"{'TOTAL':<8} {total.sum():>8.4f} W")
    for pr in phase_rows:
        nn = f" -> norm {pr['norm_W']:.4f} W" if pr["norm_W"] else ""
        print(f"  phase {pr['name']:<5} raw {pr['raw_W']:.4f} W{nn}")

    if not args.no_render:
        copper = json.load(open(args.copper)) if args.copper else None
        html, vmax = render(net, dens, os.path.join(args.out, "density"),
                            cmap=args.cmap, clip_pct=args.clip_pct, copper=copper,
                            style=args.style)
        print(f"wrote {html}  (density_<layer>.png, color scale 0..{vmax:.3g} W/mm²)")


if __name__ == "__main__":
    main()
