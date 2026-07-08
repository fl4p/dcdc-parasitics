#!/usr/bin/env python3
"""Shared FastHenry `.inp` mesh parsing + layer/geometry helpers.

Single home for the small primitives that were duplicated across `mesh_viewer.py`
and `power_copper.py` (and now also feed `density.py`): the `.inp` parser, the
z-plane layer classifier, the width-accurate track->rectangle helper, the port->cap
name map, and the layer color convention.

`parse_mesh()` is the full parser (nodes + segments w/ `w`/`h` + `.equiv` + `.external`
+ `.default sigma`). `parse_inp()` is the back-compat 3-tuple `(N, segs, ext)` the
renderers already consume.
"""
import json
import math
import re
from typing import List, Optional, Tuple, TypedDict

# layer color convention (shared by every mesh renderer)
F_CU, B_CU = "#e07000", "#12b0bb"       # top / bottom copper
VIA, LEAD, PORT, CAP = "#ff2d2d", "#ffffff", "#ffe000", "#c8c8c8"

_NODE_RE = re.compile(r"(N\S+)\s+x=([-\d.]+)\s+y=([-\d.]+)\s+z=([-\d.]+)")
_SIGMA_RE = re.compile(r"sigma=([-\d.eE+]+)")


class Seg(TypedDict):
    name: str
    na: str
    nb: str
    w: Optional[float]
    h: Optional[float]


class Mesh(TypedDict):
    nodes: dict            # {name: (x, y, z)}
    segs: List[Seg]
    equivs: List[Tuple[str, str]]
    external: List[Tuple[str, str]]
    sigma: Optional[float]


def parse_mesh(path) -> Mesh:
    """Parse a FastHenry `.inp` into its full geometry.

    Returns a dict:
        nodes    {name: (x, y, z)}                       mm
        segs     [{name, na, nb, w, h}, ...]             w/h mm (None if absent)
        equivs   [(na, nb), ...]                         zero-impedance shorts
        external [(na, nb), ...]                         .external port node pairs
        sigma    float | None                            S/mm from `.default sigma=`
    """
    nodes: dict = {}
    segs: List[Seg] = []
    equivs: List[Tuple[str, str]] = []
    external: List[Tuple[str, str]] = []
    sigma: Optional[float] = None
    for ln in open(path):
        s = ln.strip()
        if not s or s[0] == "*":
            continue
        if s.startswith(".default"):
            m = _SIGMA_RE.search(s)
            if m:
                sigma = float(m.group(1))
            continue
        if s.startswith(".equiv"):
            t = s.split()
            if len(t) >= 3:
                equivs.append((t[1], t[2]))
            continue
        if s.startswith(".external"):
            t = s.split()
            if len(t) >= 3:
                external.append((t[1], t[2]))
            continue
        m = _NODE_RE.match(s)
        if m:
            nodes[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
            continue
        if s.startswith("E"):
            t = s.split()
            if len(t) >= 3 and t[1] in nodes and t[2] in nodes:
                w = h = None
                for tok in t[3:]:
                    if tok.startswith("w="):
                        w = float(tok[2:])
                    elif tok.startswith("h="):
                        h = float(tok[2:])
                segs.append(Seg(name=t[0], na=t[1], nb=t[2], w=w, h=h))
    return Mesh(nodes=nodes, segs=segs, equivs=equivs, external=external, sigma=sigma)


def parse_inp(path):
    """Back-compat parser: `(N, segs, ext)` with segs as `(na, nb)` endpoint pairs."""
    m = parse_mesh(path)
    return m["nodes"], [(s["na"], s["nb"]) for s in m["segs"]], m["external"]


def plane(z):
    """Classify a node z (mm) into a copper plane: top (F.Cu), bot (B.Cu), or the
    z~3 mm FET exposed-lead / die plane."""
    if abs(z) < 0.5:
        return "top"
    if abs(z + 1.6) < 0.5:
        return "bot"
    return "lead"


def track_rects(tracks):
    """Real-copper tracks -> width-accurate rectangles in DATA (mm) units. matplotlib
    linewidths are POINTS, which drew thin traces as fixed hairlines regardless of
    zoom; drawing the true copper width as a polygon scales correctly."""
    out = []
    for x1, y1, x2, y2, w in tracks:
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy)
        h = w / 2.0
        if L < 1e-9:                      # zero-length track -> small square
            out.append([(x1 - h, y1 - h), (x1 + h, y1 - h), (x1 + h, y1 + h), (x1 - h, y1 + h)])
            continue
        nx, ny = -dy / L * h, dx / L * h  # perpendicular offset = half-width
        out.append([(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)])
    return out


def draw_copper_underlay(ax, cu, key, color, alpha=(0.20, 0.35, 0.45), zbase=0):
    """Draw the real PCB copper (filled zones + width-accurate tracks + pads) for one
    layer `key` ("F"/"B") from a copper.json dict, faint and in `color`, as an underlay.
    Shared by mesh_viewer / power_copper / density so the overlay is defined once.
    `alpha` = (zones, tracks, pads)."""
    from matplotlib.collections import PolyCollection      # lazy: keep module import light
    from matplotlib.patches import Polygon
    az, at, ap_ = alpha
    for ring in cu["zones"][key]:
        ax.add_patch(Polygon(ring, closed=True, facecolor=color, edgecolor="none",
                             alpha=az, zorder=zbase))
    ax.add_collection(PolyCollection(track_rects(cu["tracks"][key]), facecolors=color,
                                     edgecolors="none", alpha=at, zorder=zbase))
    for ring in cu["pads"][key]:
        ax.add_patch(Polygon(ring, closed=True, facecolor=color, alpha=ap_,
                             edgecolor="none", zorder=zbase))


def cap_names(pj):
    """port label -> cap refdes, from a loaded `.ports.json` dict."""
    cin_ports = pj.get("cin_ports", [])
    cin_used = pj.get("cin_used", [])
    names = {lbl: cin_used[i] for i, lbl in enumerate(cin_ports) if i < len(cin_used)}
    for lbl in pj.get("ports", []):
        if lbl.startswith("P_cin_"):
            names[lbl] = lbl[len("P_cin_"):]
        elif lbl == "P_bulk":
            names[lbl] = "bulk"
    return names


def load_cap_names(ports_json_path):
    """Convenience: load a `.ports.json` file and return its port->cap name map."""
    return cap_names(json.load(open(ports_json_path)))
