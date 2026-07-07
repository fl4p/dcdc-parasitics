#!/usr/bin/env python3
"""Render a FastHenry .inp mesh (from dcdc-tools/parasitics kicad_geom.py) as a
per-layer copper plot: F.Cu / B.Cu filaments, inter-layer vias, FET lead risers,
Cin caps (subtle grey -||-), and port nodes (yellow squares).

Optionally overlays the REAL PCB copper (faint filled zones/tracks/pads) under the
mesh via `--copper copper.json` (produced by copper_dump.py) — same mm frame, so it
aligns exactly, for visually verifying the mesh matches the layout.

Reads the .inp plus its <inp>.ports.json sidecar (for port->cap naming).
Emits <stem>_full.png and <stem>_zoom.png.

Usage:
    power_copper.py [model.inp] [--copper copper.json] [--zoom X0 X1 Y0 Y1] [--stem OUT]
"""
import re, json, math, argparse, html
# matplotlib is imported lazily inside the PNG-rendering path only, so the
# HTML/SVG viewer path stays pure-Python (no matplotlib dependency).

F_CU, B_CU = "#e07000", "#12b0bb"      # top / bottom copper
VIA, LEAD, PORT, CAP = "#ff2d2d", "#ffffff", "#ffe000", "#c8c8c8"


def parse_inp(path):
    N, segs, ext = {}, [], []
    for ln in open(path):
        s = ln.strip()
        m = re.match(r"(N\S+)\s+x=([-\d.]+)\s+y=([-\d.]+)\s+z=([-\d.]+)", s)
        if m:
            N[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
        elif s.startswith("E"):
            t = s.split()
            if len(t) >= 3 and t[1] in N and t[2] in N:
                segs.append((t[1], t[2]))
        elif s.startswith(".external"):
            t = s.split()
            ext.append((t[1], t[2]))
    return N, segs, ext


def cap_names(ports_json):
    """port label -> cap refdes, from the .ports.json sidecar."""
    p = json.load(open(ports_json))
    cin_ports = p.get("cin_ports", [])
    cin_used = p.get("cin_used", [])
    names = {lbl: cin_used[i] for i, lbl in enumerate(cin_ports) if i < len(cin_used)}
    for lbl in p.get("ports", []):
        if lbl.startswith("P_cin_"):
            names[lbl] = lbl[len("P_cin_"):]
        elif lbl == "P_bulk":
            names[lbl] = "bulk"
    return names


def plane(z):
    if abs(z) < 0.5:
        return "top"
    if abs(z + 1.6) < 0.5:
        return "bot"
    return "lead"                         # z≈3 mm FET exposed-lead / die plane


def classify(N, segs):
    top, bot, via, lead = [], [], [], []
    for a, b in segs:
        xa, ya, za = N[a]
        xb, yb, zb = N[b]
        pa, pb = plane(za), plane(zb)
        if pa != pb:
            (lead if "lead" in (pa, pb) else via).append((xa, ya, xb, yb))
        elif pa == "top":
            top.append([(xa, ya), (xb, yb)])
        elif pa == "bot":
            bot.append([(xa, ya), (xb, yb)])
    return top, bot, via, lead


def cap_geometry(p1, p2):
    """Geometry for a -||- capacitor symbol straddling terminal nodes p1,p2.

    Shared by the matplotlib (cap_symbol) and SVG paths so both draw the same
    plates/leads/label. Returns dict with:
      plates -> [(x1,y1,x2,y2), ...]  (2 plate line segments)
      leads  -> [(x1,y1,x2,y2), ...]  (2 lead line segments)
      label  -> (x, y)                (label anchor)
    """
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy)
    ux, uy = (1.0, 0.0) if L < 1e-3 else (dx / L, dy / L)
    vx, vy = -uy, ux
    gap, plate, leadlen = 0.3, 0.8, 0.7
    plates, leads = [], []
    for sgn in (-1, 1):
        cx, cy = mx + ux * gap * sgn, my + uy * gap * sgn
        plates.append((cx - vx * plate, cy - vy * plate, cx + vx * plate, cy + vy * plate))
        leads.append((cx, cy, cx + ux * leadlen * sgn, cy + uy * leadlen * sgn))
    return {"plates": plates, "leads": leads, "label": (mx + vx * plate * 1.8, my + vy * plate * 1.8)}


def cap_symbol(ax, p1, p2, name, z=7):
    """Subtle grey -||- capacitor symbol straddling terminal nodes p1,p2."""
    g = cap_geometry(p1, p2)
    for x1, y1, x2, y2 in g["plates"]:
        ax.plot([x1, x2], [y1, y2], color=CAP, lw=1.6, zorder=z, solid_capstyle="round")
    for x1, y1, x2, y2 in g["leads"]:
        ax.plot([x1, x2], [y1, y2], color=CAP, lw=1.0, zorder=z)
    ax.annotate(name, g["label"], color=CAP, fontsize=6.5, ha="center", va="center",
                zorder=z, alpha=0.85)


def _track_rects(tracks):
    """Each routed track -> a width-accurate rectangle in DATA (mm) units, so its
    real copper width shows and scales on zoom (matplotlib linewidths are POINTS,
    which drew thin traces as fixed hairlines regardless of zoom)."""
    rects = []
    for x1, y1, x2, y2, w in tracks:
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy)
        h = w / 2.0
        if L < 1e-9:                      # zero-length track -> small square
            rects.append([(x1 - h, y1 - h), (x1 + h, y1 - h), (x1 + h, y1 + h), (x1 - h, y1 + h)])
            continue
        nx, ny = -dy / L * h, dx / L * h  # perpendicular offset = half-width
        rects.append([(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)])
    return rects


def draw_copper(ax, cu):
    """Faint filled real PCB copper (zones/tracks/pads) UNDER the mesh (z 1-4)."""
    from matplotlib.collections import PolyCollection
    from matplotlib.patches import Polygon
    for ring in cu["zones"]["B"]:
        ax.add_patch(Polygon(ring, closed=True, facecolor=B_CU, edgecolor="none", alpha=0.16, zorder=1))
    for ring in cu["zones"]["F"]:
        ax.add_patch(Polygon(ring, closed=True, facecolor=F_CU, edgecolor="none", alpha=0.16, zorder=2))
    # tracks as width-accurate filled rectangles (data units), not point-width lines
    ax.add_collection(PolyCollection(_track_rects(cu["tracks"]["B"]), facecolors=B_CU,
                                     edgecolors="none", alpha=0.3, zorder=1))
    ax.add_collection(PolyCollection(_track_rects(cu["tracks"]["F"]), facecolors=F_CU,
                                     edgecolors="none", alpha=0.3, zorder=2))
    for ring in cu["pads"]["B"]:
        ax.add_patch(Polygon(ring, closed=True, facecolor=B_CU, alpha=0.38, zorder=3, edgecolor="none"))
    for ring in cu["pads"]["F"]:
        ax.add_patch(Polygon(ring, closed=True, facecolor=F_CU, alpha=0.38, zorder=4, edgecolor="none"))
    for e in cu["edge"]:
        ax.plot([e[0], e[2]], [e[1], e[3]], color="#666", lw=0.8, zorder=0)


def render(ax, N, top, bot, via, lead, ext, pmap, capname, xlim, ylim, cu=None):
    from matplotlib.collections import LineCollection
    ax.set_facecolor("#0a0a0a")
    ax.set_aspect("equal")
    ax.invert_yaxis()
    # 0) real PCB copper underlay (optional)
    if cu:
        draw_copper(ax, cu)
    mesh_alpha = (0.9, 0.9) if cu else (0.32, 0.40)   # brighter mesh when no copper underlay
    # 1) caps (behind mesh when mesh-only; above copper otherwise)
    cap_z = 1 if not cu else 7
    for p, cn in capname.items():
        if p in pmap and pmap[p][0] in N and pmap[p][1] in N:
            a, b = pmap[p]
            cap_symbol(ax, N[a], N[b], cn, z=cap_z)
    # 2) mesh
    ax.add_collection(LineCollection(bot, colors=B_CU, linewidths=0.6, alpha=mesh_alpha[0], zorder=5))
    ax.add_collection(LineCollection(top, colors=F_CU, linewidths=0.6, alpha=mesh_alpha[1], zorder=6))
    # 3) vias, FET leads, ports on top
    ax.scatter([v[0] for v in via] + [v[2] for v in via],
               [v[1] for v in via] + [v[3] for v in via], s=15, c=VIA, zorder=8)
    ax.scatter([v[0] for v in lead] + [v[2] for v in lead],
               [v[1] for v in lead] + [v[3] for v in lead], s=50, marker="^",
               c=LEAD, zorder=9, edgecolors="k", linewidths=.4)
    px = [N[n][0] for na, nb in ext for n in (na, nb) if n in N]
    py = [N[n][1] for na, nb in ext for n in (na, nb) if n in N]
    ax.scatter(px, py, s=34, marker="s", c=PORT, zorder=8, edgecolors="k", linewidths=.4)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)


# ---------------------------------------------------------------------------
# Self-contained SVG / HTML layer viewer (issue #7)
#
# Emits a single HTML file with inline SVG + a few lines of vanilla JS. The same
# geometry helpers (classify / _track_rects / cap_geometry) feed both the
# matplotlib PNG path above and the SVG path below. Coordinates are the raw mm
# pcbnew frame: SVG's native Y-down axis matches matplotlib's invert_yaxis(), so
# the viewer has the same orientation as the PNG with no explicit flip.
# ---------------------------------------------------------------------------

def _esc(s):
    return html.escape(str(s), quote=True)


def _f(v):
    return f"{v:.3f}"


def svg_line(x1, y1, x2, y2, cls):
    return f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" class="{cls}"/>'


def svg_polyline_rect(ring, cls):
    """Filled polygon from a ring of (x,y) points."""
    pts = " ".join(f"{_f(x)},{_f(y)}" for x, y in ring)
    return f'<polygon points="{pts}" class="{cls}"/>'


def svg_rect_c(cx, cy, half, cls):
    """Axis-aligned square centred on (cx,cy), side = 2*half (data units)."""
    return (f'<rect x="{_f(cx - half)}" y="{_f(cy - half)}" '
            f'width="{_f(2 * half)}" height="{_f(2 * half)}" class="{cls}"/>')


def svg_circle(cx, cy, r, cls):
    return f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(r)}" class="{cls}"/>'


def svg_tri(cx, cy, half, cls):
    """Upward triangle marker centred near (cx,cy)."""
    pts = f"{_f(cx)},{_f(cy - half)} {_f(cx - half)},{_f(cy + half)} {_f(cx + half)},{_f(cy + half)}"
    return f'<polygon points="{pts}" class="{cls}"/>'


def svg_text(x, y, s, cls):
    return f'<text x="{_f(x)}" y="{_f(y)}" class="{cls}">{_esc(s)}</text>'


def build_svg_groups(N, top, bot, via, lead, ext, pmap, capname, cu=None):
    """Build the per-group inner-SVG strings for the layer viewer.

    Returns a dict keyed by the SVG group id (fcu-copper, fcu-mesh, bcu-copper,
    bcu-mesh, vias, caps, ports, fet-leads, edge) -> concatenated element markup.
    """
    g = {k: [] for k in ("fcu-copper", "fcu-mesh", "bcu-copper", "bcu-mesh",
                          "vias", "caps", "ports", "fet-leads", "edge")}

    # --- real PCB copper overlay (optional), width-accurate tracks like the PNG
    if cu:
        for ring in cu["zones"]["B"]:
            g["bcu-copper"].append(svg_polyline_rect(ring, "zone bcu"))
        for ring in cu["zones"]["F"]:
            g["fcu-copper"].append(svg_polyline_rect(ring, "zone fcu"))
        for rect in _track_rects(cu["tracks"]["B"]):
            g["bcu-copper"].append(svg_polyline_rect(rect, "track bcu"))
        for rect in _track_rects(cu["tracks"]["F"]):
            g["fcu-copper"].append(svg_polyline_rect(rect, "track fcu"))
        for ring in cu["pads"]["B"]:
            g["bcu-copper"].append(svg_polyline_rect(ring, "pad bcu"))
        for ring in cu["pads"]["F"]:
            g["fcu-copper"].append(svg_polyline_rect(ring, "pad fcu"))
        for e in cu["edge"]:
            g["edge"].append(svg_line(e[0], e[1], e[2], e[3], "edge-line"))

    # --- mesh filaments (top = F.Cu, bot = B.Cu)
    for (xa, ya), (xb, yb) in top:
        g["fcu-mesh"].append(svg_line(xa, ya, xb, yb, "mesh fcu"))
    for (xa, ya), (xb, yb) in bot:
        g["bcu-mesh"].append(svg_line(xa, ya, xb, yb, "mesh bcu"))

    # --- caps (grey -||-) over their terminal nodes
    for p, cn in capname.items():
        if p in pmap and pmap[p][0] in N and pmap[p][1] in N:
            a, b = pmap[p]
            cg = cap_geometry(N[a], N[b])
            for x1, y1, x2, y2 in cg["plates"]:
                g["caps"].append(svg_line(x1, y1, x2, y2, "cap-plate"))
            for x1, y1, x2, y2 in cg["leads"]:
                g["caps"].append(svg_line(x1, y1, x2, y2, "cap-lead"))
            g["caps"].append(svg_text(cg["label"][0], cg["label"][1], cn, "cap-label"))

    # --- inter-layer via filament endpoints (red dots)
    vnodes = set()
    for x1, y1, x2, y2 in via:
        vnodes.add((round(x1, 4), round(y1, 4)))
        vnodes.add((round(x2, 4), round(y2, 4)))
    for x, y in sorted(vnodes):
        g["vias"].append(svg_circle(x, y, 0.28, "via-dot"))

    # --- FET-lead riser endpoints (white triangles)
    lnodes = set()
    for x1, y1, x2, y2 in lead:
        lnodes.add((round(x1, 4), round(y1, 4)))
        lnodes.add((round(x2, 4), round(y2, 4)))
    for x, y in sorted(lnodes):
        g["fet-leads"].append(svg_tri(x, y, 0.6, "lead-tri"))

    # --- port squares (de-duped external-terminal nodes)
    pnodes = set()
    for na, nb in ext:
        for n in (na, nb):
            if n in N:
                pnodes.add(n)
    for n in sorted(pnodes):
        x, y, _ = N[n]
        g["ports"].append(svg_rect_c(x, y, 0.55, "port-sq"))

    return g


HTML_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; height: 100vh; display: grid; grid-template-columns: 232px 1fr;
       font: 13px/1.35 ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
       color: #e8e8e8; background: #0a0a0a; }
aside { background: #141414; border-right: 1px solid #2a2a2a; padding: 14px; overflow: auto; }
h1 { font-size: 14px; margin: 0 0 12px; font-weight: 650; }
h2 { font-size: 11px; margin: 16px 0 6px; color: #9a9a9a; text-transform: uppercase; }
label { display: flex; align-items: center; gap: 8px; min-height: 26px; }
.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 2px; border: 1px solid rgba(255,255,255,.2); }
.meta { color: #8a8a8a; font-size: 11px; margin-top: 14px; }
.viewer { min-height: 0; overflow: hidden; background: #0a0a0a; cursor: grab; }
.viewer.dragging { cursor: grabbing; }
svg { width: 100%; height: 100%; display: block; background: #0a0a0a; }
.hidden { display: none !important; }
/* mesh + caps + edge use constant on-screen width regardless of zoom */
.mesh { fill: none; stroke-width: 1; vector-effect: non-scaling-stroke; opacity: .9; }
.mesh.fcu { stroke: #e07000; }
.mesh.bcu { stroke: #12b0bb; }
.zone { stroke: none; opacity: .16; }
.track { stroke: none; opacity: .3; }
.pad  { stroke: none; opacity: .4; }
.fcu  { fill: #e07000; }
.bcu  { fill: #12b0bb; }
.edge-line { stroke: #666; stroke-width: 1; vector-effect: non-scaling-stroke; fill: none; }
.cap-plate { stroke: #c8c8c8; stroke-width: 2; stroke-linecap: round; vector-effect: non-scaling-stroke; }
.cap-lead  { stroke: #c8c8c8; stroke-width: 1.2; vector-effect: non-scaling-stroke; }
.cap-label { fill: #c8c8c8; font-size: .9px; text-anchor: middle; dominant-baseline: middle; opacity: .85; }
.via-dot { fill: #ff2d2d; }
.lead-tri { fill: #ffffff; stroke: #000; stroke-width: .08; }
.port-sq { fill: #ffe000; stroke: #000; stroke-width: .08; }
"""

HTML_JS = """
const svg = document.querySelector("svg");
const viewer = document.querySelector(".viewer");
const vp = svg.viewBox.baseVal;
let vb = {x: vp.x, y: vp.y, w: vp.width, h: vp.height};
let drag = null;
function apply() { svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); }
function pt(evt) { const p = svg.createSVGPoint(); p.x = evt.clientX; p.y = evt.clientY;
  return p.matrixTransform(svg.getScreenCTM().inverse()); }
svg.addEventListener("wheel", evt => { evt.preventDefault(); const p = pt(evt);
  const f = evt.deltaY < 0 ? 0.88 : 1.14; vb.x = p.x - (p.x - vb.x) * f;
  vb.y = p.y - (p.y - vb.y) * f; vb.w *= f; vb.h *= f; apply(); }, {passive: false});
svg.addEventListener("pointerdown", evt => { drag = {x: evt.clientX, y: evt.clientY, vb: {...vb}};
  viewer.classList.add("dragging"); svg.setPointerCapture(evt.pointerId); });
svg.addEventListener("pointermove", evt => { if (!drag) return;
  const sx = vb.w / svg.clientWidth, sy = vb.h / svg.clientHeight;
  vb.x = drag.vb.x - (evt.clientX - drag.x) * sx; vb.y = drag.vb.y - (evt.clientY - drag.y) * sy; apply(); });
svg.addEventListener("pointerup", evt => { drag = null; viewer.classList.remove("dragging");
  svg.releasePointerCapture(evt.pointerId); });
// Each checkbox toggles style.display on one or more <g> groups (data-groups).
function sync() {
  document.querySelectorAll("input[data-groups]").forEach(cb => {
    cb.dataset.groups.split(",").forEach(id => {
      const el = document.getElementById(id);
      if (el) el.style.display = cb.checked ? "" : "none";
    });
  });
}
document.querySelectorAll("input[data-groups]").forEach(cb => cb.addEventListener("change", sync));
sync();
"""


def render_html(N, top, bot, via, lead, ext, pmap, capname, cu, title):
    groups = build_svg_groups(N, top, bot, via, lead, ext, pmap, capname, cu=cu)
    allx = [v[0] for v in N.values()]
    ally = [v[1] for v in N.values()]
    m = 2.0
    x0, y0 = min(allx) - m, min(ally) - m
    w, h = (max(allx) - min(allx)) + 2 * m, (max(ally) - min(ally)) + 2 * m
    viewbox = f"{_f(x0)} {_f(y0)} {_f(w)} {_f(h)}"
    # DOM order == paint order: edge + copper under mesh, markers on top.
    # Caps sit BEHIND the mesh in the mesh-only case and ON TOP when a real-copper
    # overlay is present -- matching the matplotlib cap_z (1 vs 7 vs mesh z 5/6).
    if cu:
        order = ["edge", "bcu-copper", "fcu-copper", "bcu-mesh", "fcu-mesh",
                 "caps", "vias", "fet-leads", "ports"]
    else:
        order = ["edge", "caps", "bcu-mesh", "fcu-mesh",
                 "bcu-copper", "fcu-copper", "vias", "fet-leads", "ports"]
    gsvg = "\n".join(f'<g id="{gid}">{"".join(groups[gid])}</g>' for gid in order)

    def chk(gid_list, sw, label, checked=True):
        c = " checked" if checked else ""
        s = f'<span class="swatch" style="background:{sw}"></span>' if sw else ""
        return (f'<label><input type="checkbox" data-groups="{",".join(gid_list)}"{c}>'
                f'{s}{_esc(label)}</label>')

    controls = "\n".join([
        "<h2>Layers</h2>",
        chk(["fcu-copper", "fcu-mesh"], "#e07000", "Top (F.Cu) mesh + copper"),
        chk(["bcu-copper", "bcu-mesh"], "#12b0bb", "Bottom (B.Cu) mesh + copper"),
        "<h2>Overlays</h2>",
        chk(["vias"], "#ff2d2d", "Vias (inter-layer)"),
        chk(["fet-leads"], "#ffffff", "FET-lead risers"),
        chk(["caps"], "#c8c8c8", "Caps (-||-)"),
        chk(["ports"], "#ffe000", "Ports"),
        chk(["edge"], "#666666", "Board edge"),
    ])
    counts = (f"F.Cu mesh {len(top)}, B.Cu mesh {len(bot)}, vias {len(via)}, "
              f"FET-lead risers {len(lead)}, ports {len(ext)}, caps {len(capname)}"
              + (", + real-copper overlay" if cu else ""))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{HTML_CSS}</style>
</head>
<body>
<aside>
  <h1>{_esc(title)}</h1>
  {controls}
  <div class="meta">{_esc(counts)}</div>
  <div class="meta">scroll = zoom, drag = pan</div>
</aside>
<div class="viewer">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">
    <g id="viewport">
      {gsvg}
    </g>
  </svg>
</div>
<script>{HTML_JS}</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp", nargs="?", default="model.inp")
    ap.add_argument("--ports", default=None, help="ports.json (default <inp>.ports.json)")
    ap.add_argument("--copper", default=None, help="copper.json from copper_dump.py -> real-PCB overlay")
    ap.add_argument("--zoom", nargs=4, type=float, default=[30, 64, 40, 74],
                    metavar=("X0", "X1", "Y0", "Y1"))
    ap.add_argument("--stem", default="fugu2_mesh")
    ap.add_argument("--html", default=None,
                    help="also emit a self-contained HTML layer-toggle viewer to this path")
    ap.add_argument("--no-png", action="store_true",
                    help="skip the matplotlib PNG output (HTML path is pure-Python)")
    args = ap.parse_args()

    N, segs, ext = parse_inp(args.inp)
    ports_json = args.ports or (args.inp + ".ports.json")
    ports = json.load(open(ports_json))["ports"]
    pmap = dict(zip(ports, ext))            # .external order == ports order
    capname = cap_names(ports_json)
    top, bot, via, lead = classify(N, segs)
    cu = json.load(open(args.copper)) if args.copper else None
    print(f"{args.inp}: {len(N)} nodes, F.Cu {len(top)}, B.Cu {len(bot)}, "
          f"vias {len(via)}, FET-lead risers {len(lead)}, ports {len(ext)}, caps {len(capname)}"
          f"{', + real-copper overlay' if cu else ''}")

    # --- self-contained HTML layer viewer (pure Python, no matplotlib) --------
    if args.html:
        title = f"Fugu2 commutation mesh — {args.inp}"
        with open(args.html, "w") as f:
            f.write(render_html(N, top, bot, via, lead, ext, pmap, capname, cu, title))
        print("wrote", args.html)

    if args.no_png:
        return

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    allx = [v[0] for v in N.values()]
    ally = [v[1] for v in N.values()]
    x0, x1, y0, y1 = args.zoom
    leg = ("orange=F.Cu teal=B.Cu red=vias ▲=FET-leads yellow=ports grey -||- =caps"
           + (" (faint fill=real PCB copper)" if cu else ""))
    for tag, xlim, ylim in [("full", (min(allx) - 2, max(allx) + 2), (max(ally) + 2, min(ally) - 2)),
                            ("zoom", (x0, x1), (y1, y0))]:
        fig, ax = plt.subplots(figsize=(14, 16))
        render(ax, N, top, bot, via, lead, ext, pmap, capname, xlim, ylim, cu=cu)
        ax.set_title(f"Fugu2 commutation mesh ({tag}) — {leg}", color="w", fontsize=9)
        fig.patch.set_facecolor("#000")
        fig.tight_layout()
        out = f"{args.stem}_{tag}.png"
        fig.savefig(out, dpi=115, facecolor="#000")
        print("wrote", out)


if __name__ == "__main__":
    main()
