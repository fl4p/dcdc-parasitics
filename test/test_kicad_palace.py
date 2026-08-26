#!/usr/bin/env python3
import json
import collections
import itertools
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest
from shapely import union_all
from shapely.geometry import LineString, Polygon
from shapely.strtree import STRtree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
pytest.importorskip("gmsh")

import kicad_fastercap  # noqa: E402
from kicad_fastercap import StackupLayer  # noqa: E402
from kicad_palace import (  # noqa: E402
    load_pcb_volume_dump,
    load_pcb_volumes,
    pcb_volume_source_identity,
    volumes_from_pcb_dump,
)
from kicad_palace_dump import (  # noqa: E402
    _all_copper_groups,
    _drill_record,
    _mapping,
)
from kicad_palace_schema import (  # noqa: E402
    MIN_OUTLINE_FILL_FRACTION,
    check_board_outline_fill as _check_board_outline_fill,
    outline_bounding_area_mm2,
)
from palace_mesh import (  # noqa: E402
    generate_palace_mesh,
    validate_palace_mesh_content,
    validate_palace_mesh_manifest,
)
import palace_plc_mesh  # noqa: E402
from palace_plc_mesh import (  # noqa: E402
    _nesting_parameters_valid,
    MAX_EDGE_AREA_OVERSHOOT,
    _refine_levels,
    _subdivision_count,
    _validate_conductor_edge_refinement,
    generate_palace_plc_mesh,
    validate_palace_plc_mesh_manifest,
)
from provenance import canonical_sha256, file_sha256  # noqa: E402


def _stackup(*, core_epsilon: float | None = 4.2):
    return (
        StackupLayer("F.Cu", "copper", 0.035, None, 0.0, -0.035),
        StackupLayer(
            "dielectric 1", "core", 0.93, core_epsilon, -0.035, -0.965
        ),
        StackupLayer("B.Cu", "copper", 0.035, None, -0.965, -1.0),
    )


def _polygon(group, layer, x_min, x_max, *, source_uuid):
    return {
        "group": group,
        "net": group,
        "layer": layer,
        "source_kind": "pad",
        "source_uuid": source_uuid,
        "shell": [[x_min, -0.2], [x_max, -0.2],
                  [x_max, 0.2], [x_min, 0.2]],
        "holes": [],
    }


def _dump():
    return {
        "format": "dcdc-kicad-palace-volumes-v1",
        "groups": ["A", "B"],
        "grouping_policy": "explicit",
        "copper_layers": ["F.Cu", "B.Cu"],
        "records": [
            _polygon("A", "F.Cu", -0.8, -0.4, source_uuid="a-top"),
            _polygon("A", "B.Cu", -0.8, -0.4, source_uuid="a-bottom"),
            _polygon("B", "F.Cu", 0.4, 0.8, source_uuid="b-top"),
        ],
        "drills": [{
            "group": "A",
            "net": "A",
            "source_kind": "pad",
            "source_uuid": "a-hole",
            "center_mm": [-0.6, 0.0],
            "size_mm": [0.2, 0.2],
            "shape": "circle",
            "start_layer": "F.Cu",
            "end_layer": "B.Cu",
            "plated": True,
            "unsupported_features": [],
        }],
        "board_outlines": [{
            "shell": [[-1.0, -0.5], [1.0, -0.5], [1.0, 0.5], [-1.0, 0.5]],
            "holes": [],
        }],
        "census": {
            "included": {"track": 0, "via": 0, "pad": 3, "zone": 0},
            "unassigned": [],
            "unsupported": [],
        },
        "polygon_error_mm": 0.001,
        "kicad_version": "test",
        "python_executable": "/test/kicad/python",
        "source_pcb_path": "/test/board.kicad_pcb",
        "source_pcb_sha256": "0" * 64,
    }


def test_complete_dump_builds_layer_copper_barrel_and_dielectric_volumes():
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6
    )
    assert geometry.groups == ("A", "B")
    assert {item.name for item in geometry.conductors} == {"A", "B"}
    assert len([item for item in geometry.conductors if item.name == "A"]) == 3
    barrel = next(
        item for item in geometry.conductors
        if item.name == "A" and item.z_max - item.z_min > 0.5e-3
    )
    assert barrel.z_min == pytest.approx(-0.965e-3)
    assert barrel.z_max == pytest.approx(-0.035e-3)
    assert len(geometry.dielectrics) == 1
    assert geometry.dielectrics[0].name == "dielectric 1"
    assert len(geometry.dielectrics[0].rings) == 2
    assert geometry.outer_bounds.minimum[0] < -1e-3
    assert geometry.outer_bounds.maximum[0] > 1e-3


def _plated_via_plc_mesh(tmp_path):
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6
    )
    return generate_palace_plc_mesh(
        tmp_path / "fixture.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
    )


def test_plated_via_volumes_generate_exact_conformal_plc_mesh(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    stored = validate_palace_plc_mesh_manifest(result.manifest_path)
    dispatched = validate_palace_mesh_manifest(result.manifest_path)
    assert stored == dispatched
    assert result.terminal_attributes == (("A", 101), ("B", 102))
    assert result.material_attributes == (
        ("outer", 1, 1.0), ("dielectric 1", 2, 4.2)
    )
    assert result.node_count > 0
    assert result.tetrahedron_count > 0


def test_refined_plc_preserves_source_segments_and_planar_area(tmp_path):
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6
    )
    max_area = 1e-8
    result = generate_palace_plc_mesh(
        tmp_path / "refined.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
        max_planar_area_m2=max_area,
        max_vertical_step_m=2e-4,
    )
    stored = validate_palace_plc_mesh_manifest(result.manifest_path)
    parameters = stored["provenance"]["mesh_parameters"]
    assert parameters["max_planar_area_m2"] == max_area
    assert parameters["allow_volume_steiner"] is False
    assert parameters["source_segment_max_length_m"] == math.sqrt(2 * max_area)
    assert result.node_count > _plated_via_plc_mesh(tmp_path / "coarse").node_count


def test_refine_levels_without_band_matches_uniform_subdivision():
    levels = (-0.045, -1e-3, 0.0, 0.045)
    assert _refine_levels(levels, None) == levels
    assert _refine_levels(levels, None, None) == levels
    uniform = _refine_levels(levels, 5e-3)
    assert _refine_levels(levels, 5e-3, None) == uniform
    for low, high in zip(uniform, uniform[1:]):
        assert high - low <= 5e-3 + 1e-15


def test_refine_levels_band_refines_stackup_and_leaves_air_gaps():
    # Air below, 1 mm stackup, air above -- the band covers only the stackup.
    levels = (-0.045, -1e-3, 0.0, 0.045)
    refined = _refine_levels(levels, 2.5e-4, (-1e-3, 0.0))
    # Air gap endpoints survive untouched, with nothing inserted inside them.
    assert refined[0] == -0.045
    assert refined[-1] == 0.045
    assert not [z for z in refined if -0.045 < z < -1e-3]
    assert not [z for z in refined if 0.0 < z < 0.045]
    # The banded stackup gap is subdivided to the requested step.
    stackup = [z for z in refined if -1e-3 <= z <= 0.0]
    assert len(stackup) == 5
    for low, high in zip(stackup, stackup[1:]):
        assert high - low <= 2.5e-4 + 1e-15
    assert set(levels).issubset(set(refined))
    # Same step without a band spends its budget on the 90 mm of air instead.
    assert len(refined) < len(_refine_levels(levels, 2.5e-4))


# The canary's real stackup, so the nesting tests below are about the geometry
# the project actually meshes rather than a convenient one.
CANARY_LEVELS = (-0.04585, -0.0016, -0.00159, -0.0015725, -0.001555,
                 -0.000045, -0.00001, 0.0, 0.04425)
CANARY_BAND = (-0.0016, 0.0)


def test_equal_division_does_not_nest_between_rungs():
    """Why bisection is needed at all. ceil(gap / step) is the fewest pieces
    that satisfy the step, but halving the step then reshuffles the interior
    levels instead of adding to them, and the coarse set stops being a subset.
    Fifteen of twenty-five is what two real canary meshes showed."""
    coarse = _refine_levels(CANARY_LEVELS, 1e-4, CANARY_BAND)
    fine = _refine_levels(CANARY_LEVELS, 5e-5, CANARY_BAND)
    lost = set(coarse) - set(fine)
    assert len(coarse) == 24 and len(fine) == 39
    assert len(lost) == 15


@pytest.mark.parametrize("step", [4e-4, 2e-4, 1e-4, 5e-5, 2.5e-5, 1.25e-5])
def test_bisected_levels_nest_exactly_when_the_step_halves(step):
    """Rounding the piece count up to a power of two makes halving the step
    double it, and equal division into 2n pieces contains every division point
    of n. The coarse levels survive bit-for-bit, not merely to a tolerance."""
    coarse = _refine_levels(CANARY_LEVELS, step, CANARY_BAND, nested=True)
    fine = _refine_levels(CANARY_LEVELS, step / 2.0, CANARY_BAND, nested=True)
    assert set(coarse) <= set(fine), sorted(set(coarse) - set(fine))


def test_bisected_levels_still_satisfy_the_step():
    refined = _refine_levels(CANARY_LEVELS, 1e-4, CANARY_BAND, nested=True)
    inside = [z for z in refined if CANARY_BAND[0] <= z <= CANARY_BAND[1]]
    gaps = [b - a for a, b in zip(inside, inside[1:])]
    assert gaps and max(gaps) <= 1e-4 * (1.0 + 1e-12)


def test_nesting_costs_at_most_a_doubling_of_pieces():
    """The price of nesting is bounded: a power-of-two count is never more than
    twice the minimum, so the level set cannot blow up."""
    for step in (4e-4, 2e-4, 1e-4, 5e-5, 2.5e-5):
        plain = _refine_levels(CANARY_LEVELS, step, CANARY_BAND)
        nested = _refine_levels(CANARY_LEVELS, step, CANARY_BAND, nested=True)
        assert len(nested) <= 2 * len(plain)


@pytest.mark.parametrize("gap,step,expected", [
    (1.0, 1.0, 1), (1.0, 0.6, 2), (1.0, 0.5, 2), (1.0, 0.4, 4),
    (1.0, 0.26, 4), (1.0, 0.25, 4), (1.0, 0.24, 8), (1.0, 0.125, 8),
])
def test_subdivision_count_rounds_up_to_a_power_of_two_when_nested(
        gap, step, expected):
    assert _subdivision_count(gap, step, True) == expected
    assert _subdivision_count(gap, step, False) == math.ceil(gap / step)


def test_subdivision_count_never_returns_zero_pieces():
    """A gap smaller than the step still needs one piece; zero would drop the
    gap entirely."""
    assert _subdivision_count(1e-9, 1.0, True) == 1
    assert _subdivision_count(1e-9, 1.0, False) == 1
    assert _subdivision_count(0.0, 1.0, True) == 1


def test_nested_refinement_is_off_by_default():
    """Existing meshes must be reproduced bit-for-bit, so the flag defaults to
    the old behaviour and both spellings agree."""
    assert (_refine_levels(CANARY_LEVELS, 5e-5, CANARY_BAND)
            == _refine_levels(CANARY_LEVELS, 5e-5, CANARY_BAND, nested=False))


def test_refine_levels_band_edge_tolerates_float_error_in_the_stackup_level():
    """Known-bad calibration for the band's edge comparison.

    The band is given as a round number; the level it is meant to coincide with
    comes out of the stackup arithmetic. On the canary those differ by 2.2e-19 m
    -- -1.6 mm evaluates to -0.0015999999999999999, just *above* a band edge of
    -0.0016 -- and an exact `<=` therefore refused to skip the 44 mm air gap
    below the board. Nothing raised: the mesh was simply 20x larger, which is
    how it survived a full session. Assert the resulting level count, not merely
    that the call succeeded.
    """
    # The measured canary value, one ulp above the band edge it should equal.
    board_bottom = math.nextafter(-0.0016, math.inf)
    assert board_bottom == -0.0015999999999999999
    assert board_bottom - (-0.0016) == pytest.approx(2.168e-19, rel=1e-3)
    levels = (-0.04585, board_bottom, -0.0, 0.04425)

    refined = _refine_levels(levels, 2e-5, (-0.0016, 0.0))

    # Neither air gap is tiled: they contribute their endpoints and nothing more.
    assert not [z for z in refined if -0.04585 < z < board_bottom]
    assert not [z for z in refined if -0.0 < z < 0.04425]
    # 1.6 mm at a 20 um step, plus the two outer box levels.
    assert len(refined) == 83
    # Before the fix the gap below the board added ceil(44.25 mm / 20 um) = 2213
    # levels, which is the whole 20x. Measured on b20u: 2297 levels, 2214 of
    # them in air.
    assert len(_refine_levels(levels, 2e-5)) > 2200
    assert len(refined) < len(_refine_levels(levels, 2e-5)) / 20


@pytest.mark.parametrize("shift", [0, 1, 2, -1, -2])
def test_refine_levels_band_skips_air_within_an_ulp_either_side(shift):
    """The skip must not depend on which side of the band edge the level lands."""
    edge = -0.0016
    board_bottom = edge
    for _ in range(abs(shift)):
        board_bottom = math.nextafter(
            board_bottom, math.inf if shift > 0 else -math.inf
        )
    levels = (-0.04585, board_bottom, -0.0, 0.04425)
    refined = _refine_levels(levels, 2e-5, (edge, 0.0))
    assert not [z for z in refined if -0.04585 < z < board_bottom]
    assert not [z for z in refined if -0.0 < z < 0.04425]
    assert set(levels).issubset(set(refined))
    # 83, or 84 when the ulp puts the band gap a hair over 1.6 mm and the step
    # count rounds up. What must not happen is the air gap coming back.
    assert len(refined) in (83, 84)


def _rectangle_ring(width_mm, height_mm):
    return (
        (0.0, 0.0), (width_mm, 0.0), (width_mm, height_mm), (0.0, height_mm),
    )


def test_board_outline_fill_accepts_an_enclosed_board():
    # What GetBoardPolygonOutlines returns for the canary: 45 x 40 mm solid.
    outlines = [{"shell": _rectangle_ring(45.0, 40.0), "holes": ()}]
    assert _check_board_outline_fill(outlines, 45.0 * 40.0) == pytest.approx(1.0)


def test_board_outline_fill_rejects_a_stroked_outline():
    """Known-bad calibration: the exact frame the stroking API produced.

    Four 0.1 mm Edge.Cuts gr_lines around a 45 x 40 mm board come back as a
    45.05 x 40.05 mm shell with a 44.95 x 39.95 mm hole -- 8.5 mm2 of material
    over a 1804 mm2 board. It is a valid, non-empty, correctly-wound polygon, so
    this fill check is the only thing that can tell it apart from a real board.
    """
    outlines = [{
        "shell": _rectangle_ring(45.05, 40.05),
        "holes": (_rectangle_ring(44.95, 39.95),),
    }]
    area = 45.05 * 40.05 - 44.95 * 39.95
    assert area == pytest.approx(8.5, abs=1e-9)
    with pytest.raises(ValueError, match="stroked rather than enclosed"):
        _check_board_outline_fill(outlines, 45.05 * 40.05)


def test_board_outline_fill_verdict_is_monotone_in_the_fill_fraction():
    """As the board gets thinner the verdict must never flip back to accepted."""
    bounding = 45.0 * 40.0
    accepted_below = None
    for filled in (bounding, bounding * 0.5, bounding * 0.2, bounding * 0.10001,
                   bounding * MIN_OUTLINE_FILL_FRACTION * 0.999,
                   bounding * 0.01, bounding * 0.0047, bounding * 1e-6):
        side = math.sqrt(filled)
        outlines = [{"shell": _rectangle_ring(side, side), "holes": ()}]
        try:
            _check_board_outline_fill(outlines, bounding)
        except ValueError:
            accepted_below = accepted_below if accepted_below is not None else filled
        else:
            assert accepted_below is None, (
                "a thinner outline was rejected but this thicker one passed"
            )


@pytest.mark.parametrize("outlines, bounding", [
    ([], 1800.0),                                            # nothing to judge
    ([{"shell": _rectangle_ring(45.0, 40.0), "holes": ()}], 0.0),
    ([{"shell": _rectangle_ring(45.0, 40.0), "holes": ()}], -1.0),
    ([{"shell": _rectangle_ring(45.0, 40.0), "holes": ()}], float("nan")),
    ([{"shell": _rectangle_ring(45.0, 40.0), "holes": ()}], float("inf")),
    ([{"shell": _rectangle_ring(45.0, 40.0), "holes": ()}], True),
    # A shell exactly cancelled by its hole encloses nothing at all.
    ([{"shell": _rectangle_ring(45.0, 40.0),
       "holes": (_rectangle_ring(45.0, 40.0),)}], 1800.0),
])
def test_board_outline_fill_refuses_to_pass_unevaluable_input(outlines, bounding):
    with pytest.raises(ValueError):
        _check_board_outline_fill(outlines, bounding)


def test_outline_bounding_area_matches_the_board_extent():
    # A stroked frame's extent is the board's extent to within the stroke width,
    # so the ring-derived denominator exposes the defect just as KiCad's own
    # Edge.Cuts bounding box does.
    frame = [{
        "shell": _rectangle_ring(45.05, 40.05),
        "holes": (_rectangle_ring(44.95, 39.95),),
    }]
    assert outline_bounding_area_mm2(frame) == pytest.approx(1804.2525)
    enclosed = [{"shell": _rectangle_ring(45.0, 40.0), "holes": ()}]
    assert outline_bounding_area_mm2(enclosed) == pytest.approx(1800.0)


@pytest.mark.parametrize("outlines", [
    [],
    [{"holes": ()}],                                   # no shell at all
    [{"shell": ((0.0, 0.0), (1.0, 0.0)), "holes": ()}],   # not a ring
    [{"shell": ((0.0, 0.0), (1.0, 0.0), (float("nan"), 1.0)), "holes": ()}],
    [{"shell": ((0.0, 0.0), (1.0, 0.0), (1.0,)), "holes": ()}],
    [{"shell": _rectangle_ring(45.0, 40.0), "holes": ((0.0, 0.0),)}],
])
def test_outline_extent_refuses_unreadable_geometry(outlines):
    # Unreadable must not mean fine: it must be impossible to reach a verdict
    # by handing the check something it cannot measure.
    with pytest.raises(ValueError):
        outline_bounding_area_mm2(outlines)
    with pytest.raises(ValueError):
        _check_board_outline_fill(outlines, 1800.0)


def test_loading_a_stroked_dump_is_refused(tmp_path):
    """A dump written before the fix must not load quietly.

    Fixing only the producer would leave every stroked dump already on disk
    reproducing its old numbers, because the geometry in them is valid -- just
    almost entirely absent.
    """
    dump = _dump()
    dump["board_outlines"] = [{
        "shell": list(_rectangle_ring(45.05, 40.05)),
        "holes": [list(_rectangle_ring(44.95, 39.95))],
    }]
    path = tmp_path / "stroked.json"
    path.write_text(json.dumps(dump))
    with pytest.raises(ValueError, match="stroked rather than enclosed"):
        load_pcb_volume_dump(path)


def test_loading_an_enclosed_dump_is_accepted(tmp_path):
    dump = _dump()
    dump["board_outlines"] = [{
        "shell": list(_rectangle_ring(45.0, 40.0)), "holes": [],
    }]
    path = tmp_path / "enclosed.json"
    path.write_text(json.dumps(dump))
    assert load_pcb_volume_dump(path)["board_outlines"]


@pytest.mark.parametrize("band, area, base", [
    (1e-4, None, 1e-6),            # a band with no cap refines nothing
    (None, 1e-8, 1e-6),            # a cap with no band is a global cap
    (1e-4, 1e-8, None),            # nothing to refine relative to
    (1e-4, 1e-5, 1e-6),            # "refinement" that would coarsen
    (-1.0, 1e-8, 1e-6),
    (0.0, 1e-8, 1e-6),
    (1e-4, 0.0, 1e-6),
    (1e-4, -1e-8, 1e-6),
    (float("nan"), 1e-8, 1e-6),
    (float("inf"), 1e-8, 1e-6),
    (1e-4, float("nan"), 1e-6),
    (True, 1e-8, 1e-6),
    (1e-4, True, 1e-6),
])
def test_conductor_edge_refinement_is_validated_fail_closed(band, area, base):
    with pytest.raises(ValueError):
        _validate_conductor_edge_refinement(band, area, base)


def test_conductor_edge_refinement_accepts_a_complete_spec():
    assert _validate_conductor_edge_refinement(None, None, 1e-6) == (None, None)
    assert _validate_conductor_edge_refinement(2e-4, 1e-8, 1e-6) == (2e-4, 1e-8)
    # equal is allowed: a band that merely matches the global cap is degenerate
    # but not incoherent, and rejecting it would be a surprise.
    assert _validate_conductor_edge_refinement(2e-4, 1e-6, 1e-6) == (2e-4, 1e-6)


def test_conductor_edge_refinement_grades_the_triangulation(tmp_path):
    """Graded refinement must buy edge resolution, not just more elements."""
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    common = dict(
        outer_bounds=geometry.outer_bounds, conductors=geometry.conductors,
        dielectrics=geometry.dielectrics, max_planar_area_m2=1e-6,
        max_vertical_step_m=1e-3,
    )
    coarse = generate_palace_plc_mesh(tmp_path / "coarse.msh", **common)
    graded = generate_palace_plc_mesh(
        tmp_path / "graded.msh", conductor_edge_band_m=3e-4,
        conductor_edge_max_planar_area_m2=2.5e-8, **common)
    assert graded.tetrahedron_count > coarse.tetrahedron_count
    provenance = json.loads(
        (tmp_path / "graded.msh.manifest.json").read_text())["provenance"]
    assert provenance["mesh_parameters"]["conductor_edge_band_m"] == 3e-4
    assert provenance["mesh_parameters"][
        "conductor_edge_max_planar_area_m2"] == 2.5e-8
    # and the unrefined mesh records the absence rather than omitting the key
    plain = json.loads(
        (tmp_path / "coarse.msh.manifest.json").read_text())["provenance"]
    assert plain["mesh_parameters"]["conductor_edge_band_m"] is None
    assert plain["mesh_parameters"]["conductor_edge_max_planar_area_m2"] is None


def test_a_mesh_that_never_applied_its_recorded_refinement_is_rejected(tmp_path):
    """Known-bad calibration for the in-band area guard.

    Recording the parameters is not evidence they were honoured -- a stale or
    hand-edited manifest would otherwise carry a refinement claim over a mesh
    that has none. Build without refinement, then claim it, and the guard must
    fail on the mesh rather than trust the provenance.
    """
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    path = tmp_path / "unrefined.msh"
    generate_palace_plc_mesh(
        path, outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors, dielectrics=geometry.dielectrics,
        max_planar_area_m2=1e-6, max_vertical_step_m=1e-3)
    manifest_path = tmp_path / "unrefined.msh.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["provenance"]["mesh_parameters"]["conductor_edge_band_m"] = 3e-4
    manifest["provenance"]["mesh_parameters"][
        "conductor_edge_max_planar_area_m2"] = 2.5e-8
    manifest["provenance_sha256"] = canonical_sha256(manifest["provenance"])
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    with pytest.raises(ValueError, match="refinement"):
        validate_palace_plc_mesh_manifest(manifest_path, mesh_path=path)


def test_the_in_band_overshoot_ceiling_is_far_above_what_meshing_needs():
    # Measured on the canary: 1 of 27,523 in-band triangles missed the cap, at
    # 2.39x, because it was wedged against a segment Triangle may not split.
    assert MAX_EDGE_AREA_OVERSHOOT >= 2.39 * 2


def test_refine_levels_band_must_not_overhang_the_outermost_stackup_level():
    # A band edge past the outermost stackup level makes the adjoining air gap
    # overlap the band, so that whole gap is refined -- the caller must clamp
    # the band to existing levels to get the intended saving.
    levels = (-0.045, -1e-3, 0.0, 0.045)
    clamped = _refine_levels(levels, 2.5e-4, (-1e-3, 0.0))
    overhanging = _refine_levels(levels, 2.5e-4, (-1.1e-3, 0.0))
    assert not [z for z in clamped if -0.045 < z < -1e-3]
    assert [z for z in overhanging if -0.045 < z < -1e-3]
    assert len(overhanging) > 10 * len(clamped)


@pytest.mark.parametrize("band", [
    (0.0,),
    (0.0, 1e-3, 2e-3),
    (1e-3, 1e-3),
    (1e-3, 0.0),
    (0.0, float("nan")),
    (0.0, float("inf")),
    (0.0, True),
    ("0", "1"),
    0.0,
    {"lo": 0.0, "hi": 1e-3},
])
def test_plc_mesh_rejects_invalid_vertical_refinement_band(tmp_path, band):
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6
    )
    with pytest.raises(ValueError):
        generate_palace_plc_mesh(
            tmp_path / "invalid.msh",
            outer_bounds=geometry.outer_bounds,
            conductors=geometry.conductors,
            dielectrics=geometry.dielectrics,
            max_vertical_step_m=2.5e-4,
            vertical_refinement_band_m=band,
        )


def test_banded_plc_mesh_records_band_and_beats_unbanded_cost(tmp_path):
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6
    )
    band = (-1.0e-3, 0.0)
    common = {
        "outer_bounds": geometry.outer_bounds,
        "conductors": geometry.conductors,
        "dielectrics": geometry.dielectrics,
        "max_vertical_step_m": 2.5e-4,
    }
    banded = generate_palace_plc_mesh(
        tmp_path / "banded.msh", vertical_refinement_band_m=band, **common
    )
    stored = validate_palace_plc_mesh_manifest(banded.manifest_path)
    assert stored["provenance"]["mesh_parameters"][
        "vertical_refinement_band_m"] == list(band)
    unbanded = generate_palace_plc_mesh(tmp_path / "unbanded.msh", **common)
    assert validate_palace_plc_mesh_manifest(unbanded.manifest_path)[
        "provenance"]["mesh_parameters"]["vertical_refinement_band_m"] is None
    # Confining refinement to the stackup is far cheaper than tiling the air.
    assert banded.tetrahedron_count < unbanded.tetrahedron_count


def test_plc_manifest_rejects_rehashed_semantic_and_identity_tampering(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    original = json.loads(result.manifest_path.read_text())
    mutations = (
        (lambda value: value["provenance"]["mesh_parameters"].update(
            max_vertical_step_m=True
        ), "mesh parameters"),
        (lambda value: value["provenance"]["mesh_parameters"].update(
            allow_boundary_steiner=True
        ), "mesh parameters"),
        (lambda value: value["provenance"]["mesh_parameters"].update(
            allow_volume_steiner=True
        ), "mesh parameters"),
        (lambda value: value["provenance"]["mesh_parameters"].update(
            planar_quantum_m=2 * value["provenance"]["mesh_parameters"][
                "planar_quantum_m"
            ]
        ), "mesh parameters"),
        (lambda value: value["provenance"]["mesh_parameters"].update(
            source_segment_max_length_m=1e-3
        ), "mesh parameters"),
        (lambda value: value["provenance"]["terminal_attributes"][0].__setitem__(
            0, "substituted"
        ), "terminal attributes"),
        (lambda value: value["provenance"]["mesher"].update(
            native_extension_sha256="0" * 64
        ), "native_extension hash mismatch"),
        (lambda value: value["provenance"].update(
            minimum_tetrahedron_determinant_m3=True
        ), "Jacobian witness"),
        (lambda value: value["provenance"]["source_identity"].update(
            unbound=True
        ), "source identity has unknown fields"),
    )
    for mutate, message in mutations:
        value = json.loads(json.dumps(original))
        mutate(value)
        value["provenance_sha256"] = canonical_sha256(value["provenance"])
        result.manifest_path.write_text(json.dumps(value))
        with pytest.raises(ValueError, match=message):
            validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_manifest_binds_kicad_dump_pcb_stackup_and_controls(
        tmp_path, monkeypatch):
    dump_path = tmp_path / "dump.json"
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("synthetic pcb")
    dump = _dump()
    dump["source_pcb_path"] = str(pcb_path.resolve())
    dump["source_pcb_sha256"] = file_sha256(pcb_path)
    dump_path.write_text(json.dumps(dump))
    stackup = _stackup()
    monkeypatch.setattr(kicad_fastercap, "parse_stackup", lambda _: stackup)
    geometry = load_pcb_volumes(
        dump_path, stackup, plating_thickness_m=25e-6
    )
    source = pcb_volume_source_identity(
        geometry, dump_path, pcb_path, stackup
    )
    result = generate_palace_plc_mesh(
        tmp_path / "fixture.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
        source_identity=source,
    )
    stored = validate_palace_plc_mesh_manifest(result.manifest_path)
    assert stored["provenance"]["source_identity"]["kind"] == "kicad_volume_dump"
    assert stored["provenance"]["mesh_parameters"]["planar_quantum_m"] == 5e-7
    quantum = stored["provenance"]["mesh_parameters"]["planar_quantum_m"]
    outer = stored["provenance"]["outer_bounds"]
    planar_values = [
        value
        for prism in (
            *stored["provenance"]["conductors"],
            *stored["provenance"]["dielectrics"],
        )
        for ring in prism["rings"]
        for point in ring
        for value in point
    ]
    assert all(
        abs(value / quantum - round(value / quantum)) < 1e-9
        for value in (
            *outer["minimum"][:2], *outer["maximum"][:2], *planar_values
        )
    )
    substituted = json.loads(json.dumps(stored))
    substituted["provenance"]["source_identity"]["geometry_tolerance_mm"] *= 2
    substituted["provenance_sha256"] = canonical_sha256(
        substituted["provenance"]
    )
    result.manifest_path.write_text(json.dumps(substituted))
    with pytest.raises(ValueError, match="source reconstruction"):
        validate_palace_plc_mesh_manifest(result.manifest_path)
    substituted = json.loads(json.dumps(stored))
    substituted["provenance"]["source_identity"]["coordinate_grid_mm"] *= 2
    substituted["provenance"]["mesh_parameters"]["planar_quantum_m"] *= 2
    substituted["provenance_sha256"] = canonical_sha256(
        substituted["provenance"]
    )
    result.manifest_path.write_text(json.dumps(substituted))
    with pytest.raises(ValueError, match="coordinate grid differs"):
        validate_palace_plc_mesh_manifest(result.manifest_path)
    result.manifest_path.write_text(json.dumps(stored))
    pcb_path.write_text("tampered pcb")
    with pytest.raises(ValueError, match="source file identity mismatch"):
        validate_palace_plc_mesh_manifest(result.manifest_path)



def _rebind_plc_mesh(result, lines, mutate_provenance=None):
    result.mesh_path.write_text("\n".join(lines) + "\n")
    stored = json.loads(result.manifest_path.read_text())
    provenance = stored["provenance"]
    provenance["mesh_sha256"] = file_sha256(result.mesh_path)
    if mutate_provenance is not None:
        mutate_provenance(provenance)
    stored["provenance_sha256"] = canonical_sha256(provenance)
    result.manifest_path.write_text(json.dumps(stored))


def _element_line_indices(lines, element_type):
    start = lines.index("$Elements")
    count = int(lines[start + 1])
    return [
        index for index in range(start + 2, start + 2 + count)
        if lines[index].split()[1] == str(element_type)
    ]


def test_plc_validator_rejects_rebound_inverted_tetrahedron(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    index = _element_line_indices(lines, 4)[0]
    fields = lines[index].split()
    fields[-1], fields[-2] = fields[-2], fields[-1]
    lines[index] = " ".join(fields)
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="nonpositive tetrahedron"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_material_misclassification(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    node_start = lines.index("$Nodes")
    node_count = int(lines[node_start + 1])
    coordinates = {
        int(fields[0]): tuple(float(value) for value in fields[1:])
        for fields in (
            lines[index].split()
            for index in range(node_start + 2, node_start + 2 + node_count)
        )
    }
    indices = _element_line_indices(lines, 4)
    index = next(
        index for index in indices
        if lines[index].split()[3] == "2"
        and all(
            -0.9e-3 < coordinates[node][0] < 0.9e-3
            and -0.4e-3 < coordinates[node][1] < 0.4e-3
            for node in map(int, lines[index].split()[-4:])
        )
    )
    fields = lines[index].split()
    fields[3] = fields[4] = "1"
    lines[index] = " ".join(fields)
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="material is misclassified"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_missing_boundary_face(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    start = lines.index("$Elements")
    index = _element_line_indices(lines, 2)[0]
    del lines[index]
    lines[start + 1] = str(int(lines[start + 1]) - 1)
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="boundaries do not close"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_false_jacobian_witness(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    _rebind_plc_mesh(
        result,
        lines,
        lambda provenance: provenance.update(
            minimum_tetrahedron_determinant_m3=(
                2 * provenance["minimum_tetrahedron_determinant_m3"]
            )
        ),
    )
    with pytest.raises(ValueError, match="witness differs from mesh bytes"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_sub_grid_planar_displacement(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    node_start = lines.index("$Nodes")
    node_count = int(lines[node_start + 1])
    index = next(
        index
        for index in range(node_start + 2, node_start + 2 + node_count)
        if abs(float(lines[index].split()[1])) < 0.9e-3
        and abs(float(lines[index].split()[2])) < 0.4e-3
    )
    selected = tuple(float(value) for value in lines[index].split()[1:3])
    changed = 0
    for index in range(node_start + 2, node_start + 2 + node_count):
        fields = lines[index].split()
        if tuple(float(value) for value in fields[1:3]) == selected:
            fields[1] = f"{float(fields[1]) + 1e-12:.17g}"
            lines[index] = " ".join(fields)
            changed += 1
    assert changed > 1
    _rebind_plc_mesh(result, lines)
    with pytest.raises(
        ValueError,
        match="omits a noded source boundary|crosses a noded source boundary",
    ):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_sub_tolerance_z_interface(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    node_start = lines.index("$Nodes")
    node_count = int(lines[node_start + 1])
    changed = 0
    for index in range(node_start + 2, node_start + 2 + node_count):
        fields = lines[index].split()
        if float(fields[3]) == -0.000965:
            fields[3] = f"{float(fields[3]) + 1e-15:.17g}"
            lines[index] = " ".join(fields)
            changed += 1
    assert changed > 0
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="unknown z-plane"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_partial_z_interface_warp(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    node_start = lines.index("$Nodes")
    node_count = int(lines[node_start + 1])
    index = next(
        index
        for index in range(node_start + 2, node_start + 2 + node_count)
        if float(lines[index].split()[3]) == -0.000965
    )
    fields = lines[index].split()
    fields[3] = f"{float(fields[3]) + 1e-15:.17g}"
    lines[index] = " ".join(fields)
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="unknown z-plane"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_manifest_rejects_mesh_byte_tampering(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    result.mesh_path.write_text(result.mesh_path.read_text() + "tamper\n")
    with pytest.raises(ValueError, match="mesh hash mismatch"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_planar_board_volumes_generate_grouped_conformal_palace_mesh(tmp_path):
    dump = _dump()
    dump["drills"] = []
    geometry = volumes_from_pcb_dump(
        dump, _stackup(), plating_thickness_m=25e-6
    )
    result = generate_palace_mesh(
        tmp_path / "fixture.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
        mesh_size_near_m=0.1e-3,
        mesh_size_far_m=0.5e-3,
        transition_distance_m=1e-3,
    )
    assert result.terminal_attributes == (("A", 101), ("B", 102))
    assert result.material_attributes == (
        ("outer", 1, 1.0), ("dielectric 1", 2, 4.2)
    )
    stored = validate_palace_mesh_manifest(result.manifest_path)
    validate_palace_mesh_content(result.mesh_path, stored["provenance"])


class _Uuid:
    def __init__(self, value):
        self.value = value

    def AsString(self):
        return self.value


class _Pad:
    def __init__(self, uuid, net, *, flashed=True):
        self.m_Uuid = _Uuid(uuid)
        self.net = net
        self.flashed = flashed

    def GetNetname(self):
        return self.net

    def IsOnLayer(self, layer):
        return layer == 0

    def FlashLayer(self, layer):
        return self.flashed and layer == 0


class _Zone:
    def GetIsRuleArea(self):
        return False


class _Layers:
    def CuStack(self):
        return (0,)


class _Board:
    def __init__(self, pads):
        self.pads = pads

    def GetEnabledLayers(self):
        return _Layers()

    def GetTracks(self):
        return ()

    def GetPads(self):
        return self.pads

    def Zones(self):
        return ()


class _Pcbnew:
    PAD = _Pad
    ZONE = _Zone
    PAD_DRILL_SHAPE_CIRCLE = 1
    PAD_DRILL_SHAPE_OBLONG = 2
    PAD_ATTRIB_NPTH = 1

    @staticmethod
    def ToMM(value):
        return value / 1e6


class _Size:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _DrilledPad(_Pad):
    def HasDrilledHole(self):
        return True

    def GetPrimaryDrillSize(self):
        return _Size(6_000_000, 1_000_000)

    def GetPrimaryDrillStartLayer(self):
        return 0

    def GetPrimaryDrillEndLayer(self):
        return 1

    def GetAttribute(self):
        return 0

    def GetPrimaryDrillShape(self):
        return _Pcbnew.PAD_DRILL_SHAPE_OBLONG

    def GetPosition(self):
        return _Size(2_000_000, 3_000_000)

    def IsBackdrilledOrPostMachined(self, _):
        return False

    def GetPrimaryDrillCappedFlag(self):
        return False

    def GetPrimaryDrillFilledFlag(self):
        return False


class _DrillBoard:
    @staticmethod
    def GetLayerName(layer):
        return ("F.Cu", "B.Cu")[layer]


def test_all_copper_discovery_separates_flashed_isolated_items():
    groups, item_groups = _all_copper_groups(
        _Board((_Pad("named", "N"), _Pad("isolated", ""),
                _Pad("npth", "", flashed=False))),
        _Pcbnew,
    )
    assert groups == {"N": ("N",), "isolated:isolated": ()}
    assert item_groups == {"isolated:isolated": ("isolated",)}


def test_pcbnew_boundary_records_oblong_drill_shape():
    record = _drill_record(
        _DrilledPad("slot", "N"), "pad", _DrillBoard(), "N", _Pcbnew
    )
    assert record["shape"] == "oblong"
    assert record["size_mm"] == (6.0, 1.0)
    assert record["unsupported_features"] == []


def test_all_copper_mapping_keeps_isolated_items_out_of_net_groups():
    names, net_to_group, item_to_group = _mapping(
        {"N": ("N",), "isolated:item-1": ()},
        {"isolated:item-1": ("item-1",)},
    )
    assert names == ("N", "isolated:item-1")
    assert net_to_group == {"N": "N"}
    assert item_to_group == {"item-1": "isolated:item-1"}


def test_oblong_plated_drill_builds_capsule_barrel_not_ellipse():
    dump = _dump()
    dump["drills"][0]["size_mm"] = [0.6, 0.2]
    dump["drills"][0]["shape"] = "oblong"
    geometry = volumes_from_pcb_dump(
        dump, _stackup(), plating_thickness_m=25e-6
    )
    barrel = next(
        item for item in geometry.conductors
        if item.name == "A" and item.z_max - item.z_min > 0.5e-3
    )
    bounds = Polygon(barrel.rings[0], barrel.rings[1:]).bounds
    assert bounds[2] - bounds[0] == pytest.approx(0.65e-3)
    assert bounds[3] - bounds[1] == pytest.approx(0.25e-3)
    barrel_area = Polygon(barrel.rings[0], barrel.rings[1:]).area
    expected_area = (
        (0.65 - 0.25) * 0.25 + math.pi * 0.125**2
        - (0.6 - 0.2) * 0.2 - math.pi * 0.1**2
    ) * 1e-6
    assert barrel_area == pytest.approx(expected_area, rel=2e-3)


def test_npth_drill_without_group_remains_a_void_not_a_terminal():
    dump = _dump()
    dump["drills"][0]["group"] = None
    dump["drills"][0]["plated"] = False
    geometry = volumes_from_pcb_dump(
        dump, _stackup(), plating_thickness_m=25e-6
    )
    assert len([item for item in geometry.conductors if item.name == "A"]) == 2
    assert len(geometry.dielectrics[0].rings) == 2


def test_incomplete_census_rejects_before_geometry_access():
    dump = _dump()
    dump["census"]["unassigned"].append({
        "source_kind": "track", "source_uuid": "missing", "net": "C"
    })
    dump["records"] = "must not be inspected"
    with pytest.raises(ValueError, match="unassigned copper"):
        volumes_from_pcb_dump(
            dump, _stackup(), plating_thickness_m=25e-6
        )


def test_missing_dielectric_permittivity_rejects():
    with pytest.raises(ValueError, match="no positive permittivity"):
        volumes_from_pcb_dump(
            _dump(), _stackup(core_epsilon=None), plating_thickness_m=25e-6
        )


def test_dump_loader_requires_exact_schema(tmp_path):
    path = tmp_path / "dump.json"
    value = _dump()
    value["unexpected"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="schema mismatch"):
        load_pcb_volume_dump(path)


def test_kicad_dump_import_uses_only_stdlib_and_pcbnew_boundary():
    code = f"""
import builtins, sys, types
sys.path.insert(0, {str(Path(ROOT) / 'lib')!r})
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name.split('.')[0] in {{'numpy', 'shapely', 'gmsh'}}:
        raise ImportError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
import kicad_palace_dump
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


# -- nested ladder rungs ---------------------------------------------------


def _nesting_common(geometry):
    return dict(
        outer_bounds=geometry.outer_bounds, conductors=geometry.conductors,
        dielectrics=geometry.dielectrics, max_planar_area_m2=1e-6,
        max_vertical_step_m=1e-3,
    )


def test_a_nested_rung_contains_its_parent_and_validates(tmp_path):
    """The point of the whole exercise: rung k+1 must contain rung k, so
    differencing them measures refinement rather than re-meshing."""
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    common = _nesting_common(geometry)
    parent = generate_palace_plc_mesh(
        tmp_path / "n0.msh", nesting_refinements=0, **common)
    child = generate_palace_plc_mesh(
        tmp_path / "n1.msh", nesting_refinements=1, **common)
    assert child.tetrahedron_count > parent.tetrahedron_count
    validate_palace_plc_mesh_manifest(tmp_path / "n1.msh.manifest.json")

    def xy(path):
        points = set()
        with open(path) as handle:
            for line in handle:
                if line.startswith("$Nodes"):
                    break
            for _ in range(int(next(handle))):
                parts = next(handle).split()
                points.add((float(parts[1]), float(parts[2])))
        return points

    assert xy(tmp_path / "n0.msh") <= xy(tmp_path / "n1.msh")


def test_a_nested_rung_records_that_its_segments_were_split(tmp_path):
    """refine() takes no allow_volume_steiner and does split PLC segments, so
    the manifest must not let a reader infer they are intact from a build flag
    that is no longer the whole story."""
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    common = _nesting_common(geometry)
    for refinements, intact in ((0, True), (1, False)):
        generate_palace_plc_mesh(
            tmp_path / f"s{refinements}.msh",
            nesting_refinements=refinements, **common)
        parameters = json.loads(
            (tmp_path / f"s{refinements}.msh.manifest.json").read_text()
        )["provenance"]["mesh_parameters"]
        assert parameters["nesting_refinements"] == refinements
        assert parameters["plc_segments_intact"] is intact
        # the build flag stays true of the build, and stays recorded
        assert parameters["allow_volume_steiner"] is False


def test_nesting_bisects_the_z_levels_too(tmp_path):
    """Nesting the triangulation while equal-dividing z would leave the rungs
    unnested in the other axis, which is how 15 of 25 canary levels were lost."""
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    common = _nesting_common(geometry)
    generate_palace_plc_mesh(
        tmp_path / "z0.msh", nesting_refinements=0, **common)
    generate_palace_plc_mesh(
        tmp_path / "z1.msh", nesting_refinements=1, **common)

    def levels(path):
        out = set()
        with open(path) as handle:
            for line in handle:
                if line.startswith("$Nodes"):
                    break
            for _ in range(int(next(handle))):
                out.add(float(next(handle).split()[3]))
        return out

    assert levels(tmp_path / "z0.msh") <= levels(tmp_path / "z1.msh")


@pytest.mark.parametrize("bad", [-1, True, 1.5, "1", [], 2.0])
def test_a_bad_nesting_count_is_refused(tmp_path, bad):
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    with pytest.raises(ValueError, match="nesting refinements"):
        generate_palace_plc_mesh(
            tmp_path / "bad.msh", nesting_refinements=bad,
            **_nesting_common(geometry))


def test_nesting_without_a_planar_area_is_refused(tmp_path):
    """There is no area target to halve, so the rung would be identical to its
    parent while claiming to be finer."""
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    with pytest.raises(ValueError, match="maximum planar area"):
        generate_palace_plc_mesh(
            tmp_path / "bad.msh", outer_bounds=geometry.outer_bounds,
            conductors=geometry.conductors, dielectrics=geometry.dielectrics,
            nesting_refinements=1)


def test_a_refinement_that_loses_a_parent_vertex_is_refused():
    """Known-bad calibration for the nesting guard: hand it a 'refinement' that
    drops a vertex and it must raise rather than return an unnested rung."""
    original = palace_plc_mesh._subdivide_uniformly
    palace_plc_mesh._subdivide_uniformly = (
        lambda points, triangles, refinements: (
            ((0.0, 0.0), (1.0, 0.0), (9.0, 9.0)), triangles))
    try:
        with pytest.raises(ValueError, match="not nested in its parent"):
            palace_plc_mesh._nested_refinement(
                ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), ((0, 1, 2),), 1)
    finally:
        palace_plc_mesh._subdivide_uniformly = original


def test_uniform_subdivision_is_exactly_nested_and_conforming():
    """The two properties the ladder rests on, on a mesh with a shared edge.

    Nesting is what buys Rayleigh-Ritz monotonicity and cancels re-meshing
    noise on a difference. Conformity is what stops the shared edge acquiring a
    hanging node -- both triangles must be handed the *same* midpoint vertex,
    not two vertices that merely compare equal.
    """
    points = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    triangles = ((0, 1, 2), (0, 2, 3))
    fine_points, fine_triangles = palace_plc_mesh._subdivide_uniformly(
        points, triangles, 1)
    assert set(points) <= set(fine_points)
    assert len(fine_triangles) == 4 * len(triangles)
    # 4 corners + 5 edge midpoints (the diagonal's midpoint is shared).
    assert len(fine_points) == 9
    assert len(set(fine_points)) == len(fine_points)
    diagonal_midpoint = fine_points.index((0.5, 0.5))
    # Three of each parent's four children touch the midpoint of a given
    # parent edge, so the two parents sharing the diagonal contribute six.
    # Seeing six rather than three is the point: both parents resolved the
    # shared edge to the *same* vertex index, so there is no hanging node.
    touching = [t for t in fine_triangles if diagonal_midpoint in t]
    assert len(touching) == 6, "the shared edge midpoint must be one vertex"


def test_uniform_subdivision_halves_the_element_size_each_rung():
    points = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
    triangles = ((0, 1, 2),)

    def largest_area(pts, tris):
        return max(
            abs((pts[b][0] - pts[a][0]) * (pts[c][1] - pts[a][1])
                - (pts[b][1] - pts[a][1]) * (pts[c][0] - pts[a][0])) / 2.0
            for a, b, c in tris)

    previous = largest_area(points, triangles)
    for refinements in (1, 2, 3):
        fine = palace_plc_mesh._subdivide_uniformly(
            points, triangles, refinements)
        assert largest_area(*fine) == pytest.approx(
            previous / 4.0 ** (refinements - 1) / 4.0)
    assert largest_area(*palace_plc_mesh._subdivide_uniformly(
        points, triangles, 3)) == pytest.approx(0.5 / 64.0)


def test_uniform_subdivision_keeps_every_parent_edge_covered():
    """A source segment that was an edge must still be covered by edges.

    This is the property Triangle's -r mode could not hold: with `p` it lost 2
    of 2532 segments at refinement 5, and from a finer seed 23 by refinement 2,
    rising to 108 by refinement 4 -- dropped outright, 25% to 100% of their
    length left uncovered. Subdivision cannot lose one, because each edge
    becomes two collinear halves of itself.
    """
    points = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    triangles = ((0, 1, 2), (0, 2, 3))
    parent_edges = [
        LineString([points[a], points[b]])
        for a, b, c in triangles for a, b in ((a, b), (b, c), (c, a))
    ]
    fine_points, fine_triangles = palace_plc_mesh._subdivide_uniformly(
        points, triangles, 3)
    fine_edges = union_all([
        LineString([fine_points[a], fine_points[b]])
        for a, b, c in fine_triangles for a, b in ((a, b), (b, c), (c, a))
    ])
    for edge in parent_edges:
        assert edge.difference(fine_edges).length == 0.0


def test_none_and_zero_nesting_mean_different_things(tmp_path):
    """None is "not a ladder rung": z gaps take the fewest pieces satisfying the
    step, which is what every mesh written before nesting recorded. Zero is
    "rung zero": no planar refinement yet, but the z levels already bisect so
    rung 0 nests with rung 1. Collapsing the two would leave the coarsest rung
    unnested in z against everything above it."""
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    common = _nesting_common(geometry)
    generate_palace_plc_mesh(
        tmp_path / "none.msh", nesting_refinements=None, **common)
    generate_palace_plc_mesh(
        tmp_path / "zero.msh", nesting_refinements=0, **common)
    generate_palace_plc_mesh(
        tmp_path / "one.msh", nesting_refinements=1, **common)

    def levels(name):
        out = set()
        with open(tmp_path / name) as handle:
            for line in handle:
                if line.startswith("$Nodes"):
                    break
            for _ in range(int(next(handle))):
                out.add(float(next(handle).split()[3]))
        return out

    # rung 0 nests into rung 1; the standalone mesh is not required to
    assert levels("zero.msh") <= levels("one.msh")
    parameters = json.loads(
        (tmp_path / "none.msh.manifest.json").read_text()
    )["provenance"]["mesh_parameters"]
    assert parameters["nesting_refinements"] is None
    assert parameters["plc_segments_intact"] is True


def test_a_mesh_written_before_nesting_existed_still_validates(tmp_path):
    """The nesting keys are optional, so the meshes already on disk are not
    invalidated by adding them. Absence is a complete statement of an unnested
    mesh; a half-present pair is not."""
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6)
    result = generate_palace_plc_mesh(
        tmp_path / "legacy.msh", **_nesting_common(geometry))
    path = tmp_path / "legacy.msh.manifest.json"
    manifest = json.loads(path.read_text())
    parameters = manifest["provenance"]["mesh_parameters"]
    del parameters["nesting_refinements"]
    del parameters["plc_segments_intact"]
    assert _nesting_parameters_valid(parameters)

    parameters["nesting_refinements"] = 0
    assert not _nesting_parameters_valid(parameters), (
        "a refinement count without the segment statement must be refused")
    parameters["plc_segments_intact"] = False
    assert not _nesting_parameters_valid(parameters), (
        "rung zero has not been refined, so its segments are intact")
    parameters["plc_segments_intact"] = True
    assert _nesting_parameters_valid(parameters)
    parameters["nesting_refinements"] = 2
    assert not _nesting_parameters_valid(parameters), (
        "a refined mesh must not claim intact PLC segments")


def _cell_and_segment(depth_m):
    """A triangle with a horizontal segment entering its interior by depth_m.

    At depth 0 the segment lies exactly along the triangle's base, which is the
    conforming case; a positive depth lifts it into the interior, which is the
    material-assignment error the guard exists to catch.
    """
    cell = Polygon([(0.0, 0.0), (1e-3, 0.0), (0.0, 1e-3)])
    segment = LineString([(0.0, depth_m), (5e-4, depth_m)])
    return [cell], (segment,), STRtree((segment,))


@pytest.mark.parametrize("depth_m", [1e-9, 1e-8, 5e-8, 1e-6, 1e-4])
def test_a_real_boundary_crossing_is_caught_at_every_depth(depth_m):
    """Known-bad calibration for the crossing tolerance, and its monotonicity.

    The tolerance added for the 3.9e-17 m artefact must not become a mute
    button. The shallowest depth here, 1e-9 m, is still fifty times *below* the
    5e-8 m quantum the geometry is snapped to and eight orders above the ULP
    bound -- so anything a real geometry can express is caught, and the verdict
    does not flip back to clean as the crossing gets worse.
    """
    cells, lines, tree = _cell_and_segment(depth_m)
    tolerance = palace_plc_mesh._crossing_tolerance_m(1e-3)
    crossings = palace_plc_mesh._boundary_crossings(
        cells, lines, tree, tolerance, 5e-8 / 1e4)
    assert len(crossings) == 1
    # The recorded quantity is how deep the segment reaches into the cell, not
    # how long the overlap is. Those differ by orders of magnitude and only the
    # depth distinguishes a real crossing from a segment lying on an edge.
    assert crossings[0][1] == pytest.approx(depth_m, rel=0.05)


def test_a_segment_lying_on_a_cell_edge_is_not_a_crossing():
    cells, lines, tree = _cell_and_segment(0.0)
    tolerance = palace_plc_mesh._crossing_tolerance_m(1e-3)
    assert palace_plc_mesh._boundary_crossings(
        cells, lines, tree, tolerance, 5e-8 / 1e4) == []


def test_the_crossing_tolerance_stays_far_below_the_geometry_quantum():
    """The bound must track the coordinate scale, not the geometry quantum.

    5e-8 m is what the planar geometry is snapped to. A tolerance anywhere near
    it would accept a conductor boundary genuinely cut by tens of nanometres.
    At the canary's 0.167 m coordinates the bound is ~2e-16 m, which is what
    admits the 3.9e-17 m artefact and nothing else.
    """
    assert palace_plc_mesh._crossing_tolerance_m(0.167) < 5e-8 / 1e6
    assert palace_plc_mesh._crossing_tolerance_m(0.167) > 3.925e-17
    # It scales with the coordinates rather than being a fixed epsilon.
    assert (palace_plc_mesh._crossing_tolerance_m(1.0)
            > palace_plc_mesh._crossing_tolerance_m(1e-3))


def test_a_refinement_that_does_not_refine_is_refused():
    """Known-bad calibration for the second half of the nesting guard.

    Containment is satisfied by doing nothing, so a rung that silently failed to
    refine would pass the parent-vertex check and enter the ladder as a
    duplicate of its parent. Nothing downstream would notice: the manifest keeps
    max_planar_area_m2 at the seed value on every rung, so the recorded cap is
    trivially met however little was done.
    """
    original = palace_plc_mesh._subdivide_uniformly
    palace_plc_mesh._subdivide_uniformly = (
        lambda points, triangles, refinements: (points, triangles))
    try:
        with pytest.raises(ValueError, match="did not refine as recorded"):
            palace_plc_mesh._nested_refinement(
                ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), ((0, 1, 2),), 1)
    finally:
        palace_plc_mesh._subdivide_uniformly = original


@pytest.mark.parametrize("refinements", [1, 2, 3])
def test_a_real_nested_refinement_meets_the_exact_triangle_count(refinements):
    points = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    triangles = ((0, 1, 2), (0, 2, 3))
    fine_points, fine_triangles = palace_plc_mesh._nested_refinement(
        points, triangles, refinements)
    assert len(fine_triangles) == len(triangles) * 4 ** refinements
    assert set(points) <= set(fine_points)


def test_the_coverage_grid_is_a_power_of_ten():
    """GEOS snaps by scaling with 1/gridSize, so a grid that is not exactly
    representable pushes collinear points off each other. On the canary the
    comparison that leaves 0 segments uncovered at 1e-13 leaves 160 at
    2.05e-13 and 141-193 at every power of two from 2**-46 to 2**-36."""
    for scale in (0.205, 0.0020005, 1.0, 45e-3):
        grid = palace_plc_mesh._coverage_grid_m(scale, quantum_m=1.0)
        assert grid == pytest.approx(10.0 ** round(math.log10(grid)))
        assert grid < scale


def test_the_coverage_grid_declines_when_it_would_reach_the_quantum():
    """Unevaluable is not clean. With no room between the rounding floor and
    the quantum the geometry is snapped to, snapping would start absorbing real
    geometry, so the caller keeps the unsnapped verdict and fails closed."""
    # The plated-via fixture is a real instance of this: 2e-3 m coordinates
    # against a 4.001e-15 m quantum leaves no separation at all.
    assert palace_plc_mesh._coverage_grid_m(0.0020005, 4.001e-15) is None
    # The canary has six orders of room and does get a grid.
    assert palace_plc_mesh._coverage_grid_m(0.205, 5e-8) == pytest.approx(1e-13)


def test_the_coverage_grid_stays_clear_of_the_rounding_it_absorbs():
    """It has to sit well above the coordinate ULP -- that is the size of what
    it is absorbing, and it accumulates with each refinement."""
    for scale in (0.205, 1.0, 45e-3):
        grid = palace_plc_mesh._coverage_grid_m(scale, quantum_m=1.0)
        assert grid > 1e3 * math.ulp(scale)


def test_the_stray_distance_separates_a_dropped_segment_from_a_bent_one():
    """The third coverage tier, and the only one that measures the physical
    quantity. Snapping is a comparison of representations; on the graded canary
    seed it cleared 3002 of 3003 segments and the one it missed strayed
    1.963e-17 m, which is not a dropped segment by any reading."""
    covering = union_all([LineString([(0.0, 0.0), (0.5, 0.0)]),
                          LineString([(0.5, 0.0), (1.0, 0.0)])])
    # A segment covered by edges bent off it: the whole thing is "remainder",
    # but no point of it is far from the covering.
    bent = LineString([(0.0, 1e-17), (1.0, 1e-17)])
    assert palace_plc_mesh._stray_distance_m(bent, covering) < 1e-15
    # A segment whose middle really is missing: the gap's interior is far.
    gapped = union_all([LineString([(0.0, 0.0), (0.2, 0.0)]),
                        LineString([(0.8, 0.0), (1.0, 0.0)])])
    missing = LineString([(0.0, 0.0), (1.0, 0.0)]).difference(gapped)
    assert palace_plc_mesh._stray_distance_m(missing, gapped) > 0.1


def test_the_stray_distance_samples_the_remainder_not_the_segment():
    """Sampling the whole segment could step over a gap shorter than the
    sample spacing. The remainder is exactly the disputed set, so a gap is
    always sampled at its own scale however small it is."""
    for gap in (1e-3, 1e-5, 1e-7):
        low, high = 0.5 - gap / 2.0, 0.5 + gap / 2.0
        covering = union_all([LineString([(0.0, 0.0), (low, 0.0)]),
                              LineString([(high, 0.0), (1.0, 0.0)])])
        remainder = LineString([(0.0, 0.0), (1.0, 0.0)]).difference(covering)
        stray = palace_plc_mesh._stray_distance_m(remainder, covering)
        assert stray == pytest.approx(gap / 2.0, rel=0.05)


def _diagonal_owner(left, right, key):
    """Which endpoint of the vertical quad over an edge carries the diagonal."""
    return 0 if key(left) < key(right) else 1


def test_the_prism_diagonal_is_inherited_through_subdivision():
    """The property that makes a nested ladder mean anything.

    Index order is not stable under refinement: subdivision appends midpoints
    at the end of the point list, so for a parent edge A->B with A < B the
    sub-edge A-AB keeps the parent's orientation while AB-B flips, because AB
    now outranks B. Exactly half the sub-edges invert. Measured on the canary
    seed: 18618 of 37236 inherited under index order, 37226 of 37236 under
    coordinate order.

    Coordinate order fixes it by construction -- a midpoint sorts between its
    own endpoints -- and that is what makes the tetrahedralization a function
    of the geometry rather than of the numbering.
    """
    points = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    triangles = ((0, 1, 2), (0, 2, 3))
    fine, fine_triangles = palace_plc_mesh._subdivide_uniformly(
        points, triangles, 1)
    at = {point: index for index, point in enumerate(fine)}

    inherited = {"index": 0, "coordinate": 0}
    total = 0
    for first, second, third in triangles:
        for left, right in ((first, second), (second, third), (third, first)):
            a, b = points[left], points[right]
            middle = at[((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)]
            for pair in ((at[a], middle), (middle, at[b])):
                total += 1
                if _diagonal_owner(left, right, lambda i: i) == \
                        _diagonal_owner(*pair, key=lambda i: i):
                    inherited["index"] += 1
                if _diagonal_owner(left, right, lambda i: points[i]) == \
                        _diagonal_owner(*pair, key=lambda i: fine[i]):
                    inherited["coordinate"] += 1

    assert inherited["coordinate"] == total, "coordinate order must inherit"
    assert inherited["index"] < total, "index order must not (it inverts half)"


def test_the_prism_split_is_conforming_under_coordinate_order():
    """Conformity holds for any total order, but it is the property that makes
    the mesh usable at all, so it is checked rather than argued."""
    points = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    triangles = ((0, 1, 2), (0, 2, 3))
    coordinates, tetrahedra = palace_plc_mesh._tetrahedralize(
        points, triangles, (0.0, 0.5, 1.0),
        lambda centroid, z: ("material", 1))
    counts = collections.Counter()
    for nodes, region, determinant in tetrahedra:
        assert determinant > 0.0
        for face in itertools.combinations(sorted(nodes), 3):
            counts[face] += 1
    assert set(counts.values()) <= {1, 2}, "a face may bound at most two tets"
    assert sum(1 for v in counts.values() if v == 2) > 0
