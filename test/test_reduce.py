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

# tests live in test/; import the modules from the parent package dir
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    raise SystemExit(1)
print("all reduction tests passed")
