#!/usr/bin/env python3
"""Focused tests for the opt-in meshed-net extension (`extra_nets`).

Every guard in `lib/extra_nets.py` and the `Model.drop_floating_ports` island
allowance gets a test that WATCHES IT FIRE on a constructed bad input, plus a
passing control. A guard never seen to fire is not a guard.

THE KNOWN-BAD CALIBRATION, and it is the first thing here, is
`test_calibration_*`: the concrete failure this feature exists to remove is
reconstructed in full — a probe pad on a net the extractor does not mesh, which
`_probe_pad_node_stack` refuses with `no_same_net_zone_mesh` — and then the same
pad is shown resolving to a real distributed terminal once its net is meshed.
Both halves matter: without the first, the tests would only assert the
arithmetic they wrote; without the second, the fix is unproven.

`pcbnew` is stubbed exactly as test_probe_ports.py:24 does, so the pcbnew side
(pad selection, layer walk, contact cascade) runs under system python without
KiCad.
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, LIB)
sys.path.insert(0, ROOT)

# System python does not ship KiCad's pcbnew module.
sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
import emit  # noqa: E402
import extra_nets  # noqa: E402
import kicad_geom  # noqa: E402
import probe_ports  # noqa: E402

NM = kicad_geom.NM
F_CU, B_CU = 0, 31
ZMAP = {F_CU: 0.0, B_CU: -1.6}


# --------------------------------------------------------------------------- #
# pcbnew stubs (same shape as test_probe_ports.py)
# --------------------------------------------------------------------------- #
class _Pos:
    def __init__(self, x_mm, y_mm):
        self.x = int(x_mm * NM)
        self.y = int(y_mm * NM)


class _Pad:
    def __init__(self, number, net, x_mm, y_mm, layers=(F_CU,), size_mm=1.0):
        self._number = str(number)
        self._net = net
        self._pos = _Pos(x_mm, y_mm)
        self._layers = tuple(layers)
        self._size = int(size_mm * NM)

    def GetNumber(self):
        return self._number

    def GetNetname(self):
        return self._net

    def GetPosition(self):
        return self._pos

    def IsOnLayer(self, lid):
        return lid in self._layers

    def GetSizeX(self):
        return self._size

    def GetSizeY(self):
        return self._size


class _Footprint:
    def __init__(self, ref, pads):
        self._ref = ref
        self._pads = pads

    def GetReference(self):
        return self._ref

    def Pads(self):
        return list(self._pads)


class _CuStack:
    def CuStack(self):
        return [F_CU, B_CU]


class _Board:
    def __init__(self, footprints):
        self._footprints = list(footprints)

    def GetFootprints(self):
        return list(self._footprints)

    def GetEnabledLayers(self):
        return _CuStack()


def _pad_contains_stub(pad, lid):
    px = kicad_geom.mm(pad.GetPosition().x)
    py = kicad_geom.mm(pad.GetPosition().y)
    h = kicad_geom.mm(pad.GetSizeX()) / 2.0
    return lambda x, y: abs(x - px) <= h and abs(y - py) <= h


@pytest.fixture(autouse=True)
def _stub_pad_contains(monkeypatch):
    monkeypatch.setattr(kicad_geom, "_pad_contains", _pad_contains_stub)


def _model_with_pour(net_layer_points, terminal_mode="padland"):
    m = kicad_geom.Model(terminal_mode=terminal_mode)
    m.pitch = 1.0
    for (net, lid), pts in net_layer_points.items():
        for x, y in pts:
            m.node(net, lid, x, y, ZMAP[lid], zone=True)
    return m


def _pour_grid(net, lid, cx, cy, n=3, step=0.4):
    return {(net, lid): [(cx + i * step, cy + j * step)
                         for i in range(-n, n + 1) for j in range(-n, n + 1)]}


def _entries(*nets, **kw):
    """Minimal classify() output, for the guards that consume it directly."""
    return [dict(net=n, already_meshed=False, was_gate_net=False, **kw)
            for n in nets]


# --------------------------------------------------------------------------- #
# KNOWN-BAD CALIBRATION — the failure this feature exists to remove
# --------------------------------------------------------------------------- #
def test_calibration_unmeshed_net_makes_the_probe_pad_resolve_to_no_copper():
    """Reconstruct the reported failure, and confirm the guard catches it.

    On Fugu2 this is verbatim:

        probe_ports: out_coil_bflow: pad J6.1 (net 'BflowS') resolved to NO
        copper contact on any of its layers (layer 0: no_same_net_zone_mesh; ...)

    The extractor meshes {sw, vin, gnd} + gate nets and nothing else, so a pad on
    BflowS has no pour under it. The refusal is CORRECT — the alternative is a
    bare pad-centre node that point-injects the current — which is exactly why
    the fix has to be "supply the copper", never "relax the check".
    """
    board = _Board([_Footprint("J6", [_Pad("1", "BflowS", 56.0, 75.8)]),
                    _Footprint("C10", [_Pad("1", "BflowS", 62.8, 64.3)])])
    # Only the power net is meshed — the pre-feature state of the world.
    model = _model_with_pour(_pour_grid("SW", F_CU, 46.5, 57.5))
    probes = probe_ports.parse_spec({"out_coil_bflow": ["J6.1", "C10.1"]})
    with pytest.raises(probe_ports.ProbeError) as e:
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert "resolved to NO copper contact" in str(e.value)
    assert "no_same_net_zone_mesh" in str(e.value)


def test_calibration_meshing_the_extra_net_makes_the_same_probe_resolve():
    """The other half: same board, same probe, BflowS pour now meshed.

    The pad resolves to a real distributed `padland` terminal bonded by OVERLAP
    (not by the fabricated proximity spokes that `probe_allow_proximity_bond`
    exists to gate), which is the whole point: real copper, measured, under both
    terminals.
    """
    board = _Board([_Footprint("J6", [_Pad("1", "BflowS", 56.0, 75.8)]),
                    _Footprint("C10", [_Pad("1", "BflowS", 62.8, 64.3)])])
    pour = _pour_grid("SW", F_CU, 46.5, 57.5)
    # Both BflowS pads need pour under them, and they land on the SAME
    # (net, layer) key — so extend the list rather than overwrite it.
    pour[("BflowS", F_CU)] = (_pour_grid("BflowS", F_CU, 56.0, 75.8)[("BflowS", F_CU)]
                              + _pour_grid("BflowS", F_CU, 62.8, 64.3)[("BflowS", F_CU)])
    model = _model_with_pour(pour)
    probes = probe_ports.parse_spec({"out_coil_bflow": ["J6.1", "C10.1"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    p = probes[0]
    assert p["a_node"] and p["b_node"] and p["a_node"] != p["b_node"]
    assert p["a_net"] == p["b_net"] == "BflowS"
    assert p["a_terminal"] == "padland" and p["b_terminal"] == "padland"
    assert p["a_bond"] == "overlap" and p["b_bond"] == "overlap"


# --------------------------------------------------------------------------- #
# spec parsing
# --------------------------------------------------------------------------- #
def test_parse_spec_normalizes_and_dedupes_preserving_order():
    assert extra_nets.parse_spec(["BflowS", "Bat+", "BflowS"]) == ["BflowS", "Bat+"]
    assert extra_nets.parse_spec(None) == []
    assert extra_nets.parse_spec([]) == []


def test_parse_spec_refuses_a_bare_string_instead_of_iterating_its_characters():
    # `extra_nets: GND` in YAML. list("GND") == ['G','N','D'] would be three
    # bogus nets, each of which would then trip the unknown-net guard with a
    # message about the wrong thing.
    with pytest.raises(extra_nets.ExtraNetError, match="expected a LIST"):
        extra_nets.parse_spec("GND")


@pytest.mark.parametrize("spec, match", [
    ([None], "not a string"),
    ([3], "not a string"),
    ([""], "empty net name"),
    (["  "], "empty net name"),
    (42, "expected a list"),
])
def test_parse_spec_refuses_unusable_entries(spec, match):
    with pytest.raises(extra_nets.ExtraNetError, match=match):
        extra_nets.parse_spec(spec)


# --------------------------------------------------------------------------- #
# classify — an unknown net is a hard error, not a silent drop
# --------------------------------------------------------------------------- #
def test_classify_refuses_a_net_that_carries_no_copper():
    with pytest.raises(extra_nets.ExtraNetError) as e:
        extra_nets.classify(["Bflows"], {"SW"}, {"G_HS"},
                            {"SW", "BflowS", "Bat+"})
    assert "carries no copper on this board" in str(e.value)
    # the suggestion is what turns a hard error into a one-line fix
    assert "did you mean 'BflowS'" in str(e.value)


def test_classify_marks_redundant_and_gate_nets_without_refusing_them():
    entries = extra_nets.classify(
        ["BflowS", "SW", "G_HS"], {"SW", "GND"}, {"G_HS"},
        {"SW", "GND", "G_HS", "BflowS"})
    by = {e["net"]: e for e in entries}
    assert by["BflowS"]["already_meshed"] is False
    # a power net is a pure set-union no-op...
    assert by["SW"]["already_meshed"] is True
    # ...but a GATE net is not: gate nets get tracks and vias and NO pour, so
    # naming one here really does add its pour mesh.
    assert by["G_HS"]["already_meshed"] is False
    assert by["G_HS"]["was_gate_net"] is True


def test_mesh_nets_is_the_union_and_is_empty_when_nothing_was_requested():
    assert extra_nets.mesh_nets(_entries("A", "B")) == {"A", "B"}
    assert extra_nets.mesh_nets([]) == set()


# --------------------------------------------------------------------------- #
# measure_mesh / require_meshed — the ROI guard
# --------------------------------------------------------------------------- #
def _mesh_model(zone_pts, plain_pts=()):
    m = kicad_geom.Model()
    m.pitch = 1.0
    for net, x, y in zone_pts:
        m.node(net, F_CU, x, y, 0.0, zone=True)
    for net, x, y in plain_pts:
        m.node(net, F_CU, x, y, 0.0)
    return m


def test_measure_mesh_counts_pour_nodes_separately_from_all_nodes():
    m = _mesh_model([("BflowS", 1.0, 1.0), ("BflowS", 2.0, 1.0)],
                    [("Bat+", 5.0, 5.0)])
    entries = _entries("BflowS", "Bat+", "BT+")
    extra_nets.measure_mesh(m, entries)
    by = {e["net"]: e for e in entries}
    assert (by["BflowS"]["nodes"], by["BflowS"]["zone_nodes"]) == (2, 2)
    assert (by["Bat+"]["nodes"], by["Bat+"]["zone_nodes"]) == (1, 0)
    assert (by["BT+"]["nodes"], by["BT+"]["zone_nodes"]) == (0, 0)


def test_require_meshed_fires_when_the_roi_never_reaches_the_pour():
    entries = _entries("Bat+")
    entries[0].update(nodes=0, zone_nodes=0)
    extents = {"Bat+": dict(bbox=(60.0, 75.0, 74.0, 100.0), has_zone=True,
                            zone_bbox=(60.0, 75.0, 74.0, 90.0),
                            margin_required_mm=10.8)}
    with pytest.raises(extra_nets.ExtraNetError) as e:
        extra_nets.require_meshed(entries, (20.0, 30.0, 55.0, 65.0), 8.0,
                                  extents=extents)
    msg = str(e.value)
    assert "got NO pour mesh" in msg
    assert "--margin >= 10.8 mm" in msg            # the actionable number
    assert "currently 8 mm" in msg
    assert "do NOT extend the ROI" in msg          # the policy, stated


def test_require_meshed_says_so_when_no_margin_can_ever_help():
    # A net routed with tracks only has no pour ANYWHERE, so `_pad_land_terminal`
    # will never find contacts for it. Telling this user to raise --margin would
    # send them round a loop that cannot terminate.
    entries = _entries("BflowG")
    entries[0].update(nodes=12, zone_nodes=0)
    extents = {"BflowG": dict(bbox=(55.0, 80.0, 71.0, 81.0), has_zone=False,
                              zone_bbox=None, margin_required_mm=None)}
    with pytest.raises(extra_nets.ExtraNetError) as e:
        extra_nets.require_meshed(entries, (20.0, 30.0, 80.0, 95.0), 8.0,
                                  extents=extents)
    assert "NO filled zone anywhere on the board" in str(e.value)
    assert "no --margin can help" in str(e.value)


def test_require_meshed_is_not_satisfied_by_track_and_via_nodes_alone():
    """MONOTONICITY / the bug this predicate was corrected for.

    Only `add_zones` is clipped to the ROI. `add_tracks` and `add_vias` build
    their nets board-wide, so a net whose pour is 30 mm outside the ROI still
    arrives with hundreds of nodes. Keying the guard on `nodes` (as the first
    version did — caught by running it on the real Fugu2 board) would pass that
    net, and the run would then die far downstream in the pad-land cascade with
    `no_same_net_zone_mesh`: the exact failure the guard exists to preempt.
    """
    entries = _entries("Bat+")
    entries[0].update(nodes=456, zone_nodes=0)   # plenty of nodes, no pour mesh
    extents = {"Bat+": dict(bbox=(60.0, 75.0, 74.0, 100.0), has_zone=True,
                            zone_bbox=(60.0, 75.0, 74.0, 90.0),
                            margin_required_mm=10.8)}
    with pytest.raises(extra_nets.ExtraNetError, match="got NO pour mesh"):
        extra_nets.require_meshed(entries, (20.0, 30.0, 55.0, 65.0), 8.0,
                                  extents=extents)


def test_require_meshed_control_passes_when_the_pour_is_meshed():
    entries = _entries("BflowS")
    entries[0].update(nodes=501, zone_nodes=445)
    assert extra_nets.require_meshed(entries, (10.0, 20.0, 80.0, 110.0), 8.0,
                                     extents={}) is entries


def test_box_overhang_is_zero_inside_and_the_gap_outside():
    base = (10.0, 10.0, 20.0, 20.0)
    assert kicad_geom._box_overhang(base, (12.0, 12.0, 13.0, 13.0)) == 0.0
    assert kicad_geom._box_overhang(base, (25.0, 12.0, 26.0, 13.0)) == 5.0
    assert kicad_geom._box_overhang(base, (12.0, 0.0, 13.0, 4.0)) == 6.0
    # diagonal: the uniform growth needed is the LARGER axis gap, not the
    # euclidean distance — the ROI grows as a box, not a disc.
    assert kicad_geom._box_overhang(base, (25.0, 27.0, 26.0, 28.0)) == 7.0


def test_roi_base_and_grow_compose_to_the_historical_roi():
    # _roi was split into _roi_base + _grow so the extra-net diagnostics can ask
    # "what margin would reach this?" against the UNGROWN box. The composition
    # must stay exactly the old function, or every existing run's ROI moves.
    class _BB:
        def GetLeft(self):
            return int(10.0 * NM)

        def GetRight(self):
            return int(20.0 * NM)

        def GetTop(self):
            return int(30.0 * NM)

        def GetBottom(self):
            return int(40.0 * NM)

    class _FP:
        def GetReference(self):
            return "Q1"

        def GetBoundingBox(self):
            return _BB()

    class _B:
        def GetFootprints(self):
            return [_FP()]

    topo = dict(hs=dict(refs=["Q1"]), ls=dict(refs=[]), cin=[])
    assert kicad_geom._roi_base(_B(), topo) == (10.0, 30.0, 20.0, 40.0)
    assert kicad_geom._roi(_B(), topo, 8.0) == (2.0, 22.0, 28.0, 48.0)
    assert kicad_geom._grow(None, 8.0) is None


# --------------------------------------------------------------------------- #
# Model.drop_floating_ports — the island allowance
# --------------------------------------------------------------------------- #
def _two_island_model():
    """P_pwr's loop, plus a galvanically separate 'BflowS' island with a probe."""
    m = kicad_geom.Model()
    a = m.node("VIN", F_CU, 0.0, 0.0, 0.0)
    b = m.node("GND", F_CU, 1.0, 0.0, 0.0)
    m.seg(a, b, 0.5)
    m.port("P_pwr", a, b)
    i0 = m.node("BflowS", F_CU, 50.0, 50.0, 0.0)
    i1 = m.node("BflowS", F_CU, 60.0, 50.0, 0.0)
    m.seg(i0, i1, 0.5)
    m.port("P_probe_out_coil_bflow", i0, i1)
    return m, i0, i1


def test_calibration_island_port_is_dropped_without_the_allowance():
    """KNOWN-BAD: the pre-feature behaviour, which would make extra_nets useless.

    `drop_floating_ports` keeps only ports inside P_pwr's component. Every probe
    on the output power path is outside it — the back-flow FETs, fuse and shunt
    between are components, not copper — so each would be dropped here and then
    hard-fail in `probe_ports.require_not_dropped`. Meshed copper nobody can
    measure.
    """
    m, _, _ = _two_island_model()
    dropped = m.drop_floating_ports("P_pwr")           # default: no allowance
    assert dropped == ["P_probe_out_coil_bflow"]
    assert [lbl for lbl, _, _ in m.ports] == ["P_pwr"]


def test_island_port_survives_when_its_net_was_declared():
    m, _, _ = _two_island_model()
    dropped = m.drop_floating_ports("P_pwr", island_nets={"BflowS"})
    assert dropped == []
    assert [lbl for lbl, _, _ in m.ports] == ["P_pwr", "P_probe_out_coil_bflow"]


def test_allowance_still_drops_a_genuinely_OPEN_port():
    """The NaN condition itself is never relaxed.

    Two endpoints on two DIFFERENT conductors is an open circuit: infinite
    impedance, and FastHenry NaNs the whole solve. `island_nets` must not rescue
    that, however thoroughly the nets were declared.
    """
    m = kicad_geom.Model()
    a = m.node("VIN", F_CU, 0.0, 0.0, 0.0)
    b = m.node("GND", F_CU, 1.0, 0.0, 0.0)
    m.seg(a, b, 0.5)
    m.port("P_pwr", a, b)
    x = m.node("BflowS", F_CU, 50.0, 50.0, 0.0)
    y = m.node("Bat+", F_CU, 80.0, 50.0, 0.0)   # separate conductor, no seg
    m.port("P_probe_open", x, y)
    dropped = m.drop_floating_ports("P_pwr", island_nets={"BflowS", "Bat+"})
    assert dropped == ["P_probe_open"]


def test_allowance_does_not_rescue_an_island_holding_undeclared_copper():
    """Monotonicity: a broken POWER-net port must not sneak through.

    If a Cin pad's copper fragments into an island of its own, that island holds
    power-net nodes, so it is not "the copper the user asked to add" and the old
    rule applies unchanged.
    """
    m = kicad_geom.Model()
    a = m.node("VIN", F_CU, 0.0, 0.0, 0.0)
    b = m.node("GND", F_CU, 1.0, 0.0, 0.0)
    m.seg(a, b, 0.5)
    m.port("P_pwr", a, b)
    # an island that mixes a declared extra net with un-declared GND copper
    i0 = m.node("BflowS", F_CU, 50.0, 50.0, 0.0)
    i1 = m.node("GND", F_CU, 60.0, 50.0, 0.0)
    m.seg(i0, i1, 0.5)
    m.port("P_pwr9", i0, i1)
    dropped = m.drop_floating_ports("P_pwr", island_nets={"BflowS"})
    assert dropped == ["P_pwr9"]


def test_drop_floating_ports_default_behaviour_is_unchanged():
    # The no-op proof at unit scale: the historical two-argument call, and the
    # historical one-argument call, must agree on every port.
    m1, _, _ = _two_island_model()
    m2, _, _ = _two_island_model()
    assert m1.drop_floating_ports("P_pwr") == m2.drop_floating_ports(
        "P_pwr", island_nets=())


# --------------------------------------------------------------------------- #
# reachability / prune reporting
# --------------------------------------------------------------------------- #
def test_annotate_reachability_reports_ported_isolated_and_retained():
    m, _, _ = _two_island_model()
    m.drop_floating_ports("P_pwr", island_nets={"BflowS"})
    entries = _entries("BflowS", "Bat+")
    # Bat+ is meshed but has no port and no copper of its own here
    m.node("Bat+", F_CU, 90.0, 90.0, 0.0, zone=True)
    extra_nets.measure_mesh(m, entries)
    extra_nets.annotate_reachability(m, entries, "P_pwr")
    by = {e["net"]: e for e in entries}
    assert by["BflowS"]["ported"] is True
    assert by["BflowS"]["isolated"] is True        # separate from P_pwr's copper
    assert by["BflowS"]["retained"] == 2           # both island nodes survive
    assert by["Bat+"]["ported"] is False
    assert by["Bat+"]["retained"] == 0             # prune() throws it away


def test_isolated_is_true_for_an_UNPORTED_net_not_false():
    # `isolated: false` on an unported net would read as "connected to the
    # commutation loop", which is the opposite of the truth. Measured over the
    # net's whole node set, not only its ported nodes.
    m, _, _ = _two_island_model()
    entries = _entries("Bat+")
    m.node("Bat+", F_CU, 90.0, 90.0, 0.0, zone=True)
    extra_nets.measure_mesh(m, entries)
    extra_nets.annotate_reachability(m, entries, "P_pwr")
    assert entries[0]["ported"] is False
    assert entries[0]["isolated"] is True


def test_a_net_actually_joined_to_the_loop_is_not_reported_isolated():
    # Control for the check above: GND copper that IS in P_pwr's component.
    m = kicad_geom.Model()
    a = m.node("VIN", F_CU, 0.0, 0.0, 0.0)
    b = m.node("GND", F_CU, 1.0, 0.0, 0.0)
    m.seg(a, b, 0.5)
    m.port("P_pwr", a, b)
    entries = _entries("GND")
    extra_nets.measure_mesh(m, entries)
    extra_nets.annotate_reachability(m, entries, "P_pwr")
    assert entries[0]["isolated"] is False


def test_warnings_name_the_unported_the_redundant_and_the_ported():
    entries = [
        dict(net="SW", already_meshed=True, was_gate_net=False,
             nodes=10, zone_nodes=10, ported=True, isolated=False, retained=10),
        dict(net="Bat+", already_meshed=False, was_gate_net=False,
             nodes=456, zone_nodes=387, ported=False, isolated=True, retained=0),
        dict(net="BflowS", already_meshed=False, was_gate_net=False,
             nodes=501, zone_nodes=445, ported=True, isolated=True, retained=505),
    ]
    text = "\n".join(extra_nets.run_warnings(entries))
    assert "SW is already meshed as a power net" in text
    assert "Bat+ (456 nodes) was meshed but NO port touches it" in text
    assert "prune() drops this copper" in text
    assert "BflowS is ported and therefore IN the solved deck" in text
    assert "not comparable with the same config without extra_nets" in text
    assert "galvanically ISOLATED" in text


def test_warnings_are_silent_when_there_is_nothing_to_say():
    entries = [dict(net="BflowS", already_meshed=False, was_gate_net=False,
                    nodes=10, zone_nodes=10, ported=True, isolated=False,
                    retained=10)]
    text = "\n".join(extra_nets.run_warnings(entries))
    assert "NO port touches it" not in text
    assert "already meshed as a power net" not in text


def test_manifest_carries_the_roi_and_the_policy():
    entries = _entries("BflowS")
    entries[0].update(nodes=501, zone_nodes=445, retained=505, ported=True,
                      isolated=True)
    man = extra_nets.manifest(
        entries, roi=(10.0, 20.0, 80.0, 110.0), margin=8.0,
        extents={"BflowS": dict(bbox=(53.1, 63.1, 71.2, 86.5), has_zone=True,
                                zone_bbox=(53.1, 63.3, 71.2, 86.5),
                                margin_required_mm=0.0)})
    assert man["roi_policy"] == "extra_nets_do_not_extend_roi"
    assert man["roi"] == [10.0, 20.0, 80.0, 110.0]
    assert man["margin"] == 8.0
    rec = man["nets"][0]
    assert rec["net"] == "BflowS" and rec["ported"] is True
    assert rec["zone_nodes"] == 445 and rec["retained"] == 505
    assert rec["bbox"] == [53.1, 63.1, 71.2, 86.5]


# --------------------------------------------------------------------------- #
# report.md provenance
# --------------------------------------------------------------------------- #
def _payload(nets):
    return dict(meta=dict(extra_nets=dict(
        nets=nets, roi=[10.0, 20.0, 80.0, 110.0], margin=8.0,
        roi_policy="extra_nets_do_not_extend_roi")))


def test_report_banner_is_absent_for_a_run_that_did_not_use_the_option():
    assert emit._extra_nets_banner({}) == []
    assert emit._extra_nets_banner(dict(meta={})) == []
    assert emit._extra_nets_banner(_payload([])) == []


def test_report_banner_separates_solved_copper_from_pruned_copper():
    lines = emit._extra_nets_banner(_payload([
        dict(net="BflowS", ported=True, isolated=True),
        dict(net="Bat+", ported=False, isolated=True),
    ]))
    text = "\n".join(lines)
    assert "Extra nets were meshed" in text
    assert "`BflowS` (isolated island)" in text
    assert "not** comparable" in text
    assert "Meshed but unported" in text and "`Bat+`" in text
    assert "extra nets do **not** extend it" in text


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"{len(tests)} tests — run with pytest")


if __name__ == "__main__":
    main()
