#!/usr/bin/env python3
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from experiments.palace_fixture_study import (  # noqa: E402
    _conductors,
    _finite_reference,
    _fixture,
    _near_mesh_size,
    _outer_bounds,
)


def test_palace_reuses_the_frozen_parallel_plate_geometry():
    fixture = _fixture("parallel_plate_air")
    conductors = _conductors(fixture)
    assert tuple(item.name for item in conductors) == ("terminal_1", "terminal_2")
    assert conductors[0].z_min == pytest.approx(-17.5e-6)
    assert conductors[0].z_max == pytest.approx(17.5e-6)
    assert conductors[1].z_min == pytest.approx(117.5e-6)
    assert conductors[1].z_max == pytest.approx(152.5e-6)
    assert _near_mesh_size(fixture, 1.0) == pytest.approx(100e-6 / 3.0)


def test_finite_reference_expansion_is_centered_and_contains_the_fixture():
    fixture = _fixture("parallel_plate_enclosed_fr4")
    conductors = _conductors(fixture)
    outer = _outer_bounds(fixture, conductors, 3.0)
    assert outer.minimum == pytest.approx((-21e-3, -21e-3, -20.5e-3))
    assert outer.maximum == pytest.approx((21e-3, 21e-3, 21.5e-3))
    for conductor in conductors:
        assert outer.minimum[2] < conductor.z_min < conductor.z_max < outer.maximum[2]


def test_finite_reference_uses_json_native_bounds_in_memory():
    fixture = _fixture("parallel_plate_air")
    outer = _outer_bounds(fixture, _conductors(fixture), 6.0)
    reference = _finite_reference(outer, 6.0)
    assert reference["outer_bounds_m"] == {
        "minimum": list(outer.minimum),
        "maximum": list(outer.maximum),
    }
    assert all(
        isinstance(value, list)
        for value in reference["outer_bounds_m"].values()
    )


def test_unknown_fixture_is_rejected():
    with pytest.raises(ValueError, match="unknown fixture"):
        _fixture("not-a-fixture")
