#!/usr/bin/env python3
"""Unit tests for the pure-python layers — run: python3 test_parasitics.py

Covers the parts that don't need KiCad/FastHenry: emit_svg formatting + SVG
rendering, the solve_reduce reduction maths (single-cap, parallel-cap effective
L, common-source mutual), and Model.weld (the mesh de-fragmentation fix). The
geometry+solve path (pcbnew, fasthenry) is validated by running on the real
mppt-2420-hc / Fugu2 boards — see README.

Plain asserts, no framework, matching the repo style.
"""
import os
import sys
import types

# tests live in test/; import the modules from the parent package dir
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import emit_svg  # noqa: E402

# kicad_geom does `import pcbnew` at module top; stub it so we can import the
# pure Model class (weld/node/seg use no pcbnew).
sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
import kicad_geom  # noqa: E402
import solve_reduce  # noqa: E402

W = 2 * np.pi * 5e6  # a low-MHz plateau


def _topo(**over) -> dict:
    """Fresh topology dict; override any key (hs, ls, cin, cin_used, ...)."""
    t: dict = dict(pcb="/x/b.kicad_pcb", sw="/DC/DC/SW_NODE", gnd="GND",
                   vin="/DCDC_HV+", cin=[],
                   hs=dict(refs=["Q1"], gate="HG", kelvin=False),
                   ls=dict(refs=["Q2"], gate="LG", kelvin=False))
    t.update(over)
    return t


def _p(**kw) -> dict:
    """Minimal reduced-parasitics dict for schematic() tests."""
    p: dict = dict(L_loop=7e-9, R_loop=4e-3, csi_hs=0.6e-9, csi_ls=1.2e-9,
                   L_gate_hs=9e-9, R_gate_hs=0.9, L_gate_ls=8e-9, R_gate_ls=0.9,
                   m_gate=1e-10, freq_Hz=4.64e6, meta=dict(pitch=2.0, lead_mm=3.0),
                   topo=_topo())
    p.update(kw)
    return p


# ---------------------------------------------------------------- formatting
def test_rkm():
    assert emit_svg._rkm("3R3") == "3.3 Ω"
    assert emit_svg._rkm("10R") == "10 Ω"
    assert emit_svg._rkm("4K7") == "4.7 kΩ"
    assert emit_svg._rkm("4.7, 1%") == "4.7 Ω"      # plain value + tolerance
    assert emit_svg._rkm("7R5") == "7.5 Ω"
    assert emit_svg._rkm("SS14FL") == "SS14FL"       # unrecognised passes through


def test_leaf():
    assert emit_svg._leaf("/DC/DC/SW_NODE") == "SW_NODE"
    assert emit_svg._leaf("GND") == "GND"
    assert emit_svg._leaf("Solar+") == "Solar+"
    assert emit_svg._leaf("") == ""


# ------------------------------------------------------------------- svg render
def test_schematic_wellformed():
    svg = emit_svg.schematic(_p())
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "SW_NODE" in svg and "VIN" in svg          # leaf net labels
    assert "Lscs_hs" in svg and "Lscs_ls" in svg      # CSI branches


def test_schematic_parallel_fets():
    svg = emit_svg.schematic(_p(topo=_topo(
        hs=dict(refs=["Q1", "Q3"], gate="HG", kelvin=False))))
    assert "Q1∥Q3" in svg          # paralleled pair labelled with both refs


def test_schematic_gate_network():
    hs = dict(refs=["Q1"], gate="HG", kelvin=False,
              gate_drive=dict(r=dict(ref="R1", value="3R3", driver_net="HO"),
                              d=dict(ref="D14", value="SS14FL", driver_net="HO"),
                              driver_net="HO"))
    svg = emit_svg.schematic(_p(topo=_topo(hs=hs)))
    assert "R1 3.3 Ω" in svg        # RKM-decoded series gate resistor
    assert "∥ D14" in svg           # anti-parallel diode annotation


def test_schematic_cin_bank():
    p = _p(topo=_topo(cin=["C1", "C3", "C2", "C4"], cin_used=["C1", "C3"]),
           n_cin=2, L_loop_single=8.5e-9,
           current_split={"C1": dict(re=0.6, im=0, mag=0.6),
                          "C3": dict(re=0.4, im=0, mag=0.4)})
    svg = emit_svg.schematic(p)
    assert "60%" in svg and "40%" in svg   # per-cap current split
    assert "not in" in svg                 # C2/C4 greyed out


def test_schematic_kelvin_toggle():
    # subtitle carries "(Kelvin)" vs "(non-Kelvin)" — the parenthesised form
    # distinguishes them ("(non-Kelvin)" does not contain "(Kelvin)").
    non = emit_svg.schematic(_p())
    kel = emit_svg.schematic(_p(topo=_topo(hs=dict(refs=["Q1"], gate="HG", kelvin=True))))
    assert "(Kelvin)" in kel and "(Kelvin)" not in non


# -------------------------------------------------------------- reduction maths
def _zmat(order, L, R=None):
    """Build a symmetric complex port matrix from L[i][j] (henry) and optional R."""
    n = len(order)
    Z = np.zeros((n, n), dtype=complex)
    for i in range(n):
        for j in range(n):
            r = 0.0 if R is None else R[i][j]
            Z[i, j] = complex(r, W * L[i][j])
    return Z


def test_reduce_single_cap():
    ports = ["P_pwr", "P_ghs", "P_gls"]
    L = [[8e-9, 0.7e-9, 0.1e-9],
         [0.7e-9, 9e-9, 0.05e-9],
         [0.1e-9, 0.05e-9, 7e-9]]
    R = [[4e-3, 0, 0], [0, 0.9, 0], [0, 0, 0.9]]
    p: dict = solve_reduce.reduce_parasitics({5e6: _zmat(ports, L, R)}, ports, {}, {})
    assert abs(p["L_loop"] - 8e-9) < 1e-12
    assert abs(p["R_loop"] - 4e-3) < 1e-9
    assert abs(p["csi_hs"] - 0.7e-9) < 1e-12       # |M(pwr,ghs)|
    assert abs(p["L_gate_hs"] - 9e-9) < 1e-12
    assert p["n_cin"] == 1


def test_reduce_parallel_lowers_L():
    # two cap ports (8 nH, 10 nH, mutual 3 nH) in parallel + a gate port
    ports = ["P_pwr", "P_c2", "P_ghs"]
    L = [[8e-9, 3e-9, 0.7e-9],
         [3e-9, 10e-9, 0.4e-9],
         [0.7e-9, 0.4e-9, 9e-9]]
    zc = {5e6: _zmat(ports, L)}
    p: dict = solve_reduce.reduce_parasitics(zc, ports, {}, {}, cin_ports=["P_pwr", "P_c2"])
    assert p["n_cin"] == 2
    assert abs(p["L_loop_single"] - 8e-9) < 1e-12          # nearest single cap
    # effective (L1*L2 - M^2)/(L1+L2-2M) = (80-9)/(18-6) = 5.916.. nH
    assert p["L_loop"] < p["L_loop_single"]
    assert abs(p["L_loop"] - 71e-9 / 12) < 1e-11


# ------------------------------------------------------------------- weld pass
def test_weld_bonds_near_same_net():
    m = kicad_geom.Model()
    a = m.node("SW", 0, 10.0, 10.0, 0.0)
    b = m.node("SW", 0, 10.3, 10.0, 0.0)      # 0.3 mm away, same net+layer
    assert a != b
    n = m.weld(0.6)
    assert n >= 1
    linked = any({s[1], s[2]} == {a, b} for s in m.segs)
    assert linked, "near-coincident same-net nodes should be welded"


def test_weld_ignores_other_net_and_far():
    m = kicad_geom.Model()
    a = m.node("SW", 0, 10.0, 10.0, 0.0)
    other = m.node("GND", 0, 10.1, 10.0, 0.0)   # different net, close
    far = m.node("SW", 0, 20.0, 10.0, 0.0)       # same net, far
    m.weld(0.6)
    for s in m.segs:
        pair = {s[1], s[2]}
        assert pair != {a, other}, "must not weld across nets"
        assert pair != {a, far}, "must not weld beyond tolerance"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
