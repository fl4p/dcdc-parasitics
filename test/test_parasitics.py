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

# tests live in test/; the library modules live in ../lib
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import numpy as np  # noqa: E402

import emit  # noqa: E402
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


def test_schematic_per_device_callout():
    p = _p(topo=_topo(hs=dict(refs=["Q1", "Q3"], gate="HG", kelvin=False)),
           parallel_devices=dict(
               hs=[
                   dict(ref="Q1", gate_port="P_ghs_Q1", switch_port="P_hs_Q1",
                        L_gate=9e-9, csi=1.1e-9, csi_loop=0.2e-9, r_switch=3e-3),
                   dict(ref="Q3", gate_port="P_ghs_Q3", switch_port="P_hs_Q3",
                        L_gate=14e-9, csi=2.4e-9, csi_loop=0.4e-9, r_switch=6e-3),
               ],
               ls=[]))
    svg = emit_svg.schematic(p)
    assert "per-device FET parasitics" in svg
    assert "HS Q1: Lg 9.00 nH, CSI 1.10 nH" in svg
    assert "HS Q3: Lg 14.00 nH, CSI 2.40 nH" in svg


def test_markdown_per_device_table():
    p = _p(topo=_topo(hs=dict(refs=["Q1", "Q3"], gate="HG", kelvin=False)),
           n_cin=1, L_loop_single=7e-9,
           parallel_devices=dict(
               hs=[
                   dict(ref="Q1", gate_port="P_ghs_Q1", switch_port="P_hs_Q1",
                        L_gate=9e-9, csi=1.1e-9, csi_loop=0.2e-9,
                        L_switch=11e-9, r_switch=3e-3),
               ],
               ls=[]))
    md = emit.markdown(p)
    assert "## Per-device parallel FET parasitics" in md
    assert "| HS | Q1 | `P_ghs_Q1` | `P_hs_Q1` | 9.00 nH | 1.10 nH | 0.20 nH | 11.00 nH | 3.00 mΩ |" in md


def test_missing_gate_ports_render_unavailable_not_zero():
    """Gate ports absent (--allow-missing-gate-ports): csi/L_gate come back None.
    report.md must LABEL them, and the .lib must mark 0-valued branches as
    placeholders — never a bare 0.00 nH that reads as a measured zero."""
    p = _p(csi_hs=None, csi_ls=None, csi_hs_loop=None, csi_ls_loop=None,
           L_gate_hs=None, R_gate_hs=None, L_gate_ls=None, R_gate_ls=None,
           m_gate=None, gate_ports_available=dict(hs=False, ls=False))
    md = emit.markdown(p)
    assert "| HS common-source inductance | **n/a — gate routing unavailable** |" in md
    assert "| LS common-source inductance | **n/a — gate routing unavailable** |" in md
    assert "| HS gate-loop L | n/a — gate routing unavailable |" in md
    assert "7.00 nH" in md                      # L_loop still valid + numeric

    lib, warn = emit.subckt(p)
    assert "PLACEHOLDER" in lib                  # branch labelled, not a bare 0
    assert any("UNAVAILABLE" in w for w in warn)

    # a normal (gate-present) dict still renders numeric CSI, no label
    md2 = emit.markdown(_p())
    assert "0.60 nH" in md2 and "gate routing unavailable" not in md2


def test_schematic_renders_unavailable_gate_as_na_not_zero():
    """--svg must label null csi/L_gate as 'n/a', not a fabricated 0.00 nH."""
    svg = emit_svg.schematic(_p(
        csi_hs=None, csi_ls=None, L_gate_hs=None, R_gate_hs=None,
        L_gate_ls=None, R_gate_ls=None, m_gate=None,
        gate_ports_available=dict(hs=False, ls=False)))
    assert "n/a" in svg
    # the CSI/gate labels specifically must not read as a measured zero. L_loop
    # (7 nH) legitimately renders numerically, so only assert no 0.00 *nH* leaked
    # from the now-None gate fields: with all gate fields None, a "0.00 nH" would
    # only come from the bug this guards.
    assert "0.00 nH" not in svg
    # a normal dict still shows numeric CSI and no n/a
    svg2 = emit_svg.schematic(_p())
    assert "2.40 nH" in svg2 or "0.60 nH" in svg2


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


def test_model_uses_configured_copper_thickness():
    m = kicad_geom.Model(cu_thickness=0.07)
    a = m.node("N", 0, 0.0, 0.0, 0.0)
    b = m.node("N", 0, 1.0, 0.0, 0.0)
    m.seg(a, b, 0.5)
    assert m.segs[-1][4] == 0.07


def test_demarcation_plane_cap_only_whole_cell_bridge():
    m = kicad_geom.Model()
    hs1 = m.node("VIN", 0, 0.0, 0.0, 0.0)
    hs2 = m.node("VIN", 0, 1.0, 0.0, 0.0)
    ls1 = m.node("GND", 0, 2.0, 0.0, 0.0)
    ls2 = m.node("GND", 0, 3.0, 0.0, 0.0)
    topo = dict(
        hs=dict(_devices=[dict(_drn_pad_node=hs1), dict(_drn_pad_node=hs2)]),
        ls=dict(_devices=[dict(_src_pad_node=ls1), dict(_src_pad_node=ls2)]))
    kicad_geom.setup_demarcation_plane(m, topo, "cap_only", closure="cell_bridge")
    eq = {frozenset(e) for e in m.equivs}
    assert frozenset((hs1, hs2)) in eq
    assert frozenset((ls1, ls2)) in eq
    assert frozenset((hs1, ls1)) in eq
    plane = topo["demarcation_plane"]
    assert plane["closure"] == "cell_bridge"
    assert plane["cap_only_bridge"] == [hs1, ls1]


def test_demarcation_plane_per_fet_does_not_bridge_groups():
    m = kicad_geom.Model()
    hs = m.node("VIN", 0, 0.0, 0.0, 0.0)
    ls = m.node("GND", 0, 1.0, 0.0, 0.0)
    topo = dict(hs=dict(_drn_pad_node=hs), ls=dict(_src_pad_node=ls))
    kicad_geom.setup_demarcation_plane(m, topo, "cap_only", closure="per_fet")
    assert m.equivs == []
    plane = topo["demarcation_plane"]
    assert plane["closure"] == "per_fet"
    assert "cap_only_bridge" not in plane


def test_demarcation_plane_switch_residual_port():
    m = kicad_geom.Model()
    hs = m.node("VIN", 0, 0.0, 0.0, 0.0)
    ls = m.node("GND", 0, 1.0, 0.0, 0.0)
    topo = dict(hs=dict(_drn_pad_node=hs), ls=dict(_src_pad_node=ls))
    kicad_geom.setup_demarcation_plane(m, topo, "switch_residual", closure="cell_bridge")
    assert ("P_sw_residual", hs, ls) in m.ports
    assert topo["demarcation_plane"]["switch_residual_port"] == "P_sw_residual"


def test_switch_residual_validation_rejects_extra_solved_ports():
    m = kicad_geom.Model()
    hs = m.node("VIN", 0, 0.0, 0.0, 0.0)
    ls = m.node("GND", 0, 1.0, 0.0, 0.0)
    g0 = m.node("GATE", 0, 2.0, 0.0, 0.0)
    g1 = m.node("GATE", 0, 3.0, 0.0, 0.0)
    topo = dict(hs=dict(_drn_pad_node=hs), ls=dict(_src_pad_node=ls))
    kicad_geom.setup_demarcation_plane(m, topo, "switch_residual", closure="cell_bridge")
    kicad_geom.validate_switch_residual_ports(m, topo)  # no raise

    m.port("P_ghs_Q1", g0, g1)
    try:
        kicad_geom.validate_switch_residual_ports(m, topo)
    except ValueError as e:
        assert "extra solved port" in str(e)
        assert "P_ghs_Q1" in str(e)
    else:
        raise AssertionError("switch_residual must reject non-gauge ports")


def test_demarcation_plane_per_fet_switch_residual_ports():
    m = kicad_geom.Model()
    hd = m.node("VIN", 0, 0.0, 0.0, 0.0)
    hs = m.node("SW", 0, 0.5, 0.0, 0.0)
    ld = m.node("SW", 0, 1.0, 0.0, 0.0)
    ls = m.node("GND", 0, 1.5, 0.0, 0.0)
    topo = dict(
        hs=dict(_devices=[dict(ref="QH", _drn_pad_node=hd, _src_pad_node=hs)]),
        ls=dict(_devices=[dict(ref="QL", _drn_pad_node=ld, _src_pad_node=ls)]))
    kicad_geom.setup_demarcation_plane(m, topo, "switch_residual", closure="per_fet")
    assert ("P_sw_residual_hs_QH", hd, hs) in m.ports
    assert ("P_sw_residual_ls_QL", ld, ls) in m.ports


def test_demarcation_plane_per_fet_skips_equiv_residual_ports():
    m = kicad_geom.Model()
    hd = m.node("VIN", 0, 0.0, 0.0, 0.0)
    hs = m.node("SW", 0, 0.5, 0.0, 0.0)
    ld = m.node("SW", 0, 1.0, 0.0, 0.0)
    ls = m.node("GND", 0, 1.5, 0.0, 0.0)
    m.equiv(hd, hs)
    m.equiv(ld, ls)
    topo = dict(
        hs=dict(_devices=[dict(ref="QH", _drn_pad_node=hd, _src_pad_node=hs)]),
        ls=dict(_devices=[dict(ref="QL", _drn_pad_node=ld, _src_pad_node=ls)]))
    kicad_geom.setup_demarcation_plane(m, topo, "switch_residual", closure="per_fet")
    plane = topo["demarcation_plane"]
    assert plane["switch_residual_ports"] == []
    assert {e["label"] for e in plane["switch_residual_ports_skipped"]} == {
        "P_sw_residual_hs_QH",
        "P_sw_residual_ls_QL",
    }
    assert plane["gauge_fix_status"] == "structurally_not_required"
    assert plane["gauge_fix_reason"] == "zero_by_plane_p_equiv"
    assert not [p for p in m.ports if p[0].startswith("P_sw_residual")]


def test_prune_retains_explicit_unported_components():
    m = kicad_geom.Model()
    pa = m.node("VIN", 0, 0.0, 0.0, 0.0)
    pb = m.node("GND", 0, 1.0, 0.0, 0.0)
    m.seg(pa, pb, 0.5)
    m.port("P_pwr", pa, pb)

    fa = m.node("SW", 0, 10.0, 0.0, 0.0)
    fb = m.node("SW", 0, 11.0, 0.0, 0.0)
    m.seg(fa, fb, 0.5)
    m.keep_nodes.add(fa)

    seen = m.prune()
    assert fa in seen and fb in seen
    assert any(s[1] == fa and s[2] == fb for s in m.segs)


def test_zero_lead_lumped_parallel_fets_fail_loud():
    try:
        kicad_geom._require_valid_lead_parallel_mode("hs", ["Q1", "Q3"], 0.0, "lumped")
    except ValueError as e:
        assert "lumped parallel FETs" in str(e)
        assert "--parallel-fets per-device" in str(e)
    else:
        raise AssertionError("zero-lead lumped parallel FETs should fail")

    kicad_geom._require_valid_lead_parallel_mode("hs", ["Q1"], 0.0, "lumped")
    kicad_geom._require_valid_lead_parallel_mode("hs", ["Q1", "Q3"], 0.0, "per-device")


def test_mesh_complexity_counts_sweep_points_and_work_units():
    stats = dict(nodes=10, segs=20, ports=3)
    c = kicad_geom.mesh_complexity(stats, nwinc=2, nhinc=3,
                                   fmin=3.9e4, fmax=1e8, ndec=3)
    assert c["freq_points"] == 11
    assert c["filament_subdivisions"] == 6
    assert c["filaments_est"] == 120
    assert c["work_units"] == 120 * 120 * 3 * 11


def test_freq_count_boundaries():
    assert kicad_geom.freq_count(1e5, 1e8, 3) == 10
    assert kicad_geom.freq_count(1e5, 1e5, 3) == 1
    assert kicad_geom.freq_count(0, 1e8, 3) == 0
    assert kicad_geom.freq_count(1e8, 1e5, 3) == 0


def test_axis_cuts_include_pitch_bounds_and_polygon_vertices():
    cuts = kicad_geom._axis_cuts(0.0, 5.0, 2.0, extras=[1.25, 4.75])
    assert cuts == [0.0, 1.25, 2.0, 4.0, 4.75, 5.0]


def test_axis_cuts_merge_tiny_slivers():
    cuts = kicad_geom._axis_cuts(0.0, 2.0, 1.0, extras=[1.001])
    assert cuts == [0.0, 1.0, 2.0]


def test_bounded_poly_axis_vertices_drops_large_cross_product():
    outline = [(float(i), float(i % 7)) for i in range(100)]
    xs, ys, note = kicad_geom._bounded_poly_axis_vertices([(outline, [])])
    assert xs == []
    assert ys == []
    assert note["reason"] == "too_many_polygon_vertex_cuts"


def test_polygon_inclusive_contains_outer_edge_but_not_hole_interior():
    poly = ([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)],
            [[(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0)]])
    assert kicad_geom.point_in_polys_inclusive(0.0, 2.0, [poly])
    assert kicad_geom.point_in_polys_inclusive(0.5, 0.5, [poly])
    assert not kicad_geom.point_in_polys_inclusive(2.0, 2.0, [poly])


def test_segment_inside_rejects_void_crossing():
    poly = ([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)],
            [[(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0)]])
    contains = kicad_geom._polys_contains_inclusive([poly])
    assert kicad_geom._segment_inside(contains, 0.0, 0.5, 4.0, 0.5, step=0.25)
    assert not kicad_geom._segment_inside(contains, 0.0, 2.0, 4.0, 2.0, step=0.25)


class _FakePad:
    def GetSizeX(self):
        return 1_000_000

    def GetSizeY(self):
        return 1_000_000


class _FakeFp:
    def GetReference(self):
        return "C1"


def _terminal_mode_model(mode):
    m = kicad_geom.Model(terminal_mode=mode)
    for x, y in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)):
        m.node("VIN", 0, x, y, 0.0, zone=True)
    return m


def _with_fake_pad_contains(fn):
    old = kicad_geom._pad_contains
    kicad_geom._pad_contains = lambda pad, lid: (
        lambda x, y: -0.1 <= x <= 1.1 and -0.1 <= y <= 1.1)
    try:
        return fn()
    finally:
        kicad_geom._pad_contains = old


def test_padland_terminal_equivs_all_pad_contacts():
    def run():
        m = _terminal_mode_model("padland")
        n = kicad_geom._pad_land_terminal(m, "VIN", 0, 0.5, 0.5, 0.0,
                                          _FakePad(), fp=_FakeFp())
        assert n in m.distributed_terminals
        assert len(m.equivs) == 4
        assert not m.segs
        assert m.terminal_regions[-1]["mode"] == "padland"
        assert m.terminal_regions[-1]["ref"] == "C1"
    _with_fake_pad_contains(run)


def test_single_terminal_uses_one_nearest_pad_contact():
    def run():
        m = _terminal_mode_model("single")
        n = kicad_geom._pad_land_terminal(m, "VIN", 0, 0.5, 0.5, 0.0,
                                          _FakePad(), fp=_FakeFp())
        assert n in m.zone_nodes
        assert not m.equivs
        assert not m.segs
        assert m.terminal_regions[-1]["mode"] == "single"
        assert m.terminal_regions[-1]["used_contacts"] == 1
    _with_fake_pad_contains(run)


def test_finite_terminal_connects_pad_contacts_with_segments():
    def run():
        m = _terminal_mode_model("finite")
        n = kicad_geom._pad_land_terminal(m, "VIN", 0, 0.5, 0.5, 0.0,
                                          _FakePad(), fp=_FakeFp())
        assert n in m.distributed_terminals
        assert not m.equivs
        assert len(m.segs) == 4
        assert m.terminal_regions[-1]["mode"] == "finite"
        assert m.terminal_regions[-1]["contact_width"] > 0
    _with_fake_pad_contains(run)


def test_point_terminal_uses_legacy_pad_center_fallback():
    def run():
        m = _terminal_mode_model("point")
        n = kicad_geom._pad_land_terminal(m, "VIN", 0, 0.5, 0.5, 0.0,
                                          _FakePad(), fp=_FakeFp())
        assert n is None
        assert not m.equivs
        assert not m.segs
        assert m.terminal_fallbacks[-1]["reason"] == "legacy_point_mode"
    _with_fake_pad_contains(run)


def test_gate_driver_node_ignores_disconnected_same_net_island():
    m = kicad_geom.Model()
    gate = m.node("GATE", 0, 0.0, 0.0, 0.0)
    driver = m.node("GATE", 0, 5.0, 0.0, 0.0)
    island = m.node("GATE", 0, 100.0, 0.0, 0.0)
    m.seg(gate, driver, 0.2)
    assert kicad_geom.gate_driver_node(m, "GATE", gate) == driver
    assert kicad_geom.gate_driver_node(m, "GATE", gate) != island


def test_gate_driver_node_rejects_pad_only_component():
    m = kicad_geom.Model()
    gate = m.node("GATE", 0, 0.0, 0.0, 0.0)
    island = m.node("GATE", 0, 100.0, 0.0, 0.0)
    assert kicad_geom.gate_driver_node(m, "GATE", gate) is None
    assert kicad_geom.gate_driver_node(m, "GATE", gate) != island


def test_per_device_port_label_collision_fails_loud():
    try:
        kicad_geom._require_unique_device_labels("hs", ["Q-1", "Q_1"], "per-device")
    except ValueError as e:
        assert "collision" in str(e)
        assert "P_ghs_Q_1" in str(e)
    else:
        raise AssertionError("normalized per-device port-label collision must fail")


def test_required_ports_reject_missing_power_loop():
    m = kicad_geom.Model()
    a = m.node("GATE", 0, 0.0, 0.0, 0.0)
    b = m.node("GATE", 0, 1.0, 0.0, 0.0)
    m.port("P_ghs", a, b)
    m.port("P_gls", a, b)
    try:
        kicad_geom.validate_required_ports(m, _topo())
    except ValueError as e:
        assert "P_pwr" in str(e)
    else:
        raise AssertionError("missing P_pwr must fail")


# ---------------------------------------------- issue #6: same-net pour tracks
# A 20 mm square pour outline (mm) for the containment guard.
_POUR = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


def _drop(pa, pb, contains, roi=None):
    """Faithful mirror of add_tracks' real drop decision: sample densely along
    the track (via the SAME _track_samples helper the production code uses) and
    drop iff EVERY sample is inside both the pour and the ROI."""
    samples = kicad_geom._track_samples(pa[0], pa[1], pb[0], pb[1])
    return all(kicad_geom._in_roi(px, py, roi) and contains(px, py)
               for px, py in samples)


def test_point_in_polys_basic():
    assert kicad_geom.point_in_polys(5.0, 5.0, [_POUR])       # inside
    assert not kicad_geom.point_in_polys(25.0, 5.0, [_POUR])  # outside
    assert not kicad_geom.point_in_polys(5.0, 5.0, [])        # no pour -> outside


def test_point_in_poly_with_holes():
    # a thermal-relief / clearance void inside the fill is bare board, not copper
    hole = [(8.0, 8.0), (12.0, 8.0), (12.0, 12.0), (8.0, 12.0)]
    assert kicad_geom.point_in_poly_with_holes(2.0, 2.0, _POUR, [hole])   # copper
    assert not kicad_geom.point_in_poly_with_holes(10.0, 10.0, _POUR, [hole])  # in hole
    # same via the (outline, holes) form of point_in_polys
    assert not kicad_geom.point_in_polys(10.0, 10.0, [(_POUR, [hole])])


def test_track_fully_inside_pour_is_dropped():
    contains = lambda x, y: kicad_geom.point_in_polys(x, y, [_POUR])
    # whole span inside the same-net pour -> redundant, drop
    assert _drop((2.0, 2.0), (18.0, 18.0), contains)


def test_track_on_pourless_net_is_kept():
    # a net with no pour has no index entry -> contains is None -> never dropped
    pour_index = {("SW", 0): lambda x, y: kicad_geom.point_in_polys(x, y, [_POUR])}
    contains = pour_index.get(("HG", 0))   # gate net: not in index
    assert contains is None                # add_tracks keeps it (no drop path)


def test_track_straddling_pour_edge_is_kept():
    contains = lambda x, y: kicad_geom.point_in_polys(x, y, [_POUR])
    # starts inside, ends well outside the pour edge -> a sample lands outside
    assert not _drop((10.0, 10.0), (30.0, 10.0), contains)
    # endpoints inside but the middle crosses a gap in a split pour -> kept
    left = [(0.0, 0.0), (8.0, 0.0), (8.0, 20.0), (0.0, 20.0)]
    right = [(12.0, 0.0), (20.0, 0.0), (20.0, 20.0), (12.0, 20.0)]
    c2 = lambda x, y: kicad_geom.point_in_polys(x, y, [left, right])
    assert c2(4.0, 10.0) and c2(16.0, 10.0) and not c2(10.0, 10.0)  # gap in middle
    assert not _drop((4.0, 10.0), (16.0, 10.0), c2)


def test_track_dipping_through_void_is_kept():
    # 3-point sampling would be fooled: start/mid/end all on copper, but a
    # clearance void sits between the midpoint and an endpoint. Dense sampling
    # (via _track_samples) lands a point in the void -> track kept.
    hole = [(13.0, 9.0), (15.0, 9.0), (15.0, 11.0), (13.0, 11.0)]  # ~2mm void off-mid
    contains = lambda x, y: kicad_geom.point_in_polys(x, y, [(_POUR, [hole])])
    assert contains(2.0, 10.0) and contains(10.0, 10.0) and contains(18.0, 10.0)
    assert not _drop((2.0, 10.0), (18.0, 10.0), contains)  # dense sampling saves it


def test_track_outside_roi_is_kept_even_if_in_pour():
    # whole span inside the pour, but the ROI excludes it -> no mesh substitute,
    # so the track must be kept.
    contains = lambda x, y: kicad_geom.point_in_polys(x, y, [_POUR])
    roi = (0.0, 0.0, 5.0, 5.0)                     # small ROI in the corner
    assert _drop((1.0, 1.0), (4.0, 4.0), contains, roi=roi)      # inside ROI: dropped
    assert not _drop((10.0, 10.0), (18.0, 18.0), contains, roi=roi)  # outside ROI: kept


def test_track_samples_spacing():
    pts = kicad_geom._track_samples(0.0, 0.0, 1.0, 0.0, step=0.25)
    assert pts[0] == (0.0, 0.0) and pts[-1] == (1.0, 0.0)
    # consecutive spacing never exceeds the requested step
    for (x0, _), (x1, _) in zip(pts, pts[1:]):
        assert x1 - x0 <= 0.25 + 1e-9
    # degenerate zero-length still yields the endpoints (>= 3 points)
    assert len(kicad_geom._track_samples(3.0, 3.0, 3.0, 3.0)) >= 3


def test_required_ports_reject_missing_gate_loop():
    m = kicad_geom.Model()
    a = m.node("VIN", 0, 0.0, 0.0, 0.0)
    b = m.node("GND", 0, 1.0, 0.0, 0.0)
    m.port("P_pwr", a, b)
    m.port("P_gls", a, b)
    try:
        kicad_geom.validate_required_ports(m, _topo())
    except ValueError as e:
        assert "P_ghs" in str(e)
    else:
        raise AssertionError("missing gate-loop port must fail")


def test_required_ports_allow_missing_gate_loop_when_explicit():
    m = kicad_geom.Model()
    a = m.node("VIN", 0, 0.0, 0.0, 0.0)
    b = m.node("GND", 0, 1.0, 0.0, 0.0)
    m.port("P_pwr", a, b)
    m.port("P_gls", a, b)
    kicad_geom.validate_required_ports(
        m, _topo(), allow_missing_gate_ports=True)  # no raise


def test_required_ports_still_reject_missing_power_loop_when_gate_missing_allowed():
    m = kicad_geom.Model()
    a = m.node("GATE", 0, 0.0, 0.0, 0.0)
    b = m.node("GATE", 0, 1.0, 0.0, 0.0)
    m.port("P_ghs", a, b)
    m.port("P_gls", a, b)
    try:
        kicad_geom.validate_required_ports(
            m, _topo(), allow_missing_gate_ports=True)
    except ValueError as e:
        assert "P_pwr" in str(e)
    else:
        raise AssertionError("missing P_pwr must fail even when gate ports are allowed")


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
