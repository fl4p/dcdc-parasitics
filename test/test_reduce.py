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

# 10b. CSI should use the side-specific switch ports when available. The full
#      Vin->GND loop mutual can cancel for HS, but gate-drive CSI is the mutual
#      with that switch's own source-lead path.
rows10b = ["P_pwr", "P_ghs", "P_gls", "P_hs", "P_ls"]
i10b = {p: i for i, p in enumerate(rows10b)}
L10b = np.eye(len(rows10b)) * 5e-9
L10b[i10b["P_pwr"], i10b["P_pwr"]] = 20e-9
L10b[i10b["P_ghs"], i10b["P_pwr"]] = L10b[i10b["P_pwr"], i10b["P_ghs"]] = 0.02e-9
L10b[i10b["P_gls"], i10b["P_pwr"]] = L10b[i10b["P_pwr"], i10b["P_gls"]] = 1.7e-9
L10b[i10b["P_ghs"], i10b["P_hs"]] = L10b[i10b["P_hs"], i10b["P_ghs"]] = 1.8e-9
L10b[i10b["P_gls"], i10b["P_ls"]] = L10b[i10b["P_ls"], i10b["P_gls"]] = 1.9e-9
p10b = reduce(L10b, rows10b, ["P_pwr"])
check("CSI side ports override full-loop HS cancellation",
      abs(p10b["csi_hs"] - 1.8e-9) < 1e-15 and
      abs(p10b["csi_hs_loop"] - 0.02e-9) < 1e-15,
      f"side={p10b['csi_hs']*1e9:.2f} loop={p10b['csi_hs_loop']*1e9:.2f} nH")
check("CSI side ports still report LS side mutual",
      abs(p10b["csi_ls"] - 1.9e-9) < 1e-15 and
      abs(p10b["csi_ls_loop"] - 1.7e-9) < 1e-15,
      f"side={p10b['csi_ls']*1e9:.2f} loop={p10b['csi_ls_loop']*1e9:.2f} nH")

# 10c. Per-device parallel-FET ports: each physical gate/source branch is preserved
#      in parallel_devices, while side-level scalars remain max-per-device
#      compatibility values for legacy consumers.
rows10c = ["P_pwr", "P_ghs_Q1", "P_ghs_Q3", "P_gls_Q2",
           "P_hs_Q1", "P_hs_Q3", "P_ls_Q2"]
i10c = {p: i for i, p in enumerate(rows10c)}
L10c = np.eye(len(rows10c)) * 4e-9
L10c[i10c["P_ghs_Q1"], i10c["P_ghs_Q1"]] = 9e-9
L10c[i10c["P_ghs_Q3"], i10c["P_ghs_Q3"]] = 14e-9
L10c[i10c["P_gls_Q2"], i10c["P_gls_Q2"]] = 7e-9
L10c[i10c["P_ghs_Q1"], i10c["P_hs_Q1"]] = L10c[i10c["P_hs_Q1"], i10c["P_ghs_Q1"]] = 1.1e-9
L10c[i10c["P_ghs_Q3"], i10c["P_hs_Q3"]] = L10c[i10c["P_hs_Q3"], i10c["P_ghs_Q3"]] = 2.4e-9
L10c[i10c["P_gls_Q2"], i10c["P_ls_Q2"]] = L10c[i10c["P_ls_Q2"], i10c["P_gls_Q2"]] = 0.8e-9
L10c[i10c["P_hs_Q1"], i10c["P_hs_Q1"]] = 11e-9
L10c[i10c["P_hs_Q3"], i10c["P_hs_Q3"]] = 16e-9
R10c = np.zeros_like(L10c)
R10c[i10c["P_pwr"], i10c["P_pwr"]] = 1e-3
R10c[i10c["P_hs_Q1"], i10c["P_hs_Q1"]] = 3e-3
R10c[i10c["P_hs_Q3"], i10c["P_hs_Q3"]] = 6e-3
R10c[i10c["P_ls_Q2"], i10c["P_ls_Q2"]] = 4e-3
Z10c = R10c.astype(complex) + 1j * W * L10c.astype(complex)
topo10c = {"parallel_fets": "per-device",
           "hs": {"device_ports": [
               {"ref": "Q1", "gate_label": "P_ghs_Q1", "switch_label": "P_hs_Q1"},
               {"ref": "Q3", "gate_label": "P_ghs_Q3", "switch_label": "P_hs_Q3"},
           ]},
           "ls": {"device_ports": [
               {"ref": "Q2", "gate_label": "P_gls_Q2", "switch_label": "P_ls_Q2"},
           ]}}
p10c = sr.reduce_parasitics({1e5: R10c.astype(complex), 5e6: Z10c}, rows10c, topo10c, {},
                            plateau=5e6, cin_ports=["P_pwr"])
hsdev = {d["ref"]: d for d in p10c["parallel_devices"]["hs"]}
check("per-device HS gate L preserved",
      abs(hsdev["Q1"]["L_gate"] - 9e-9) < 1e-15 and
      abs(hsdev["Q3"]["L_gate"] - 14e-9) < 1e-15,
      ", ".join(f'{r}={hsdev[r]["L_gate"]*1e9:.1f}nH' for r in ("Q1", "Q3")))
check("per-device HS CSI preserved",
      abs(hsdev["Q1"]["csi"] - 1.1e-9) < 1e-15 and
      abs(hsdev["Q3"]["csi"] - 2.4e-9) < 1e-15,
      ", ".join(f'{r}={hsdev[r]["csi"]*1e9:.1f}nH' for r in ("Q1", "Q3")))
check("per-device side scalar is conservative max",
      abs(p10c["L_gate_hs"] - 14e-9) < 1e-15 and
      abs(p10c["csi_hs"] - 2.4e-9) < 1e-15)
check("per-device switch-path L preserved",
      abs(hsdev["Q1"]["L_switch"] - 11e-9) < 1e-15 and
      abs(hsdev["Q3"]["L_switch"] - 16e-9) < 1e-15,
      ", ".join(f'{r}={hsdev[r]["L_switch"]*1e9:.1f}nH' for r in ("Q1", "Q3")))
check("per-device aggregate conduction R is parallel effective",
      abs(p10c["r_hs"] - 2e-3) < 1e-12,
      f'{p10c["r_hs"]*1e3:.3f} mΩ')

# 10d. Legacy lumped mode may have sidecar topology records, but they must not be
#      interpreted as per-device ports or warn as if per-device extraction ran.
rows10d = ["P_pwr", "P_ghs", "P_gls", "P_hs", "P_ls"]
L10d = np.eye(len(rows10d)) * 5e-9
L10d[1, 3] = L10d[3, 1] = 1.6e-9
topo10d = {"parallel_fets": "lumped",
           "hs": {"device_ports": [
               {"ref": "Q1", "gate_label": "P_ghs", "switch_label": "P_hs"},
               {"ref": "Q3", "gate_label": "P_ghs", "switch_label": "P_hs"},
           ]}}
p10d = reduce(L10d, rows10d, ["P_pwr"], topo10d)
check("lumped ignores device_ports manifest",
      "parallel_devices" not in p10d and
      not any("per-device parallel-FET" in w for w in p10d["reduce_warn"]),
      str(p10d.get("parallel_devices")))

# 10e. Ref fallback preserves normalized refs that contain underscores.
rows10e = ["P_pwr", "P_ghs_Q_1", "P_gls_Q2", "P_hs_Q_1", "P_ls_Q2"]
topo10e = {"parallel_fets": "per-device",
           "hs": {"device_ports": [
               {"gate_label": "P_ghs_Q_1", "switch_label": "P_hs_Q_1"},
           ]},
           "ls": {"device_ports": [
               {"ref": "Q2", "gate_label": "P_gls_Q2", "switch_label": "P_ls_Q2"},
           ]}}
p10e = reduce(np.eye(len(rows10e)) * 5e-9, rows10e, ["P_pwr"], topo10e)
check("per-device ref fallback preserves underscores",
      p10e["parallel_devices"]["hs"][0]["ref"] == "Q_1",
      p10e["parallel_devices"]["hs"][0]["ref"])

# 11. cin branch decomposition: shared Vin/GND trunk + private branch per cap.
L_sh, Lb = 8e-9, [2e-9, 3e-9, 5e-9]
R_sh, Rb = 0.5e-3, [1e-3, 2e-3, 0.5e-3]
R_sh_dc, Rb_dc = 0.3e-3, [0.7e-3, 1.4e-3, 0.35e-3]
m = len(Lb)
capports = ["P_pwr", "P_pwr1", "P_pwr2"]
Zpl = np.zeros((m, m), dtype=complex)     # plateau (L info)
Z100k2 = np.zeros((m, m), dtype=complex)  # scalar-network R damping basis
Zdc2 = np.zeros((m, m), dtype=complex)    # near-DC R reporting basis
wpl = 2 * np.pi * 5e6
for a in range(m):
    for b in range(m):
        Lij = L_sh + (Lb[a] if a == b else 0.0)
        Rij = R_sh + (Rb[a] if a == b else 0.0)
        Rij_dc = R_sh_dc + (Rb_dc[a] if a == b else 0.0)
        Zpl[a, b] = 1j * wpl * Lij
        Z100k2[a, b] = Rij + 1j * (2 * np.pi * 1e5) * Lij
        Zdc2[a, b] = Rij_dc + 1j * (2 * np.pi * 1e3) * Lij
topo11 = {"cin_used": ["C1", "C2", "C3"],
          "cin_class": {"C1": "bulk", "C2": "bulk", "C3": "mlcc"},
          "cin_net": [{"ref": "C1", "cls": "bulk", "label": "P_pwr"},
                      {"ref": "C2", "cls": "bulk", "label": "P_pwr1"},
                      {"ref": "C3", "cls": "mlcc", "label": "P_pwr2"}]}
p11 = sr.reduce_parasitics({1e3: Zdc2, 1e5: Z100k2, 5e6: Zpl}, capports, topo11, {},
                           plateau=5e6, cin_ports=capports)
brs = {b["ref"]: b for b in p11["cin_branches"]}
check("cin L_shared trunk", abs(p11["cin_L_shared"] - L_sh) < 1e-15,
      f"{p11['cin_L_shared']*1e9:.3f} nH")
check("cin Lb per cap (diag - trunk)",
      all(abs(brs[r]["Lb"] - Lb[i]) < 1e-15 for i, r in enumerate(["C1", "C2", "C3"])),
      ", ".join(f"{brs[r]['Lb']*1e9:.2f}" for r in ["C1", "C2", "C3"]))
check("cin Rb per cap at R_100k compatibility basis",
      all(abs(brs[r]["Rb"] - Rb[i]) < 1e-12 for i, r in enumerate(["C1", "C2", "C3"])),
      ", ".join(f"{brs[r]['Rb']*1e3:.2f}" for r in ["C1", "C2", "C3"]))
brs_dc = {b["ref"]: b for b in p11["cin_branches_lf"]}
check("cin Rb per cap at R_100k while LF view uses R_dc",
      all(abs(brs[r]["Rb"] - Rb[i]) < 1e-12 for i, r in enumerate(["C1", "C2", "C3"])) and
      all(abs(brs_dc[r]["Rb"] - Rb_dc[i]) < 1e-12 for i, r in enumerate(["C1", "C2", "C3"])) and
      abs(p11["cin_R_shared"] - R_sh) < 1e-12 and
      abs(p11["cin_R_shared_lf"] - R_sh_dc) < 1e-12,
      f'R100k={p11["cin_R_shared"]*1e3:.2f}m Rdc={p11["cin_R_shared_lf"]*1e3:.2f}m')
check("cin class passthrough",
      brs["C1"]["cls"] == "bulk" and brs["C3"]["cls"] == "mlcc")

# 11b. LF schematic data must come from the lowest swept frequency, not the
#      plateau/ring matrix used for the HF loop.
L_sh_lf, Lb_lf = 12e-9, [4e-9, 6e-9, 8e-9]
Zlo2_lf = np.zeros((m, m), dtype=complex)
for a in range(m):
    for b in range(m):
        Lij_lf = L_sh_lf + (Lb_lf[a] if a == b else 0.0)
        Rij = R_sh + (Rb[a] if a == b else 0.0)
        Zlo2_lf[a, b] = Rij + 1j * (2 * np.pi * 39e3) * Lij_lf
p11b = sr.reduce_parasitics({39e3: Zlo2_lf, 5e6: Zpl}, capports, topo11, {},
                            plateau=5e6, cin_ports=capports)
brs_lf = {b["ref"]: b for b in p11b["cin_branches_lf"]}
check("cin LF view uses lowest swept frequency for L_shared",
      abs(p11b["cin_L_shared_lf"] - L_sh_lf) < 1e-15 and
      abs(p11b["cin_L_shared"] - L_sh) < 1e-15,
      f'lf={p11b["cin_L_shared_lf"]*1e9:.2f}nH hf={p11b["cin_L_shared"]*1e9:.2f}nH')
check("cin LF view uses lowest swept frequency for Lb",
      all(abs(brs_lf[r]["Lb"] - Lb_lf[i]) < 1e-15 for i, r in enumerate(["C1", "C2", "C3"])),
      ", ".join(f"{brs_lf[r]['Lb']*1e9:.2f}" for r in ["C1", "C2", "C3"]))
check("cin LF view records actual frequency",
      abs(p11b["cin_branch_freq_Hz"] - 39e3) < 1e-9,
      f'{p11b["cin_branch_freq_Hz"]:g} Hz')

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

# 13. Regression (reviewer Finding 1): WITHOUT cin_net, cin_branches must stay None
#     even with >=2 cin_ports — never decompose the HF/cin-parallel set as if it
#     were the full bank (that silently drops the bulk caps).
p13 = sr.reduce_parasitics({1e5: Zl, 5e6: Zp}, allports, {"cin_used": ["C1", "C2", "C3"]},
                           {}, plateau=5e6, cin_ports=allports)   # no cin_net in topo
check("no cin_net => cin_branches is None (no partial-bank leak)",
      p13.get("cin_branches") is None, f"{p13.get('cin_branches')}")

# 14. Heterogeneous bank (bulk self-L >> mlcc) pulls the off-diagonal mean above
#     the mlcc diagonals; the trunk must clamp to min-diag so NO Lb goes negative
#     (the real Fugu2 C11/C12 finding).
Lmat = np.array([[8.5, 8.5, 10.0], [8.5, 8.6, 10.0], [10.0, 10.0, 15.0]]) * 1e-9
Zh = 1j * (2 * np.pi * 5e6) * Lmat.astype(complex)
cap3 = ["P_pwr", "P_pwr1", "P_pwr2"]
topo14 = {"cin_net": [{"ref": "C1", "cls": "mlcc", "label": "P_pwr"},
                      {"ref": "C2", "cls": "mlcc", "label": "P_pwr1"},
                      {"ref": "CB", "cls": "bulk", "label": "P_pwr2"}]}
p14 = sr.reduce_parasitics({1e5: Zh, 5e6: Zh}, cap3, topo14, {},
                           plateau=5e6, cin_ports=cap3)
b14 = {b["ref"]: b for b in p14["cin_branches"]}
check("heterogeneous: no negative Lb (mean-offdiag 9.5 > min-diag 8.5)",
      all(b["Lb"] >= 0 for b in p14["cin_branches"]),
      ", ".join(f'{r}={b14[r]["Lb"]*1e9:.2f}' for r in ["C1", "C2", "CB"]))
check("heterogeneous: raw trunk clamped to min diagonal",
      abs(p14["cin_L_shared_raw"] - 8.5e-9) < 1e-15,
      f'{p14["cin_L_shared_raw"]*1e9:.3f} nH')
check("heterogeneous: model trunk does not exceed selected loop",
      p14["cin_L_shared"] <= p14["L_loop"] + 1e-18,
      f'shared={p14["cin_L_shared"]*1e9:.3f} loop={p14["L_loop"]*1e9:.3f}')
check("heterogeneous: clamp warns",
      any("clamped" in w for w in p14["reduce_warn"]),
      "; ".join(w for w in p14["reduce_warn"] if "clamp" in w) or "(no warn!)")

# 15. Trunk-excluded switch-side residuals (cin_network double-count contract):
#     L_loop/r_hs/r_ls are the FULL bulk-anchored loop and overlap the cin trunk, so
#     consumers with cin_network must place the *_switch residuals in Lloop instead.
#     L_loop_switch = L_loop - cin_L_shared; the trunk R_shared is subtracted ONCE,
#     allocated per-side proportional to r_hs:r_ls. R_shared uses the 100 kHz
#     damping basis; the DC switch path must not clamp it downward.
w_pl15, w_100k15 = 2 * np.pi * 5e6, 2 * np.pi * 1e5
# plateau L (nH): P_pwr self 7, P_cin_C2 self 9, shared off-diag 6 -> cin_L_shared 6
Lpl15 = np.array([[7, 0, 0, 6], [0, 3, 0, 0],
                  [0, 0, 3, 0], [6, 0, 0, 9]]) * 1e-9
Zpl15 = 1j * w_pl15 * Lpl15.astype(complex)
# 100 kHz R damping basis (mOhm): r_hs=3 (P_hs diag), r_ls=6 (P_ls diag);
# P_pwr<->P_cin_C2 off-diag 6 -> cin_R_shared 6 (diagonals 8 >= 6, unclamped)
R100k15 = np.array([[8, 0, 0, 6], [0, 3, 0, 0],
                    [0, 0, 6, 0], [6, 0, 0, 8]]) * 1e-3
Z100k15 = R100k15.astype(complex) + 1j * w_100k15 * Lpl15.astype(complex)
ports15 = ["P_pwr", "P_hs", "P_ls", "P_cin_C2"]
topo15 = {"cin_net": [{"ref": "C1", "cls": "mlcc", "label": "P_pwr"},
                      {"ref": "C2", "cls": "bulk", "label": "P_cin_C2"}]}
p15 = sr.reduce_parasitics({1e5: Z100k15, 5e6: Zpl15}, ports15, topo15, {},
                           plateau=5e6, cin_ports=["P_pwr"])
check("residual: L_loop_switch = L_loop - cin_L_shared",
      abs(p15["L_loop_switch"] - 1e-9) < 1e-15, f'{p15["L_loop_switch"]*1e9:.3f} nH')
check("residual: r_hs_switch = r_hs - Rsh*r_hs/(r_hs+r_ls)",
      abs(p15["r_hs_switch"] - 1e-3) < 1e-9, f'{p15["r_hs_switch"]*1e3:.3f} mOhm')
check("residual: r_ls_switch = r_ls - Rsh*r_ls/(r_hs+r_ls)",
      abs(p15["r_ls_switch"] - 2e-3) < 1e-9, f'{p15["r_ls_switch"]*1e3:.3f} mOhm')
check("residual: trunk subtracted exactly once (sum = r_hs+r_ls-R_shared)",
      abs((p15["r_hs_switch"] + p15["r_ls_switch"])
          - (p15["r_hs"] + p15["r_ls"] - p15["cin_R_shared"])) < 1e-12)

# 15b. If the full-bank trunk basis exceeds the selected HF loop basis, preserve
#      raw diagnostics but clamp the model fields consumed by the loss deck.
Lpl15b = np.array([[7, 3, 0, 6],
                   [3, 7, 0, 0],
                   [0, 0, 3, 0],
                   [6, 0, 0, 9]]) * 1e-9
Zpl15b = 1j * w_pl15 * Lpl15b.astype(complex)
R100k15b = np.array([[8, 0, 0, 6], [0, 8, 0, 0],
                     [0, 0, 3, 0], [6, 0, 0, 8]]) * 1e-3
Z100k15b = R100k15b.astype(complex) + 1j * w_100k15 * Lpl15b.astype(complex)
ports15b = ["P_pwr", "P_pwr1", "P_ls", "P_cin_C2"]
topo15b = {"cin_net": [{"ref": "C1", "cls": "mlcc", "label": "P_pwr"},
                       {"ref": "C2", "cls": "bulk", "label": "P_cin_C2"}]}
p15b = sr.reduce_parasitics({1e5: Z100k15b, 5e6: Zpl15b}, ports15b, topo15b, {},
                            plateau=5e6, cin_ports=["P_pwr", "P_pwr1"])
b15b = {b["ref"]: b for b in p15b["cin_branches"]}
b15b_raw = {b["ref"]: b for b in p15b["cin_branches_raw"]}
check("residual clamp: raw shared kept",
      abs(p15b["cin_L_shared_raw"] - 6e-9) < 1e-15,
      f'{p15b["cin_L_shared_raw"]*1e9:.3f} nH')
check("residual clamp: model shared limited to selected loop",
      p15b["cin_shared_model"]["clamped"] and
      abs(p15b["cin_L_shared"] - p15b["L_loop"]) < 1e-15,
      f'shared={p15b["cin_L_shared"]*1e9:.3f} loop={p15b["L_loop"]*1e9:.3f}')
check("residual clamp: model switch L is zero but raw is negative",
      abs(p15b["L_loop_switch"]) < 1e-18 and p15b["L_loop_switch_raw"] < 0,
      f'model={p15b["L_loop_switch"]*1e9:.3f} raw={p15b["L_loop_switch_raw"]*1e9:.3f}')
check("residual clamp: branches recomputed from model shared",
      b15b["C1"]["Lb"] > b15b_raw["C1"]["Lb"] and
      b15b["C2"]["Lb"] > b15b_raw["C2"]["Lb"],
      f'C1 {b15b_raw["C1"]["Lb"]*1e9:.2f}->{b15b["C1"]["Lb"]*1e9:.2f} nH')
R100k15c = np.array([[8, 0, 0, 6], [0, 1, 0, 0],
                     [0, 0, 1, 0], [6, 0, 0, 8]]) * 1e-3
Rdc15c = np.array([[0.8, 0, 0, 0.6], [0, 1, 0, 0],
                   [0, 0, 1, 0], [0.6, 0, 0, 0.8]]) * 1e-3
Z100k15c = R100k15c.astype(complex) + 1j * w_100k15 * Lpl15.astype(complex)
Zdc15c = Rdc15c.astype(complex) + 1j * (2 * np.pi * 1e3) * Lpl15.astype(complex)
p15c = sr.reduce_parasitics({1e3: Zdc15c, 1e5: Z100k15c, 5e6: Zpl15},
                            ports15, topo15, {}, plateau=5e6,
                            cin_ports=["P_pwr"])
check("scalar R_100k trunk is not clamped by DC switch path",
      abs(p15c["cin_R_shared"] - 6e-3) < 1e-12 and
      abs(p15c["cin_R_shared_lf"] - 0.6e-3) < 1e-12,
      f'R100k={p15c["cin_R_shared"]*1e3:.2f}m Rdc={p15c["cin_R_shared_lf"]*1e3:.2f}m')
check("scalar cin model invalid when residual is negative",
      p15b["cin_model_valid"] is False,
      str(p15b.get("cin_model")))
check("scalar cin model reports negative residual diagnostic",
      any(d.get("code") == "negative_switch_residual"
          for d in p15b["cin_model_diagnostics"]),
      str(p15b["cin_model_diagnostics"]))
check("switch separability is not evaluated without cap-only matrix",
      p15b["cin_model"]["switch_separability"]["status"] == "not_evaluated",
      str(p15b["cin_model"]["switch_separability"]))

# 16. Matrix request on a pad-ideal/no-lead fixture resolves to identity basis:
# the full Cin port matrix is the model; there is no separate switch trunk/gauge.
L16 = np.array([[4.0, 3.0], [3.0, 5.0]]) * 1e-9
R16 = np.array([[8.0, 1.0], [1.0, 9.0]]) * 1e-3
R16dc = np.array([[5.0, 0.4], [0.4, 6.0]]) * 1e-3
Z16 = R16.astype(complex) + 1j * w_pl15 * L16.astype(complex)
Z16dc = R16dc.astype(complex) + 1j * (2 * np.pi * 1e3) * L16.astype(complex)
Z16_100k = R16.astype(complex) + 1j * (2 * np.pi * 1e5) * L16.astype(complex)
topo16 = {
    "cin_network_model": "matrix",
    "fet_closure": "pad_ideal",
    "cin_net": [
        {"ref": "C1", "cls": "mlcc", "label": "P_pwr"},
        {"ref": "C2", "cls": "mlcc", "label": "P_pwr1"},
    ],
}
p16 = sr.reduce_parasitics({1e3: Z16dc, 1e5: Z16_100k, 5e6: Z16},
                           ["P_pwr", "P_pwr1"], topo16, {},
                           plateau=5e6, cin_ports=["P_pwr", "P_pwr1"])
check("identity matrix resolves mode=matrix",
      p16["cin_model"]["mode"] == "matrix" and p16["cin_model"]["basis"] == "identity",
      str(p16["cin_model"]))
check("identity matrix is valid for mode",
      p16["cin_model_valid"] is True and p16["cin_model"]["matrix_valid"] is True,
      str(p16["cin_model"]))
check("identity matrix carries full L/R matrix and zero trunk",
      p16["cin_matrix"]["L"] == L16.tolist() and
      p16["cin_matrix"]["R"] == R16.tolist() and
      p16["cin_matrix"]["R_100k"] == R16.tolist() and
      p16["cin_matrix"]["R_dc"] == R16dc.tolist() and
      p16["cin_matrix"]["R_100k_freq_Hz"] == 1e5 and
      p16["cin_matrix"]["R_dc_freq_Hz"] == 1e3 and
      p16["cin_matrix"]["L_sw_element"] == 0.0,
      str(p16["cin_matrix"]))
check("identity matrix gauge structurally not required",
      p16["cin_model"]["gauge_fix_status"] == "structurally_not_required" and
      p16["cin_model"]["gauge_fix_reason"] == "zero_by_plane_p_equiv" and
      p16["cin_matrix"]["switch_board_copper"] == "in_matrix",
      str(p16["cin_model"]))

# Exact rail equality must be refused by the producer, matching the loss consumer's
# abs(K) >= 0.95 refusal.
L16b = np.array([[1.0, 0.95], [0.95, 1.0]]) * 1e-9
R16b = np.eye(2) * 1e-3
Z16b = R16b.astype(complex) + 1j * w_pl15 * L16b.astype(complex)
p16b = sr.reduce_parasitics({5e6: Z16b}, ["P_pwr", "P_pwr1"], topo16, {},
                            plateau=5e6, cin_ports=["P_pwr", "P_pwr1"])
check("identity matrix refuses exact K rail",
      p16b["cin_matrix"]["kmax"] == 0.95 and
      p16b["cin_matrix"]["spice_realizable"] is False and
      p16b["cin_model_valid"] is False,
      str(p16b["cin_matrix"]))

# 17. Additive switch-separability fit with explicit port gauge.
refs17 = ["C1", "C2", "C3"]
Lcap17 = np.diag([4.0, 5.0, 6.0]) * 1e-9
Lcap17[0, 1] = Lcap17[1, 0] = 1.2e-9
Lcap17[0, 2] = Lcap17[2, 0] = 0.8e-9
Lcap17[1, 2] = Lcap17[2, 1] = 1.1e-9
Lsw17 = 0.4e-9
mphys17 = np.array([0.1, 0.2, -0.05]) * 1e-9
delta17 = Lsw17 + mphys17[:, None] + mphys17[None, :]
fit17 = sr._fit_switch_additive_delta(Lcap17 + delta17, Lcap17, refs17, Lsw17,
                                      floor=0.06e-9)
check("additive fit recovers physical gauge m_i",
      np.allclose(fit17["m_i_physical"], mphys17, atol=1e-18),
      str(fit17))
check("additive fit emits modeling gauge element values",
      abs(fit17["regauge_c"] - 0.1e-9) < 1e-18 and
      abs(fit17["L_sw_element"] - 0.6e-9) < 1e-18 and
      np.allclose(fit17["m_i_modeling"], [0.0, 0.1e-9, -0.15e-9], atol=1e-18),
      str(fit17))
check("additive fit residual is zero for separable matrix",
      fit17["residual_fro"] < 1e-18 and fit17["residual_max_abs"] < 1e-18,
      str(fit17))
check("additive fit significant couplings use modeling gauge floor",
      [d["ref"] for d in fit17["significant_couplings"]] == ["C2", "C3"],
      str(fit17["significant_couplings"]))

# Non-exact data must use the same all-pairs objective as the reported
# Frobenius residual, not an unweighted upper-triangle fit.
B17b = np.array([
    [2.0, 4.0, 1.0],
    [4.0, 5.0, 3.0],
    [1.0, 3.0, 7.0],
]) * 1e-10
Lsw17b = 0.5e-9
fit17b = sr._fit_switch_additive_delta(Lcap17 + Lsw17b + B17b, Lcap17,
                                       refs17, Lsw17b)
s17b = float(np.sum(B17b) / (2.0 * len(refs17)))
expected_m17b = (np.sum(B17b, axis=1) - s17b) / len(refs17)
expected_recon17b = Lsw17b + expected_m17b[:, None] + expected_m17b[None, :]
expected_resid17b = (Lsw17b + B17b) - expected_recon17b
check("additive fit noisy case uses all-pairs Frobenius objective",
      np.allclose(fit17b["m_i_physical"], expected_m17b, atol=1e-18) and
      abs(fit17b["residual_fro"] - np.linalg.norm(expected_resid17b, ord="fro")) < 1e-18,
      str(fit17b))

try:
    sr._fit_switch_additive_delta(Lcap17 - 0.2e-9, Lcap17, refs17, 0.1e-9)
except ValueError as e:
    check("additive fit refuses nonpositive modeling switch element",
          "L_sw_element" in str(e), str(e))
else:
    check("additive fit refuses nonpositive modeling switch element", False,
          "expected ValueError")

# 18. Future decomposed matrix payload: cap-only matrix plus L_sw_element and
# modeling-gauge K(cap, L_sw) couplings.
Rcap18 = np.eye(3) * 1e-3
payload18 = sr._cin_decomposed_matrix_payload(
    Lcap17 + delta17, Lcap17, Rcap18, refs17, Lsw17, floor=0.06e-9)
check("decomposed matrix payload resolves sw-coupling mode",
      payload18["mode"] == "matrix_with_sw_coupling" and
      payload18["basis"] == "cap_only_additive" and
      payload18["gauge_fix_status"] == "fixed" and
      payload18["switch_separability"]["status"] == "passed" and
      payload18["decomposition_valid"] is True,
      str(payload18))
check("decomposed matrix payload emits L_sw element and cap-switch K",
      abs(payload18["L_sw_element"] - 0.6e-9) < 1e-18 and
      [d["ref"] for d in payload18["switch_couplings"]] == ["C2", "C3"] and
      all(abs(d["K"]) > 0 for d in payload18["switch_couplings"]),
      str(payload18["switch_couplings"]))
payload18b = sr._cin_decomposed_matrix_payload(
    Lcap17 + delta17, Lcap17, Rcap18, refs17, Lsw17, floor=0.2e-9)
check("decomposed matrix payload resolves plain matrix when m_i below floor",
      payload18b["mode"] == "matrix" and not payload18b["switch_couplings"],
      str(payload18b))

Lcap18c = np.eye(3) * 1e-9
mphys18c = np.array([0.0, 0.0, 0.95]) * 1e-9
Lsw18c = 1e-9
delta18c = Lsw18c + mphys18c[:, None] + mphys18c[None, :]
payload18c = sr._cin_decomposed_matrix_payload(
    Lcap18c + delta18c, Lcap18c, Rcap18, refs17, Lsw18c, floor=0.01e-9)
check("decomposed matrix payload refuses exact switch K rail",
      np.isclose(payload18c["switch_kmax"], 0.95, rtol=0.0, atol=1e-12) and
      payload18c["spice_realizable"] is False,
      str(payload18c))

payload18d = sr._cin_decomposed_matrix_payload(
    Lcap17 + Lsw17b + B17b, Lcap17, Rcap18, refs17, Lsw17b, floor=1e-12)
check("decomposed matrix payload falls back on nonseparable residual",
      payload18d["mode"] == "none" and
      payload18d["full_multiport_required"] is True and
      payload18d["full_multiport_valid"] is False and
      payload18d["decomposition_valid"] is False and
      payload18d["switch_separability"]["status"] == "failed" and
      payload18d["separability_fit"]["residual_fro"] > payload18d["separability_fit"]["floor"],
      str(payload18d))
check("none mode is not valid until full-multiport emission exists",
      sr._cin_model_valid_for_mode({
          "mode": "none",
          "full_multiport_required": True,
          "full_multiport_valid": False,
      }) is False)

# 19. Reducer-side handoff from full/cap-only/switch-residual runs.
cin_net19 = [
    {"ref": "C1", "label": "P_pwr"},
    {"ref": "C2", "label": "P_pwr1"},
    {"ref": "C3", "label": "P_pwr2"},
]
full19 = dict(
    ports=["P_pwr", "P_pwr1", "P_pwr2"],
    port_L=(Lcap17 + delta17).tolist(),
    port_R=Rcap18.tolist(),
    port_R_100k=(Rcap18 * 2.0).tolist(),
    port_R_dc=(Rcap18 * 0.5).tolist(),
    R_100k_freq_Hz=1e5,
    R_dc_freq_Hz=1e3,
    topo={"cin_extraction_basis": "full_loop", "cin_net": cin_net19})
cap19 = dict(
    ports=["P_pwr", "P_pwr1", "P_pwr2"],
    port_L=Lcap17.tolist(),
    port_R=Rcap18.tolist(),
    port_R_100k=(Rcap18 * 2.0).tolist(),
    port_R_dc=(Rcap18 * 0.5).tolist(),
    R_100k_freq_Hz=1e5,
    R_dc_freq_Hz=1e3,
    topo={"cin_extraction_basis": "cap_only", "cin_net": cin_net19})
sw19 = dict(
    ports=["P_sw_residual"],
    port_L=[[Lsw17]],
    port_R=[[0.0]],
    topo={"cin_extraction_basis": "switch_residual",
          "demarcation_plane": {"switch_residual_port": "P_sw_residual"}})
payload19 = sr._cin_matrix_from_reductions(full19, cap19, sw19, floor=0.06e-9)
check("cin matrix combiner builds decomposed payload from three runs",
      payload19["mode"] == "matrix_with_sw_coupling" and
      payload19["refs"] == refs17 and
      payload19["source_runs"]["switch_residual_port"] == "P_sw_residual" and
      payload19["R"] == (Rcap18 * 2.0).tolist() and
      payload19["R_100k"] == (Rcap18 * 2.0).tolist() and
      payload19["R_dc"] == (Rcap18 * 0.5).tolist() and
      payload19["R_100k_freq_Hz"] == 1e5 and
      payload19["R_dc_freq_Hz"] == 1e3,
      str(payload19))

full19_partial = dict(full19)
cap19_partial = dict(cap19)
full19_partial["ports"] = ["P_pwr", "P_pwr2"]
cap19_partial["ports"] = ["P_pwr", "P_pwr2"]
try:
    sr._cin_matrix_from_reductions(full19_partial, cap19_partial, sw19, floor=0.06e-9)
except ValueError as e:
    check("cin matrix combiner refuses partial cin_net ports",
          "missing from solved ports" in str(e), str(e))
else:
    check("cin matrix combiner refuses partial cin_net ports", False,
          "expected ValueError")

cap19_bad = dict(cap19)
cap19_bad["topo"] = {"cin_extraction_basis": "cap_only",
                     "cin_net": [dict(cin_net19[1]), dict(cin_net19[0]), dict(cin_net19[2])]}
try:
    sr._cin_matrix_from_reductions(full19, cap19_bad, sw19, floor=0.06e-9)
except ValueError as e:
    check("cin matrix combiner refuses mismatched cap refs",
          "refs differ" in str(e), str(e))
else:
    check("cin matrix combiner refuses mismatched cap refs", False,
          "expected ValueError")

cap19_wrong_basis = dict(cap19)
cap19_wrong_basis["topo"] = dict(cap19["topo"])
cap19_wrong_basis["topo"]["cin_extraction_basis"] = "full_loop"
try:
    sr._cin_matrix_from_reductions(full19, cap19_wrong_basis, sw19, floor=0.06e-9)
except ValueError as e:
    check("cin matrix combiner refuses wrong run basis",
          "expected 'cap_only'" in str(e), str(e))
else:
    check("cin matrix combiner refuses wrong run basis", False,
          "expected ValueError")

sw19_multi = dict(sw19)
sw19_multi["topo"] = {
    "cin_extraction_basis": "switch_residual",
    "demarcation_plane": {"switch_residual_ports": ["P_sw_residual", "P_sw_residual2"]},
}
try:
    sr._cin_matrix_from_reductions(full19, cap19, sw19_multi, floor=0.06e-9)
except ValueError as e:
    check("cin matrix combiner refuses declared multiport switch gauge",
          "exactly one gauge port" in str(e), str(e))
else:
    check("cin matrix combiner refuses declared multiport switch gauge", False,
          "expected ValueError")

sw19_bad = dict(sw19)
sw19_bad["ports"] = []
try:
    sr._cin_matrix_from_reductions(full19, cap19, sw19_bad, floor=0.06e-9)
except ValueError as e:
    check("cin matrix combiner refuses missing switch gauge port",
          "gauge port" in str(e), str(e))
else:
    check("cin matrix combiner refuses missing switch gauge port", False,
          "expected ValueError")

# without cin_net the residuals must stay None (nothing to subtract)
check("residual: None when no cin_net",
      p13.get("L_loop_switch") is None and p13.get("r_hs_switch") is None)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    raise SystemExit(1)
print("all reduction tests passed")
