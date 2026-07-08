#!/usr/bin/env python3
"""Headless KiPEX-style cross-check for simple-hb using the temp pcbnew adapter."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys

sys.path.insert(0, "/private/tmp/kipex-run")

from kipex_pcbnew_crosscheck import Board, BoardLayer, FASTHENRY, kx  # noqa: E402


PCB = "/Users/fab/dev/ee/hw/simple-hb/simple-hb.kicad_pcb"
QUAD_MAX = float(os.environ.get("KIPEX_QUAD_MAX", "3.0"))
QUAD_MIN = float(os.environ.get("KIPEX_QUAD_MIN", "1.0"))
OUT = os.environ.get("KIPEX_OUT", "/private/tmp/kipex-run/simple_hb_threeleg")


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
    os.makedirs(OUT, exist_ok=True)
    board = Board(PCB)
    tr = kx.Translator(board, {})
    tr.set_frequency_range(39_000, 3_900_000, 1)
    tr.set_quad_limits(QUAD_MAX, QUAD_MIN)

    fcu = BoardLayer.BL_F_Cu
    ports = [
        ("P_vin", board.find_pad("C1", 1), fcu, board.find_pad("Q1", 2), fcu),
        ("P_sw", board.find_pad("Q1", 3), fcu, board.find_pad("Q2", 2), fcu),
        ("P_gnd", board.find_pad("Q2", 3), fcu, board.find_pad("C1", 2), fcu),
    ]
    for name, a, al, b, bl in ports:
        tr.add_port_from_pads(a, al, b, bl, name)
    err = tr.translate()
    if err:
        raise RuntimeError(err)

    inp = os.path.join(OUT, "kipex_threeleg.inp")
    with open(inp, "w") as f:
        tr.export(f, "KiPEX translator via pcbnew adapter")

    subprocess.run([FASTHENRY, "-p", "diag", "-S", "kipex", os.path.basename(inp)],
                   cwd=OUT, check=True)
    out = {"ports": [p[0] for p in ports], "quad_limits": [QUAD_MAX, QUAD_MIN], "frequencies": {}}
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
