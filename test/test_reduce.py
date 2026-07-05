#!/usr/bin/env python3
"""Synthetic tests for the --cin-parallel effective-loop reduction.

No FastHenry / KiCad needed — feeds hand-built port impedance matrices straight
into solve_reduce and checks the reduction against closed-form answers. Covers
the failure modes the EE review flagged: single-cap regression, shared-path
parallel law, the coupled 2-port formula, CSI degeneracy, reversed-port polarity
(must warn), and an ill-conditioned near-coincident pair (must warn).

    python3 test/test_reduce.py   # prints PASS/FAIL per case, exits nonzero on any fail
"""
import os
import sys

# tests live in test/; the library modules live in ../lib
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import numpy as np  # noqa: E402

import solve_reduce as sr  # noqa: E402

W = 2 * np.pi * 5e6
FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def Zof(Lmatrix):
    """Complex port Z at W from a real inductance matrix (nH-free, henries)."""
    return 1j * W * np.asarray(Lmatrix, dtype=float)


def reduce(Lmatrix, ports, cin_ports, topo=None, **kw):
    return sr.reduce_parasitics({5e6: Zof(Lmatrix)}, ports, topo or {}, {},
                                plateau=5e6, cin_ports=cin_ports, **kw)


# 1. N=1 regression: effective must equal the single-cap self-L exactly.
p = reduce([[9e-9]], ["P_pwr"], ["P_pwr"])
check("N=1 regression", abs(p["L_loop"] - 9e-9) < 1e-15,
      f"L_loop={p['L_loop']*1e9:.4f} nH")

# 2. Shared-path parallel law: Lf shared in every entry, Lb per-cap on diagonal.
Lf, Lb = 8e-9, 4e-9
for N, want in ((1, 12e-9), (2, 10e-9), (4, 9e-9)):
    M = Lf * np.ones((N, N)) + Lb * np.eye(N)
    ports = [f"P_pwr{i}" if i else "P_pwr" for i in range(N)]
    p = reduce(M, ports, ports)
    check(f"shared-path N={N} (Lf+Lb/N)", abs(p["L_loop"] - want) < 1e-15,
          f"{p['L_loop']*1e9:.3f} nH (want {want*1e9:.3f})")

# 3. Coupled 2-port closed form: Z_eff = (Z1 Z2 - M^2)/(Z1+Z2-2M).
L1, L2, M12 = 10e-9, 6e-9, 3e-9
p = reduce([[L1, M12], [M12, L2]], ["P_pwr", "P_pwr1"], ["P_pwr", "P_pwr1"])
want = (L1 * L2 - M12 ** 2) / (L1 + L2 - 2 * M12)
check("coupled 2-port formula", abs(p["L_loop"] - want) < 1e-15,
      f"{p['L_loop']*1e9:.4f} nH (want {want*1e9:.4f})")
# positive coupling must give MORE than the uncoupled parallel of the two
uncoupled = L1 * L2 / (L1 + L2)
check("coupled > uncoupled parallel", p["L_loop"] > uncoupled,
      f"{p['L_loop']*1e9:.3f} > {uncoupled*1e9:.3f}")

# 4. CSI degeneracy + reweighting: 2 caps, gate couples equally (Msg) to each.
Msg = 1e-9
M = np.zeros((3, 3))
M[:2, :2] = Lf * np.ones((2, 2)) + Lb * np.eye(2)
M[2, 2] = 10e-9
M[2, :2] = Msg
M[:2, 2] = Msg
topo = {"cin_used": ["C1", "C2"]}
p2 = reduce(M, ["P_pwr", "P_pwr1", "P_ghs"], ["P_pwr", "P_pwr1"], topo)
check("CSI 2-cap equal-coupling == Msg", abs(p2["csi_hs"] - Msg) < 1e-15,
      f"{p2['csi_hs']*1e9:.4f} nH")
p1 = reduce(M[:1, :1].tolist() if False else
            [[Lf + Lb, Msg], [Msg, 10e-9]], ["P_pwr", "P_ghs"], ["P_pwr"])
check("CSI 1-cap == |L[pwr,gate]|", abs(p1["csi_hs"] - Msg) < 1e-15,
      f"{p1['csi_hs']*1e9:.4f} nH")

# 5. Bracket ordering: single (upper) >= ideal parallel (lower).
p = reduce(Lf * np.ones((3, 3)) + Lb * np.eye(3),
           ["P_pwr", "P_pwr1", "P_pwr2"], ["P_pwr", "P_pwr1", "P_pwr2"])
check("bracket single>=ideal", p["L_loop_single"] >= p["L_loop_ideal"],
      f"upper={p['L_loop_single']*1e9:.2f} lower={p['L_loop_ideal']*1e9:.2f}")
shares = sum(s["mag"] for s in p["current_split"].values())
check("current split ~ sums to 1", abs(shares - 1.0) < 1e-9, f"sum|w|={shares:.4f}")

# 6. Reversed-port polarity: shared mutual flips sign -> spuriously low L, must warn.
Mrev = np.array([[Lf + Lb, -Lf], [-Lf, Lf + Lb]])
p = reduce(Mrev, ["P_pwr", "P_pwr1"], ["P_pwr", "P_pwr1"],
           {"cin_used": ["C1", "C2"]})
check("reversed polarity warns", any("polarity" in w or "sign" in w or "floor" in w
                                      for w in p["reduce_warn"]),
      "; ".join(p["reduce_warn"]) or "(no warning!)")

# 7. Near-singular / near-coincident caps: cond(Zc) huge, must warn.
eps = 1e-15
Msing = np.array([[10e-9, 10e-9 - eps], [10e-9 - eps, 10e-9]])
p = reduce(Msing, ["P_pwr", "P_pwr1"], ["P_pwr", "P_pwr1"],
           {"cin_used": ["C1", "C2"]})
check("ill-conditioned warns", any("conditioned" in w for w in p["reduce_warn"]),
      f"cond={p['cond_Zc']:.1e}")

# 8. ESL physical point differs from ideal and moves the split.
Mbank = np.array([[Lf + Lb, Lf], [Lf, Lf + 2 * Lb]])  # asymmetric branches
ideal = reduce(Mbank, ["P_pwr", "P_pwr1"], ["P_pwr", "P_pwr1"])
phys = reduce(Mbank, ["P_pwr", "P_pwr1"], ["P_pwr", "P_pwr1"],
              cin_esl=1e-9, cin_esr=2e-3)
check("ESL yields physical point", phys["L_loop_physical"] is not None
      and abs(phys["L_loop"] - ideal["L_loop"]) > 1e-12,
      f"ideal={ideal['L_loop']*1e9:.3f} phys={phys['L_loop']*1e9:.3f} nH")

# 9. Conduction split: per-side R read at the LOWEST freq; r_hs+r_ls+r_sw == loop.
rows = ["P_pwr", "P_ghs", "P_gls", "P_bulk", "P_hs", "P_ls"]
ix = {p: i for i, p in enumerate(rows)}
n = len(rows)
Rhs, Rls, Rsw = 1.2e-3, 3.4e-3, 0.4e-3      # ohms (asymmetric: LS heavier)
Zlo = np.zeros((n, n), dtype=complex)
Zlo[ix["P_hs"], ix["P_hs"]] = Rhs
Zlo[ix["P_ls"], ix["P_ls"]] = Rls
Zlo[ix["P_bulk"], ix["P_bulk"]] = Rhs + Rls + Rsw
for k in range(n):                          # add inductive part (must not affect R)
    Zlo[k, k] += 1j * (2 * np.pi * 1e5) * 5e-9
zc9 = {1e5: Zlo, 5e6: Zlo * 50}             # min key 1e5 = conduction read point
topo9 = {"cond_ref": {"ref": "C10", "cls": "bulk"}}
pc = sr.reduce_parasitics(zc9, rows, topo9, {}, plateau=5e6, cin_ports=["P_pwr"])
check("conduction R_hs at f_dc", abs(pc["r_hs"] - Rhs) < 1e-12, f"{pc['r_hs']*1e3:.3f} mΩ")
check("conduction R_ls at f_dc", abs(pc["r_ls"] - Rls) < 1e-12, f"{pc['r_ls']*1e3:.3f} mΩ")
check("SW spreading residual", abs(pc["r_sw"] - Rsw) < 1e-12, f"{pc['r_sw']*1e3:.3f} mΩ")
check("cond_ref passthrough", (pc.get("cond_ref") or {}).get("ref") == "C10")

# 10. Reconstruction guard: r_hs+r_ls exceeding the LF loop R must warn.
Zbad = Zlo.copy()
Zbad[ix["P_bulk"], ix["P_bulk"]] = (Rhs + Rls - 0.5e-3) + 1j * (2 * np.pi * 1e5) * 5e-9
pb = sr.reduce_parasitics({1e5: Zbad, 5e6: Zbad * 50}, rows, topo9, {},
                          plateau=5e6, cin_ports=["P_pwr"])
check("conduction over-budget warns",
      any("conduction" in w for w in pb["reduce_warn"]),
      "; ".join(pb["reduce_warn"]) or "(no warning!)")

# 11. cin branch decomposition: shared Vin/GND trunk + private branch per cap.
L_sh, Lb = 8e-9, [2e-9, 3e-9, 5e-9]
R_sh, Rb = 0.5e-3, [1e-3, 2e-3, 0.5e-3]
m = len(Lb)
capports = ["P_pwr", "P_pwr1", "P_pwr2"]
Zpl = np.zeros((m, m), dtype=complex)     # plateau (L info)
Zlo2 = np.zeros((m, m), dtype=complex)    # low freq (R info)
wpl = 2 * np.pi * 5e6
for a in range(m):
    for b in range(m):
        Lij = L_sh + (Lb[a] if a == b else 0.0)
        Rij = R_sh + (Rb[a] if a == b else 0.0)
        Zpl[a, b] = 1j * wpl * Lij
        Zlo2[a, b] = Rij + 1j * (2 * np.pi * 1e5) * Lij
topo11 = {"cin_used": ["C1", "C2", "C3"],
          "cin_class": {"C1": "bulk", "C2": "bulk", "C3": "mlcc"}}
p11 = sr.reduce_parasitics({1e5: Zlo2, 5e6: Zpl}, capports, topo11, {},
                           plateau=5e6, cin_ports=capports)
brs = {b["ref"]: b for b in p11["cin_branches"]}
check("cin L_shared trunk", abs(p11["cin_L_shared"] - L_sh) < 1e-15,
      f"{p11['cin_L_shared']*1e9:.3f} nH")
check("cin Lb per cap (diag - trunk)",
      all(abs(brs[r]["Lb"] - Lb[i]) < 1e-15 for i, r in enumerate(["C1", "C2", "C3"])),
      ", ".join(f"{brs[r]['Lb']*1e9:.2f}" for r in ["C1", "C2", "C3"]))
check("cin Rb per cap at f_dc",
      all(abs(brs[r]["Rb"] - Rb[i]) < 1e-12 for i, r in enumerate(["C1", "C2", "C3"])),
      ", ".join(f"{brs[r]['Rb']*1e3:.2f}" for r in ["C1", "C2", "C3"]))
check("cin class passthrough",
      brs["C1"]["cls"] == "bulk" and brs["C3"]["cls"] == "mlcc")

# 12. cin_net label path: HF L_loop stays single-cap (P_pwr) while the branch
#     decomposition spans bulk caps that are NOT in cin_ports.
L_sh2, Lb2 = 6e-9, [1e-9, 2e-9, 4e-9]
allports = ["P_pwr", "P_cin_C2", "P_cin_C3"]
Zp = np.zeros((3, 3), dtype=complex)
Zl = np.zeros((3, 3), dtype=complex)
for a in range(3):
    for b in range(3):
        Lij = L_sh2 + (Lb2[a] if a == b else 0.0)
        Zp[a, b] = 1j * (2 * np.pi * 5e6) * Lij
        Zl[a, b] = (1e-3 if a == b else 0.5e-3) + 1j * (2 * np.pi * 1e5) * Lij
topo12 = {"cin_net": [{"ref": "C1", "cls": "mlcc", "label": "P_pwr"},
                      {"ref": "C2", "cls": "bulk", "label": "P_cin_C2"},
                      {"ref": "C3", "cls": "bulk", "label": "P_cin_C3"}]}
p12 = sr.reduce_parasitics({1e5: Zl, 5e6: Zp}, allports, topo12, {},
                           plateau=5e6, cin_ports=["P_pwr"])   # HF = single MLCC
check("cin_net: HF L_loop is single-cap P_pwr",
      abs(p12["L_loop"] - (L_sh2 + Lb2[0])) < 1e-15, f"{p12['L_loop']*1e9:.3f} nH")
check("cin_net: decomposition spans all 3 caps",
      len(p12["cin_branches"]) == 3 and
      {b["ref"] for b in p12["cin_branches"]} == {"C1", "C2", "C3"})
check("cin_net: bulk caps classified from cin_net",
      all(b["cls"] == "bulk" for b in p12["cin_branches"] if b["ref"] in ("C2", "C3")))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    raise SystemExit(1)
print("all reduction tests passed")
