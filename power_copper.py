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
import os, sys, json, math, argparse
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from mesh_geom import (F_CU, B_CU, VIA, LEAD, PORT, CAP,  # noqa: E402
                       parse_inp, plane, load_cap_names, draw_copper_underlay)


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


def cap_symbol(ax, p1, p2, name, z=7):
    """Subtle grey -||- capacitor symbol straddling terminal nodes p1,p2."""
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy)
    ux, uy = (1.0, 0.0) if L < 1e-3 else (dx / L, dy / L)
    vx, vy = -uy, ux
    gap, plate, leadlen = 0.3, 0.8, 0.7
    for sgn in (-1, 1):
        cx, cy = mx + ux * gap * sgn, my + uy * gap * sgn
        ax.plot([cx - vx * plate, cx + vx * plate], [cy - vy * plate, cy + vy * plate],
                color=CAP, lw=1.6, zorder=z, solid_capstyle="round")
        ax.plot([cx, cx + ux * leadlen * sgn], [cy, cy + uy * leadlen * sgn],
                color=CAP, lw=1.0, zorder=z)
    ax.annotate(name, (mx + vx * plate * 1.8, my + vy * plate * 1.8), color=CAP,
                fontsize=6.5, ha="center", va="center", zorder=z, alpha=0.85)


def draw_copper(ax, cu):
    """Faint filled real PCB copper (zones/tracks/pads) UNDER the mesh (B behind F)."""
    draw_copper_underlay(ax, cu, "B", B_CU, alpha=(0.16, 0.3, 0.38), zbase=1)
    draw_copper_underlay(ax, cu, "F", F_CU, alpha=(0.16, 0.3, 0.38), zbase=2)
    for e in cu["edge"]:
        ax.plot([e[0], e[2]], [e[1], e[3]], color="#666", lw=0.8, zorder=0)


def render(ax, N, top, bot, via, lead, ext, pmap, capname, xlim, ylim, cu=None):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp", nargs="?", default="model.inp")
    ap.add_argument("--ports", default=None, help="ports.json (default <inp>.ports.json)")
    ap.add_argument("--copper", default=None, help="copper.json from copper_dump.py -> real-PCB overlay")
    ap.add_argument("--zoom", nargs=4, type=float, default=[30, 64, 40, 74],
                    metavar=("X0", "X1", "Y0", "Y1"))
    ap.add_argument("--stem", default="fugu2_mesh")
    args = ap.parse_args()

    N, segs, ext = parse_inp(args.inp)
    ports_json = args.ports or (args.inp + ".ports.json")
    ports = json.load(open(ports_json))["ports"]
    pmap = dict(zip(ports, ext))            # .external order == ports order
    capname = load_cap_names(ports_json)
    top, bot, via, lead = classify(N, segs)
    cu = json.load(open(args.copper)) if args.copper else None
    print(f"{args.inp}: {len(N)} nodes, F.Cu {len(top)}, B.Cu {len(bot)}, "
          f"vias {len(via)}, FET-lead risers {len(lead)}, ports {len(ext)}, caps {len(capname)}"
          f"{', + real-copper overlay' if cu else ''}")

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
