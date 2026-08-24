import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

import experiments.fastercap_fugu_diagnostic_study as study


def pinned_geometry(tolerance=0.001):
    return {
        "pcb_sha256": study.PCB_SHA256,
        "filled_zone_record_count": 10,
        "groups": {
            "SW": ["SW"], "VIN": ["Solar+"], "PGND": ["BuckGND"]
        },
        "source_mode": "filled_zones_only",
        "geometry_tolerance_mm": tolerance,
        "area_mm2": dict(study.PINNED_AREA_MM2),
        "panel_count": {"SW": 10, "VIN": 20, "PGND": 30},
        "component_count": {"SW": 5, "VIN": 2, "PGND": 3},
        "solver_panel_count": {"SW": 10, "VIN": 20, "PGND": 30},
        "solver_conductor_names": ["SW", "VIN", "PGND"],
        "triangulation": {
            "engine": "MeshPy Triangle constrained-quality",
            "minimum_angle_deg": 20.0,
            "boundary": "exact constrained polygon segments",
        },
        "reduction": {
            "representation": "effective_thickness",
            "effective_thickness_m": 34e-6,
            "anchor": "midplane",
            "source_mode": "filled_zones_only",
            "material_scope": "air_only",
            "fixture_id": None,
            "qualification_state": "outside_fixture_envelope",
            "physical_validation_authorized": False,
            "lifecycle_ceiling": "numerically_converged_diagnostic",
            "source_thickness_m": {
                name: [35e-6, 35e-6] for name in study.CONDUCTOR_NAMES
            },
            "source_panel_count": {"SW": 10, "VIN": 20, "PGND": 30},
            "candidate_panel_count": {"SW": 10, "VIN": 20, "PGND": 30},
            "electrical_assembly": {
                "solver_policy": "grouped",
                "assignments": {"SW": "SW", "VIN": "VIN", "PGND": "PGND"},
                "group_order": ["SW", "VIN", "PGND"],
                "postsolve_transform": "C_group = A.T @ C_base @ A",
            },
        },
    }


def test_pinned_geometry_validation_rejects_provenance_and_area_changes():
    assert study.validate_fugu_geometry(pinned_geometry(), 0.001) == ()
    changed = pinned_geometry()
    changed["reduction"]["material_scope"] = "full_stackup"
    changed["area_mm2"]["SW"] *= 1.001
    failures = study.validate_fugu_geometry(changed, 0.001)
    assert any("material_scope" in failure for failure in failures)
    assert any("area" in failure for failure in failures)


def test_ladder_uses_unsymmetrized_raw_matrices():
    coarse = np.array([
        [500e-15, -300e-15],
        [-300e-15, 500e-15],
    ])
    fine = np.array([
        [500e-15, -292.95e-15],
        [-293.15e-15, 500e-15],
    ])
    raw = study.evaluate_ladder([
        {"raw_matrix_f": coarse.tolist()},
        {"raw_matrix_f": fine.tolist()},
        {"raw_matrix_f": fine.tolist()},
    ])
    averaged = 0.5 * (fine + fine.T)
    symmetric = study.evaluate_ladder([
        {"raw_matrix_f": coarse.tolist()},
        {"raw_matrix_f": averaged.tolist()},
        {"raw_matrix_f": averaged.tolist()},
    ])
    assert raw["passed"] is False
    assert symmetric["passed"] is True


def fake_geometry(tolerance):
    return {
        "geometry_tolerance_mm": tolerance,
        "deck": f"geometry-{tolerance}.lst",
        "failures": [],
    }


def test_solver_rung_pins_automatic_settings_and_raw_matrix(monkeypatch):
    calls = []
    raw = np.array([[2e-12, -1e-12], [-1e-12, 2e-12]])
    averaged = 0.5 * (raw + raw.T)

    def fake_run(deck, **kwargs):
        calls.append((deck, kwargs))
        transcript = SimpleNamespace(
            iterations=[SimpleNamespace(
                matrix=SimpleNamespace(values=raw), refined_panels=1234
            )],
            initial_panels=100,
            solver_runtime_s=2.5,
            resource_observations={
                "peak_rss_bytes": 1000, "directory_bytes": 2000
            },
        )
        return SimpleNamespace(
            transcript=transcript,
            matrix=SimpleNamespace(values=averaged),
            manifest_path="run.json",
        )

    monkeypatch.setattr(study, "run_fastercap", fake_run)
    record = study.solve_rung(
        {
            "geometry_tolerance_mm": 0.0005,
            "deck": "model.lst",
            "solver_conductor_names": ["sw_part", "vin_part"],
            "ideal_short_assignments": {
                "sw_part": "SW", "vin_part": "VIN"
            },
            "ideal_short_group_order": ["SW", "VIN"],
        },
        0.0125,
        "FasterCap",
    )
    assert calls == [("model.lst", {
        "conductor_names": ("sw_part", "vin_part"),
        "executable": "FasterCap",
        "relative_error": 0.0125,
        "timeout": study.RESOURCE_TIMEOUT_S,
        "resource_class": study.RESOURCE_CLASS,
    })]
    assert record["raw_matrix_f"] == raw.tolist()
    assert record["matrix_f"] == averaged.tolist()


def test_failed_endpoint_prevents_all_ladders_and_matrix(
        tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    executable = tmp_path / "FasterCap"
    board.write_text("board")
    executable.write_text("solver")
    calls = []

    monkeypatch.setattr(
        study, "extract_geometry",
        lambda board, output, tolerance: fake_geometry(tolerance),
    )

    def rejected(geometry, solver_relative_error, executable):
        calls.append((geometry["geometry_tolerance_mm"], solver_relative_error))
        return {
            "state": "rejected_diagnostic",
            "manifest": "rejected.json",
            "failures": ["resource limit"],
        }

    monkeypatch.setattr(study, "solve_rung", rejected)
    path, report = study.run_study(board, tmp_path / "out", executable)
    assert calls == [(
        study.PRODUCTION_GEOMETRY_MM,
        study.PRODUCTION_SOLVER_RELATIVE_ERROR,
    )]
    assert report["state"] == "rejected_diagnostic"
    assert report["geometry_ladder"]["passed"] is False
    assert report["solver_ladder"]["passed"] is False
    assert report["uncertainty_f"] is None
    assert "matrix_artifact" not in report
    document = json.loads(path.read_text())
    content_id = document.pop("content_id_sha256")
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == content_id


def test_shared_endpoint_and_both_ladders_produce_conservative_uncertainty(
        tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    executable = tmp_path / "FasterCap"
    board.write_text("board")
    executable.write_text("solver")
    calls = []
    monkeypatch.setattr(
        study, "extract_geometry",
        lambda board, output, tolerance: fake_geometry(tolerance),
    )

    def converged(geometry, solver_relative_error, executable):
        geometry_tolerance = geometry["geometry_tolerance_mm"]
        calls.append((geometry_tolerance, solver_relative_error))
        geometry_delta = {
            0.002: 2.0e-15, 0.001: 1.0e-15, 0.0005: 0.0,
        }[geometry_tolerance]
        solver_delta = {
            0.05: 2.5e-15, 0.025: 1.2e-15, 0.0125: 0.0,
        }[solver_relative_error]
        value = 100e-15 + geometry_delta + solver_delta
        matrix = [[value, -50e-15], [-50e-15, value]]
        return {
            "state": "numerically_converged_diagnostic",
            "manifest": f"g{geometry_tolerance}-a{solver_relative_error}.json",
            "raw_matrix_f": matrix,
            "matrix_f": matrix,
            "failures": [],
        }

    monkeypatch.setattr(study, "solve_rung", converged)
    _, report = study.run_study(board, tmp_path / "out", executable)
    assert len(calls) == 5
    endpoint = (
        study.PRODUCTION_GEOMETRY_MM,
        study.PRODUCTION_SOLVER_RELATIVE_ERROR,
    )
    assert calls.count(endpoint) == 1
    assert report["geometry_ladder"]["passed"] is True
    assert report["solver_ladder"]["passed"] is True
    assert report["state"] == "numerically_converged_diagnostic"
    assert np.max(report["uncertainty_f"]) == pytest.approx(1.3e-15)
    assert report["matrix_artifact"]
