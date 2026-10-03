#!/usr/bin/env python3
"""Self-contained HTML layer viewer for a FastHenry `.inp` mesh.

Rasterizes each layer/group to a transparent PNG at a fixed extent, then emits a
tiny single-file HTML that stacks them as toggleable <img> layers with pan/zoom.
The browser composites raster instantly, so even the 0.2mm mesh (170k+ filaments)
stays responsive — no 100k-element SVG DOM.

Toggles (an image shows only while every toggle it belongs to is on):
  Top (F.Cu)      F copper underlay (if --copper) + F mesh; also gates top caps/ports
  Bottom (B.Cu)   B copper underlay + B mesh; also gates bottom caps/ports
  Vias            inter-layer vias
  FET leads       FET-lead risers
  Capacitors      cap glyphs + references on every layer
  Ports           port-node markers on every layer
  Board edge      Edge.Cuts outline (needs --copper)

With --copper, the PCB copper is split into separate per-layer PNGs so hiding a
layer hides its copper too. An opacity slider in Overlays fades both copper
layers together.

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
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402

from mesh_geom import (F_CU, B_CU, VIA, LEAD, PORT, CAP,  # noqa: E402
                       parse_inp, plane, cap_names, draw_copper_underlay)


CAP_PLATE_MM = 0.8   # half-length of a cap glyph's plates, perpendicular to p1-p2


def cap_glyph(ax, p1, p2):
    """Subtle grey -||- capacitor glyph straddling terminal nodes p1,p2."""
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]; L = math.hypot(dx, dy)
    ux, uy = (1.0, 0.0) if L < 1e-3 else (dx / L, dy / L); vx, vy = -uy, ux
    g, pl, ll = 0.3, CAP_PLATE_MM, 0.7
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
    W_mesh = (max(xs) - min(xs)) + 2 * m
    # The canvas covers the board edge too, when one is known: sized to the mesh
    # alone, every image clipped the Edge.Cuts outline to the mesh ROI, so on
    # Fugu2 only part of the left edge survived and zoom could not recover the rest
    # (review of 49e8c93, finding 5). Resolution is held at the mesh's original
    # px/mm by growing the canvas, its longer side capped at MAX_PX.
    edge = (cu or {}).get("edge") or []
    for e in edge:
        xs += [e[0], e[2]]; ys += [e[1], e[3]]
    bbox = (min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m)   # x0,x1,y0,y1
    W = bbox[1] - bbox[0]; H = bbox[3] - bbox[2]
    MAX_PX = 16000
    want = 9.0 * W / W_mesh
    figw = min(want, MAX_PX / dpi, MAX_PX / dpi * W / H)   # longer side <= MAX_PX
    figsize = (figw, figw * H / W)
    pxw, pxh = int(figw * dpi), int(figw * H / W * dpi)
    px_per_mm = pxw / W
    # Past the cap the board gets FEWER px/mm than its mesh alone would -- say so,
    # in the summary and the page, rather than lose detail silently (review of
    # dfd0972, finding 4). MAX_PX bounds the PNGs' memory; --dpi does not lift it.
    capped = (f"canvas capped at {MAX_PX} px: {px_per_mm:.1f} px/mm, not the "
              f"{want * dpi / W:.1f} px/mm the mesh alone gets") if want > figw else ""
    if capped:
        print(f"mesh_viewer: WARNING {capped}", file=sys.stderr)
    stem = os.path.splitext(out_html)[0]
    layers = []  # (id, label, filename)

    def layer(lid, groups, draw):
        """Rasterize one image. `groups` are the toggles it belongs to; the image is
        shown only while ALL of them are on, so a top-layer cap hides with either
        "Top" or "Capacitors"."""
        fig, ax = _new_ax(bbox, figsize, dpi)
        draw(ax)
        fn = f"{stem}_{lid}.png"
        fig.savefig(fn, transparent=True, dpi=dpi); plt.close(fig)
        layers.append((lid, groups, fn))

    # Caps and port markers are classified by the LAYER (z) they sit on, and drawn as
    # their own images, one per (kind, layer). Baking them into the copper image, as
    # this viewer used to, left no way to hide cap/port clutter while keeping the mesh,
    # and drawing vias and FET leads into one image left no way to tell them apart --
    # capabilities the superseded issue7 HTML viewer had (7 toggles; this had 3).
    # Merge review of d70128c, Codex finding (issue7 superseded, not equivalent).
    tcap, bcap, ocap = [], [], []
    for p in capname:
        if p in pmap and pmap[p][0] in N and pmap[p][1] in N:
            a1, b1 = pmap[p]; pl = plane(N[a1][2])
            (tcap if pl == "top" else bcap if pl == "bot" else ocap).append(
                (N[a1], N[b1], capname[p]))
    tport, bport, oport = [], [], []
    for na, nb in ext:
        for n in (na, nb):
            if n in N:
                pl = plane(N[n][2]); pt = (N[n][0], N[n][1])
                (tport if pl == "top" else bport if pl == "bot" else oport).append(pt)

    def caps(lst):
        def d(ax):
            for p1, p2, _ in lst:
                cap_glyph(ax, p1, p2)
        return d

    def cap_labels(lst):
        # Each cap's reference, as the superseded issue7 viewer labelled them (review
        # of 49e8c93, finding 4). Anchored just past the glyph's plate tip, on the
        # side the plates point to, and aligned AWAY from it, so the text box never
        # covers its own symbol; a fixed (10, 8) pt offset put the backing across the
        # plates of the module's C1/C2 (review of dfd0972, finding 3). Labels are
        # their own images stacked above every glyph, so another layer's plate
        # cannot cross the text either.
        def d(ax):
            for p1, p2, name in lst:
                mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
                dx, dy = p2[0] - p1[0], p2[1] - p1[1]; L = math.hypot(dx, dy)
                ux, uy = (1.0, 0.0) if L < 1e-3 else (dx / L, dy / L)
                vx, vy = -uy, ux
                if vy > 0 or (vy == 0 and vx < 0):     # prefer the side above on screen
                    vx, vy = -vx, -vy
                r = CAP_PLATE_MM + 0.25
                ha = "left" if vx > 0.38 else "right" if vx < -0.38 else "center"
                va = "bottom" if vy < -0.38 else "top" if vy > 0.38 else "center"
                ax.text(mx + vx * r, my + vy * r, name, ha=ha, va=va, fontsize=4,
                        color=CAP, clip_on=True,
                        bbox=dict(boxstyle="round,pad=0.15", fc="#141414",
                                  ec="none", alpha=0.8))
        return d

    def ports_mk(lst):
        def d(ax):
            if lst:
                ax.scatter([q[0] for q in lst], [q[1] for q in lst], s=8, marker="s",
                           c=PORT, edgecolors="k", linewidths=.25)
        return d

    def ends(segs4):
        return ([v[0] for v in segs4] + [v[2] for v in segs4],
                [v[1] for v in segs4] + [v[3] for v in segs4])

    if cu:
        layer("fcu_pcb", ["fcu"], lambda ax: draw_copper_underlay(ax, cu, "F", F_CU))
    layer("fcu", ["fcu"], lambda ax: ax.add_collection(
        LineCollection(top, colors=F_CU, linewidths=0.3)))
    if cu:
        layer("bcu_pcb", ["bcu"], lambda ax: draw_copper_underlay(ax, cu, "B", B_CU))
    layer("bcu", ["bcu"], lambda ax: ax.add_collection(
        LineCollection(bot, colors=B_CU, linewidths=0.3)))
    layer("via", ["via"], lambda ax: ax.scatter(*ends(via), s=10, c=VIA))
    layer("lead", ["lead"], lambda ax: ax.scatter(
        *ends(lead), s=45, marker="^", c=LEAD, edgecolors="k", linewidths=.4))
    # a cap/port on F or B also obeys that layer's toggle; FET-plane ones only their own
    for side, lc, lp in (("fcu", tcap, tport), ("bcu", bcap, bport), ("other", ocap, oport)):
        g = [] if side == "other" else [side]
        layer(f"cap_{side}", ["cap"] + g, caps(lc))
        layer(f"port_{side}", ["port"] + g, ports_mk(lp))
    if edge:
        layer("edge", ["edge"], lambda ax: ax.add_collection(LineCollection(
            [[(e[0], e[1]), (e[2], e[3])] for e in edge], colors="#d8d8d8", linewidths=0.6)))
    for side, lc in (("fcu", tcap), ("bcu", bcap), ("other", ocap)):
        layer(f"caplbl_{side}", ["cap"] + ([] if side == "other" else [side]),
              cap_labels(lc))

    def src(fn):
        if embed:
            return "data:image/png;base64," + base64.b64encode(open(fn, "rb").read()).decode()
        return os.path.basename(fn)

    imgs = "\n".join(f'<img id="{lid}" class="ly" data-g="{" ".join(g)}" src="{src(fn)}">'
                     for lid, g, fn in layers)

    def chk(group, sw, label):
        return (f'<label><input type="checkbox" class="tg" data-g="{group}" checked '
                f'onchange="vis()"><span class="swatch" style="background:{sw}"></span>'
                f'{label}</label>')
    def op_slider(ids):
        ids_js = "','".join(ids)
        return (f'<label class="op-row"><span>opacity</span>'
                f'<input type="range" min="0" max="1" step="0.05" value="1" '
                f'oninput="[\'{ids_js}\'].forEach(id=>document.getElementById(id).style.opacity=this.value)"></label>')
    pcb_op = "\n" + op_slider(["fcu_pcb", "bcu_pcb"]) if cu else ""
    controls = ("<h2>Layers</h2>\n" + chk("fcu", F_CU, "Top (F.Cu)")
                + "\n" + chk("bcu", B_CU, "Bottom (B.Cu)") + pcb_op
                + "\n<h2>Overlays</h2>\n" + chk("via", VIA, "Vias")
                + "\n" + chk("lead", LEAD, "FET leads")
                + "\n" + chk("cap", CAP, "Capacitors")
                + "\n" + chk("port", PORT, "Ports")
                + ("\n" + chk("edge", "#d8d8d8", "Board edge") if edge else ""))
    total_png = sum(os.path.getsize(fn) for _, _, fn in layers)
    counts = (f"F.Cu mesh {len(top)}, B.Cu mesh {len(bot)}, vias {len(via)}, "
              f"ports {len(ext)}, caps {len(capname)}" + (", + real-copper overlay" if cu else "")
              + (f" · <b style=color:#fa6>{capped}</b>" if capped else ""))
    html = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>FastHenry mesh + PCB</title>
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
 <h1>FastHenry mesh + PCB</h1>
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
function vis(){{let on=new Set([...document.querySelectorAll('.tg')].filter(c=>c.checked).map(c=>c.dataset.g));
 document.querySelectorAll('.ly').forEach(i=>{{i.style.display=i.dataset.g.split(' ').every(g=>on.has(g))?'block':'none'}})}}
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
            + f", {pxw}x{pxh}px, {px_per_mm:.1f} px/mm"
            + (f"; WARNING {capped}" if capped else "") + ")")


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
