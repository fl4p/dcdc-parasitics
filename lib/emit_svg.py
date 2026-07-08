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
import re

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


def _res_v(cx, y0, y1, col=WIRE, w=1.8, half_w=5.0, body=0.62):
    """Vertical resistor from (cx,y0) to (cx,y1): IEC/EU rectangle body with
    leads. `body` is the fraction of the span occupied by the box."""
    bl = (y1 - y0) * body
    ya = (y0 + y1) / 2 - bl / 2
    yb = ya + bl
    return (_line(cx, y0, cx, ya, col, w) + _line(cx, yb, cx, y1, col, w) +
            f"<rect x='{cx-half_w:.1f}' y='{ya:.1f}' width='{2*half_w:.1f}' "
            f"height='{bl:.1f}' fill='white' stroke='{col}' stroke-width='{w}'/>")


def _res_h(cy, x0, x1, col=WIRE, w=1.8, half_h=5.0, body=0.55):
    """Horizontal resistor from (x0,cy) to (x1,cy): IEC/EU rectangle body + leads."""
    bl = (x1 - x0) * body
    xa = (x0 + x1) / 2 - bl / 2
    xb = xa + bl
    return (_line(x0, cy, xa, cy, col, w) + _line(xb, cy, x1, cy, col, w) +
            f"<rect x='{xa:.1f}' y='{cy-half_h:.1f}' width='{bl:.1f}' "
            f"height='{2*half_h:.1f}' fill='white' stroke='{col}' stroke-width='{w}'/>")


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
    s.append(_txt(cx + 8, cy + 44, _clip(ref, 16), 13, INK, "left", "bold"))
    s.append(_txt(bx - 22, cy + 46, "body diode", 9.5, MUTE, "middle", ital=True))
    return "".join(s), (dx, cy - 34), (dx, cy + 34), (gx, cy)


def _fmtL(v):
    return f"{v*1e9:.2f} nH"


def _fmtR(v):
    return f"{v*1e3:.1f} mΩ"


def _fmtL_na(v):
    """Inductance label, or 'n/a' when the value is None — a gate port was
    unavailable (routing dropped, --allow-missing-gate-ports), which is NOT a
    measured zero. Keeps the schematic from drawing a fabricated 0.00 nH."""
    return "n/a" if _num(v) is None else _fmtL(_num(v))


def _fmtR_na(v):
    return "n/a" if _num(v) is None else _fmtR(_num(v))


def _leaf(net):
    """Leaf net name — drop the KiCad sheet path (/DC/DC/SW_NODE -> SW_NODE)."""
    return (net or "").strip().split("/")[-1]


def _rkm(v):
    """Decode a resistor value to a clean ohm string: RKM (3R3 -> '3.3 Ω',
    4K7 -> '4.7 kΩ') or a plain number ('4.7, 1%' -> '4.7 Ω'). Drops a trailing
    tolerance field; passes anything unrecognized through unchanged."""
    v = (v or "").strip().split(",")[0].strip()
    m = re.fullmatch(r"(\d*)[Rr](\d*)", v)
    if m:
        a, b = m.group(1) or "0", m.group(2)
        return f"{a}.{b} Ω" if b else f"{a} Ω"
    m = re.fullmatch(r"(\d*)[kK](\d*)", v)
    if m:
        a, b = m.group(1) or "0", m.group(2)
        return f"{a}.{b} kΩ" if b else f"{a} kΩ"
    if re.fullmatch(r"\d+(\.\d+)?", v):
        return f"{v} Ω"
    return v


def _num(v):
    """Coerce to float, or None if absent/null/non-numeric. Robust against the
    JSON schema's `null`-for-not-computed convention on the standalone path."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clip(s, n):
    """Truncate an over-long label so it can't run off the fixed-width canvas."""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _parallel_device_rows(p):
    pdev = p.get("parallel_devices") or {}
    rows = []
    for role, side in (("hs", "HS"), ("ls", "LS")):
        for d in pdev.get(role) or []:
            rows.append((side, d.get("ref", "?"),
                         _num(d.get("L_gate")) or 0.0,
                         _num(d.get("csi")) or 0.0))
    return rows


def schematic(p):
    t = p["topo"]
    m = p.get("meta", {})
    csi_hs = max(_num(p.get("csi_hs")) or 0.0, 0.0)
    csi_ls = max(_num(p.get("csi_ls")) or 0.0, 0.0)
    L_loop = _num(p.get("L_loop")) or 0.0
    rest = max(L_loop - csi_hs - csi_ls, 0.0)
    loop_hs = loop_ls = rest / 2.0
    # ring R_loop is split across the two loop branches by the real conduction
    # proportion r_hs:r_ls (not 50/50), matching emit.py's subckt; the LF per-switch
    # conduction R (r_hs/r_ls) is a distinct, near-DC number shown alongside.
    r_hs, r_ls = _num(p.get("r_hs")), _num(p.get("r_ls"))
    R_loop = max(_num(p.get("R_loop")) or 0.0, 0.0)          # floor-clamp like the L side
    # one None-check drives both the frac split and the title "Two R" line, so the
    # two guards can't drift apart (and r_hs/r_ls narrow to float inside).
    if r_hs is not None and r_ls is not None:
        have_rsplit = True
        frac_hs = min(1.0, max(0.0, r_hs / (r_hs + r_ls))) if r_hs + r_ls > 0 else 0.5
    else:
        have_rsplit = False
        frac_hs = 0.5
    rser_hs = R_loop * frac_hs
    rser_ls = R_loop * (1.0 - frac_hs)

    W, H = 860, 692
    xc = 380          # FET column
    xcap = 200        # Cin bank (near rail); parallel legs grow left of here
    xdrv = 640        # gate drivers
    y_vin, y_gnd = 108, 592

    # vertical y-plan (see module notes). A resistor band carries each side's share
    # of the lumped ring R_loop: y_rl0..y_rl1 on HS (VIN→Lloop_hs), y_rls0..y_rls1 on
    # LS (SW→Lloop_ls) — symmetric, matching emit.py's Rser on Lloop_hs / Lloop_ls.
    y_rl0, y_rl1 = 116, 146
    y_lh0, y_lh1 = 150, 192
    cy_hs = 226
    y_nhs = 284
    y_lsh0, y_lsh1 = 294, 332
    y_sw = 350
    y_rls0, y_rls1 = 358, 388
    y_lsl0, y_lsl1 = 392, 434
    cy_ls = 468
    y_nls = 526
    y_lsl_cs0, y_lsl_cs1 = 536, 574

    s = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' height='{H}' "
         f"viewBox='0 0 {W} {H}'>",
         f"<rect width='{W}' height='{H}' fill='white'/>"]

    # ---- input-cap bank: which caps the model actually ported (cin_used) as
    # parallel legs, the rest greyed (present on the board, not in the loop) ----
    cy_cap = (y_vin + y_gnd) / 2
    cin_all = t.get("cin", []) or []
    cin_used = t.get("cin_used") or (cin_all[:1] if cin_all else [])
    _ncin = _num(p.get("n_cin"))
    n_used = int(_ncin) if _ncin else (len(cin_used) or 1)
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
        return word if not net or net.upper() == word else f"{word}  {_clip(net, 16)}"
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
    if not disp:
        s.append(_txt(xcap, cy_cap + 22, "(no Cin in model)", 9.5, MUTE, "middle", ital=True))
    elif n_used <= 1:
        s.append(_txt(xcap, cy_cap + 22, "nearest (in loop)", 9.5, CAPCOL, "middle"))
    else:
        tag = f"{n_used} caps ∥" + (f" (+{collapsed} more ported)" if collapsed else "")
        s.append(_txt((used_xs[0]+used_xs[-1])/2, cy_cap + 48, tag, 9.5, CAPCOL, "middle", ital=True))
        s.append(_txt((used_xs[0]+used_xs[-1])/2, cy_cap + 61, "share = current split", 9,
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
    # commutation-loop copper R — the HF *ring* R_loop, split across the two loop
    # branches by the real r_hs:r_ls conduction proportion (this is the HS share).
    s.append(_line(xc, y_vin, xc, y_rl0, WIRE))
    s.append(_res_v(xc, y_rl0, y_rl1))
    s.append(_txt(xc - 14, (y_rl0+y_rl1)/2 - 2, "R_loop·hs", 11.5, INK, "end", "bold"))
    s.append(_txt(xc - 14, (y_rl0+y_rl1)/2 + 12, f"{_fmtR(rser_hs)}", 11, INK, "end"))
    s.append(_txt(xc - 14, (y_rl0+y_rl1)/2 + 24,
                  f"ring R_loop {_fmtR(R_loop)}, r_hs:r_ls split", 8.5,
                  MUTE, "end", ital=True))
    s.append(_line(xc, y_rl1, xc, y_lh0, WIRE))
    s.append(_coil_v(xc, y_lh0, y_lh1))
    s.append(_txt(xc - 14, (y_lh0+y_lh1)/2 - 2, "Lloop_hs", 11.5, INK, "end", "bold"))
    s.append(_txt(xc - 14, (y_lh0+y_lh1)/2 + 12, f"{_fmtL(loop_hs)}", 11, INK, "end"))
    fet_hs, d_hs, sc_hs, g_hs = _nfet(xc, cy_hs, "∥".join(t["hs"]["refs"]) or "Q_HS")
    s.append(_line(xc, y_lh1, d_hs[0], d_hs[1], WIRE))   # coil -> drain
    s.append(fet_hs)
    # source -> nHS node
    s.append(_line(sc_hs[0], sc_hs[1], xc, y_nhs, WIRE))
    s.append(_dot(xc, y_nhs))
    # die-source node = exposed HSKEL pin (0 Ω Rhskel in the subckt); Kelvin gate
    # return taps here, non-Kelvin taps SW below Lscs_hs.
    s.append(_txt(xc - 12, y_nhs - 4, "nHS · HSKEL", 10.5, INK, "end", ital=True))
    s.append(_txt(xc - 12, y_nhs + 9, "(HS die-src pin)", 9, MUTE, "end", ital=True))
    # Lscs_hs (CSI, red) nHS -> SW
    s.append(_coil_v(xc, y_lsh0, y_lsh1, CSI, 2.4))
    s.append(_line(xc, y_nhs, xc, y_lsh0, WIRE))
    s.append(_txt(xc - 14, (y_lsh0+y_lsh1)/2 - 2, "Lscs_hs", 11.5, CSI, "end", "bold"))
    s.append(_txt(xc - 14, (y_lsh0+y_lsh1)/2 + 12, f"{_fmtL_na(p.get('csi_hs'))}", 11, CSI, "end"))
    # SW node
    s.append(_line(xc, y_lsh1, xc, y_sw, WIRE))
    s.append(_dot(xc, y_sw, CSI))
    s.append(_line(xc - 18, y_sw, xc, y_sw, WIRE))
    s.append(_txt(xc - 24, y_sw + 4, _leaf(t.get("sw", "")), 12, INK, "end", "bold"))

    # ================= LS side =================
    # ring R_loop LS share — a real resistor symbol symmetric to the HS side (both
    # are first-class Rser on Lloop_hs/Lloop_ls in emit.py's subckt). rser_ls is
    # often the LARGER half (frac_hs<0.5 at any duty below 50%), so it must be drawn
    # as a component, not demoted to a caption.
    s.append(_line(xc, y_sw, xc, y_rls0, WIRE))
    s.append(_res_v(xc, y_rls0, y_rls1))
    s.append(_txt(xc - 14, (y_rls0+y_rls1)/2 - 2, "R_loop·ls", 11.5, INK, "end", "bold"))
    s.append(_txt(xc - 14, (y_rls0+y_rls1)/2 + 12, f"{_fmtR(rser_ls)}", 11, INK, "end"))
    s.append(_line(xc, y_rls1, xc, y_lsl0, WIRE))
    s.append(_coil_v(xc, y_lsl0, y_lsl1))
    s.append(_txt(xc - 14, (y_lsl0+y_lsl1)/2 - 2, "Lloop_ls", 11.5, INK, "end", "bold"))
    s.append(_txt(xc - 14, (y_lsl0+y_lsl1)/2 + 12, f"{_fmtL(loop_ls)}", 11, INK, "end"))
    fet_ls, d_ls, sc_ls, g_ls = _nfet(xc, cy_ls, "∥".join(t["ls"]["refs"]) or "Q_LS")
    s.append(_line(xc, y_lsl1, d_ls[0], d_ls[1], WIRE))
    s.append(fet_ls)
    s.append(_line(sc_ls[0], sc_ls[1], xc, y_nls, WIRE))
    s.append(_dot(xc, y_nls))
    # die-source node = exposed LSKEL pin (0 Ω Rlskel in the subckt)
    s.append(_txt(xc - 12, y_nls - 4, "nLS · LSKEL", 10.5, INK, "end", ital=True))
    s.append(_txt(xc - 12, y_nls + 9, "(LS die-src pin)", 9, MUTE, "end", ital=True))
    s.append(_coil_v(xc, y_lsl_cs0, y_lsl_cs1, CSI, 2.4))
    s.append(_line(xc, y_nls, xc, y_lsl_cs0, WIRE))
    s.append(_txt(xc - 14, (y_lsl_cs0+y_lsl_cs1)/2 - 2, "Lscs_ls", 11.5, CSI, "end", "bold"))
    s.append(_txt(xc - 14, (y_lsl_cs0+y_lsl_cs1)/2 + 12, f"{_fmtL_na(p.get('csi_ls'))}", 11, CSI, "end"))
    s.append(_line(xc, y_lsl_cs1, xc, y_gnd, WIRE))

    # ================= gate drivers =================
    # The gate-return node sets whether CSI is in the gate loop:
    #   non-Kelvin -> return to the power-source node (SW / GND), BELOW Lscs, so the
    #                 return current shares the source lead (CSI in loop).
    #   Kelvin     -> return to the die-source tap (nHS / nLS), ABOVE Lscs (excluded).
    def driver(cy, gxy, die_y, src_y, l_gate, r_gate, kelvin, tag, gd=None):
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
        # -> Lghs coil -> [series Rg ∥ anti-parallel D] -> driver output pin (no jog).
        out.append(_line(gx, cy, gx + 8, cy, WIRE))
        out.append(_coil_h(cy, gx + 8, gx + 28, WIRE))
        # gate-trace copper series R (Rser on Lghs/Lgls) — a real element in the
        # subckt, drawn as its own glyph like the loop R; distinct from any discrete
        # gate Rg drawn further right toward the driver.
        out.append(_res_h(cy, gx + 28, gx + 50, WIRE))
        out.append(_txt((gx + 8 + gx + 28) / 2, cy - 8, tag.split()[0] + " gate", 9.5,
                        INK, "middle"))
        out.append(_txt(gx + 66, cy + 24, f"L={_fmtL_na(l_gate)}", 9.5, INK, "start"))
        out.append(_txt(gx + 66, cy + 36, f"R={_fmtR_na(r_gate)} (Cu)", 9.5, INK, "start"))
        gd_r = (gd or {}).get("r")
        gd_d = (gd or {}).get("d")
        if gd_r or gd_d:
            # discrete gate network sits between the coil and the driver pin. Not in
            # the FastHenry extraction (copper only) — drawn for context.
            xL, xR = bx - 92, bx - 32
            rc, bw2, bh2 = (xL + xR) / 2, 30, 11
            out.append(_line(gx + 50, cy, xL, cy, WIRE))
            out.append(_line(xR, cy, bx, cy, WIRE))
            if gd_r:
                out.append(_line(xL, cy, rc - bw2 / 2, cy, WIRE))
                out.append(_line(rc + bw2 / 2, cy, xR, cy, WIRE))
                out.append(f"<rect x='{rc-bw2/2:.1f}' y='{cy-bh2/2:.1f}' width='{bw2}' "
                           f"height='{bh2}' fill='white' stroke='{INK}' stroke-width='1.4'/>")
                out.append(_txt(rc, cy - 9, f"{gd_r['ref']} {_rkm(gd_r['value'])}", 9,
                                INK, "middle", "bold"))
            else:
                out.append(_line(xL, cy, xR, cy, WIRE))
            if gd_d:
                # anti-parallel diode branch below (cathode toward driver = turn-off bypass)
                yb, dc = cy + 16, rc
                out.append(_line(xL, cy, xL, yb, WIRE, 1.3))
                out.append(_line(xR, cy, xR, yb, WIRE, 1.3))
                out.append(_line(xL, yb, dc - 5, yb, WIRE, 1.3))
                out.append(_line(dc + 5, yb, xR, yb, WIRE, 1.3))
                out.append(f"<path d='M {dc-5:.1f} {yb-5:.1f} L {dc-5:.1f} {yb+5:.1f} "
                           f"L {dc+5:.1f} {yb:.1f} Z' fill='none' stroke='{INK}' stroke-width='1.2'/>")
                out.append(_line(dc + 5, yb - 5, dc + 5, yb + 5, INK, 1.2))
                out.append(_txt(dc, yb + 13, f"∥ {gd_d['ref']}", 8.5, MUTE, "middle", ital=True))
        else:
            out.append(_line(gx + 50, cy, bx, cy, WIRE))
        # return leg exits the box BOTTOM edge (clear of the block) and drops to the
        # return node (solid = shares CSI / non-Kelvin, grey-dashed = Kelvin tap).
        rpx = bx + bw - 16
        dash = "5 3" if kelvin else None
        out.append(_line(rpx, by + bh, rpx, ret_y, WIRE, 1.6, dash=dash))
        out.append(_line(rpx, ret_y, xc, ret_y, WIRE, 1.6, dash=dash))
        return "".join(out)

    s.append(driver(cy_hs, g_hs, y_nhs, y_sw, p.get("L_gate_hs"),
                    p.get("R_gate_hs"), t["hs"]["kelvin"], "HS gate",
                    t["hs"].get("gate_drive")))
    s.append(driver(cy_ls, g_ls, y_nls, y_gnd, p.get("L_gate_ls"),
                    p.get("R_gate_ls"), t["ls"]["kelvin"], "LS gate",
                    t["ls"].get("gate_drive")))

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
    if n_used > 1:
        single = _num(p.get("L_loop_single")) or L_loop
        loop_txt = (f"Commutation loop L = {_fmtL(L_loop)} "
                    f"({n_used} caps ∥;  single-cap bound {_fmtL(single)})"
                    f"   R = {_fmtR(R_loop)}")
    else:
        loop_txt = f"Commutation loop L = {_fmtL(L_loop)}  (R = {_fmtR(R_loop)})"
    s.append(_txt(20, 62, loop_txt, 12, INK, "start", "bold"))
    if have_rsplit:
        s.append(_txt(
            20, 80,
            f"Two R: ring R_loop {_fmtR(R_loop)} (HF, split HS {_fmtR(rser_hs)} / "
            f"LS {_fmtR(rser_ls)})   ·   conduction r_hs {_fmtR(r_hs)} / "
            f"r_ls {_fmtR(r_ls)} (LF, ×D / ×(1−D))", 10.5, "#0a7a52", "start"))

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
    pdev_rows = _parallel_device_rows(p)
    if pdev_rows:
        px, py, pw = 566, H - 92, 270
        ph = 26 + min(len(pdev_rows), 4) * 14 + (12 if len(pdev_rows) > 4 else 0)
        s.append(f"<rect x='{px}' y='{py}' width='{pw}' height='{ph}' rx='4' "
                 f"fill='#fffdf5' stroke='#d0b35a' stroke-width='1.2'/>")
        s.append(_txt(px + 10, py + 17, "per-device FET parasitics", 10.5,
                      INK, "start", "bold"))
        for i, (side, ref, lg, csi) in enumerate(pdev_rows[:4]):
            s.append(_txt(px + 10, py + 33 + i * 14,
                          f"{side} {ref}: Lg {_fmtL(lg)}, CSI {_fmtL(csi)}",
                          9.5, INK, "start"))
        if len(pdev_rows) > 4:
            s.append(_txt(px + 10, py + 33 + 4 * 14,
                          f"+{len(pdev_rows) - 4} more in parasitics.json",
                          9, MUTE, "start", ital=True))
    s.append("</svg>")
    return "\n".join(s) + "\n"


def emit_svg(p, path):
    with open(path, "w") as f:
        f.write(schematic(p))


if __name__ == "__main__":
    import sys
    p = json.load(open(sys.argv[1]))
    sys.stdout.write(schematic(p))
