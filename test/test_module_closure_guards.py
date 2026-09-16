"""Known-bad calibration for `kicad_geom.validate_module_ports` (guard 7).

Each test CONSTRUCTS the failure the guard exists to catch and asserts the guard
fires, plus one positive control so the suite cannot pass by refusing everything
(the monotonicity trap: a guard that always raises is not a guard).

`Model` is pure python -- node interning, segs, equivs, ports -- so the decks
below are built directly rather than through pcbnew, exactly as
test_parasitics.py does. The two mutations that need the REAL board (deleting the
closure, and wiring it to the wrong terminal) were run against
examples/buck-tpsm33610-module.yaml on 2026-09-16 and both produced the
"P_pwr terminals are NOT connected" error; see the commit message.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
import kicad_geom  # noqa: E402


def _topo(vin_node, gnd_node, ref="U1"):
    return dict(kind="module",
                module=dict(ref=ref, _vin_node=vin_node, _gnd_node=gnd_node))


def _deck(closed=True, cin_on_module_pads=False):
    """Cin -> VIN copper -> [module VIN pad] =closure= [module GND pad] -> GND
    copper -> Cin. Two straight runs of copper and one ideal link, i.e. the
    smallest thing shaped like the real extraction."""
    m = kicad_geom.Model()
    cin_v = m.node("VIN", 0, 0.0, 0.0, 0.0)
    pad_v = m.node("VIN", 0, 2.0, 0.0, 0.0)
    pad_g = m.node("GND", 0, 2.0, 1.0, 0.0)
    cin_g = m.node("GND", 0, 0.0, 1.0, 0.0)
    m.seg(cin_v, pad_v, 0.5)
    m.seg(pad_g, cin_g, 0.5)
    if closed:
        m.equiv(pad_v, pad_g)
    if cin_on_module_pads:
        m.port("P_pwr", pad_v, pad_g)      # the port IS the closure
    else:
        m.port("P_pwr", cin_v, cin_g)
    return m, pad_v, pad_g


def test_positive_control_a_well_formed_module_deck_passes():
    m, pv, pg = _deck()
    kicad_geom.validate_module_ports(m, _topo(pv, pg))     # must not raise


def test_missing_p_pwr():
    m, pv, pg = _deck()
    m.ports = []
    with pytest.raises(ValueError, match="P_pwr input-cap commutation port missing"):
        kicad_geom.validate_module_ports(m, _topo(pv, pg))


def test_no_closure_means_no_loop():
    """The target failure: without the pad-plane closure FastHenry reports
    'Number of meshes: 0' and exits 1. The guard must say so first."""
    m, pv, pg = _deck(closed=False)
    with pytest.raises(ValueError, match="NOT connected"):
        kicad_geom.validate_module_ports(m, _topo(pv, pg))


def test_port_spanning_ideal_links_only():
    """A deck where P_pwr sits directly across the closure would solve to ~0 nH
    and ~0 mOhm -- a number, in the right units, that measured no copper."""
    m, pv, pg = _deck(cin_on_module_pads=True)
    with pytest.raises(ValueError, match="IDEAL LINKS ONLY"):
        kicad_geom.validate_module_ports(m, _topo(pv, pg))


def test_loop_must_pass_through_the_module():
    """The closure exists and a loop exists, but the loop does not go through the
    declared module pads: here the Cin pads are joined by their own copper and the
    module sits on a stub. The reported number would be some other loop."""
    m = kicad_geom.Model()
    cin_v = m.node("VIN", 0, 0.0, 0.0, 0.0)
    cin_g = m.node("GND", 0, 0.0, 1.0, 0.0)
    m.seg(cin_v, cin_g, 0.5)                     # a loop that avoids the module
    pad_v = m.node("VIN", 0, 9.0, 0.0, 0.0)      # module pads, reachable ONLY
    pad_g = m.node("GND", 0, 9.0, 1.0, 0.0)      # through the closure
    m.equiv(pad_v, pad_g)
    m.port("P_pwr", cin_v, cin_g)
    with pytest.raises(ValueError, match="did not pass through the module"):
        kicad_geom.validate_module_ports(m, _topo(pad_v, pad_g))


def test_closure_nodes_must_have_been_recorded():
    m, pv, pg = _deck()
    with pytest.raises(ValueError, match="build_module did not run"):
        kicad_geom.validate_module_ports(m, _topo(None, None))
