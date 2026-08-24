#!/usr/bin/env python3
from dataclasses import asdict
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import experiments.fastercap_reduction_study as study  # noqa: E402
from experiments.fastercap_reduction_study import (  # noqa: E402
    Candidate,
    REDUCTION_ABS_F,
    REDUCTION_REL,
    build_geometry,
    candidates,
    entrywise_gate,
    fixtures,
)
from lib.maxwell import MaxwellMatrix, raw_maxwell_gate_results  # noqa: E402


def _named(values, name):
    return next(value for value in values if value.name == name)


def test_fixture_dimensions_and_scope_are_explicit():
    values = fixtures()
    assert tuple(item.name for item in values) == (
        "parallel_plate_air",
        "pcb_like_coplanar_air",
        "parallel_plate_enclosed_fr4",
    )
    assert all(item.source_thickness_m == pytest.approx(35e-6)
               for item in values)
    assert values[-1].relative_permittivity == pytest.approx(4.2)
    assert values[-1].dielectric_bounds_m is not None
    assert all(item.scope for item in values)


def test_candidate_knobs_have_finite_si_defaults_and_explicit_semantics():
    values = candidates()
    assert tuple(item.name for item in values) == (
        "physical_35um", "effective_34um", "effective_1um", "simplify_1um"
    )
    assert all(item.anchor == "midplane" for item in values)
    assert all(item.documented_semantics for item in values)
    assert _named(values, "simplify_1um").simplify_tolerance_m == pytest.approx(1e-6)
    assert not any(item.representation == "thin_sheet" for item in values)

    with pytest.raises(ValueError, match="finite positive"):
        Candidate("bad", "effective_thickness", float("nan"), "midplane", 0, "x")
    with pytest.raises(ValueError, match="simplification tolerance"):
        Candidate("bad", "boundary_simplification", 1e-6,
                  "midplane", -1e-6, "x")
    with pytest.raises(ValueError, match="unknown anchor"):
        Candidate("bad", "effective_thickness", 1e-6, "moving", 0, "x")
    with pytest.raises(ValueError, match="unknown representation"):
        Candidate("bad", "thin_sheet", 1e-6, "midplane", 0, "x")


def test_effective_thickness_clearance_gate_depends_on_fixture_topology():
    fixture_values = fixtures()
    candidate_values = candidates()
    thin = _named(candidate_values, "effective_1um")
    parallel = _named(fixture_values, "parallel_plate_air")
    coplanar = _named(fixture_values, "pcb_like_coplanar_air")

    _, _, parallel_metrics = build_geometry(parallel, thin)
    assert not parallel_metrics.valid
    assert parallel_metrics.clearance_change_m == pytest.approx(34e-6)
    assert parallel_metrics.allowed_clearance_change_m == pytest.approx(1e-6)

    _, _, coplanar_metrics = build_geometry(coplanar, thin)
    assert coplanar_metrics.valid
    assert coplanar_metrics.clearance_change_m == pytest.approx(0.0)
    assert coplanar_metrics.maximum_anchor_displacement_m == pytest.approx(0.0)
    assert coplanar_metrics.maximum_surface_displacement_m == pytest.approx(17e-6)


def test_fixed_face_anchor_preserves_declared_source_face():
    fixture = _named(fixtures(), "pcb_like_coplanar_air")
    candidate = Candidate(
        "fixed-bottom", "effective_thickness", 1e-6,
        "z_min_face", 0.0, "fixed material-boundary face",
    )
    surfaces, _, metrics = build_geometry(fixture, candidate)
    z_values = [point[2] for surface in surfaces
                for panel in surface.panels for point in panel]
    assert min(z_values) == pytest.approx(-17.5e-6)
    assert max(z_values) == pytest.approx(-16.5e-6)
    assert metrics.maximum_anchor_displacement_m == pytest.approx(0.0)
    assert metrics.maximum_surface_displacement_m == pytest.approx(34e-6)


def test_boundary_simplification_reduces_panels_within_geometry_gates():
    fixture = _named(fixtures(), "pcb_like_coplanar_air")
    candidate = _named(candidates(), "simplify_1um")
    _, _, metrics = build_geometry(fixture, candidate)
    assert metrics.valid
    assert metrics.maximum_area_relative_error <= 1e-4
    assert metrics.clearance_change_m <= metrics.allowed_clearance_change_m
    assert metrics.candidate_input_panels < metrics.source_input_panels
    assert metrics.minimum_boundary_segment_m >= 0.5e-6
    assert metrics.minimum_triangle_angle_deg >= 5.0
    assert metrics.maximum_area_relative_error == pytest.approx(0.0)
    assert metrics.maximum_anchor_displacement_m == pytest.approx(0.0)
    assert metrics.maximum_planar_boundary_displacement_m == pytest.approx(0.0)


def test_selection_requires_tight_rung_refined_panel_reduction(
        tmp_path, monkeypatch):
    matrix = [[10e-12, -5e-12], [-5e-12, 10e-12]]

    def fake_ladder(output, fixture, candidate, executable, ladder):
        _, _, geometry = build_geometry(fixture, candidate)
        if not geometry.valid:
            return {
                "geometry": asdict(geometry), "runs": [],
                "ladder_passed": False, "ladder_differences_f": [],
                "ladder_allowances_f": [], "reasons": list(geometry.reasons),
            }
        refined = {
            "physical_35um": 100,
            "effective_34um": 90,
            "effective_1um": 110,
            "simplify_1um": 110,
        }[candidate.name]
        runs = [
            {
                "raw_matrix_f": matrix,
                "matrix_f": matrix,
                "refined_panels": refined,
            }
            for _ in ladder
        ]
        return {
            "geometry": asdict(geometry), "runs": runs,
            "ladder_passed": True,
            "ladder_differences_f": [[[0.0]], [[0.0]]],
            "ladder_allowances_f": [[[1.0]], [[1.0]]],
        }

    monkeypatch.setattr(study, "_run_ladder", fake_ladder)
    _, report = study.run_study(tmp_path, executable="unused")
    for results in report["results"].values():
        assert results["effective_34um"]["verdict"] == "fixture_qualified"
        assert results["simplify_1um"]["verdict"] == "rejected"
        assert "no tight-rung refined-panel reduction" in (
            results["simplify_1um"]["reasons"][-1]
        )


def test_asymmetric_raw_matrix_cannot_qualify_via_averaging(
        tmp_path, monkeypatch):
    reference_raw = np.array([
        [500e-15, -300e-15],
        [-300e-15, 500e-15],
    ])
    candidate_raw = np.array([
        [500e-15, -294.9e-15],
        [-295.1e-15, 500e-15],
    ])
    candidate_averaged = 0.5 * (candidate_raw + candidate_raw.T)
    ladder_raw = np.array([
        [500e-15, -292.95e-15],
        [-293.15e-15, 500e-15],
    ])
    ladder_averaged = 0.5 * (ladder_raw + ladder_raw.T)
    for values in (reference_raw, candidate_raw, ladder_raw):
        gates = raw_maxwell_gate_results(MaxwellMatrix(("a", "b"), values))
        assert all(result["passed"] for result in gates.values())
    assert not entrywise_gate(
        reference_raw, candidate_raw, REDUCTION_ABS_F, REDUCTION_REL
    )[0]
    assert entrywise_gate(
        reference_raw, candidate_averaged, REDUCTION_ABS_F, REDUCTION_REL
    )[0]
    assert not study._evaluate_ladder(
        (reference_raw, ladder_raw, ladder_raw)
    )[0]
    assert study._evaluate_ladder(
        (reference_raw, ladder_averaged, ladder_averaged)
    )[0]

    def fake_ladder(output, fixture, candidate, executable, ladder):
        _, _, geometry = build_geometry(fixture, candidate)
        if not geometry.valid:
            return {
                "geometry": asdict(geometry), "runs": [],
                "ladder_passed": False, "ladder_differences_f": [],
                "ladder_allowances_f": [], "reasons": list(geometry.reasons),
            }
        raw = candidate_raw if candidate.name == "effective_34um" else reference_raw
        downstream = (
            candidate_averaged
            if candidate.name == "effective_34um" else reference_raw
        )
        refined = 90 if candidate.name == "effective_34um" else 100
        return {
            "geometry": asdict(geometry),
            "runs": [
                {
                    "raw_matrix_f": raw.tolist(),
                    "matrix_f": downstream.tolist(),
                    "refined_panels": refined,
                }
                for _ in ladder
            ],
            "ladder_passed": True,
            "ladder_differences_f": [[[0.0]], [[0.0]]],
            "ladder_allowances_f": [[[1.0]], [[1.0]]],
        }

    monkeypatch.setattr(study, "_run_ladder", fake_ladder)
    _, report = study.run_study(tmp_path, executable="unused")
    for results in report["results"].values():
        result = results["effective_34um"]
        assert result["verdict"] == "rejected"
        assert result["reduction_budget_passed"] is False


def test_reduction_entry_gate_uses_fixed_two_femtofarad_plus_one_percent():
    reference = np.array([[100e-15]])
    boundary_delta = (
        REDUCTION_ABS_F + REDUCTION_REL * reference[0, 0]
    ) / (1.0 - REDUCTION_REL)
    on_limit = reference + boundary_delta
    passed, difference, allowance = entrywise_gate(
        reference, on_limit, REDUCTION_ABS_F, REDUCTION_REL
    )
    assert passed
    np.testing.assert_allclose(difference, allowance, rtol=1e-12, atol=1e-30)

    over = on_limit + 1e-18
    assert not entrywise_gate(
        reference, over, REDUCTION_ABS_F, REDUCTION_REL
    )[0]
