#!/usr/bin/env python3
"""Unit tests for the DC resistive loss-density solver (density.py).

Pure asserts, repo style. Runs on hand-written `.inp` meshes with known-analytic
answers — no KiCad / FastHenry / SPICE needed (this is exactly why density.py is
loss-agnostic and takes currents as parameters).

    python3 test_density.py     (main or fetlib venv; needs numpy + scipy)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import density  # noqa: E402
from mesh_geom import parse_mesh  # noqa: E402

SIGMA = 5.8e4          # S/mm


def _write(text):
    fd, path = tempfile.mkstemp(suffix=".inp")
    os.write(fd, text.encode())
    os.close(fd)
    return path


def _bar(w=2.0, h=0.035, length=10.0):
    return f""".units mm
.default sigma={SIGMA:g} nwinc=1 nhinc=1 z=0
N0 x=0 y=0 z=0
N1 x={length:g} y=0 z=0
E1 N0 N1 w={w:g} h={h:g}
.external N0 N1
.freq fmin=1e5 fmax=1e8 ndec=3
.end
"""


def _net(text):
    return density.Network(parse_mesh(_write(text)), SIGMA)


def test_single_bar_ohms_and_power():
    w, h, L, i = 2.0, 0.035, 10.0, 10.0
    R = L / (SIGMA * w * h)
    net = _net(_bar(w, h, L))
    a, b = net.node_row("N0"), net.node_row("N1")
    v = net.solve({a: +i, b: -i})
    P = net.seg_power(v)
    assert abs(P.sum() - i * i * R) < 1e-9, (P.sum(), i * i * R)
    # effective R from V/I
    Reff = (v[a] - v[b]) / i
    assert abs(Reff - R) / R < 1e-9, (Reff, R)
    # uniform density = P / (w*L)
    dens = P[0] / (w * L)
    assert abs(dens - i * i * R / (w * L)) < 1e-12
    print(f"OK single bar: R={R*1e3:.4f} mΩ, P={P.sum():.5f} W, dens={dens:.5f} W/mm²")


def test_two_parallel_bars_split_equally():
    # two identical bars N0->N1 (w=1 each) == one w=2 bar in parallel
    w, h, L, i = 1.0, 0.035, 10.0, 10.0
    text = f""".units mm
.default sigma={SIGMA:g} nwinc=1 nhinc=1 z=0
N0 x=0 y=0 z=0
N1 x={L:g} y=0 z=0
N2 x=0 y=5 z=0
N3 x={L:g} y=5 z=0
E1 N0 N1 w={w:g} h={h:g}
E2 N2 N3 w={w:g} h={h:g}
.equiv N0 N2
.equiv N1 N3
.external N0 N1
.freq fmin=1e5 fmax=1e8 ndec=3
.end
"""
    net = _net(text)
    a, b = net.node_row("N0"), net.node_row("N1")
    v = net.solve({a: +i, b: -i})
    P = net.seg_power(v)
    Rbar = L / (SIGMA * w * h)
    Rpar = Rbar / 2.0
    assert abs(P.sum() - i * i * Rpar) < 1e-9, (P.sum(), i * i * Rpar)
    # equal split -> the two segments dissipate the same
    assert abs(P[0] - P[1]) < 1e-12, P
    print(f"OK parallel bars: total {P.sum():.5f} W, split {P[0]:.5f}/{P[1]:.5f}")


def test_equiv_short_is_zero_loss():
    # bar with an extra node shorted to N1 via .equiv, plus a zero-length E across it
    w, h, L, i = 2.0, 0.035, 10.0, 10.0
    text = f""".units mm
.default sigma={SIGMA:g} nwinc=1 nhinc=1 z=0
N0 x=0 y=0 z=0
N1 x={L:g} y=0 z=0
N2 x={L:g} y=0 z=0
E1 N0 N1 w={w:g} h={h:g}
E2 N1 N2 w={w:g} h={h:g}
.equiv N1 N2
.external N0 N2
.freq fmin=1e5 fmax=1e8 ndec=3
.end
"""
    net = _net(text)
    # E2 spans two nodes merged by .equiv -> dropped, only E1 carries loss
    assert len(net.segs) == 1, [s[:3] for s in net.segs]
    a, b = net.node_row("N0"), net.node_row("N2")
    v = net.solve({a: +i, b: -i})
    P = net.seg_power(v)
    R = L / (SIGMA * w * h)
    assert abs(P.sum() - i * i * R) < 1e-9, (P.sum(), i * i * R)
    print(f"OK equiv short: {len(net.segs)} live seg, P={P.sum():.5f} W")


def test_normalization_to_bucket():
    # norm_W rescales a phase so its Σ equals the reference bucket
    text = _bar()
    ports = _write('{"ports":["P_pwr"],"cin_ports":["P_pwr"],"cin_used":["C1"]}')
    inp = _write(text)
    spec = {"phases": [{"name": "bar", "port": "P_pwr", "i_rms": 10.0, "norm_W": 0.5}]}
    mesh = parse_mesh(inp)
    _net_, total, _sigma, rows = density.compute(mesh, spec, ports)
    assert abs(total.sum() - 0.5) < 1e-9, total.sum()
    assert rows[0]["raw_W"] > 0 and abs(rows[0]["W"] - 0.5) < 1e-9
    print(f"OK normalization: raw {rows[0]['raw_W']:.5f} -> {rows[0]['W']:.5f} W")


def test_layer_bucketing():
    # a top (z=0) and bottom (z=-1.6) planar segment + a via between them
    w, h = 2.0, 0.035
    text = f""".units mm
.default sigma={SIGMA:g} nwinc=1 nhinc=1 z=0
N0 x=0 y=0 z=0
N1 x=5 y=0 z=0
N2 x=5 y=0 z=-1.6
N3 x=0 y=0 z=-1.6
E1 N0 N1 w={w:g} h={h:g}
E2 N1 N2 w={w:g} h={h:g}
E3 N2 N3 w={w:g} h={h:g}
.external N0 N3
.freq fmin=1e5 fmax=1e8 ndec=3
.end
"""
    net = _net(text)
    layers = {s[7] for s in net.segs}
    assert "F.Cu" in layers and "B.Cu" in layers and "vias" in layers, layers
    print(f"OK layer bucketing: {sorted(layers)}")


ALL = [test_single_bar_ohms_and_power, test_two_parallel_bars_split_equally,
       test_equiv_short_is_zero_loss, test_normalization_to_bucket,
       test_layer_bucketing]

if __name__ == "__main__":
    for t in ALL:
        t()
    print(f"\nall {len(ALL)} density tests passed")
