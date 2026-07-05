#!/usr/bin/env python3
"""Run FastHenry on a .inp, parse the multiport Zc.mat, reduce to named parasitics.

System-python step (numpy). The port order in Zc.mat follows the `.external`
order emitted by kicad_geom.py: [P_pwr, P_ghs, P_gls].

    L_ij(f) = Im(Z_ij) / (2*pi*f)      R_ij(f) = Re(Z_ij)

Reduced parasitics (at the low-MHz plateau):
    L_loop      = L[pwr,pwr]           commutation-loop inductance (SW peak V)
    R_loop      = R[pwr,pwr]           HF ring-loop R (MLCC-anchored, skin-elevated)
    L_gate_hs   = L[ghs,ghs]           HS gate-loop inductance
    L_gate_ls   = L[gls,gls]
    csi_hs      = |L[pwr,ghs]|         HS common-source inductance (shared source lead)
    csi_ls      = |L[pwr,gls]|
    m_gate      = L[ghs,gls]

Conduction-loss R (at the LOWEST swept freq ~= DC, bulk-electrolytic-anchored, so
the MLCCs — near-open at the 39 kHz fundamental — are excluded):
    r_hs        = R[hs,hs]             HS conduction copper (Vin_bulk -> SW via HS)
    r_ls        = R[ls,ls]             LS conduction copper (SW -> GND_bulk via LS)
    r_loop_cond = R[bulk,bulk]         full LF conduction loop over the bulk cap
    r_sw        = r_loop_cond-r_hs-r_ls   SW-node spreading residual

With --emit-cin-network the full input bank is ported (topo['cin_net']) and
decomposed into a shared Vin/GND trunk + per-cap private branch (copper only):
    cin_branches = [{ref, cls, Lb, Rb}]   per-cap branch L/R (Lb=L[i,i]-L_shared)
    cin_L_shared, cin_R_shared            the shared trunk (loss.py adds cap C/ESR)
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


def _eff_commutation(Z, cin_idx, zcap=None):
    """Effective 2-terminal commutation impedance of the input caps driven in
    parallel (common voltage across every Cin port, other ports open).

    For N cap ports with complex port matrix Zc, driving all at the same voltage
    V gives port currents I = V*Zc^-1*1, total I_tot = V*(1^T Zc^-1 1), so the
    effective impedance seen by the FET commutation current is

        Z_eff = V / I_tot = 1 / (1^T Zc^-1 1).

    This folds in every mutual Mij between the cap-return loops exactly; for N=1
    it degenerates to Zc[0,0] (the single-cap loop). With `zcap` (a per-cap
    complex branch impedance ESR+jw*ESL) it adds each cap's own parasitics in
    series, making the current split physical at the ring frequency instead of
    the ideal-cap (copper-only) limit.

    Solves Zc x = 1 (never forms an explicit inverse — Zc can be ill-conditioned
    when caps are closely coupled). Returns (Z_eff, y, denom, cond, weights),
    where y are the branch currents, weights = y/denom sum to 1 (the current
    split), and cond is the 2-norm condition number for a reliability check."""
    Zc = Z[np.ix_(cin_idx, cin_idx)].astype(complex)
    if zcap is not None:
        Zc = Zc + np.diag(zcap)
    ones = np.ones(len(cin_idx), dtype=complex)
    y = np.linalg.solve(Zc, ones)
    denom = complex(y.sum())
    cond = float(np.linalg.cond(Zc))
    return 1.0 / denom, y, denom, cond, y / denom


def _cin_branch_decomp(Lm, Rm, cin_idx, refs, cls_map) -> "dict | None":
    """Partial-inductance decomposition of the ported input-cap bank into a shared
    trunk + per-cap private branch, for the `--emit-cin-network` LF model.

    With a single common Vin/GND trunk feeding every cap, the port L/R matrices
    read (to first order): diagonal L[i,i] = L_shared + Lb_i, off-diagonal
    L[i,j] = L_shared (only the trunk is common). So the trunk is the mean of the
    off-diagonals and each private branch is diagonal minus trunk:

        L_shared = mean(L[i,j], i!=j)     Lb_i = L[i,i] - L_shared
        R_shared = mean(R[i,j], i!=j)     Rb_i = R[i,i] - R_shared   (at f_dc)

    Returns per-cap {ref, cls, Lb, Rb} + the shared trunk L/R + the off-diagonal
    spread (a high spread means the single-trunk model is a poor fit -> warn). The
    consumer (loss.py) assembles the SPICE `cin_network` as one series trunk
    (L_shared/R_shared) feeding each cap's branch (Lb_i/Rb_i) + its datasheet
    C/ESR/ESL. None if <2 caps (no shared/branch split is defined)."""
    n = len(cin_idx)
    if n < 2:
        return None
    offL = [Lm[a, b] for i, a in enumerate(cin_idx)
            for j, b in enumerate(cin_idx) if i != j]
    offR = [Rm[a, b] for i, a in enumerate(cin_idx)
            for j, b in enumerate(cin_idx) if i != j]
    L_shared = float(np.mean(offL))
    R_shared = float(np.mean(offR))
    L_spread = float(np.std(offL))
    branches = []
    for k, a in enumerate(cin_idx):
        ref = refs[k] if k < len(refs) else f"P{a}"
        branches.append(dict(ref=ref, cls=(cls_map or {}).get(ref, "mlcc"),
                             Lb=float(Lm[a, a]) - L_shared,
                             Rb=float(Rm[a, a]) - R_shared))
    return dict(branches=branches, L_shared=L_shared, R_shared=R_shared,
                L_spread=L_spread)


def reduce_parasitics(zc, ports, topo, meta, plateau=5e6, cin_ports=None,
                      cin_esl=0.0, cin_esr=0.0) -> dict:
    f, Z = pick_plateau(zc, plateau)
    w = 2 * np.pi * f
    L = Z.imag / w
    R = Z.real
    idx = {p: i for i, p in enumerate(ports)}
    ih, il = idx.get("P_ghs"), idx.get("P_gls")

    if not cin_ports:
        cin_ports = ["P_pwr"]
    cin_idx = [idx[c] for c in cin_ports if c in idx]
    if not cin_idx:
        cin_idx = [idx["P_pwr"]] if "P_pwr" in idx else [0]

    # optional per-cap branch impedance (ESR + jw*ESL) -> physical current split
    def zcap_at(wf):
        if cin_esl <= 0 and cin_esr <= 0:
            return None
        return np.array([cin_esr + 1j * wf * cin_esl] * len(cin_idx), dtype=complex)

    # headline reduction (physical if ESL/ESR given, else ideal copper-only)
    Zeff, y, denom, cond, weights = _eff_commutation(Z, cin_idx, zcap_at(w))
    L_loop = float(Zeff.imag / w)
    R_loop = float(Zeff.real)
    # bracket: ideal copper-only (lower bound) and nearest single cap (upper bound)
    Zeff0, _, _, _, _ = _eff_commutation(Z, cin_idx, None)
    L_loop_ideal = float(Zeff0.imag / w)
    i0 = cin_idx[0]
    L_loop_single = float(Z[i0, i0].imag / w)
    per_cap_L = {ports[j]: float(Z[j, j].imag / w) for j in cin_idx}

    # per-frequency effective L/R across the whole sweep (confirm the plateau)
    sweep = []
    for ff in sorted(zc.keys()):
        Zf = zc[ff]
        wf = 2 * np.pi * ff
        Ze, _, _, cf, _ = _eff_commutation(Zf, cin_idx, zcap_at(wf))
        sweep.append(dict(f=ff, L_eff=float(Ze.imag / wf),
                          R_eff=float(Ze.real), cond=cf))

    # current split keyed by refdes (cin_used order matches cin_ports order)
    refs = (topo or {}).get("cin_used", []) if isinstance(topo, dict) else []
    split = {}
    for k, ci in enumerate(cin_idx):
        name = refs[k] if k < len(refs) else ports[ci]
        wk = complex(weights[k])
        split[name] = dict(re=wk.real, im=wk.imag, mag=abs(wk))

    warn = []
    if cond > 1e6:
        warn.append(f"Zc ill-conditioned (cond={cond:.1e}) — parallel reduction "
                    f"unreliable; check cap ports / near-coincident caps")
    neg = [n for n, s in split.items() if s["re"] < -1e-3]
    if neg:
        warn.append(f"negative current share on {neg} — circulating current "
                    f"between coupled caps; review port polarity / geometry")
    if len(cin_idx) > 1 and L_loop_ideal > L_loop_single + 1e-12:
        warn.append("effective loop L exceeds single-cap L — unexpected for "
                    "parallel caps; check mutual signs / port polarity")
    if len(cin_idx) > 1 and L_loop_ideal < L_loop_single / len(cin_idx) - 1e-12:
        # positively-coupled parallel caps can't drop below the uncoupled floor
        warn.append(f"effective loop L ({L_loop_ideal*1e9:.2f} nH) is below the "
                    f"uncoupled parallel floor ({L_loop_single/len(cin_idx)*1e9:.2f} nH) "
                    f"— likely reversed cap-port polarity or a mutual-sign error")

    # ---- conduction-path resistances (LF, per-side) ----
    # Read R at the LOWEST swept frequency: there the skin depth (>2 mm at 100 kHz)
    # dwarfs the copper thickness, so this is the DC/fundamental conduction R, not
    # the skin-elevated ring-plateau R_loop. P_hs/P_ls are anchored on the bulk cap
    # (the fundamental source), so r_hs/r_ls are each switch's true conduction
    # copper; P_bulk is the full LF loop, and r_sw is the SW-node spreading residual.
    f_dc = min(zc.keys())
    R_dc = zc[f_dc].real

    def rdc(label):
        i = idx.get(label)
        return float(R_dc[i, i]) if i is not None else None

    r_hs, r_ls, r_loop_cond = rdc("P_hs"), rdc("P_ls"), rdc("P_bulk")
    r_sw = None
    if r_hs is not None and r_ls is not None and r_loop_cond is not None:
        r_sw = r_loop_cond - r_hs - r_ls
        if r_sw < -0.05e-3:   # -0.05 mOhm tolerance
            warn.append(
                f"conduction R_hs+R_ls ({(r_hs + r_ls) * 1e3:.2f} mOhm) exceeds LF "
                f"loop R ({r_loop_cond * 1e3:.2f} mOhm) — check P_hs/P_ls port "
                f"polarity or SW-node reference")
    cond_ref = (topo or {}).get("cond_ref") if isinstance(topo, dict) else None

    # ---- per-cap branch decomposition for --emit-cin-network ----
    # Uses the dedicated full-bank port set (topo['cin_net']: bulk+mlcc, one port
    # per cap) that geometry adds only under --emit-cin-network — SEPARATE from the
    # MLCC-only HF cin_ports above, so the L_loop reduction is never perturbed.
    # Falls back to the HF set (single-cap -> None) when cin_net is absent.
    cin_class = (topo or {}).get("cin_class") if isinstance(topo, dict) else None
    cin_net = (topo or {}).get("cin_net") if isinstance(topo, dict) else None
    if cin_net:
        net_idx = [idx[e["label"]] for e in cin_net if e.get("label") in idx]
        net_refs = [e["ref"] for e in cin_net if e.get("label") in idx]
        net_cls = {e["ref"]: e.get("cls", "mlcc") for e in cin_net}
    else:
        net_idx, net_refs, net_cls = cin_idx, refs, cin_class
    cin_dec = _cin_branch_decomp(L, R_dc, net_idx, net_refs, net_cls)
    if cin_dec:
        _Lsh = float(cin_dec["L_shared"])
        _Lsp = float(cin_dec["L_spread"])
        if _Lsh > 0 and _Lsp > 0.5 * _Lsh:
            warn.append(
                f"cin branch decomposition: off-diagonal L spread high "
                f"({_Lsp*1e9:.2f} vs shared {_Lsh*1e9:.2f} nH) — single shared-trunk "
                f"model approximate; per-cap Lb less reliable")

    def eff_csi(g):
        """Effective common-source mutual: gate-loop voltage per unit *total*
        commutation current, using the parallel-cap current distribution y.
        Reduces to |L[pwr,gate]| when a single cap is ported."""
        if g is None:
            return 0.0
        m = Z[g, cin_idx]                      # gate<->each-cap coupling row
        Zmg = complex(np.dot(m, y) / denom)
        return abs(Zmg.imag / w)

    def LL(a, b):
        return float(L[a, b]) if (a is not None and b is not None) else 0.0

    def RR(a):
        return float(R[a, a]) if a is not None else 0.0

    physical = cin_esl > 0 or cin_esr > 0
    p = dict(
        freq_Hz=f,
        L_loop=L_loop, R_loop=R_loop,
        L_loop_ideal=L_loop_ideal,            # copper-only lower bound
        L_loop_single=L_loop_single,          # nearest single cap, upper bound
        L_loop_physical=(L_loop if physical else None),
        per_cap_L=per_cap_L, current_split=split,
        cin_esl=cin_esl, cin_esr=cin_esr, cond_Zc=cond, reduce_warn=warn,
        L_eff_sweep=sweep, n_cin=len(cin_idx),
        L_gate_hs=LL(ih, ih), R_gate_hs=RR(ih),
        L_gate_ls=LL(il, il), R_gate_ls=RR(il),
        r_hs=r_hs, r_ls=r_ls, r_loop_cond=r_loop_cond, r_sw=r_sw,
        r_cond_freq=f_dc, cond_ref=cond_ref,
        cin_branches=(cin_dec["branches"] if cin_dec else None),
        cin_L_shared=(cin_dec["L_shared"] if cin_dec else None),
        cin_R_shared=(cin_dec["R_shared"] if cin_dec else None),
        csi_hs=eff_csi(ih),
        csi_ls=eff_csi(il),
        m_gate=LL(ih, il),
        port_L=L.tolist(), port_R=R.tolist(), ports=ports, cin_ports=cin_ports,
        topo=topo, meta=meta,
    )
    return p


def solve(inp, ports, topo, meta, plateau=5e6, suffix="dcdc", cin_ports=None,
          cin_esl=0.0, cin_esr=0.0):
    zc = parse_zc(run_fasthenry(inp, suffix=suffix))
    return reduce_parasitics(zc, ports, topo, meta, plateau, cin_ports=cin_ports,
                             cin_esl=cin_esl, cin_esr=cin_esr)


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
    print(f"L_loop    = {p['L_loop']*nH:7.2f} nH   R_loop = {p['R_loop']*1e3:.2f} mOhm (HF ring)")
    print(f"L_gate_hs = {p['L_gate_hs']*nH:7.2f} nH   L_gate_ls = {p['L_gate_ls']*nH:7.2f} nH")
    print(f"CSI_hs    = {p['csi_hs']*nH:7.2f} nH   CSI_ls    = {p['csi_ls']*nH:7.2f} nH")
    if p.get("r_hs") is not None:
        print(f"R_hs      = {p['r_hs']*1e3:7.2f} mOhm R_ls   = {p['r_ls']*1e3:.2f} mOhm "
              f"(LF conduction @ {p['r_cond_freq']:g} Hz, SW spread {p.get('r_sw',0)*1e3:.2f} mOhm)")
