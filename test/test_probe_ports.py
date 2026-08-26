#!/usr/bin/env python3
"""Focused tests for user-declared probe ports (`probe_ports`).

Every guard in `lib/probe_ports.py` / `kicad_geom.build_probe_terminals` gets a
test that WATCHES IT FIRE on a constructed bad input, plus a passing control.
A guard never seen to fire is not a guard.

`pcbnew` is stubbed exactly as test_gate_net_override.py:15 does, so the pcbnew
side (pad selection, layer walk, contact cascade) is exercised under system
python without KiCad.
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
import kicad_geom  # noqa: E402
import probe_ports  # noqa: E402

NM = kicad_geom.NM
F_CU, B_CU = 0, 31
ZMAP = {F_CU: 0.0, B_CU: -1.6}


# --------------------------------------------------------------------------- #
# pcbnew stubs
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
    """Stand-in for kicad_geom._pad_contains: a square land around the pad."""
    if getattr(pad, "_no_land", False):
        return None
    px = kicad_geom.mm(pad.GetPosition().x)
    py = kicad_geom.mm(pad.GetPosition().y)
    h = kicad_geom.mm(pad.GetSizeX()) / 2.0
    return lambda x, y: abs(x - px) <= h and abs(y - py) <= h


@pytest.fixture(autouse=True)
def _stub_pad_contains(monkeypatch):
    monkeypatch.setattr(kicad_geom, "_pad_contains", _pad_contains_stub)


def _model_with_pour(net_layer_points, terminal_mode="padland"):
    """Model carrying a meshed pour: {(net, layer): [(x, y), ...]}."""
    m = kicad_geom.Model(terminal_mode=terminal_mode)
    m.pitch = 1.0
    for (net, lid), pts in net_layer_points.items():
        for x, y in pts:
            m.node(net, lid, x, y, ZMAP[lid], zone=True)
    return m


def _pour_grid(net, lid, cx, cy, n=3, step=0.4):
    pts = [(cx + i * step, cy + j * step)
           for i in range(-n, n + 1) for j in range(-n, n + 1)]
    return {(net, lid): pts}


# --------------------------------------------------------------------------- #
# spec parsing (structure / type / naming)
# --------------------------------------------------------------------------- #
def test_parse_spec_accepts_the_yaml_mapping_form():
    probes = probe_ports.parse_spec({"cap_at_d9": ["D9.2", "D9.3"],
                                     "cap_q2_j3": ["J3.1", "Q2.3"]})
    assert [p["name"] for p in probes] == ["cap_at_d9", "cap_q2_j3"]
    assert probes[0]["label"] == "P_probe_cap_at_d9"
    assert (probes[0]["a_ref"], probes[0]["a_pad"]) == ("D9", "2")
    assert (probes[1]["b_ref"], probes[1]["b_pad"]) == ("Q2", "3")


def test_parse_spec_round_trips_through_the_wire_form():
    spec = {"cap_at_d9": ["D9.2", "D9.3"], "snubber_land": ["R11.1", "C8.2"]}
    wire = probe_ports.to_arg(spec)
    assert wire == "cap_at_d9=D9.2:D9.3,snubber_land=R11.1:C8.2"
    assert probe_ports.parse_spec(wire) == probe_ports.parse_spec(spec)


def test_parse_spec_empty_is_empty_not_an_error():
    assert probe_ports.parse_spec(None) == []
    assert probe_ports.parse_spec({}) == []
    assert probe_ports.parse_spec("") == []


@pytest.mark.parametrize("spec, match", [
    ({"a": ["D9.2"]}, "exactly TWO terminals"),
    ({"a": ["D9.2", "D9.3", "D9.1"]}, "exactly TWO terminals"),
    ({"a": "D9.2:D9.3"}, "two-element list"),
    ({"a": ["D9", "D9.3"]}, "not 'REF.PAD'"),
    ({"a": ["D9.2.1", "D9.3"]}, "not 'REF.PAD'"),
    ({"cap at d9": ["D9.2", "D9.3"]}, "not usable as a port label"),
    ({"a": ["D9.2", "D9.2"]}, "both terminals are D9.2"),
    (["D9.2", "D9.3"], "expected a mapping"),
])
def test_parse_spec_refuses_malformed_declarations(spec, match):
    with pytest.raises(probe_ports.ProbeError, match=match):
        probe_ports.parse_spec(spec)


def test_probe_names_that_would_sanitize_are_refused_not_merged():
    # `cap-a` and `cap.a` both sanitize to P_probe_cap_a. Accepting them would
    # silently emit ONE port for TWO declared probes.
    with pytest.raises(probe_ports.ProbeError, match="not usable as a port label"):
        probe_ports.parse_spec({"cap-a": ["D9.2", "D9.3"],
                                "cap.a": ["J3.1", "Q2.3"]})
    # control: the underscore form is fine and stays distinct
    probes = probe_ports.parse_spec({"cap_a": ["D9.2", "D9.3"],
                                     "cap_b": ["J3.1", "Q2.3"]})
    assert {p["label"] for p in probes} == {"P_probe_cap_a", "P_probe_cap_b"}


def test_wire_form_rejects_a_missing_separator():
    with pytest.raises(probe_ports.ProbeError, match="expected name=REF.PAD:REF.PAD"):
        probe_ports.parse_spec("cap_at_d9=D9.2")


# --------------------------------------------------------------------------- #
# GUARD 1 — refdes absent from the board
# --------------------------------------------------------------------------- #
def test_guard1_unknown_refdes_is_a_hard_error_not_a_silent_skip():
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0)])])
    model = _model_with_pour(_pour_grid("SW", F_CU, 10.0, 10.0))
    probes = probe_ports.parse_spec({"p": ["ZZ9.1", "D9.2"]})
    with pytest.raises(probe_ports.ProbeError, match=r"refdes 'ZZ9' is not on this board"):
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)


def test_guard1_control_known_refdes_resolves():
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert probes[0]["a_node"] and probes[0]["b_node"]
    assert probes[0]["a_node"] != probes[0]["b_node"]
    assert probes[0]["a_net"] == "SW"


# --------------------------------------------------------------------------- #
# GUARD 2 — pad number absent from that footprint
# --------------------------------------------------------------------------- #
def test_guard2_unknown_pad_number_names_the_pads_that_do_exist():
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    model = _model_with_pour(_pour_grid("SW", F_CU, 10.0, 10.0))
    probes = probe_ports.parse_spec({"p": ["D9.7", "D9.2"]})
    with pytest.raises(probe_ports.ProbeError) as e:
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert "has no pad '7'" in str(e.value)
    assert "Pads on D9: 2, 3" in str(e.value)


# --------------------------------------------------------------------------- #
# GUARD 3 — pad resolves to no copper contact
# --------------------------------------------------------------------------- #
def test_guard3_pad_with_no_copper_contact_is_a_hard_error():
    # D9.3 sits on a net with no meshed pour at all (outside the ROI, say). The
    # legacy path would silently substitute a bare pad-centre node.
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("3", "GND", 40.0, 40.0)])])
    model = _model_with_pour(_pour_grid("SW", F_CU, 10.0, 10.0))
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    with pytest.raises(probe_ports.ProbeError) as e:
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert "resolved to NO copper contact" in str(e.value)
    assert "--margin" in str(e.value)


def test_guard3_control_pad_over_a_pour_bonds_and_records_the_terminal():
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert probes[0]["a_terminal"] == "padland"
    assert probes[0]["terminal_segs_added"] > 0


def test_guard3_point_terminal_mode_is_accepted_and_labelled_as_such():
    # --terminal-mode point makes a pad-centre node the RUN-WIDE terminal model,
    # so it is not a silent substitution — but it must be visible in provenance.
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour, terminal_mode="point")
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert probes[0]["a_terminal"] == "point_mode"


# --------------------------------------------------------------------------- #
# an existing pad terminal is REUSED, never re-cascaded
# --------------------------------------------------------------------------- #
def test_probe_on_an_already_terminated_pad_adds_no_copper():
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    # first consumer (stands in for build_fet / cin_ports) terminates both pads
    probes_first = probe_ports.parse_spec({"first": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes_first)
    segs_after_first = len(model.segs)

    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert len(model.segs) == segs_after_first, "re-cascading duplicated pad spokes"
    assert probes[0]["terminal_segs_added"] == 0
    assert probes[0]["terminal_nodes_added"] == 0
    assert probes[0]["a_terminal"] == "reused_existing_terminal"
    assert probes[0]["a_node"] == probes_first[0]["a_node"]


def test_partly_fallen_back_pad_does_not_get_a_duplicate_barrel():
    # A 2-layer pad whose pour exists only on F.Cu: the first consumer bonds on
    # F.Cu (a distributed terminal) and falls back to a bare node on B.Cu, then
    # links them with a vertical barrel. A second consumer must not add a SECOND
    # barrel just because the B.Cu node is not in `distributed_terminals`.
    pads = [_Pad("2", "SW", 10.0, 10.0, layers=(F_CU, B_CU)),
            _Pad("3", "GND", 14.0, 10.0, layers=(F_CU, B_CU))]
    board = _Board([_Footprint("D9", pads)])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    first = probe_ports.parse_spec({"first": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, first)
    segs_after_first = len(model.segs)
    assert first[0]["a_terminal"].startswith("padland")
    assert "point_fallback" in first[0]["a_terminal"]     # B.Cu had no pour

    second = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    # allow_proximity: on B.Cu (no pour) the FIRST pass left a bare pad-centre
    # node, and _pad_via_top_contacts then offers it to the second pass as a
    # same-layer "via top" — a proximity bond the new F4 guard refuses by default.
    # That interaction is real but orthogonal; this test is about the BARREL.
    kicad_geom.build_probe_terminals(board, model, ZMAP, second, allow_proximity=True)
    assert len(model.segs) == segs_after_first, "duplicate THT barrel added"
    assert second[0]["terminal_segs_added"] == 0


def test_single_mode_does_not_add_a_second_barrel_over_the_legacy_stack():
    # CODEX FINDING 1a. In `single` mode _pad_land_terminal returns a POUR node and
    # never registers it in distributed_terminals, and that node is not at the pad
    # centre — so an existence check at the pad centre finds nothing and the probe
    # re-adds a barrel the legacy stack already placed.
    pads = [_Pad("2", "SW", 10.0, 10.0, layers=(F_CU, B_CU)),
            _Pad("3", "GND", 14.0, 10.0, layers=(F_CU, B_CU))]
    board = _Board([_Footprint("D9", pads)])
    pour = dict(_pour_grid("SW", F_CU, 10.0, 10.0))
    pour.update(_pour_grid("SW", B_CU, 10.0, 10.0))
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    pour.update(_pour_grid("GND", B_CU, 14.0, 10.0))
    model = _model_with_pour(pour, terminal_mode="single")
    first = probe_ports.parse_spec({"first": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, first)
    segs_after_first = len(model.segs)
    assert segs_after_first > 0, "the legacy-equivalent stack placed no barrel"

    second = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, second)
    assert len(model.segs) == segs_after_first, "duplicate barrel in single mode"


def test_pad_centre_pour_nodes_still_get_their_barrel():
    # CODEX FINDING 1b, the opposite direction: pour nodes landing exactly on the
    # pad centre on BOTH layers "pre-exist", but nothing joins them vertically.
    # Skipping the barrel there would leave the pad stack open.
    pads = [_Pad("2", "SW", 10.0, 10.0, layers=(F_CU, B_CU)),
            _Pad("3", "GND", 14.0, 10.0, layers=(F_CU, B_CU))]
    board = _Board([_Footprint("D9", pads)])
    pour = dict(_pour_grid("SW", F_CU, 10.0, 10.0))
    pour.update(_pour_grid("SW", B_CU, 10.0, 10.0))
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    pour.update(_pour_grid("GND", B_CU, 14.0, 10.0))
    model = _model_with_pour(pour)          # grid includes the exact pad centre
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    a_f = model.existing_node("SW", F_CU, 10.0, 10.0, ZMAP[F_CU])
    a_b = model.existing_node("SW", B_CU, 10.0, 10.0, ZMAP[B_CU])
    assert a_f and a_b
    assert model.has_direct_link(a_f, a_b), "pad stack left open — no vertical barrel"


def test_pad_on_no_copper_layer_is_refused_not_invented():
    # CODEX FINDING 2a. The legacy stack substitutes cu[0]; for a probe that
    # fabricates a land on a layer the pad does not touch and then bonds it to
    # whatever pour happens to be there.
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0, layers=()),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    segs0 = len(model.segs)
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    with pytest.raises(probe_ports.ProbeError, match="on NO copper layer"):
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert len(model.segs) == segs0, "invented terminal copper before refusing"


def test_duplicate_pad_numbers_on_one_net_land_on_the_largest():
    # An SMD power package splits one terminal across several lands (thermal tab
    # plus lead fingers), and a terminal-block footprint carries alternate lands
    # for two mounting orientations. Same number, same net, one TERMINAL -- that
    # is normal, and refusing it blocks every power-path probe on the board. Land
    # on the biggest land: it carries the terminal's current, and the choice must
    # not depend on the order pads happen to appear in the footprint.
    small = _Pad("2", "SW", 20.0, 10.0, size_mm=0.5)
    big = _Pad("2", "SW", 10.0, 10.0, size_mm=2.0)
    board = _Board([_Footprint("D9", [small, big, _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour[("SW", F_CU)] += _pour_grid("SW", F_CU, 20.0, 10.0)[("SW", F_CU)]
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    # The port sits over the 2.0 mm land, not the 0.5 mm one it was declared after.
    where = {v: k for k, v in model._nodes.items()}[probes[0]["a_node"]]
    assert abs(where[2] * kicad_geom.SNAP - 10.0) < 0.5, where


def test_duplicate_pad_numbers_on_different_nets_are_refused():
    # CODEX FINDING 2b, narrowed to the case that is genuinely ambiguous: two
    # lands, one number, DIFFERENT nets. 'D9.2' then names no single node and
    # picking one silently decides which loop was measured.
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("2", "BuckGND", 20.0, 10.0),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    segs0 = len(model.segs)
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    with pytest.raises(probe_ports.ProbeError) as e:
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    msg = str(e.value)
    assert "2 pads numbered '2'" in msg and "DIFFERENT nets" in msg
    assert "BuckGND" in msg and "SW" in msg
    assert len(model.segs) == segs0, "invented terminal copper before refusing"


def test_perturbation_attribution_is_order_independent():
    # CODEX FINDING 5. Per-probe seg/node deltas credit shared terminal geometry to
    # whichever probe ran FIRST. Three probes forming a triangle over three
    # previously-unterminated pads is the discriminating case: whichever runs last
    # finds BOTH its pads already built and, on the delta alone, reports "I added
    # nothing" — a false "harmless" that moves with declaration order.
    def build(order):
        board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                          _Pad("3", "GND", 14.0, 10.0),
                                          _Pad("4", "GND", 18.0, 10.0)])])
        pour = _pour_grid("SW", F_CU, 10.0, 10.0)
        # same (net, layer) key twice -> MERGE the point lists, don't replace
        pour[("GND", F_CU)] = (_pour_grid("GND", F_CU, 14.0, 10.0)[("GND", F_CU)]
                               + _pour_grid("GND", F_CU, 18.0, 10.0)[("GND", F_CU)])
        model = _model_with_pour(pour)
        probes = probe_ports.parse_spec({name: ends for name, ends in order})
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
        for pr in probes:
            model.port(pr["label"], pr["a_node"], pr["b_node"])
        # a non-probe baseline port so `base_seeds` is non-empty
        model.port("P_pwr", probes[0]["a_node"], probes[0]["b_node"])
        probe_ports.annotate_perturbation(model, probes)
        return {p["name"]: p["pulled_new_copper"] for p in probes}

    tri = [("a", ["D9.2", "D9.3"]), ("b", ["D9.2", "D9.4"]), ("c", ["D9.3", "D9.4"])]
    abc = build(tri)
    cba = build(list(reversed(tri)))
    assert abc == cba, f"verdict depends on declaration order: {abc} vs {cba}"
    assert all(abc.values()), "shared new terminal copper reported as harmless"


def test_two_endpoints_that_intern_to_one_node_are_refused():
    # Two same-net pads inside one SNAP cell. The terminal-identity guard (F3b)
    # now catches this FIRST and with a more precise message; either way it is a
    # refusal, never a port across one node.
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("9", "SW", 10.0, 10.0)])])
    model = _model_with_pour(_pour_grid("SW", F_CU, 10.0, 10.0))
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.9"]})
    with pytest.raises(probe_ports.ProbeError, match="lands on the terminal already built for D9.2"):
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)


def test_F3b_reuse_is_bound_to_the_pad_that_built_the_terminal():
    # CODEX-2 FINDING 3b: the node key is only (net, layer, snapped xy), so a
    # DIFFERENT same-net pad within one SNAP cell interned to the same node and
    # was handed the first pad's contact region. Reproduced with a small pad and
    # a LARGER pad at the same spot: the large one reported
    # `reused_existing_terminal`, added zero contacts, and inherited the small
    # pad's single-node patch — a material change to spreading impedance.
    small = _Pad("2", "SW", 10.0, 10.0, size_mm=0.3)
    large = _Pad("7", "SW", 10.0, 10.0, size_mm=3.0)
    gnd = _Pad("3", "GND", 14.0, 10.0)
    board = _Board([_Footprint("D9", [small, large, gnd])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    kicad_geom.build_probe_terminals(
        board, model, ZMAP, probe_ports.parse_spec({"first": ["D9.2", "D9.3"]}))
    owner = model.terminal_owner[
        model.existing_node("SW", F_CU, 10.0, 10.0, ZMAP[F_CU])]
    assert owner["ref"] == "D9" and owner["pad"] == "2"

    other = probe_ports.parse_spec({"p": ["D9.7", "D9.3"]})
    with pytest.raises(probe_ports.ProbeError) as e:
        kicad_geom.build_probe_terminals(board, model, ZMAP, other)
    assert "lands on the terminal already built for D9.2" in str(e.value)


def test_F3b_control_the_same_pad_still_reuses():
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    kicad_geom.build_probe_terminals(
        board, model, ZMAP, probe_ports.parse_spec({"first": ["D9.2", "D9.3"]}))
    segs = len(model.segs)
    again = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, again)
    assert again[0]["a_terminal"] == "reused_existing_terminal"
    assert len(model.segs) == segs


def _proximity_board():
    """A pad with NO overlapping mesh node and a same-net pour node 1 mm away."""
    pad = _Pad("1", "SW", 10.0, 10.0, size_mm=0.1)
    board = _Board([_Footprint("R11", [pad]),
                    _Footprint("C8", [_Pad("2", "GND", 14.0, 10.0)])])
    model = kicad_geom.Model()
    model.pitch = 1.0
    model.node("SW", F_CU, 11.0, 10.0, ZMAP[F_CU], zone=True)   # 1 mm away
    for x, y in _pour_grid("GND", F_CU, 14.0, 10.0)[("GND", F_CU)]:
        model.node("GND", F_CU, x, y, ZMAP[F_CU], zone=True)
    return board, model


def test_F4_proximity_only_bond_is_refused_by_default():
    # CODEX-2 FINDING 4: no mesh node overlaps the pad, so _pad_land_terminal
    # fabricates spokes to nearby pour nodes and returns an ordinary "padland"
    # terminal. For a mount-loop measurement those spokes ARE the quantity.
    board, model = _proximity_board()
    probes = probe_ports.parse_spec({"snubber_land": ["R11.1", "C8.2"]})
    with pytest.raises(probe_ports.ProbeError) as e:
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    msg = str(e.value)
    assert "bonded by PROXIMITY" in msg
    assert "--probe-allow-proximity-bond" in msg and "--pitch" in msg


def test_F4_proximity_bond_is_allowed_explicitly_and_recorded():
    board, model = _proximity_board()
    probes = probe_ports.parse_spec({"snubber_land": ["R11.1", "C8.2"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes,
                                     allow_proximity=True)
    assert probes[0]["a_bond"] == "proximity"
    assert probes[0]["a_proximity"] is True
    assert probes[0]["b_bond"] == "overlap"        # C8 really overlaps its pour
    assert "a_bond" in probe_ports.manifest(probes)[0]


def test_F4_real_overlap_is_not_flagged_as_proximity():
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("3", "GND", 14.0, 10.0)])])
    pour = _pour_grid("SW", F_CU, 10.0, 10.0)
    pour.update(_pour_grid("GND", F_CU, 14.0, 10.0))
    model = _model_with_pour(pour)
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    kicad_geom.build_probe_terminals(board, model, ZMAP, probes)
    assert probes[0]["a_bond"] == "overlap" and probes[0]["b_bond"] == "overlap"
    assert probes[0]["a_proximity"] is False


def test_intern_to_one_node_guard_still_covers_point_mode():
    # In point mode nothing is registered in distributed_terminals, so the
    # identity guard cannot fire — the original a_node==b_node refusal is what
    # keeps a port-across-one-node from being emitted. Keep it exercised.
    board = _Board([_Footprint("D9", [_Pad("2", "SW", 10.0, 10.0),
                                      _Pad("9", "SW", 10.0, 10.0)])])
    model = _model_with_pour(_pour_grid("SW", F_CU, 10.0, 10.0), terminal_mode="point")
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.9"]})
    with pytest.raises(probe_ports.ProbeError, match="intern to the SAME node"):
        kicad_geom.build_probe_terminals(board, model, ZMAP, probes)


# --------------------------------------------------------------------------- #
# GUARD 3b — the probe must span COPPER, not a synthesized short
# --------------------------------------------------------------------------- #
def _fet_like_model(lead_mm):
    """Pad -> die riser -> .equiv die short -> die riser -> pad, as build_fet emits.

    `lead_mm=0` puts the riser at 0.001 mm (the real deck's value), which is what
    collapses the closure onto the pads.
    """
    m = kicad_geom.Model()
    drn_pad = m.node("SW", F_CU, 10.0, 10.0, 0.0)
    src_pad = m.node("GND", F_CU, 14.0, 10.0, 0.0)
    riser = max(lead_mm, 0.001)
    drn_die = m.node("SW", F_CU, 10.0, 10.0, riser)
    src_die = m.node("GND", F_CU, 14.0, 10.0, riser)
    m.seg(drn_pad, drn_die, 1.0)
    m.seg(src_pad, src_die, 1.0)
    m.equiv(drn_die, src_die)                 # the die-plane closure
    return m, drn_pad, src_pad


def test_probe_across_a_zero_lead_device_closure_is_refused():
    # THE MEASURED CASE: Fugu2, lead_mm=0, D9.2->D9.3 returned 1.702e-15 H.
    model, drn_pad, src_pad = _fet_like_model(lead_mm=0)
    probes = [dict(name="cap_at_d9", label="P_probe_cap_at_d9",
                   a="D9.2", b="D9.3", a_node=drn_pad, b_node=src_pad)]
    with pytest.raises(probe_ports.ProbeError) as e:
        probe_ports.require_not_shorted(model, probes)
    msg = str(e.value)
    assert "SHORTED" in msg and "die-plane closure" in msg
    assert "capacitor's own ESL" in msg


def test_probe_over_real_copper_is_not_mistaken_for_a_short():
    # CONTROL, and the discriminator: cap_q2_j3-like, real copper between.
    model, drn_pad, _src = _fet_like_model(lead_mm=0)
    far = model.node("GND", F_CU, 40.0, 10.0, 0.0)
    mid = model.node("GND", F_CU, 27.0, 10.0, 0.0)
    model.seg(far, mid, 1.0)
    model.seg(mid, drn_pad, 1.0)
    probes = [dict(name="cap_q2_j3", label="P_probe_cap_q2_j3",
                   a="J3.1", b="Q2.3", a_node=drn_pad, b_node=far)]
    probe_ports.require_not_shorted(model, probes)          # no raise


def test_short_detection_does_not_fire_on_a_long_equiv_free_chain():
    # Monotonicity: many real segments in series is the opposite of a short.
    m = kicad_geom.Model()
    prev = m.node("SW", F_CU, 0.0, 0.0, 0.0)
    first = prev
    for i in range(1, 12):
        n = m.node("SW", F_CU, float(i), 0.0, 0.0)
        m.seg(prev, n, 1.0)
        prev = n
    probes = [dict(name="p", label="P_probe_p", a="A.1", b="B.1",
                   a_node=first, b_node=prev)]
    probe_ports.require_not_shorted(m, probes)              # no raise


def test_probe_across_device_terminals_is_refused_even_with_real_leads():
    # With lead_mm>0 the closure is no longer a SHORT, so require_not_shorted lets
    # it through — but the port still reports synthesized package geometry under a
    # name that promises board copper. The verdict must not depend on lead_mm.
    model, drn_pad, src_pad = _fet_like_model(lead_mm=3.0)
    probes = [dict(name="cap_at_d9", label="P_probe_cap_at_d9",
                   a="D9.2", b="D9.3", a_node=drn_pad, b_node=src_pad)]
    probe_ports.require_not_shorted(model, probes)          # not a short any more
    with pytest.raises(probe_ports.ProbeError, match="both terminals of device D9"):
        probe_ports.require_not_across_device_closure(
            probes, {"D9": {drn_pad, src_pad}})
    # control: endpoints on two DIFFERENT devices are not one device's closure
    probe_ports.require_not_across_device_closure(
        probes, {"D9": {drn_pad}, "Q2": {src_pad}})


def test_snap_quoted_in_the_message_matches_the_one_actually_used():
    # probe_ports keeps a literal so it stays importable without pcbnew, but the
    # BEHAVIOUR uses kicad_geom.SNAP. If they drift, the refusal message quotes a
    # threshold the guard does not use.
    assert probe_ports.SNAP_MM == kicad_geom.SNAP


def test_device_closure_guard_is_gated_on_the_device_being_closed():
    # Under a cap_only basis the FET is NOT shorted drain-source, so a port across
    # its tabs measures a real board loop; refusing it would be over-strict and the
    # message would be false. Pin that the structural guard is gated on
    # fet_closure, while the evidence-based one is not.
    import inspect
    lines = inspect.getsource(kicad_geom.build).splitlines()
    def indent_of(needle):
        for l in lines:
            if needle in l and not l.strip().startswith("#"):
                return len(l) - len(l.lstrip())
        raise AssertionError(f"{needle} not found in build()")
    gate = indent_of('if fet_closure == "full_loop":')
    structural = indent_of("probe_ports_lib.require_not_across_device_closure(")
    evidence = indent_of("probe_ports_lib.require_not_shorted(")
    assert structural > gate, "structural guard is not inside the fet_closure gate"
    assert evidence == gate, "evidence-based guard must NOT be gated on fet_closure"


def test_device_closure_nodes_harvests_both_parallel_models():
    per_dev = {"hs": {"_devices": [dict(ref="Q1", _drn_pad_node="N1",
                                        _src_pad_node="N2", _die_src="N3",
                                        _die_drn="N4")]},
               "ls": {"_devices": [dict(ref="D9", _drn_pad_node="N5",
                                        _src_pad_node="N6", _die_src="N7",
                                        _die_drn=None)]}}
    got = kicad_geom._device_closure_nodes(per_dev)
    assert got == {"Q1": {"N1", "N2", "N3", "N4"}, "D9": {"N5", "N6", "N7"}}
    lumped = {"ls": {"refs": ["Q2"], "_drn_pad_node": "N8",
                     "_src_pad_node": "N9", "_die_src": "N10"}}
    assert kicad_geom._device_closure_nodes(lumped) == {"Q2": {"N8", "N9", "N10"}}
    assert kicad_geom._device_closure_nodes({}) == {}


# --------------------------------------------------------------------------- #
# GUARD 4 — duplicate / colliding port labels
# --------------------------------------------------------------------------- #
def test_guard4_label_collision_with_a_generated_port_fires():
    ports = [("P_pwr", "N1", "N2"), ("P_probe_x", "N3", "N4"),
             ("P_probe_x", "N5", "N6")]
    with pytest.raises(probe_ports.ProbeError, match="port label collision"):
        probe_ports.require_unique_labels(ports)


def test_guard4_control_distinct_labels_pass():
    ports = [("P_pwr", "N1", "N2"), ("P_probe_x", "N3", "N4"),
             ("P_probe_y", "N5", "N6")]
    probe_ports.require_unique_labels(ports)


# --------------------------------------------------------------------------- #
# GUARD 5 — same node pair as an existing port (singular Zc)
# --------------------------------------------------------------------------- #
def test_guard5_same_node_pair_as_an_existing_port_fires_and_says_what_to_read():
    ports = [("P_ls_D9", "NA", "NB"), ("P_probe_cap_at_d9", "NB", "NA")]
    with pytest.raises(probe_ports.ProbeError) as e:
        probe_ports.require_distinct_node_pairs(ports)
    msg = str(e.value)
    assert "P_probe_cap_at_d9" in msg and "P_ls_D9" in msg
    assert "Error on factor" in msg
    assert "read P_ls_D9 from port_L/port_R instead" in msg


def test_guard5_two_probes_on_one_node_pair_also_fire():
    ports = [("P_pwr", "N0", "N1"), ("P_probe_a", "NA", "NB"),
             ("P_probe_b", "NA", "NB")]
    with pytest.raises(probe_ports.ProbeError, match="P_probe_a, P_probe_b"):
        probe_ports.require_distinct_node_pairs(ports)


def test_guard5_control_distinct_node_pairs_pass():
    ports = [("P_ls_D9", "NA", "NB"), ("P_probe_cap_at_d9", "NA", "NC")]
    probe_ports.require_distinct_node_pairs(ports)


def test_guard5_ignores_a_pre_existing_duplicate_that_has_no_probe():
    # Not ours to diagnose — and must not be mis-reported as a probe collision.
    ports = [("P_hs", "NA", "NB"), ("P_ls", "NA", "NB")]
    probe_ports.require_distinct_node_pairs(ports)


# --------------------------------------------------------------------------- #
# GUARD 6 — a probe dropped as floating
# --------------------------------------------------------------------------- #
def test_guard6_probe_dropped_as_floating_is_a_hard_error():
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    ports = [("P_pwr", "N1", "N2")]
    with pytest.raises(probe_ports.ProbeError, match="not connected to the commutation loop"):
        probe_ports.require_not_dropped(probes, ["P_probe_p"], ports)


def test_guard6_also_fires_when_the_label_just_vanished_without_being_listed():
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    with pytest.raises(probe_ports.ProbeError, match="P_probe_p"):
        probe_ports.require_not_dropped(probes, [], [("P_pwr", "N1", "N2")])


def test_guard6_control_surviving_probe_passes():
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    ports = [("P_pwr", "N1", "N2"), ("P_probe_p", "N3", "N4")]
    probe_ports.require_not_dropped(probes, [], ports)


def test_guard6_fires_end_to_end_through_drop_floating_ports():
    model = kicad_geom.Model()
    a = model.node("Vin", F_CU, 0.0, 0.0, 0.0)
    b = model.node("GND", F_CU, 1.0, 0.0, 0.0)
    model.seg(a, b, 1.0)
    # an island of copper with no path to the P_pwr component
    c = model.node("SW", F_CU, 90.0, 90.0, 0.0)
    d = model.node("GND", F_CU, 91.0, 90.0, 0.0)
    model.seg(c, d, 1.0)
    model.port("P_pwr", a, b)
    model.port("P_probe_p", c, d)
    dropped = model.drop_floating_ports("P_pwr")
    assert dropped == ["P_probe_p"]
    probes = probe_ports.parse_spec({"p": ["D9.2", "D9.3"]})
    with pytest.raises(probe_ports.ProbeError, match="P_probe_p"):
        probe_ports.require_not_dropped(probes, dropped, model.ports)


# --------------------------------------------------------------------------- #
# GUARD 7 — perturbation is measured, never assumed
# --------------------------------------------------------------------------- #
def _perturb_model():
    model = kicad_geom.Model()
    a = model.node("Vin", F_CU, 0.0, 0.0, 0.0)
    b = model.node("GND", F_CU, 1.0, 0.0, 0.0)
    model.seg(a, b, 1.0)
    model.port("P_pwr", a, b)
    return model, a, b


def test_guard7_probe_on_isolated_copper_is_flagged():
    model, a, b = _perturb_model()
    c = model.node("SW", F_CU, 50.0, 0.0, 0.0)
    d = model.node("SW", F_CU, 51.0, 0.0, 0.0)
    model.seg(c, d, 1.0)
    model.port("P_probe_p", c, d)
    probes = [dict(name="p", label="P_probe_p", a_node=c, b_node=d,
                   terminal_segs_added=0, terminal_nodes_added=0)]
    probe_ports.annotate_perturbation(model, probes)
    assert probes[0]["pulled_new_copper"] is True
    assert probes[0]["retained_nodes_added"] == 2
    assert probes[0]["perturbation_basis"] == "measured"


def test_guard7_probe_on_already_ported_copper_is_not_flagged():
    model, a, b = _perturb_model()
    model.port("P_probe_p", a, b)
    probes = [dict(name="p", label="P_probe_p", a_node=a, b_node=b,
                   terminal_segs_added=0, terminal_nodes_added=0)]
    probe_ports.annotate_perturbation(model, probes)
    assert probes[0]["pulled_new_copper"] is False
    assert probes[0]["retained_nodes_added"] == 0


def test_guard7_endpoint_built_during_the_probe_phase_counts_even_with_zero_deltas():
    # CODEX FINDING 5, the discriminating case. A probe whose endpoints are both
    # inside the baseline component AND whose own seg/node deltas are zero, because
    # an EARLIER probe paid for building the terminals it shares. Without the
    # order-free signal this reports "harmless"; the copper is still probe-added.
    model, a, b = _perturb_model()
    model.port("P_probe_second", a, b)
    probes = [dict(name="second", label="P_probe_second", a_node=a, b_node=b,
                   terminal_segs_added=0, terminal_nodes_added=0,
                   endpoint_new_in_probe_phase=True)]
    probe_ports.annotate_perturbation(model, probes)
    assert probes[0]["retained_nodes_added"] == 0        # baseline sees both nodes
    assert probes[0]["pulled_new_copper"] is True


def test_guard7_terminal_geometry_alone_counts_as_perturbation():
    model, a, b = _perturb_model()
    model.port("P_probe_p", a, b)
    probes = [dict(name="p", label="P_probe_p", a_node=a, b_node=b,
                   terminal_segs_added=4, terminal_nodes_added=1)]
    probe_ports.annotate_perturbation(model, probes)
    assert probes[0]["pulled_new_copper"] is True


def test_guard7_no_baseline_ports_reports_perturbed_not_fine():
    # Absence of a baseline must never encode "the probe was harmless".
    model = kicad_geom.Model()
    a = model.node("SW", F_CU, 0.0, 0.0, 0.0)
    b = model.node("GND", F_CU, 1.0, 0.0, 0.0)
    model.seg(a, b, 1.0)
    model.port("P_probe_p", a, b)
    probes = [dict(name="p", label="P_probe_p", a_node=a, b_node=b,
                   terminal_segs_added=0, terminal_nodes_added=0)]
    probe_ports.annotate_perturbation(model, probes)
    assert probes[0]["pulled_new_copper"] is True
    assert probes[0]["perturbation_basis"] == "no_baseline_ports"


# --------------------------------------------------------------------------- #
# GUARD 8 — switch_residual basis refuses probe ports
# --------------------------------------------------------------------------- #
def test_guard8_switch_residual_basis_refuses_probe_ports():
    with pytest.raises(ValueError, match="switch_residual"):
        kicad_geom.build(
            _Board([]), {"sw": "SW", "vin": "Vin", "gnd": "GND",
                         "hs": {"gate": "HG", "refs": []},
                         "ls": {"gate": "LG", "refs": []}, "cin": []},
            cin_extraction_basis="switch_residual",
            probe_ports={"p": ["D9.2", "D9.3"]})


def test_guard8_message_explains_why_and_what_to_do():
    monkey = kicad_geom.validate_switch_residual_ports
    assert monkey  # the validator this guard exists to stay ahead of
    err = None
    try:
        kicad_geom.build(
            _Board([]), {"sw": "SW", "vin": "Vin", "gnd": "GND",
                         "hs": {"gate": "HG", "refs": []},
                         "ls": {"gate": "LG", "refs": []}, "cin": []},
            cin_extraction_basis="switch_residual",
            probe_ports={"p": ["D9.2", "D9.3"]})
    except ValueError as e:
        err = str(e)
    assert err and "full_loop" in err and "residual gauge" in err


# --------------------------------------------------------------------------- #
# the guards are actually WIRED IN
# --------------------------------------------------------------------------- #
def test_every_guard_is_invoked_from_build():
    # The guard tests above call the helpers directly, so deleting their
    # production call sites would leave them all green. `build()` needs a real
    # board to run end to end, so pin the wiring at the source level instead —
    # crude, but it is what fails if a call site is dropped.
    import inspect
    src = inspect.getsource(kicad_geom.build)
    for fn in ("require_unique_labels", "require_distinct_node_pairs",
               "require_not_dropped", "annotate_perturbation", "manifest",
               "require_no_production_cin_network", "require_not_shorted",
               "require_not_across_device_closure"):
        assert f"probe_ports_lib.{fn}(" in src, f"{fn} is not called from build()"
    assert "build_probe_terminals(" in src
    # ...and the terminals are built BEFORE the stitch, or the port floats away
    assert src.index("build_probe_terminals(") < src.index("model.stitch_zones("), \
        "probe terminals are created after stitch_zones — they will never bond"
    # ...and the ports are created BEFORE the floating-port drop the guard reads
    assert src.index("probe_ports_lib.require_unique_labels(") \
        < src.index("model.drop_floating_ports("), \
        "probe ports are added after drop_floating_ports — guard 6 cannot see them"
    # The refusals that live inside the terminal walk rather than in probe_ports.
    stack = inspect.getsource(kicad_geom._probe_pad_node_stack)
    # (the message is split across f-string lines, so match a contiguous fragment)
    assert "lands on the terminal " in stack and "already built for " in stack, \
        "terminal-identity refusal (F3b) is not on the production path"
    assert 'owner.get("ref") == ref and owner.get("pad") == num' in stack, \
        "reuse is not bound to the pad identity that built the terminal"
    assert "if own_proximity and not allow_proximity:" in stack, \
        "proximity refusal (F4) is not on the production path"
    assert "allow_proximity=allow_proximity" in \
        inspect.getsource(kicad_geom.build_probe_terminals), \
        "the proximity policy never reaches the terminal walk"
    # ...and the numeric validation, which lives downstream in the reducer.
    red = inspect.getsource(solve_reduce.reduce_parasitics)
    assert "_validate_probe_numerics(i, lbl)" in red, \
        "probe numeric validation (F1) is not called from reduce_parasitics"


def test_production_cin_network_is_refused_on_a_perturbed_deck():
    # CODEX FINDING 3: cin_branches / cin_matrix are a PRODUCTION output the loss
    # deck consumes; they must not be reduced from a deck a diagnostic probe added
    # terminal copper to.
    dirty = [dict(name="cap_q2_j3", pulled_new_copper=True)]
    clean = [dict(name="cap_at_d9", pulled_new_copper=False)]
    with pytest.raises(probe_ports.ProbeError, match="Refusing"):
        probe_ports.require_no_production_cin_network(dirty, True, "matrix")
    with pytest.raises(probe_ports.ProbeError, match="Refusing"):
        probe_ports.require_no_production_cin_network(dirty, True, "scalar_trunk")
    # controls: an unperturbed probe, or a run that emits no Cin network at all
    probe_ports.require_no_production_cin_network(clean, True, "matrix")
    probe_ports.require_no_production_cin_network(dirty, False, "matrix")
    probe_ports.require_no_production_cin_network([], True, "matrix")
    # ...and it is wired into build()
    import inspect
    assert "probe_ports_lib.require_no_production_cin_network(" in \
        inspect.getsource(kicad_geom.build)


# --------------------------------------------------------------------------- #
# manifest / label helpers
# --------------------------------------------------------------------------- #
def test_manifest_is_json_safe_and_carries_the_perturbation_verdict():
    import json
    probes = probe_ports.parse_spec({"cap_at_d9": ["D9.2", "D9.3"]})
    probes[0].update(a_node="N1", b_node="N2", a_net="SW", b_net="GND",
                     a_terminal="padland", b_terminal="padland",
                     terminal_segs_added=3, terminal_nodes_added=1)
    probe_ports.annotate_perturbation(
        _perturb_model()[0], probes)
    man = probe_ports.manifest(probes)
    json.dumps(man)
    assert man[0]["label"] == "P_probe_cap_at_d9"
    assert man[0]["pulled_new_copper"] is True
    assert "a_node" not in man[0], "node names are run-local, not provenance"


# --------------------------------------------------------------------------- #
# the derived parasitics.json block (solve_reduce)
# --------------------------------------------------------------------------- #
import numpy as np  # noqa: E402
import solve_reduce  # noqa: E402

_W = 2 * np.pi * 5e6


def _reduce(Lmatrix, ports, topo):
    zc = {5e6: 1j * _W * np.asarray(Lmatrix, dtype=float)}
    return solve_reduce.reduce_parasitics(zc, ports, topo, {}, plateau=5e6,
                                          cin_ports=["P_pwr"])


def _topo(pulled=False, **over):
    entry = dict(name="cap_at_d9", label="P_probe_cap_at_d9", a="D9.2", b="D9.3",
                 pulled_new_copper=pulled, retained_nodes_added=0,
                 a_terminal="padland", b_terminal="padland")
    entry.update(over)
    return {"probe_ports": [entry]}


def test_derived_probe_block_exposes_L_R_and_the_mutual_to_the_loop():
    p = _reduce([[9e-9, 1.5e-9], [1.5e-9, 2e-9]],
                ["P_pwr", "P_probe_cap_at_d9"], _topo())
    blk = p["probe_ports"]["cap_at_d9"]
    assert blk["label"] == "P_probe_cap_at_d9"
    assert blk["a"] == "D9.2" and blk["b"] == "D9.3"
    assert blk["L"] == pytest.approx(2e-9)
    assert blk["M_to_loop"] == pytest.approx(1.5e-9)
    assert blk["M_to_loop_port"] == "P_pwr"
    assert blk["pulled_new_copper"] is False
    # the extra port must not disturb the headline loop reduction
    assert p["L_loop"] == pytest.approx(9e-9)
    assert p["ports"][-1] == "P_probe_cap_at_d9"


def test_M_to_loop_is_the_effective_mutual_not_just_the_first_cin_row():
    # CODEX FINDING 6. Two identical caps in parallel carry equal current, so the
    # mutual to the reduced loop is the MEAN of the two probe rows, not the first.
    Lb = 4e-9
    Lm = [
        [Lb,   0.0,  1e-9],       # P_pwr
        [0.0,  Lb,   5e-9],       # P_pwr1
        [1e-9, 5e-9, 2e-9],       # probe
    ]
    zc = {5e6: 1j * _W * np.asarray(Lm, dtype=float)}
    p = solve_reduce.reduce_parasitics(
        zc, ["P_pwr", "P_pwr1", "P_probe_cap_at_d9"], _topo(), {},
        plateau=5e6, cin_ports=["P_pwr", "P_pwr1"])
    blk = p["probe_ports"]["cap_at_d9"]
    assert blk["M_to_loop"] == pytest.approx(3e-9), "reported the first Cin row"
    assert blk["M_to_first_cin"] == pytest.approx(1e-9)   # the raw row, kept
    assert blk["M_to_loop_basis"] == ["P_pwr", "P_pwr1"]


def test_probe_ports_perturbed_is_a_top_level_flag():
    clean = _reduce([[9e-9, 0.0], [0.0, 2e-9]],
                    ["P_pwr", "P_probe_cap_at_d9"], _topo())
    assert clean["probe_ports_perturbed"] is False
    dirty = _reduce([[9e-9, 0.0], [0.0, 2e-9]],
                    ["P_pwr", "P_probe_cap_at_d9"], _topo(pulled=True))
    assert dirty["probe_ports_perturbed"] is True
    none = _reduce([[9e-9]], ["P_pwr"], {})
    assert none["probe_ports_perturbed"] is None
    # unknown verdict must stay UNKNOWN, not collapse to False
    topo = _topo()
    del topo["probe_ports"][0]["pulled_new_copper"]
    unk = _reduce([[9e-9, 0.0], [0.0, 2e-9]],
                  ["P_pwr", "P_probe_cap_at_d9"], topo)
    assert unk["probe_ports_perturbed"] is None


def test_F1_nan_only_in_the_probe_row_is_refused_not_emitted():
    # CODEX-2 FINDING 1: a valid P_pwr row with NaN ONLY in the probe row used to
    # emit L/R/R_dc/L_ring/R_ring/M_to_loop all as nan, with no warning.
    Lm = [[9e-9, 0.0], [0.0, float("nan")]]
    with pytest.raises(ValueError, match="non-finite"):
        _reduce(Lm, ["P_pwr", "P_probe_cap_at_d9"], _topo())


def test_F1_non_positive_self_inductance_is_refused():
    for bad in (0.0, -2e-9):
        with pytest.raises(ValueError, match="not positive"):
            _reduce([[9e-9, 0.0], [0.0, bad]],
                    ["P_pwr", "P_probe_cap_at_d9"], _topo())


def test_F1_materially_negative_self_resistance_is_refused():
    # R comes from Z.real; build Z directly so R can be made negative.
    Z = np.array([[1j * _W * 9e-9, 0.0],
                  [0.0, -5e-3 + 1j * _W * 2e-9]], dtype=complex)
    with pytest.raises(ValueError, match="materially negative"):
        solve_reduce.reduce_parasitics({5e6: Z},
                                       ["P_pwr", "P_probe_cap_at_d9"], _topo(), {},
                                       plateau=5e6, cin_ports=["P_pwr"])


def test_F1_non_reciprocal_probe_submatrix_is_refused():
    Lm = [[9e-9, 3e-9], [8e-9, 2e-9]]          # L[0,1] != L[1,0], grossly
    with pytest.raises(ValueError, match="not reciprocal"):
        _reduce(Lm, ["P_pwr", "P_probe_cap_at_d9"], _topo())


def test_F1_thresholds_do_not_false_fire_on_realistic_values():
    # CONTROL, and the reason the thresholds are what they are: the real Fugu2
    # solve has scaled reciprocity 9.6e-4 and cond(Cin+probe)=49, while the NAIVE
    # relative reciprocity metric reads 1.386 and cond(full)=2.6e6 on that same
    # valid board. A guard using either of those would refuse every real run.
    Lm = [[8.56e-9, 2.79e-12, 7.25e-10],
          [2.79e-12, 8.40e-9, 2.01e-10],
          [7.25e-10, 2.01e-10, 1.2055e-9]]
    p = _reduce(Lm, ["P_pwr", "P_pwr1", "P_probe_cap_at_d9"], _topo())
    assert p["probe_ports"]["cap_at_d9"]["L"] == pytest.approx(1.2055e-9)


def test_no_probe_ports_declared_leaves_the_block_absent():
    p = _reduce([[9e-9]], ["P_pwr"], {})
    assert p["probe_ports"] is None


def test_declared_probe_missing_from_the_solved_ports_raises():
    # The extractor's own guard 6 makes this impossible; if it shows up here the
    # sidecar does not describe this solve. Emitting a null "measurement" instead
    # would be the exact failure the guards exist to prevent.
    with pytest.raises(ValueError, match="absent from the solved port list"):
        _reduce([[9e-9]], ["P_pwr"], _topo())


def test_unknown_perturbation_verdict_is_reported_as_unknown_not_as_clean():
    topo = _topo()
    del topo["probe_ports"][0]["pulled_new_copper"]
    p = _reduce([[9e-9, 0.0], [0.0, 2e-9]],
                ["P_pwr", "P_probe_cap_at_d9"], topo)
    assert p["probe_ports"]["cap_at_d9"]["pulled_new_copper"] is None
    assert any("pulled_new_copper" in w for w in p["reduce_warn"])


def test_pulled_new_copper_true_reaches_the_output():
    p = _reduce([[9e-9, 0.0], [0.0, 2e-9]],
                ["P_pwr", "P_probe_cap_at_d9"],
                _topo(pulled=True, retained_nodes_added=412))
    blk = p["probe_ports"]["cap_at_d9"]
    assert blk["pulled_new_copper"] is True
    assert blk["retained_nodes_added"] == 412


def test_is_probe_label():
    assert probe_ports.is_probe_label("P_probe_x")
    assert not probe_ports.is_probe_label("P_pwr")
    assert not probe_ports.is_probe_label(None)
