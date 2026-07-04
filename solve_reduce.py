#!/usr/bin/env python3
"""Run FastHenry on a .inp, parse the multiport Zc.mat, reduce to named parasitics.

System-python step (numpy). The port order in Zc.mat follows the `.external`
order emitted by kicad_geom.py: [P_pwr, P_ghs, P_gls].

    L_ij(f) = Im(Z_ij) / (2*pi*f)      R_ij(f) = Re(Z_ij)

Reduced parasitics (at the low-MHz plateau):
    L_loop      = L[pwr,pwr]           commutation-loop inductance (SW peak V)
    R_loop      = R[pwr,pwr]
    L_gate_hs   = L[ghs,ghs]           HS gate-loop inductance
    L_gate_ls   = L[gls,gls]
    csi_hs      = |L[pwr,ghs]|         HS common-source inductance (shared source lead)
    csi_ls      = |L[pwr,gls]|
    m_gate      = L[ghs,gls]
"""
import os
import re
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FASTHENRY = os.environ.get("FASTHENRY", "/Users/fab/dev/vendor/FastHenry2/bin/fasthenry")


def run_fasthenry(inp, suffix="dcdc", fasthenry=FASTHENRY, cwd=None):
    """Run fasthenry; return the path to the produced Zc<suffix>.mat."""
    cwd = cwd or os.path.dirname(os.path.abspath(inp)) or "."
    subprocess.run([fasthenry, "-p", "diag", "-S", suffix, os.path.basename(inp)],
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.join(cwd, f"Zc{suffix}.mat")


def parse_zc(path):
    """Return {freq_Hz: ZmatrixComplex(n,n)}."""
    out = {}
    freq = None
    rows = []
    n = 0
    hdr = re.compile(r"Impedance matrix for frequency\s*=\s*([-\d.eE+]+)\s+(\d+)\s*x\s*(\d+)")
    for ln in open(path):
        m = hdr.search(ln)
        if m:
            if freq is not None and rows:
                out[freq] = np.array(rows)
            freq = float(m.group(1)); n = int(m.group(2)); rows = []
            continue
        if freq is None:
            continue
        toks = ln.replace("j", "").split()
        if len(toks) >= 2 * n:
            vals = [float(t) for t in toks[:2 * n]]
            rows.append([complex(vals[2 * k], vals[2 * k + 1]) for k in range(n)])
    if freq is not None and rows:
        out[freq] = np.array(rows)
    return out


def pick_plateau(zc, target=5e6):
    """Return (freq, Z) at the frequency closest to `target` (low-MHz L plateau)."""
    f = min(zc.keys(), key=lambda x: abs(np.log10(x) - np.log10(target)))
    return f, zc[f]


def reduce_parasitics(zc, ports, topo, meta, plateau=5e6):
    f, Z = pick_plateau(zc, plateau)
    w = 2 * np.pi * f
    L = Z.imag / w
    R = Z.real
    idx = {p: i for i, p in enumerate(ports)}
    ip, ih, il = idx["P_pwr"], idx.get("P_ghs"), idx.get("P_gls")

    def LL(a, b):
        return float(L[a, b]) if (a is not None and b is not None) else 0.0

    def RR(a):
        return float(R[a, a]) if a is not None else 0.0

    p = dict(
        freq_Hz=f,
        L_loop=LL(ip, ip), R_loop=RR(ip),
        L_gate_hs=LL(ih, ih), R_gate_hs=RR(ih),
        L_gate_ls=LL(il, il), R_gate_ls=RR(il),
        csi_hs=abs(LL(ip, ih)),
        csi_ls=abs(LL(ip, il)),
        m_gate=LL(ih, il),
        port_L=L.tolist(), port_R=R.tolist(), ports=ports,
        topo=topo, meta=meta,
    )
    return p


def solve(inp, ports, topo, meta, plateau=5e6, suffix="dcdc"):
    zc = parse_zc(run_fasthenry(inp, suffix=suffix))
    return reduce_parasitics(zc, ports, topo, meta, plateau)


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="parse a FastHenry Zc.mat -> parasitics")
    ap.add_argument("zc")
    ap.add_argument("--ports", default="P_pwr,P_ghs,P_gls")
    ap.add_argument("--plateau", type=float, default=5e6)
    args = ap.parse_args()
    zc = parse_zc(args.zc)
    p = reduce_parasitics(zc, args.ports.split(","), {}, {}, args.plateau)
    nH = 1e9
    print(f"plateau f = {p['freq_Hz']:g} Hz")
    print(f"L_loop    = {p['L_loop']*nH:7.2f} nH   R_loop = {p['R_loop']*1e3:.2f} mOhm")
    print(f"L_gate_hs = {p['L_gate_hs']*nH:7.2f} nH   L_gate_ls = {p['L_gate_ls']*nH:7.2f} nH")
    print(f"CSI_hs    = {p['csi_hs']*nH:7.2f} nH   CSI_ls    = {p['csi_ls']*nH:7.2f} nH")
