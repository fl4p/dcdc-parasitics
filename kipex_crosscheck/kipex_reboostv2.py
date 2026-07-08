#!/usr/bin/env python3
"""Headless KiPEX-style cross-check for ReboostV2 (GaN half-bridge).

Uses the pcbnew adapter to feed KiPEX's polygon meshing without the
pad-land terminal bonding that inflates dcdc-tools extraction on
Altium-imported boards (clearance voids force point injection).

ReboostV2 topology:
  HS = Q1 (GS61008T GaN), drain = Vb, source = HSS
  LS = Q2 (GS61008T GaN), drain = HSS, source = GND
  Cin = C37 (1uF 1206 MLCC, nearest to FETs)

Commutation loop: C37.Vb -> Q1.drain -> Q1.source=Q2.drain (HSS) -> Q2.source (GND) -> C37.GND
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys

KICROSS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KICROSS)

from kipex_pcbnew_adapter import Board, BoardLayer, FASTHENRY, kx  # noqa: E402

PCB = os.environ.get("REBOOST_PCB", "")
QUAD_MAX = float(os.environ.get("KIPEX_QUAD_MAX", "3.0"))
QUAD_MIN = float(os.environ.get("KIPEX_QUAD_MIN", "1.0"))
OUT = os.environ.get("KIPEX_OUT", os.path.join(KICROSS, "out_reboostv2"))


def read_zmat(path):
    freqs = {}
    freq = None
    n = None
    rows = []
    for line in open(path):
        if "Impedance matrix for frequency" in line:
            if freq is not None and rows:
                freqs[freq] = rows
            parts = line.replace("=", " ").split()
            freq = float(parts[4])
            n = int(parts[5])
            rows = []
            continue
        if freq is None:
            continue
        vals = line.replace("j", "").split()
        if n and len(vals) >= 2 * n:
            row = [complex(float(vals[2 * i]), float(vals[2 * i + 1])) for i in range(n)]
            rows.append(row)
    if freq is not None and rows:
        freqs[freq] = rows
    return freqs


def run():
    if not PCB:
        sys.exit("Set REBOOST_PCB env var to the .kicad_pcb path")
    os.makedirs(OUT, exist_ok=True)
    board = Board(PCB)

    tr = kx.Translator(board, {})
    tr.set_frequency_range(39_000, 3_900_000, 1)
    tr.set_quad_limits(QUAD_MAX, QUAD_MIN)

    fcu = BoardLayer.BL_F_Cu
    bcu = BoardLayer.BL_B_Cu

    # GS61008T pads: D=drain, S=source, G1=turn-on gate, G2=turn-off gate
    # C37: pad 1=GND, pad 2=Vb
    ports = [
        ("P_vin", board.find_pad("C37", "2"), fcu, board.find_pad("Q1", "D"), fcu),
        ("P_sw", board.find_pad("Q1", "S"), fcu, board.find_pad("Q2", "D"), fcu),
        ("P_gnd", board.find_pad("Q2", "S"), fcu, board.find_pad("C37", "1"), fcu),
    ]
    for name, a, al, b, bl in ports:
        tr.add_port_from_pads(a, al, b, bl, name)
    err = tr.translate()
    if err:
        raise RuntimeError(err)

    inp = os.path.join(OUT, "kipex_threeleg.inp")
    with open(inp, "w") as f:
        tr.export(f, "KiPEX translator via pcbnew adapter — ReboostV2")

    subprocess.run([FASTHENRY, "-p", "diag", "-S", "kipex", os.path.basename(inp)],
                   cwd=OUT, check=True)
    out = {"ports": [p[0] for p in ports], "quad_limits": [QUAD_MAX, QUAD_MIN],
           "pcb": PCB, "frequencies": {}}
    for freq, z in sorted(read_zmat(os.path.join(OUT, "Zckipex.mat")).items()):
        zloop = sum(z[i][j] for i in range(3) for j in range(3))
        out["frequencies"][str(freq)] = {
            "R_loop_ohm": zloop.real,
            "L_loop_H": zloop.imag / (2 * math.pi * freq),
            "port_R_ohm": [z[i][i].real for i in range(3)],
            "port_L_H": [z[i][i].imag / (2 * math.pi * freq) for i in range(3)],
            "Z_matrix": [[(z[i][j].real, z[i][j].imag) for j in range(3)] for i in range(3)],
        }
    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    run()
