#!/usr/bin/env python3
"""Per-port endpoint connectivity, for every extraction path (discrete FETs too).

The defect: Model.drop_floating_ports built its reference conductor from BOTH
terminals of the seed port. When P_pwr itself spanned two disconnected conductors
-- wrong --gnd net, a cap pad that never bonded, a closure to the wrong terminal --
both conductors counted as "the loop", so P_pwr and every other port across the
same gap survived. FastHenry then failed with "err 5" (one such port) or returned
a finite ~4e16 nH matrix (two). Only the module path refused this (its check 1).

The invariant is per port: each port's two terminals on ONE conductor. Disjoint
conductors are otherwise legitimate (declared extra_nets islands).
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
import kicad_geom  # noqa: E402


def _split_loop():
    """VIN copper and GND copper that never meet: the P_pwr port spans the gap."""
    m = kicad_geom.Model()
    v0, v1 = m.node("VIN", 0, 0.0, 0.0, 0.0), m.node("VIN", 0, 5.0, 0.0, 0.0)
    g0, g1 = m.node("GND", 0, 0.0, 1.0, 0.0), m.node("GND", 0, 5.0, 1.0, 0.0)
    m.seg(v0, v1, 0.5)
    m.seg(g0, g1, 0.5)
    m.port("P_pwr", v0, g0)
    return m, (v0, v1), (g0, g1)


def test_known_bad_seed_across_a_gap_is_refused():
    m, _, _ = _split_loop()
    assert m.drop_floating_ports("P_pwr") == []        # seed kept, for the validator
    with pytest.raises(ValueError, match=r"P_pwr span copper that is NOT connected"):
        kicad_geom.validate_port_connectivity(m)


def test_known_bad_second_port_across_the_same_gap():
    """The two-port case that solved to a finite ~4e16 nH: both ports are named."""
    m, (v0, v1), (g0, g1) = _split_loop()
    m.port("P_hs", v1, g1)
    assert m.drop_floating_ports("P_pwr") == []
    with pytest.raises(ValueError, match="P_pwr, P_hs span copper that is NOT connected"):
        kicad_geom.validate_port_connectivity(m)


@pytest.mark.parametrize("flip", [False, True])
def test_an_open_seed_drops_nothing_whichever_way_round(flip):
    """With an open P_pwr, seeding from one terminal dropped the healthy gate ports
    on the other conductor, so the path validator reported "missing gate ports"
    instead of the open loop -- and which ports went depended on terminal order
    (review of a900891). Nothing is dropped now; the open seed is what is named."""
    m = kicad_geom.Model()
    a0, a1 = m.node("VIN", 0, 0.0, 0.0, 0.0), m.node("VIN", 0, 1.0, 0.0, 0.0)
    m.seg(a0, a1, 0.5)                                    # small island
    b = [m.node("GND", 0, 10.0 + i, 0.0, 0.0) for i in range(5)]
    for x, y in zip(b, b[1:]):
        m.seg(x, y, 0.5)                                  # main conductor
    m.port("P_pwr", *((b[0], a0) if flip else (a0, b[0])))
    m.port("P_ghs", b[1], b[2])
    m.port("P_gls", b[3], b[4])
    assert m.drop_floating_ports("P_pwr") == []
    assert [p[0] for p in m.ports] == ["P_pwr", "P_ghs", "P_gls"]
    with pytest.raises(ValueError, match=r"port\(s\) P_pwr span"):
        kicad_geom.validate_port_connectivity(m)


def test_connected_loop_passes_and_keeps_every_port():
    m, (v0, v1), (g0, g1) = _split_loop()
    m.seg(v1, g1, 0.5)                                   # close the loop
    m.port("P_hs", v1, g1)
    assert m.drop_floating_ports("P_pwr") == []
    kicad_geom.validate_port_connectivity(m)             # must not raise


def test_a_floating_cap_is_still_only_dropped():
    """The existing behaviour for a distant cap whose pad never bonded."""
    m, (v0, v1), (g0, g1) = _split_loop()
    m.seg(v1, g1, 0.5)
    x, y = m.node("VIN", 0, 50.0, 0.0, 0.0), m.node("GND", 0, 50.0, 1.0, 0.0)
    m.port("P_cin_C99", x, y)
    assert m.drop_floating_ports("P_pwr") == ["P_cin_C99"]
    kicad_geom.validate_port_connectivity(m)


def test_a_declared_island_port_is_legitimate():
    m, (v0, v1), (g0, g1) = _split_loop()
    m.seg(v1, g1, 0.5)
    i0, i1 = m.node("BflowS", 0, 50.0, 50.0, 0.0), m.node("BflowS", 0, 60.0, 50.0, 0.0)
    m.seg(i0, i1, 0.5)
    m.port("P_probe_out", i0, i1)
    assert m.drop_floating_ports("P_pwr", island_nets={"BflowS"}) == []
    kicad_geom.validate_port_connectivity(m)


def test_an_equiv_counts_as_connection():
    m, (v0, v1), (g0, g1) = _split_loop()
    m.equiv(v1, g1)                                      # e.g. a FET die closure
    kicad_geom.validate_port_connectivity(m)
