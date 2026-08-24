#!/usr/bin/env python3
"""Bounded dual-axis Fugu2 FasterCap diagnostic convergence study."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from fastercap import FasterCapRunRejected, run_fastercap  # noqa: E402
from fastercap_reduction import load_geometry_manifest  # noqa: E402
from maxwell import (  # noqa: E402
    MaxwellMatrix,
    aggregate_ideal_shorts,
    maxwell_gate_results,
    maxwell_to_branches,
    raw_maxwell_gate_results,
    symmetrize_maxwell,
)

REPORT_FORMAT = "dcdc-fastercap-fugu-diagnostic-study-v1"
POLICY = "fastercap-pcb-gates-v1"
PCB_SHA256 = "32906787c3c5bfaf9b0efc0156b6ae40c0d2b29dd69c354e75a8ee8a8b0d3665"
GROUPS = ("SW=SW", "VIN=Solar+", "PGND=BuckGND")
CONDUCTOR_NAMES = ("SW", "VIN", "PGND")
PINNED_AREA_MM2 = {
    "SW": 246.740840474834,
    "VIN": 1027.438931275436,
    "PGND": 1556.0643058115102,
}
PINNED_COMPONENT_COUNT = {"SW": 5, "VIN": 2, "PGND": 3}
AREA_RELATIVE_LIMIT = 1e-4
GEOMETRY_RUNGS_MM = (0.002, 0.001, 0.0005)
SOLVER_RUNGS = (0.05, 0.025, 0.0125)
PRODUCTION_GEOMETRY_MM = GEOMETRY_RUNGS_MM[-1]
PRODUCTION_SOLVER_RELATIVE_ERROR = SOLVER_RUNGS[-1]
LADDER_ABSOLUTE_F = 1e-15
LADDER_RELATIVE = 0.02
RESOURCE_CLASS = "fugu_diagnostic"
RESOURCE_TIMEOUT_S = 20 * 60


def _sha256_bytes(content):
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path):
    with open(path, "rb") as stream:
        return _sha256_bytes(stream.read())


def _write_blob(directory, kind, content):
    digest = _sha256_bytes(content)
    path = Path(directory) / f"extraction.{kind}.{digest}.bin"
    path.write_bytes(content)
    return str(path), digest


def _write_report(output, report):
    canonical = json.dumps(
        report, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    digest = _sha256_bytes(canonical)
    document = dict(report)
    document["content_id_sha256"] = digest
    path = Path(output) / f"fugu-diagnostic-study.{digest}.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


def entrywise_gate(reference, candidate):
    reference = np.asarray(reference, dtype=float)
    candidate = np.asarray(candidate, dtype=float)
    allowance = (
        LADDER_ABSOLUTE_F
        + LADDER_RELATIVE * np.maximum(np.abs(reference), np.abs(candidate))
    )
    difference = np.abs(candidate - reference)
    return bool(np.all(difference <= allowance)), difference, allowance


def evaluate_ladder(records):
    differences = []
    allowances = []
    passed = []
    for coarse, fine in zip(records, records[1:]):
        result, difference, allowance = entrywise_gate(
            coarse["raw_matrix_f"], fine["raw_matrix_f"]
        )
        passed.append(result)
        differences.append(difference.tolist())
        allowances.append(allowance.tolist())
    return {
        "passed": len(records) >= 3 and all(passed),
        "differences_f": differences,
        "allowances_f": allowances,
    }


def validate_fugu_geometry(document, expected_tolerance_mm):
    failures = []
    if document.get("pcb_sha256") != PCB_SHA256:
        failures.append("PCB hash does not match pinned Fugu2 benchmark")
    if document.get("filled_zone_record_count") != 10:
        failures.append("filled-zone record count is not 10")
    if document.get("groups") != {
        "SW": ["SW"], "VIN": ["Solar+"], "PGND": ["BuckGND"]
    }:
        failures.append("conductor groups do not match pinned benchmark")
    if document.get("source_mode") != "filled_zones_only":
        failures.append("source mode is not filled_zones_only")
    if document.get("geometry_tolerance_mm") != expected_tolerance_mm:
        failures.append("geometry tolerance does not match requested rung")
    reduction = document.get("reduction", {})
    expected_reduction = {
        "representation": "effective_thickness",
        "effective_thickness_m": 34e-6,
        "anchor": "midplane",
        "source_mode": "filled_zones_only",
        "material_scope": "air_only",
        "fixture_id": None,
        "qualification_state": "outside_fixture_envelope",
        "physical_validation_authorized": False,
        "lifecycle_ceiling": "numerically_converged_diagnostic",
    }
    for key, expected in expected_reduction.items():
        if reduction.get(key) != expected:
            failures.append(f"reduction provenance field {key!r} does not match")
    assembly = reduction.get("electrical_assembly", {})
    if assembly.get("solver_policy") != "grouped":
        failures.append("solver policy is not grouped")
    if assembly.get("group_order") != list(CONDUCTOR_NAMES):
        failures.append("solver group order does not match pinned groups")
    solver_names = document.get("solver_conductor_names", [])
    assignments = assembly.get("assignments", {})
    expected_assignments = {name: name for name in CONDUCTOR_NAMES}
    if solver_names != list(CONDUCTOR_NAMES):
        failures.append("solver conductors do not match pinned groups")
    if assignments != expected_assignments:
        failures.append("solver assignments are not the pinned identity mapping")
    if document.get("component_count") != PINNED_COMPONENT_COUNT:
        failures.append("component census does not match pinned Fugu geometry")
    if document.get("solver_panel_count") != document.get("panel_count"):
        failures.append("solver panel census does not match grouped conductors")
    triangulation = document.get("triangulation", {})
    if triangulation != {
        "engine": "MeshPy Triangle constrained-quality",
        "minimum_angle_deg": 20.0,
        "boundary": "exact constrained polygon segments",
    }:
        failures.append("triangulation policy does not match the frozen quality mesh")
    source_thickness = reduction.get("source_thickness_m", {})
    if set(source_thickness) != set(CONDUCTOR_NAMES):
        failures.append("source thickness census does not match conductor groups")
    for name, values in source_thickness.items():
        if name not in CONDUCTOR_NAMES or len(values) != 2 or not np.allclose(
                values, 35e-6, rtol=0.0, atol=1e-15):
            failures.append(f"source thickness for {name!r} is not two 35 um layers")
    if reduction.get("source_panel_count") != document.get("panel_count"):
        failures.append("source panel census does not match geometry manifest")
    if reduction.get("candidate_panel_count") != document.get("panel_count"):
        failures.append("candidate panel census does not match geometry manifest")
    areas = document.get("area_mm2", {})
    for name, expected in PINNED_AREA_MM2.items():
        value = areas.get(name)
        if (value is None or abs(value - expected)
                > AREA_RELATIVE_LIMIT * expected):
            failures.append(f"area for {name!r} exceeds pinned benchmark gate")
    return tuple(failures)


def extract_geometry(board, output, tolerance_mm):
    rung_dir = Path(output) / f"geometry_{tolerance_mm:.9g}mm"
    rung_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(ROOT / "extract_capacitance.py"),
        str(Path(board).resolve()),
    ]
    for group in GROUPS:
        command.extend(("--group", group))
    command.extend((
        "--geometry-tolerance", f"{tolerance_mm:.17g}",
        "--representation", "effective-thickness",
        "--effective-thickness-um", "34",
        "--anchor", "midplane",
        "-o", str(rung_dir),
    ))
    completed = subprocess.run(command, capture_output=True, check=False)
    stdout_path, stdout_hash = _write_blob(
        rung_dir, "stdout", completed.stdout
    )
    stderr_path, stderr_hash = _write_blob(
        rung_dir, "stderr", completed.stderr
    )
    record = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": stdout_path,
        "stdout_sha256": stdout_hash,
        "stderr": stderr_path,
        "stderr_sha256": stderr_hash,
    }
    if completed.returncode != 0:
        record["failures"] = ["geometry extraction command failed"]
        return record
    manifests = list(rung_dir.glob(
        "filled_zones_effective_34um_midplane_air_only_diagnostic."
        "geometry.*.json"
    ))
    if len(manifests) != 1:
        record["failures"] = ["geometry extraction did not emit exactly one manifest"]
        return record
    try:
        document = load_geometry_manifest(manifests[0])
    except ValueError as error:
        record["failures"] = [f"geometry manifest rejected: {error}"]
        return record
    failures = validate_fugu_geometry(document, tolerance_mm)
    assembly = document["reduction"]["electrical_assembly"]
    record.update({
        "geometry_manifest": str(manifests[0]),
        "geometry_content_id": document["content_id_sha256"],
        "deck": document["deck"],
        "deck_sha256": document["deck_sha256"],
        "deck_manifest": document["deck_manifest"],
        "deck_manifest_sha256": document["deck_manifest_sha256"],
        "area_mm2": document["area_mm2"],
        "panel_count": document["panel_count"],
        "solver_panel_count": document["solver_panel_count"],
        "solver_conductor_names": document["solver_conductor_names"],
        "ideal_short_assignments": assembly.get("assignments", {}),
        "ideal_short_group_order": assembly.get("group_order", []),
        "failures": list(failures),
    })
    return record


def aggregate_component_matrix(geometry, raw_values):
    names = tuple(geometry["solver_conductor_names"])
    base = MaxwellMatrix(names, raw_values)
    grouped_raw = aggregate_ideal_shorts(
        base,
        geometry["ideal_short_assignments"],
        tuple(geometry["ideal_short_group_order"]),
    )
    raw_gates = raw_maxwell_gate_results(grouped_raw)
    failures = [
        f"aggregated raw matrix gate failed: {name}"
        for name, result in raw_gates.items() if not result["passed"]
    ]
    grouped = None
    downstream_gates = None
    if not failures:
        try:
            grouped = symmetrize_maxwell(grouped_raw)
            downstream_gates = maxwell_gate_results(
                grouped, rtol=0.0, atol=0.0
            )
            maxwell_to_branches(grouped)
        except ValueError as error:
            failures.append(f"aggregated matrix rejected: {error}")
    return grouped_raw, grouped, raw_gates, downstream_gates, failures


def solve_rung(geometry, solver_relative_error, executable):
    record = {
        "geometry_tolerance_mm": geometry["geometry_tolerance_mm"],
        "solver_relative_error": solver_relative_error,
        "deck": geometry["deck"],
    }
    try:
        result = run_fastercap(
            geometry["deck"],
            conductor_names=tuple(geometry["solver_conductor_names"]),
            executable=executable,
            relative_error=solver_relative_error,
            timeout=RESOURCE_TIMEOUT_S,
            resource_class=RESOURCE_CLASS,
        )
    except FasterCapRunRejected as error:
        record.update({
            "state": "rejected_diagnostic",
            "manifest": str(error.manifest_path),
            "failures": list(error.transcript.failures),
        })
        return record
    final = result.transcript.iterations[-1]
    observation = result.transcript.resource_observations
    grouped_raw, grouped, raw_gates, downstream_gates, failures = (
        aggregate_component_matrix(geometry, final.matrix.values)
    )
    record.update({
        "state": (
            "numerically_converged_diagnostic" if not failures
            else "rejected_diagnostic"
        ),
        "manifest": str(result.manifest_path),
        "base_raw_matrix_f": final.matrix.values.tolist(),
        "base_matrix_f": result.matrix.values.tolist(),
        "raw_matrix_f": grouped_raw.values.tolist(),
        "matrix_f": None if grouped is None else grouped.values.tolist(),
        "aggregation_raw_gate_results": raw_gates,
        "aggregation_downstream_gate_results": downstream_gates,
        "initial_panels": result.transcript.initial_panels,
        "refined_panels": final.refined_panels,
        "solver_runtime_s": result.transcript.solver_runtime_s,
        "peak_rss_bytes": observation.get("peak_rss_bytes"),
        "directory_bytes": observation.get("directory_bytes"),
        "failures": failures,
    })
    return record


def run_study(board, output, executable):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    geometry = {}
    for tolerance in GEOMETRY_RUNGS_MM:
        record = extract_geometry(board, output, tolerance)
        record["geometry_tolerance_mm"] = tolerance
        geometry[tolerance] = record
    geometry_failures = [
        failure
        for record in geometry.values()
        for failure in record.get("failures", [])
    ]
    runs = {}
    endpoint = (PRODUCTION_GEOMETRY_MM, PRODUCTION_SOLVER_RELATIVE_ERROR)
    run_order = [
        endpoint,
        (GEOMETRY_RUNGS_MM[0], PRODUCTION_SOLVER_RELATIVE_ERROR),
        (GEOMETRY_RUNGS_MM[1], PRODUCTION_SOLVER_RELATIVE_ERROR),
        (PRODUCTION_GEOMETRY_MM, SOLVER_RUNGS[0]),
        (PRODUCTION_GEOMETRY_MM, SOLVER_RUNGS[1]),
    ]
    if not geometry_failures:
        for key in run_order:
            runs[key] = solve_rung(geometry[key[0]], key[1], executable)
            if runs[key]["state"] != "numerically_converged_diagnostic":
                break

    geometry_ladder_records = [
        runs.get((tolerance, PRODUCTION_SOLVER_RELATIVE_ERROR))
        for tolerance in GEOMETRY_RUNGS_MM
    ]
    solver_ladder_records = [
        runs.get((PRODUCTION_GEOMETRY_MM, tolerance))
        for tolerance in SOLVER_RUNGS
    ]
    complete_geometry = all(
        record is not None
        and record["state"] == "numerically_converged_diagnostic"
        for record in geometry_ladder_records
    )
    complete_solver = all(
        record is not None
        and record["state"] == "numerically_converged_diagnostic"
        for record in solver_ladder_records
    )
    geometry_ladder = (
        evaluate_ladder(geometry_ladder_records) if complete_geometry
        else {"passed": False, "differences_f": [], "allowances_f": []}
    )
    solver_ladder = (
        evaluate_ladder(solver_ladder_records) if complete_solver
        else {"passed": False, "differences_f": [], "allowances_f": []}
    )
    all_runs_passed = complete_geometry and complete_solver
    qualified = (
        not geometry_failures and all_runs_passed
        and geometry_ladder["passed"] and solver_ladder["passed"]
    )
    all_differences = (
        geometry_ladder["differences_f"] + solver_ladder["differences_f"]
    )
    uncertainty = None
    if qualified:
        uncertainty = np.max(np.asarray(all_differences), axis=0).tolist()
    report = {
        "format": REPORT_FORMAT,
        "gate_policy": POLICY,
        "board": str(Path(board).resolve()),
        "board_sha256": _sha256_file(board),
        "executable": str(Path(executable).resolve()),
        "executable_sha256": _sha256_file(executable),
        "implementation_sha256": {
            "study": _sha256_file(__file__),
            "extract_capacitance": _sha256_file(ROOT / "extract_capacitance.py"),
            "fastercap_reduction": _sha256_file(
                ROOT / "lib" / "fastercap_reduction.py"
            ),
            "kicad_fastercap": _sha256_file(
                ROOT / "lib" / "kicad_fastercap.py"
            ),
            "kicad_dump": _sha256_file(
                ROOT / "lib" / "kicad_fastercap_dump.py"
            ),
        },
        "geometry_rungs_mm": GEOMETRY_RUNGS_MM,
        "solver_rungs": SOLVER_RUNGS,
        "production_endpoint": {
            "geometry_tolerance_mm": PRODUCTION_GEOMETRY_MM,
            "solver_relative_error": PRODUCTION_SOLVER_RELATIVE_ERROR,
        },
        "geometry": {str(key): value for key, value in geometry.items()},
        "runs": {
            f"g={key[0]:.9g},a={key[1]:.9g}": value
            for key, value in runs.items()
        },
        "geometry_ladder": geometry_ladder,
        "solver_ladder": solver_ladder,
        "uncertainty_f": uncertainty,
        "state": (
            "numerically_converged_diagnostic" if qualified
            else "rejected_diagnostic"
        ),
        "limitations": [
            "filled zones only",
            "air only; no dielectric interfaces",
            "outside the qualified synthetic-fixture envelope",
            "not a physical PCB capacitance model",
            "cannot enter Slice 6",
        ],
    }
    if qualified:
        endpoint_record = runs[endpoint]
        matrix_payload = {
            "names": CONDUCTOR_NAMES,
            "raw_values_f": endpoint_record["raw_matrix_f"],
            "values_f": endpoint_record["matrix_f"],
            "uncertainty_f": uncertainty,
        }
        canonical = json.dumps(
            matrix_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        digest = _sha256_bytes(canonical)
        matrix_path = output / f"fugu-diagnostic-matrix.{digest}.json"
        matrix_path.write_text(json.dumps(
            {**matrix_payload, "content_id_sha256": digest},
            indent=2, sort_keys=True,
        ) + "\n")
        report["matrix_artifact"] = str(matrix_path)
        report["matrix_content_id_sha256"] = digest
    path = _write_report(output, report)
    return path, report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run fixed dual-axis Fugu2 diagnostic convergence ladders"
    )
    parser.add_argument("board")
    parser.add_argument("--executable", required=True)
    parser.add_argument("-o", "--out", required=True)
    args = parser.parse_args(argv)
    path, report = run_study(args.board, args.out, args.executable)
    print(f"report: {path}")
    print(f"state: {report['state']}")
    if report.get("matrix_artifact"):
        print(f"matrix: {report['matrix_artifact']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
