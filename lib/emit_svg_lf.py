#!/usr/bin/env python3
"""Render the LF input-cap branch network (`cin_network`) as a standalone SVG.

This is the *low-frequency* companion to emit_svg.py's HF half-bridge. It draws the
`--emit-cin-network` decomposition: a shared Vin/GND trunk (`cin_L_shared`/
`cin_R_shared`) feeding one private branch per input cap (`Lb`/`Rb`, copper only),
with the cap itself as bulk electrolytic (the 39 kHz ripple carrier) or MLCC (~open
at the fundamental). It is the picture of the subckt loss.py assembles after adding
each cap's datasheet C/ESR/ESL from dslib.

Needs `cin_branches` in the parasitics dict (i.e. a genuine --emit-cin-network run).

    emit_svg_lf.py parasitics.json > cin_network.svg
"""
import json
import os
import sys

from emit_svg import (INK, MUTE, WIRE, _cap, _coil_v, _dot, _line, _res_v, _txt)

BULK = "#0a7a52"      # bulk electrolytic — the ripple carrier
MLCC = "#5b7fb0"      # ceramic — ~open at fsw
TRUNK = "#b0670a"     # shared trunk


def _fmtL(h):
    return f"{h*1e9:.2f} nH"


def _fmtR(o):
    return f"{o*1e3:.2f} mΩ"


def schematic_lf(p):
    br = p.get("cin_branches") or []
    if not br:
        return None
    t = p.get("topo", {})
    n = len(br)
    bw = 100
    x0 = 150                      # trunk column
    xb0 = x0 + 90                 # first branch
    W = max(760, xb0 + n * bw + 40)
    H = 560
    y_vin, y_gnd = 122, 476
    y_bus = 206                   # trunk-bus / branch tops
    s = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' height='{H}' "
         f"viewBox='0 0 {W} {H}'>",
         f"<rect width='{W}' height='{H}' fill='white'/>"]

    # ---- title ----
    board = os.path.basename(t.get("pcb", "") or "")
    s.append(_txt(24, 30, f"Input-cap branch network (LF · cin_network) — {board}",
                  15, INK, "start", "bold"))
    s.append(_txt(24, 49,
                  f"shared Vin/GND trunk {_fmtL(p.get('cin_L_shared', 0))} / "
                  f"{_fmtR(p.get('cin_R_shared', 0))}  ·  copper only — loss.py adds "
                  f"each cap's C/ESR/ESL from dslib", 11, MUTE, "start"))

    xb_last = xb0 + (n - 1) * bw
    # ---- rails ----
    s.append(_line(x0 - 20, y_vin, xb_last + 24, y_vin, WIRE, 2.6))   # VIN
    s.append(_line(x0 - 20, y_gnd, xb_last + 24, y_gnd, WIRE, 2.6))   # GND
    def _rail(word, net):
        net = (net or "").split("/")[-1]
        return word if not net or net.upper() == word else f"{word}  {net}"
    s.append(_txt(xb_last + 28, y_vin + 4, _rail("VIN", t.get("vin")), 12, INK, "start", "bold"))
    s.append(_txt(xb_last + 28, y_gnd + 4, _rail("GND", t.get("gnd")), 12, INK, "start", "bold"))

    # ---- shared trunk: VIN -> Rtrunk -> Ltrunk -> bus ----
    s.append(_line(x0, y_vin, x0, y_vin + 8, WIRE))
    s.append(_res_v(x0, y_vin + 8, y_vin + 34, TRUNK))
    s.append(_coil_v(x0, y_vin + 38, y_bus, TRUNK, 2.2))
    s.append(_txt(x0 - 12, y_vin + 26, "trunk", 11, TRUNK, "end", "bold"))
    s.append(_txt(x0 - 12, y_vin + 40, _fmtL(p.get("cin_L_shared", 0)), 9.5, TRUNK, "end"))
    s.append(_txt(x0 - 12, y_vin + 52, _fmtR(p.get("cin_R_shared", 0)), 9.5, TRUNK, "end"))
    # trunk bus (the common node feeding every branch)
    s.append(_line(x0, y_bus, xb_last, y_bus, WIRE, 2.2))
    s.append(_dot(x0, y_bus))

    # ---- per-cap branches ----
    y_l0, y_l1 = y_bus + 14, y_bus + 50    # Lb coil
    y_r0, y_r1 = y_l1 + 8, y_l1 + 40       # Rb resistor
    y_cap = 372                            # cap plates
    for i, b in enumerate(br):
        x = xb0 + i * bw
        col = BULK if b["cls"] == "bulk" else MLCC
        s.append(_dot(x, y_bus, WIRE, 2.4))
        s.append(_txt(x, y_bus - 8, b["ref"], 11, col, "middle", "bold"))
        s.append(_line(x, y_bus, x, y_l0, WIRE))
        s.append(_coil_v(x, y_l0, y_l1, col, 2.0))            # Lb
        s.append(_line(x, y_l1, x, y_r0, WIRE))
        s.append(_res_v(x, y_r0, y_r1, col))                 # Rb
        s.append(_line(x, y_r1, x, y_cap - 9, WIRE))
        # cap: bulk gets a filled polarized plate, mlcc plain
        s.append(_cap(x, y_cap, col))
        if b["cls"] == "bulk":
            s.append(_line(x - 6, y_cap - 13, x + 6, y_cap - 13, col, 3.2))  # + plate emphasis
        s.append(_line(x, y_cap + 7, x, y_gnd, WIRE))
        # labels
        s.append(_txt(x + 9, (y_l0 + y_l1) / 2 + 3, _fmtL(b["Lb"]).replace(" ", ""), 8.5, col, "start"))
        s.append(_txt(x + 9, (y_r0 + y_r1) / 2 + 3, _fmtR(b["Rb"]).replace(" ", ""), 8.5, col, "start"))
        s.append(_txt(x, y_cap + 26, "C/ESR", 8, MUTE, "middle", ital=True))
        s.append(_txt(x, y_cap + 37, "→dslib", 8, MUTE, "middle", ital=True))

    # ---- legend + conduction note ----
    ly = H - 40
    s.append(f"<rect x='24' y='{ly-9}' width='16' height='10' fill='none' stroke='{BULK}' stroke-width='2'/>")
    s.append(_txt(46, ly, "bulk electrolytic — carries the 39 kHz ripple (ESR loss)", 11, BULK, "start"))
    s.append(f"<rect x='400' y='{ly-9}' width='16' height='10' fill='none' stroke='{MLCC}' stroke-width='2'/>")
    s.append(_txt(422, ly, "MLCC — ~open at fsw, sources the HF edge", 11, MLCC, "start"))
    rhs, rls = p.get("r_hs"), p.get("r_ls")
    if rhs is not None and rls is not None:
        s.append(_txt(24, ly + 18,
                      f"per-switch conduction R (bank → switches): r_hs {_fmtR(rhs)} · "
                      f"r_ls {_fmtR(rls)}   — see the HF schematic for the loop", 10.5, INK, "start"))
    s.append("</svg>")
    return "\n".join(s) + "\n"


def emit_svg_lf(p, path):
    svg = schematic_lf(p)
    if svg is None:
        return False
    with open(path, "w") as f:
        f.write(svg)
    return True


if __name__ == "__main__":
    p = json.load(open(sys.argv[1]))
    out = schematic_lf(p)
    if out is None:
        sys.stderr.write("no cin_branches in JSON — run with --emit-cin-network\n")
        raise SystemExit(1)
    sys.stdout.write(out)
