#!/usr/bin/env python3
"""Render the extracted half-bridge parasitics as a standalone SVG schematic.

Dependency-free (stdlib only). Draws a 1:1 picture of the network emitted by
`emit.py`'s `.SUBCKT pwrstage`: the commutation loop (Cin -> HS -> SW -> LS ->
GND) as a rectangle, each parasitic inductor as a coil labelled with its nH/mOhm
value, and the two **common-source source-lead inductors in red** — the branch
each gate loop shares with the power loop.

Input is the same reduced-parasitics dict `p` used by emit.py (fields: L_loop,
R_loop, L_gate_hs/ls, R_gate_hs/ls, csi_hs, csi_ls, m_gate, freq_Hz, topo, meta).
The cap bank reflects what the model actually used: one solid Cin closing the
loop (the port sits across the nearest cap) and, if the board has more caps
bridging Vin<->GND, a greyed note that they are present but NOT in the modeled
loop — keeping the single-cap heuristic honest.

    emit_svg.py parasitics.json > schematic.svg      # fast path, no re-solve
"""
import html
import json
import os

# ---- palette -------------------------------------------------------------
INK = "#1a1a1a"
MUTE = "#8a8a8a"
CSI = "#d02020"        # shared source-lead (common-source inductance)
WIRE = "#333333"
LOOPFILL = "#eef3fb"   # faint fill under the commutation loop rectangle
CAPCOL = "#1f6feb"
FONT = "font-family='ui-sans-serif,Helvetica,Arial,sans-serif'"


def _esc(s):
    return html.escape(str(s), quote=True)


def _txt(x, y, s, size: float = 12, col=INK, anchor="start", weight="normal", ital=False):
    st = f"italic" if ital else "normal"
    return (f"<text x='{x:.1f}' y='{y:.1f}' {FONT} font-size='{size}' "
            f"fill='{col}' text-anchor='{anchor}' font-weight='{weight}' "
            f"font-style='{st}'>{_esc(s)}</text>")


def _line(x1, y1, x2, y2, col=WIRE, w=1.8, dash=None):
    d = f" stroke-dasharray='{dash}'" if dash else ""
    return (f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' "
            f"stroke='{col}' stroke-width='{w}'{d}/>")


def _dot(x, y, col=WIRE, r=3.2):
    return f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{r}' fill='{col}'/>"


def _coil_v(cx, y0, y1, col=WIRE, w=2.0, n=4):
    """Vertical inductor from (cx,y0) to (cx,y1), bumps bulging right."""
    h = (y1 - y0) / n
    r = h / 2.0
    d = [f"M {cx:.1f} {y0:.1f}"]
    for _ in range(n):
        d.append(f"a {r:.1f} {r:.1f} 0 0 1 0 {h:.1f}")
    return (f"<path d='{' '.join(d)}' fill='none' stroke='{col}' "
            f"stroke-width='{w}'/>")


def _coil_h(cy, x0, x1, col=WIRE, w=2.0, n=4):
    """Horizontal inductor from (x0,cy) to (x1,cy), bumps bulging up."""
    wd = (x1 - x0) / n
    r = wd / 2.0
    d = [f"M {x0:.1f} {cy:.1f}"]
    for _ in range(n):
        d.append(f"a {r:.1f} {r:.1f} 0 0 1 {wd:.1f} 0")
    return (f"<path d='{' '.join(d)}' fill='none' stroke='{col}' "
            f"stroke-width='{w}'/>")


def _cap(cx, cy, col=CAPCOL, w=2.4, gap=7, half=15):
    """Vertical capacitor centred at (cx,cy): two horizontal plates."""
    return (_line(cx - half, cy - gap, cx + half, cy - gap, col, w) +
            _line(cx - half, cy + gap, cx + half, cy + gap, col, w))


def _nfet(cx, cy, ref, col=INK):
    """N-channel MOSFET, current vertical (drain top, source bottom), gate right.
    Returns (svg, drain_xy, source_xy, gate_xy). Includes the intrinsic body
    diode on the left (where Qrr lives)."""
    s = []
    dx = cx           # drain/source terminal x
    gx = cx + 46      # gate terminal x
    # gate bar + lead
    s.append(_line(gx, cy, cx + 18, cy, col, 1.8))
    s.append(_line(cx + 18, cy - 16, cx + 18, cy + 16, col, 2.2))
    # channel: three broken segments
    s.append(_line(cx + 8, cy - 16, cx + 8, cy - 8, col, 2.2))
    s.append(_line(cx + 8, cy - 4, cx + 8, cy + 4, col, 2.2))
    s.append(_line(cx + 8, cy + 8, cx + 8, cy + 16, col, 2.2))
    # drain lead (top) and source lead (bottom)
    s.append(_line(cx + 8, cy - 12, cx, cy - 12, col, 1.8))
    s.append(_line(dx, cy - 12, dx, cy - 34, col, 1.8))
    s.append(_line(cx + 8, cy + 12, cx, cy + 12, col, 1.8))
    s.append(_line(dx, cy + 12, dx, cy + 34, col, 1.8))
    # gate->channel connections at mid
    s.append(_line(cx + 18, cy, cx + 8, cy, col, 1.8))
    # source arrow (N-channel: points toward channel)
    s.append(f"<path d='M {cx+8:.1f} {cy+12:.1f} l 7 -3 l 0 6 z' fill='{col}'/>")
    # intrinsic body diode on the left: anode=source(bottom), cathode=drain(top)
    bx = cx - 20
    s.append(_line(bx, cy - 30, bx, cy + 30, MUTE, 1.4))
    s.append(_line(bx, cy - 30, dx, cy - 30, MUTE, 1.4))      # cathode -> drain
    s.append(_line(bx, cy + 30, dx, cy + 30, MUTE, 1.4))      # anode -> source
    s.append(f"<path d='M {bx-6:.1f} {cy+4:.1f} l 12 0 l -6 -12 z' "
             f"fill='none' stroke='{MUTE}' stroke-width='1.4'/>")  # triangle up
    s.append(_line(bx - 6, cy - 8, bx + 6, cy - 8, MUTE, 1.4))     # cathode bar
    s.append(_txt(gx + 2, cy + 18, ref, 13, INK, "start", "bold"))
    s.append(_txt(bx - 10, cy + 46, "body diode", 9.5, MUTE, "middle", ital=True))
    return "".join(s), (dx, cy - 34), (dx, cy + 34), (gx, cy)


def _fmtL(v):
    return f"{v*1e9:.2f} nH"


def _fmtR(v):
    return f"{v*1e3:.1f} mΩ"


def _leaf(net):
    """Leaf net name — drop the KiCad sheet path (/DC/DC/SW_NODE -> SW_NODE)."""
    return (net or "").strip().split("/")[-1]


def schematic(p):
    t = p["topo"]
    m = p.get("meta", {})
    csi_hs = max(p.get("csi_hs", 0.0), 0.0)
    csi_ls = max(p.get("csi_ls", 0.0), 0.0)
    rest = max(p["L_loop"] - csi_hs - csi_ls, 0.0)
    loop_hs = loop_ls = rest / 2.0

    W, H = 860, 660
    xc = 380          # FET column
    xcap = 200        # Cin bank (near rail); parallel legs grow left of here
    xdrv = 640        # gate drivers
    y_vin, y_gnd = 108, 560

    # vertical y-plan (see module notes)
    y_lh0, y_lh1 = 122, 168
    cy_hs = 202
    y_nhs = 262
    y_lsh0, y_lsh1 = 274, 320
    y_sw = 340
    y_lsl0, y_lsl1 = 352, 398
    cy_ls = 432
    y_nls = 492
    y_lsl_cs0, y_lsl_cs1 = 504, 542

    s = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' height='{H}' "
         f"viewBox='0 0 {W} {H}'>",
         f"<rect width='{W}' height='{H}' fill='white'/>"]

    # ---- input-cap bank: which caps the model actually ported (cin_used) as
    # parallel legs, the rest greyed (present on the board, not in the loop) ----
    cy_cap = (y_vin + y_gnd) / 2
    cin_all = t.get("cin", []) or []
    cin_used = t.get("cin_used") or (cin_all[:1] if cin_all else [])
    n_used = int(p.get("n_cin", len(cin_used) or 1))
    split = p.get("current_split") or {}
    others = [c for c in cin_all if c not in set(cin_used)]

    disp = cin_used[:4]                       # draw up to 4 legs discretely
    collapsed = max(len(cin_used) - len(disp), 0)
    pitch_cap = 34
    used_xs = [xcap - k * pitch_cap for k in range(len(disp))]
    x_leg_min = used_xs[-1] if used_xs else xcap
    # greyed column further out when few legs, so its label clears the modeled leg
    gx = x_leg_min - (46 + max(0, 4 - len(disp)) * 12) if others else x_leg_min
    x_left = gx - 30

    # faint commutation-loop rectangle (Cin - HS - SW - LS - GND)
    s.append(f"<rect x='{x_left}' y='{y_vin}' width='{xc-x_left}' "
             f"height='{y_gnd-y_vin}' fill='{LOOPFILL}' stroke='none'/>")
    s.append(_txt((xcap + xc) / 2, 196, "commutation loop", 12, "#9fb4d6",
                  "middle", ital=True))
    s.append(_txt((xcap + xc) / 2, 210, "(Cin→HS→SW→LS→GND)", 10.5, "#9fb4d6",
                  "middle", ital=True))

    # ---- rails (extend left to the outermost cap) ----
    s.append(_line(x_left, y_vin, xdrv+60, y_vin, WIRE, 2.6))
    s.append(_line(x_left, y_gnd, xdrv+60, y_gnd, WIRE, 2.6))
    def _rail(word, net):
        net = _leaf(net)
        return word if not net or net.upper() == word else f"{word}  {net}"
    s.append(_txt(xdrv+64, y_vin+4, _rail("VIN", t.get("vin")), 12, INK, "start", "bold"))
    s.append(_txt(xdrv+64, y_gnd+4, _rail("GND", t.get("gnd")), 12, INK, "start", "bold"))

    # ported caps: parallel legs closing the loop
    for k, ref in enumerate(disp):
        cxk = used_xs[k]
        s.append(_line(cxk, y_vin, cxk, cy_cap - 7, WIRE, 1.6))
        s.append(_line(cxk, cy_cap + 7, cxk, y_gnd, WIRE, 1.6))
        s.append(_cap(cxk, cy_cap))
        s.append(_txt(cxk, cy_cap - 12, ref, 10.5, CAPCOL, "middle", "bold"))
        sh = split.get(ref)
        if n_used > 1 and sh:
            s.append(_txt(cxk, cy_cap + 22, f"{sh['mag']*100:.0f}%", 9.5, CAPCOL, "middle"))
    if n_used <= 1:
        s.append(_txt(xcap, cy_cap + 22, "nearest (in loop)", 9.5, CAPCOL, "middle"))
    else:
        tag = f"{n_used} caps ∥" + (f" (+{collapsed} more ported)" if collapsed else "")
        s.append(_txt((used_xs[0]+used_xs[-1])/2, cy_cap + 36, tag, 9.5, CAPCOL, "middle", ital=True))
        s.append(_txt((used_xs[0]+used_xs[-1])/2, cy_cap + 48, "share = current split", 9,
                      MUTE, "middle", ital=True))

    # greyed caps present on the board but NOT in the modeled loop
    if others:
        lbl = ", ".join(others) if len(others) <= 4 else f"{len(others)} more caps"
        s.append(_line(gx, y_vin, gx, cy_cap - 7, MUTE, 1.2, dash="4 3"))
        s.append(_line(gx, cy_cap + 7, gx, y_gnd, MUTE, 1.2, dash="4 3"))
        s.append(_cap(gx, cy_cap, MUTE, 1.6))
        s.append(_txt(gx, cy_cap - 12, f"+{lbl}", 9.5, MUTE, "middle"))
        s.append(_txt(gx, cy_cap + 20, "not in", 9, MUTE, "middle", ital=True))
        s.append(_txt(gx, cy_cap + 31, "modeled loop", 9, MUTE, "middle", ital=True))

    # ================= HS side =================
    s.append(_line(xc, y_vin, xc, y_lh0, WIRE))
    s.append(_coil_v(xc, y_lh0, y_lh1))
    s.append(_txt(xc - 14, (y_lh0+y_lh1)/2 - 2, "Lloop_hs", 11.5, INK, "end", "bold"))
    s.append(_txt(xc - 14, (y_lh0+y_lh1)/2 + 12, f"{_fmtL(loop_hs)}", 11, INK, "end"))
    fet_hs, d_hs, sc_hs, g_hs = _nfet(xc, cy_hs, (t["hs"]["refs"] or ["Q_HS"])[0])
    s.append(_line(xc, y_lh1, d_hs[0], d_hs[1], WIRE))   # coil -> drain
    s.append(fet_hs)
    # source -> nHS node
    s.append(_line(sc_hs[0], sc_hs[1], xc, y_nhs, WIRE))
    s.append(_dot(xc, y_nhs))
    s.append(_txt(xc - 12, y_nhs - 4, "nHS", 10.5, INK, "end", ital=True))
    s.append(_txt(xc - 12, y_nhs + 9, "(HS die-src)", 9, MUTE, "end", ital=True))
    # Lscs_hs (CSI, red) nHS -> SW
    s.append(_coil_v(xc, y_lsh0, y_lsh1, CSI, 2.4))
    s.append(_line(xc, y_nhs, xc, y_lsh0, WIRE))
    s.append(_txt(xc - 14, (y_lsh0+y_lsh1)/2 - 2, "Lscs_hs", 11.5, CSI, "end", "bold"))
    s.append(_txt(xc - 14, (y_lsh0+y_lsh1)/2 + 12, f"{_fmtL(csi_hs)}", 11, CSI, "end"))
    # SW node
    s.append(_line(xc, y_lsh1, xc, y_sw, WIRE))
    s.append(_dot(xc, y_sw, CSI))
    s.append(_txt(xc - 150, y_sw + 4, _leaf(t.get("sw", "")), 12, INK, "start", "bold"))
    s.append(_line(xc - 18, y_sw, xc, y_sw, WIRE))

    # ================= LS side =================
    s.append(_line(xc, y_sw, xc, y_lsl0, WIRE))
    s.append(_coil_v(xc, y_lsl0, y_lsl1))
    s.append(_txt(xc - 14, (y_lsl0+y_lsl1)/2 - 2, "Lloop_ls", 11.5, INK, "end", "bold"))
    s.append(_txt(xc - 14, (y_lsl0+y_lsl1)/2 + 12, f"{_fmtL(loop_ls)}", 11, INK, "end"))
    fet_ls, d_ls, sc_ls, g_ls = _nfet(xc, cy_ls, (t["ls"]["refs"] or ["Q_LS"])[0])
    s.append(_line(xc, y_lsl1, d_ls[0], d_ls[1], WIRE))
    s.append(fet_ls)
    s.append(_line(sc_ls[0], sc_ls[1], xc, y_nls, WIRE))
    s.append(_dot(xc, y_nls))
    s.append(_txt(xc - 12, y_nls - 4, "nLS", 10.5, INK, "end", ital=True))
    s.append(_txt(xc - 12, y_nls + 9, "(LS die-src)", 9, MUTE, "end", ital=True))
    s.append(_coil_v(xc, y_lsl_cs0, y_lsl_cs1, CSI, 2.4))
    s.append(_line(xc, y_nls, xc, y_lsl_cs0, WIRE))
    s.append(_txt(xc - 14, (y_lsl_cs0+y_lsl_cs1)/2 - 2, "Lscs_ls", 11.5, CSI, "end", "bold"))
    s.append(_txt(xc - 14, (y_lsl_cs0+y_lsl_cs1)/2 + 12, f"{_fmtL(csi_ls)}", 11, CSI, "end"))
    s.append(_line(xc, y_lsl_cs1, xc, y_gnd, WIRE))

    # ================= gate drivers =================
    # The gate-return node sets whether CSI is in the gate loop:
    #   non-Kelvin -> return to the power-source node (SW / GND), BELOW Lscs, so the
    #                 return current shares the source lead (CSI in loop).
    #   Kelvin     -> return to the die-source tap (nHS / nLS), ABOVE Lscs (excluded).
    def driver(cy, gxy, die_y, src_y, l_gate, r_gate, kelvin, tag):
        out = []
        gx = gxy[0]
        ret_y = die_y if kelvin else src_y
        # driver box
        bx, by, bw, bh = xdrv, cy - 22, 74, 44
        out.append(f"<rect x='{bx}' y='{by}' width='{bw}' height='{bh}' rx='4' "
                   f"fill='#fbfbf7' stroke='{INK}' stroke-width='1.6'/>")
        out.append(_txt(bx + bw/2, cy - 4, tag, 11, INK, "middle", "bold"))
        out.append(_txt(bx + bw/2, cy + 11, "driver", 10, MUTE, "middle"))
        # gate drive: one straight horizontal run at the gate level (cy) — FET gate
        # -> Lghs coil -> driver output pin on the box's left edge (no jog).
        out.append(_line(gx, cy, gx + 8, cy, WIRE))
        out.append(_coil_h(cy, gx + 8, gx + 28, WIRE))
        out.append(_line(gx + 28, cy, bx, cy, WIRE))
        out.append(_txt((gx + 8 + gx + 28) / 2, cy - 8, tag.split()[0] + " gate", 9.5,
                        INK, "middle"))
        out.append(_txt(gx + 34, cy + 24, f"L={_fmtL(l_gate)}", 9.5, INK, "start"))
        out.append(_txt(gx + 34, cy + 36, f"R={_fmtR(r_gate)}", 9.5, INK, "start"))
        # return leg exits the box BOTTOM edge (clear of the block) and drops to the
        # return node (solid = shares CSI / non-Kelvin, grey-dashed = Kelvin tap).
        rpx = bx + bw - 16
        dash = "5 3" if kelvin else None
        out.append(_line(rpx, by + bh, rpx, ret_y, WIRE, 1.6, dash=dash))
        out.append(_line(rpx, ret_y, xc, ret_y, WIRE, 1.6, dash=dash))
        return "".join(out)

    s.append(driver(cy_hs, g_hs, y_nhs, y_sw, p["L_gate_hs"],
                    p["R_gate_hs"], t["hs"]["kelvin"], "HS gate"))
    s.append(driver(cy_ls, g_ls, y_nls, y_gnd, p["L_gate_ls"],
                    p["R_gate_ls"], t["ls"]["kelvin"], "LS gate"))

    # ================= title + legend =================
    board = os.path.basename(t.get("pcb", "") or "")
    s.append(_txt(20, 26, f"Power-stage parasitics — {board}", 15, INK,
                  "start", "bold"))
    hs_kv = "Kelvin" if t["hs"]["kelvin"] else "non-Kelvin"
    ls_kv = "Kelvin" if t["ls"]["kelvin"] else "non-Kelvin"
    sub = (f"HS {','.join(t['hs']['refs'])} ({hs_kv})   "
           f"LS {','.join(t['ls']['refs'])} ({ls_kv})   "
           f"{p['freq_Hz']:.2g} Hz plateau, {m.get('pitch','?')} mm mesh, "
           f"{m.get('lead_mm','?')} mm lead")
    s.append(_txt(20, 44, sub, 11, MUTE, "start"))
    if int(p.get("n_cin", 1)) > 1:
        loop_txt = (f"Commutation loop L = {_fmtL(p['L_loop'])} "
                    f"({int(p['n_cin'])} caps ∥;  single-cap bound {_fmtL(p['L_loop_single'])})"
                    f"   R = {_fmtR(p.get('R_loop',0))}")
    else:
        loop_txt = f"Commutation loop L = {_fmtL(p['L_loop'])}  (R = {_fmtR(p.get('R_loop',0))})"
    s.append(_txt(20, 62, loop_txt, 12, INK, "start", "bold"))

    # legend (bottom-left)
    ly = H - 46
    s.append(_line(24, ly, 52, ly, CSI, 2.4))
    s.append(_txt(58, ly + 4,
                  "common-source inductance (source lead shared by power + gate loop)",
                  11, CSI, "start"))
    s.append(_line(24, ly + 18, 52, ly + 18, WIRE, 2.0))
    s.append(_txt(58, ly + 22,
                  "commutation-loop / gate-loop inductance   ·   Lgate label shows total "
                  "(incl. shared Lscs)", 11, INK, "start"))
    s.append("</svg>")
    return "\n".join(s) + "\n"


def emit_svg(p, path):
    with open(path, "w") as f:
        f.write(schematic(p))


if __name__ == "__main__":
    import sys
    p = json.load(open(sys.argv[1]))
    sys.stdout.write(schematic(p))
