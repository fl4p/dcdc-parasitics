#!/usr/bin/env python3
"""Focused tests for in-memory FET gate-net overrides."""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, LIB)

# System Python does not ship KiCad's pcbnew module. The override logic only
# needs PCB_TRACK, supplied by the fixture below.
sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
import gate_net_override  # noqa: E402


class _Pos:
    def __init__(self, x_mm, y_mm):
        self.x = int(x_mm * gate_net_override.NM)
        self.y = int(y_mm * gate_net_override.NM)


class _Net:
    def __init__(self, name):
        self.name = name


class _Pad:
    def __init__(self, number, net, x_mm, y_mm):
        self._number = str(number)
        self._net = net
        self._pos = _Pos(x_mm, y_mm)

    def GetNumber(self):
        return self._number

    def GetNetname(self):
        return self._net.name

    def GetPosition(self):
        return self._pos

    def SetNet(self, net):
        self._net = net


class _Footprint:
    def __init__(self, ref, pads):
        self._ref = ref
        self._pads = pads

    def GetReference(self):
        return self._ref

    def Pads(self):
        return list(self._pads)


class _Track:
    def __init__(self, board):
        self.board = board

    def SetStart(self, value):
        self.start = value

    def SetEnd(self, value):
        self.end = value

    def SetLayer(self, value):
        self.layer = value

    def SetNet(self, value):
        self.net = value

    def SetWidth(self, value):
        self.width = value


class _Board:
    def __init__(self, footprints, nets):
        self._footprints = footprints
        self._nets = {net.name: net for net in nets}
        self.added = []

    def GetFootprints(self):
        return list(self._footprints)

    def FindNet(self, name):
        return self._nets.get(name)

    def GetLayerID(self, name):
        assert name == "B.Cu"
        return 31

    def Add(self, item):
        self.added.append(item)


def test_override_connects_to_declared_sibling_gate_not_nearer_resistor(monkeypatch):
    gate = _Net("Net-(Q2-G)")
    gnd = _Net("BuckGND")
    d9_gate = _Pad("1", gnd, 0, 0)
    q2_gate = _Pad("1", gate, 0, 10.4)
    # This is much nearer than Q2 and reproduces BUG-1. Even pad number 1 must
    # not win because R8 is not a declared FET sibling.
    r8_gate = _Pad("1", gate, 0, 1.0)
    board = _Board([
        _Footprint("D9", [d9_gate]),
        _Footprint("Q2", [q2_gate]),
        _Footprint("R8", [r8_gate]),
    ], [gate, gnd])
    monkeypatch.setattr(gate_net_override.pcbnew, "PCB_TRACK", _Track, raising=False)

    realized = gate_net_override.apply(
        board, "D9=Net-(Q2-G)", fet_refs=["Q2", "D9"])

    assert d9_gate.GetNetname() == "Net-(Q2-G)"
    assert len(board.added) == 1
    track = board.added[0]
    assert track.start is d9_gate.GetPosition()
    assert track.end is q2_gate.GetPosition()
    assert track.end is not r8_gate.GetPosition()
    assert track.layer == 31
    assert track.net is gate
    assert track.width == int(0.5 * gate_net_override.NM)
    assert realized == {
        "D9": {
            "net": "Net-(Q2-G)",
            "gate_pad": "1",
            "anchor_mode": "sibling_search",
            "target_ref": "Q2",
            "target_pad": "1",
            "track_layer": "B.Cu",
            "track_width_mm": 0.5,
            "track_length_mm": pytest.approx(10.4),
        }
    }


def test_override_refuses_endpoint_outside_declared_fet_siblings(monkeypatch):
    gate = _Net("GATE")
    gnd = _Net("GND")
    board = _Board([
        _Footprint("D9", [_Pad("1", gnd, 0, 0)]),
        _Footprint("R8", [_Pad("1", gate, 0, 1)]),
    ], [gate, gnd])
    monkeypatch.setattr(gate_net_override.pcbnew, "PCB_TRACK", _Track, raising=False)

    with pytest.raises(SystemExit, match="no pad 1 on declared sibling FET refs"):
        gate_net_override.apply(
            board, "D9=GATE", fet_refs=["D9", "Q2"])
    assert board.added == []


def test_lumped_override_warning_is_explicit(capsys):
    gate_net_override.warn_if_lumped("lumped", "D9=GATE")
    assert "does not produce a per-device L_gate/CSI result" in capsys.readouterr().err

    gate_net_override.warn_if_lumped("per-device", "D9=GATE")
    assert capsys.readouterr().err == ""


def _flu_board():
    """flu as built: the LS FET in the D9 land, gate wired to Q2.1, Q2 NOT populated."""
    gate = _Net("Net-(Q2-G)")
    gnd = _Net("BuckGND")
    other = _Net("Net-(Q1-G)")
    d9_gate = _Pad("1", gnd, 0, 0)
    q2_gate = _Pad("1", gate, 0, 10.4)      # non-populated footprint's gate pad
    q1_gate = _Pad("1", other, 0, 3.0)      # a declared sibling, but on ANOTHER net
    board = _Board([
        _Footprint("D9", [d9_gate]),
        _Footprint("Q2", [q2_gate]),
        _Footprint("Q1", [q1_gate]),
    ], [gate, gnd, other])
    return board, gate, d9_gate, q2_gate


def test_single_d9_without_anchor_is_refused(monkeypatch):
    """The blocker this feature exists to remove, reproduced first.

    With ls_ref [D9] the only declared LS ref is also the overridden one, and the HS siblings
    sit on their own gate nets -- so no declared sibling carries pad 1 on Net-(Q2-G).
    """
    board, _, _, _ = _flu_board()
    monkeypatch.setattr(gate_net_override.pcbnew, "PCB_TRACK", _Track, raising=False)
    with pytest.raises(SystemExit) as e:
        gate_net_override.apply(board, "D9=Net-(Q2-G)", fet_refs=["D9", "Q1"])
    # and the message must point at the way out, or the next person re-derives it
    assert "anchor_ref.anchor_pad" in str(e.value)


def test_explicit_anchor_reaches_a_non_populated_footprint(monkeypatch):
    board, gate, d9_gate, q2_gate = _flu_board()
    monkeypatch.setattr(gate_net_override.pcbnew, "PCB_TRACK", _Track, raising=False)

    realized = gate_net_override.apply(
        board, "D9=Net-(Q2-G)@Q2.1", fet_refs=["D9", "Q1"])

    assert d9_gate.GetNetname() == "Net-(Q2-G)"
    track = board.added[0]
    assert track.end is q2_gate.GetPosition()
    assert track.net is gate
    assert realized["D9"]["target_ref"] == "Q2"
    assert realized["D9"]["anchor_mode"] == "explicit"
    assert realized["D9"]["track_length_mm"] == pytest.approx(10.4)


def test_anchor_on_the_wrong_net_is_refused(monkeypatch):
    """The anchor is the far end of a wire that EXISTS; it is not a graft point."""
    board, _, _, _ = _flu_board()
    monkeypatch.setattr(gate_net_override.pcbnew, "PCB_TRACK", _Track, raising=False)
    with pytest.raises(SystemExit) as e:
        gate_net_override.apply(board, "D9=Net-(Q2-G)@Q1.1", fet_refs=["D9", "Q1"])
    assert "not the override net" in str(e.value)


def test_anchor_pointing_at_a_missing_footprint_or_pad_is_refused(monkeypatch):
    board, _, _, _ = _flu_board()
    monkeypatch.setattr(gate_net_override.pcbnew, "PCB_TRACK", _Track, raising=False)
    with pytest.raises(SystemExit) as e:
        gate_net_override.apply(board, "D9=Net-(Q2-G)@Q77.1", fet_refs=["D9", "Q1"])
    assert "not found" in str(e.value)
    with pytest.raises(SystemExit) as e:
        gate_net_override.apply(board, "D9=Net-(Q2-G)@Q2.9", fet_refs=["D9", "Q1"])
    assert "has no pad" in str(e.value)


def test_malformed_anchor_is_refused():
    for bad in ("D9=Net-(Q2-G)@", "D9=Net-(Q2-G)@Q2", "D9=Net-(Q2-G)@Q2.1.2", "D9=@Q2.1"):
        with pytest.raises(SystemExit):
            gate_net_override._parse(bad)


def test_anchor_may_not_chain_onto_another_overridden_ref(monkeypatch):
    """Two synthetic wires in series would make the modelled length order-dependent."""
    board, _, _, _ = _flu_board()
    monkeypatch.setattr(gate_net_override.pcbnew, "PCB_TRACK", _Track, raising=False)
    with pytest.raises(SystemExit) as e:
        gate_net_override.apply(
            board, "D9=Net-(Q2-G)@Q2.1,Q2=Net-(Q2-G)", fet_refs=["D9", "Q1", "Q2"])
    assert "overridden ref" in str(e.value)
