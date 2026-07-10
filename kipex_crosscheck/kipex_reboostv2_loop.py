#!/usr/bin/env python3
"""KiPEX polygon-mesh commutation-loop L for ReboostV2 — single-cap-port form.

The 3-port copper-leg formulation (kipex_reboostv2.py) breaks on this board:
the SW-leg port P_sw spans Q1.S->Q2.D, but both pads sit on one continuous
HSS pour, so the quad mesh .equiv-chains their nodes into one electrical node
and FastHenry rejects the zero-span port ("Nodes ... are the same node").

This script uses the physically-correct commutation-loop measurement instead
(the same one dcdc-tools and a real double-pulse test use): SHORT both FET
dies (drain<->source .equiv, the closed channel) and inject at the input cap.
Current then flows C37.Vb -> Q1.D -[HS short]- SW pour -> Q2.D -[LS short]-
Q2.S -> GND pour -> C37.GND, and L_loop = the self-inductance seen from the
cap port. No SW-node port, so no collision — and the endpoints of the two
die shorts are on different nets, so they never merge either.

    KICAD_PY=.../Versions/Current/bin/python3
    REBOOST_PCB=/path/reboost_groundtruth.kicad_pcb \
    KIPEX_OUT=/tmp/kipex_gt_loop \
    $KICAD_PY kipex_crosscheck/kipex_reboostv2_loop.py
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
from kipex_src.translator import Equivalence, Port  # noqa: E402

PCB = os.environ.get("REBOOST_PCB", "")
QUAD_MAX = float(os.environ.get("KIPEX_QUAD_MAX", "3.0"))
QUAD_MIN = float(os.environ.get("KIPEX_QUAD_MIN", "1.0"))
# Sweep band + plateau read-out frequency. Defaults match dcdc-tools
# (1 kHz..100 MHz, 3/decade, L-plateau read at 5 MHz) so the polygon-path
# L_loop is directly comparable to the grid-mesh number.
FMIN = float(os.environ.get("KIPEX_FMIN", "1e3"))
FMAX = float(os.environ.get("KIPEX_FMAX", "1e8"))
NDEC = int(os.environ.get("KIPEX_NDEC", "3"))
PLATEAU = float(os.environ.get("KIPEX_PLATEAU", "5e6"))
OUT = os.environ.get("KIPEX_OUT", os.path.join(KICROSS, "out_reboostv2_loop"))


def read_zmat(path):
    freqs, freq, n, rows = {}, None, None, []
    for line in open(path):
        if "Impedance matrix for frequency" in line:
            if freq is not None and rows:
                freqs[freq] = rows
            parts = line.replace("=", " ").split()
            freq = float(parts[4]); n = int(parts[5]); rows = []
            continue
        if freq is None:
            continue
        vals = line.replace("j", "").split()
        if n and len(vals) >= 2 * n:
            rows.append([complex(float(vals[2 * i]), float(vals[2 * i + 1]))
                         for i in range(n)])
    if freq is not None and rows:
        freqs[freq] = rows
    return freqs


def run():
    if not PCB:
        sys.exit("Set REBOOST_PCB env var to the .kicad_pcb path")
    os.makedirs(OUT, exist_ok=True)
    board = Board(PCB)

    tr = kx.Translator(board, {})
    tr.set_frequency_range(FMIN, FMAX, NDEC)
    tr.set_quad_limits(QUAD_MAX, QUAD_MIN)

    fcu = BoardLayer.BL_F_Cu

    # Resolve every pad node we need by registering it as a (helper) port; after
    # translate() we read the resolved start/end Nodes back off processed_ports.
    # Order matters — we index processed_ports by it below.
    #   0: cap port    C37.Vb(2) -> C37.GND(1)   (the ONE real .external)
    #   1: HS die      Q1.D      -> Q1.S          (-> .equiv short)
    #   2: LS die      Q2.D      -> Q2.S          (-> .equiv short)
    pad = board.find_pad
    helper_ports = [
        ("P_cin", pad("C37", "2"), pad("C37", "1")),
        ("HS_die", pad("Q1", "D"), pad("Q1", "S")),
        ("LS_die", pad("Q2", "D"), pad("Q2", "S")),
    ]
    for name, a, b in helper_ports:
        tr.add_port_from_pads(a, fcu, b, fcu, name)

    err = tr.translate()
    if err:
        raise RuntimeError(err)

    cap, hs, ls = tr.processed_ports[0], tr.processed_ports[1], tr.processed_ports[2]
    # Close the two FET channels: ideal drain<->source short (endpoints are on
    # different nets, so this never collides with anything).
    tr.eqivs.append(Equivalence([hs.start, hs.end]))
    tr.eqivs.append(Equivalence([ls.start, ls.end]))
    # Keep ONLY the cap port as the FastHenry .external.
    tr.processed_ports = [Port(cap.start, cap.end, "P_cin")]

    inp = os.path.join(OUT, "kipex_loop.inp")
    with open(inp, "w") as f:
        tr.export(f, "KiPEX loop (shorted dies, cap port) — ReboostV2")

    subprocess.run([FASTHENRY, "-p", "diag", "-S", "kiloop",
                    os.path.basename(inp)], cwd=OUT, check=True)

    out = {"pcb": PCB, "quad_limits": [QUAD_MAX, QUAD_MIN],
           "formulation": "shorted-dies single cap port", "frequencies": {}}
    zc_by_freq = read_zmat(os.path.join(OUT, "Zckiloop.mat"))
    for freq, z in sorted(zc_by_freq.items()):
        zc = z[0][0]
        out["frequencies"][str(freq)] = {
            "R_loop_ohm": zc.real,
            "L_loop_nH": zc.imag / (2 * math.pi * freq) * 1e9,
        }
    # L-plateau read: frequency closest to PLATEAU in log-space (matches
    # dcdc-tools pick_plateau, so the two numbers compare at the same freq).
    if zc_by_freq:
        fp = min(zc_by_freq, key=lambda x: abs(math.log10(x) - math.log10(PLATEAU)))
        zp = zc_by_freq[fp][0][0]
        out["plateau_freq_Hz"] = fp
        out["L_loop_nH"] = zp.imag / (2 * math.pi * fp) * 1e9
        out["R_loop_mOhm"] = zp.real * 1e3
    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    run()
