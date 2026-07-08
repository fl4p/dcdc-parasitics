#!/usr/bin/env python3
"""Self-contained HTML layer viewer for a FastHenry `.inp` mesh.

Rasterizes each layer/group to a transparent PNG at a fixed extent, then emits a
tiny single-file HTML that stacks them as toggleable <img> layers with pan/zoom.
The browser composites raster instantly, so even the 0.2mm mesh (170k+ filaments)
stays responsive — no 100k-element SVG DOM.

Layers:
  PCB copper      Real PCB copper underlay (if --copper) — fade with the opacity slider
  Top (F.Cu)      F mesh + top-layer SMD caps/ports
  Bottom (B.Cu)   B mesh + bottom-layer SMD caps/ports
  Vias/FET leads  inter-layer vias, FET-lead risers, FET-plane caps/ports (shared)

With --copper <json> (from copper_dump.py) the REAL PCB copper is drawn faint under
the mesh in the same mm frame, so it aligns with zero coordinate transform.

extract_parasitics.py calls build_viewer() to drop `mesh.html` into the output set;
it also runs standalone:

    mesh_viewer.py model.inp [--copper copper.json] [--ports P.json] [--out mesh.html]
"""
import argparse
import base64
import json
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402

from mesh_geom import (F_CU, B_CU, VIA, LEAD, PORT, CAP,  # noqa: E402
                       parse_inp, plane, cap_names, draw_copper_underlay)


def cap_glyph(ax, p1, p2):
    """Subtle grey -||- capacitor glyph straddling terminal nodes p1,p2."""
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]; L = math.hypot(dx, dy)
    ux, uy = (1.0, 0.0) if L < 1e-3 else (dx / L, dy / L); vx, vy = -uy, ux
    g, pl, ll = 0.3, 0.8, 0.7
    for s in (-1, 1):
        cx, cy = mx + ux * g * s, my + uy * g * s
        ax.plot([cx - vx * pl, cx + vx * pl], [cy - vy * pl, cy + vy * pl], color=CAP, lw=1.6, solid_capstyle="round")
        ax.plot([cx, cx + ux * ll * s], [cy, cy + uy * ll * s], color=CAP, lw=1.0)


def _new_ax(bbox, figsize, dpi):
    fig = plt.figure(figsize=figsize, dpi=dpi)
    ax = fig.add_axes((0, 0, 1, 1))          # fill the canvas exactly -> layers align
    ax.set_xlim(bbox[0], bbox[1]); ax.set_ylim(bbox[3], bbox[2])   # y inverted
    ax.set_aspect("equal"); ax.axis("off")
    return fig, ax


def build_viewer(inp, out_html, ports_json=None, copper=None, dpi=300, embed=True):
    """Render `inp` as a layered raster HTML viewer at `out_html`.

    ports_json defaults to `<inp>.ports.json`; copper is an optional copper.json
    (from copper_dump.py) for the real-PCB underlay. Returns a short summary string.
    """
    ports_json = ports_json or (inp + ".ports.json")
    N, segs, ext = parse_inp(inp)
    pj = json.load(open(ports_json))
    ports = pj["ports"]
    pmap = dict(zip(ports, ext))
    capname = cap_names(pj)
    cu = json.load(open(copper)) if copper else None

    top, bot, via, lead = [], [], [], []
    for A, B in segs:
        xa, ya, za = N[A]; xb, yb, zb = N[B]; pa, pb = plane(za), plane(zb)
        if pa != pb:
            (lead if "lead" in (pa, pb) else via).append((xa, ya, xb, yb))
        elif pa == "top":
            top.append([(xa, ya), (xb, yb)])
        elif pa == "bot":
            bot.append([(xa, ya), (xb, yb)])

    xs = [v[0] for v in N.values()]; ys = [v[1] for v in N.values()]
    m = 2.0
    bbox = (min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m)   # x0,x1,y0,y1
    W = bbox[1] - bbox[0]; H = bbox[3] - bbox[2]
    figw = 9.0; figsize = (figw, figw * H / W)
    pxw, pxh = int(figw * dpi), int(figw * H / W * dpi)
    stem = os.path.splitext(out_html)[0]
    layers = []  # (id, label, filename)

    def layer(lid, label, draw):
        fig, ax = _new_ax(bbox, figsize, dpi)
        draw(ax)
        fn = f"{stem}_{lid}.png"
        fig.savefig(fn, transparent=True, dpi=dpi); plt.close(fig)
        layers.append((lid, label, fn))

    # classify caps + port-node markers by the LAYER (z) they sit on, so an SMD cap/port
    # on a layer hides when that layer is hidden. (FET-plane z~3 nodes -> shared annot.)
    tcap, bcap, ocap = [], [], []
    for p in capname:
        if p in pmap and pmap[p][0] in N and pmap[p][1] in N:
            a1, b1 = pmap[p]; pl = plane(N[a1][2])
            (tcap if pl == "top" else bcap if pl == "bot" else ocap).append((N[a1], N[b1]))
    tport, bport, oport = [], [], []
    for na, nb in ext:
        for n in (na, nb):
            if n in N:
                pl = plane(N[n][2]); pt = (N[n][0], N[n][1])
                (tport if pl == "top" else bport if pl == "bot" else oport).append(pt)

    def caps(ax, lst):
        for p1, p2 in lst:
            cap_glyph(ax, p1, p2)

    def ports_mk(ax, lst):
        if lst:
            ax.scatter([q[0] for q in lst], [q[1] for q in lst], s=8, marker="s",
                       c=PORT, edgecolors="k", linewidths=.25)

    if cu:
        def d_pcb(ax):
            draw_copper_underlay(ax, cu, "F", F_CU)
            draw_copper_underlay(ax, cu, "B", B_CU)
        layer("pcb", "PCB copper", d_pcb)

    def d_fcu(ax):
        ax.add_collection(LineCollection(top, colors=F_CU, linewidths=0.3))
        caps(ax, tcap); ports_mk(ax, tport)
    layer("fcu", "Top (F.Cu) + top SMD", d_fcu)

    def d_bcu(ax):
        ax.add_collection(LineCollection(bot, colors=B_CU, linewidths=0.3))
        caps(ax, bcap); ports_mk(ax, bport)
    layer("bcu", "Bottom (B.Cu) + bottom SMD", d_bcu)

    def d_annot(ax):
        ax.scatter([v[0] for v in via] + [v[2] for v in via], [v[1] for v in via] + [v[3] for v in via], s=10, c=VIA)
        ax.scatter([v[0] for v in lead] + [v[2] for v in lead], [v[1] for v in lead] + [v[3] for v in lead],
                   s=45, marker="^", c=LEAD, edgecolors="k", linewidths=.4)
        caps(ax, ocap); ports_mk(ax, oport)
    layer("annot", "Vias / FET leads", d_annot)

    def src(fn):
        if embed:
            return "data:image/png;base64," + base64.b64encode(open(fn, "rb").read()).decode()
        return os.path.basename(fn)

    imgs = "\n".join(f'<img id="{lid}" class="ly" src="{src(fn)}">' for lid, _, fn in layers)
    lab = {lid: l for lid, l, _ in layers}

    def chk(lid, sw, label):
        return (f'<label><input type="checkbox" checked '
                f'onchange="document.getElementById(\'{lid}\').style.display=this.checked?\'block\':\'none\'">'
                f'<span class="swatch" style="background:{sw}"></span>{label}</label>')
    def op_slider(lid):
        return (f'<label class="op-row"><span>opacity</span>'
                f'<input type="range" min="0" max="1" step="0.05" value="1" '
                f'oninput="document.getElementById(\'{lid}\').style.opacity=this.value"></label>')
    pcb_ctrl = ("\n" + chk("pcb", F_CU, lab["pcb"]) + op_slider("pcb")) if cu else ""
    controls = ("<h2>Layers</h2>\n" + chk("fcu", F_CU, lab["fcu"]) + "\n" + chk("bcu", B_CU, lab["bcu"])
                + "\n<h2>Overlays</h2>\n" + chk("annot", VIA, lab["annot"]) + pcb_ctrl)
    total_png = sum(os.path.getsize(fn) for _, _, fn in layers)
    counts = (f"F.Cu mesh {len(top)}, B.Cu mesh {len(bot)}, vias {len(via)}, "
              f"ports {len(ext)}, caps {len(capname)}" + (", + real-copper overlay" if cu else ""))
    html = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Fugu2 mesh + PCB</title>
<style>
:root{{color-scheme:dark}} *{{box-sizing:border-box}}
body{{margin:0;height:100vh;display:grid;grid-template-columns:232px 1fr;
 font:13px/1.35 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;color:#e8e8e8;background:#0a0a0a}}
aside{{background:#141414;border-right:1px solid #2a2a2a;padding:14px;overflow:auto}}
h1{{font-size:14px;margin:0 0 12px;font-weight:650}}
h2{{font-size:11px;margin:16px 0 6px;color:#9a9a9a;text-transform:uppercase}}
label{{display:flex;align-items:center;gap:8px;min-height:26px;cursor:pointer}}
.swatch{{display:inline-block;width:12px;height:12px;border-radius:2px;border:1px solid rgba(255,255,255,.2)}}
.op-row{{display:flex;align-items:center;gap:6px;min-height:22px;padding-left:20px;font-size:11px;color:#9a9a9a}}
.op-row input[type=range]{{flex:1;accent-color:#6af;cursor:pointer}}
.meta{{color:#8a8a8a;font-size:11px;margin-top:14px}}
.viewer{{min-height:0;overflow:hidden;background:#0a0a0a;cursor:grab;position:relative;
 user-select:none;-webkit-user-select:none}}
.viewer.dragging{{cursor:grabbing}}
#stage{{position:absolute;transform-origin:0 0}}
.ly{{position:absolute;top:0;left:0;width:{pxw}px;height:{pxh}px;
 -webkit-user-drag:none;user-drag:none;pointer-events:none}}
</style></head><body>
<aside>
 <h1>Fugu2 mesh + PCB</h1>
 {controls}
 <div class=meta>{counts}</div>
 <div class=meta>scroll = zoom, drag = pan · <a href="#" onclick="reset();return false" style=color:#6af>reset</a></div>
</aside>
<div class="viewer" id="vp"><div id="stage" style="width:{pxw}px;height:{pxh}px">{imgs}</div></div>
<script>
let s=1,tx=0,ty=0,st=document.getElementById('stage'),vp=document.getElementById('vp');
function ap(){{st.style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{s}})`}}
function reset(){{let r=vp.getBoundingClientRect();s=Math.min(r.width/{pxw},r.height/{pxh});
 tx=(r.width-{pxw}*s)/2;ty=(r.height-{pxh}*s)/2;ap()}}
addEventListener('load',reset);addEventListener('resize',reset);
vp.addEventListener('wheel',e=>{{e.preventDefault();let r=vp.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
 let k=Math.exp(-e.deltaY*0.0015);tx=mx-(mx-tx)*k;ty=my-(my-ty)*k;s*=k;ap()}},{{passive:false}});
let dr=0,px,py;vp.addEventListener('mousedown',e=>{{e.preventDefault();dr=1;px=e.clientX;py=e.clientY;vp.classList.add('dragging')}});
vp.addEventListener('dragstart',e=>e.preventDefault());
addEventListener('mousemove',e=>{{if(!dr)return;tx+=e.clientX-px;ty+=e.clientY-py;px=e.clientX;py=e.clientY;ap()}});
addEventListener('mouseup',()=>{{dr=0;vp.classList.remove('dragging')}});
</script></body></html>"""
    open(out_html, "w").write(html)
    return (f"{os.path.basename(out_html)} ({len(html) / 1024:.0f} KB html"
            + (" embedded" if embed else f" + {len(layers)} PNG {total_png / 1024:.0f} KB")
            + f", {pxw}x{pxh}px)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp", nargs="?", default="model.inp")
    ap.add_argument("--ports", default=None)
    ap.add_argument("--copper", default=None)
    ap.add_argument("--out", default="mesh.html")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--no-embed", action="store_true", help="write sidecar PNGs instead of inlining")
    a = ap.parse_args()
    print("wrote", build_viewer(a.inp, a.out, ports_json=a.ports, copper=a.copper,
                                dpi=a.dpi, embed=not a.no_embed))


if __name__ == "__main__":
    main()
