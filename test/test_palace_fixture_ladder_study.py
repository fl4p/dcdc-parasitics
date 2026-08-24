#!/usr/bin/env python3
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import experiments.palace_fixture_ladder_study as ladder_module  # noqa: E402
from experiments.palace_fixture_ladder_study import (  # noqa: E402
    RUNGS,
    _bound_artifact_failures,
    _entrywise_gate,
    _evaluate_fixture,
    _outer_row_sum_gate,
    _p_shared_mesh_gate,
)
import palace as palace_module  # noqa: E402
from palace import (  # noqa: E402
    MESH_LIMITS,
    RESOURCE_LIMITS,
    PalaceMaterial,
    PalaceTerminal,
    write_palace_config,
)
from palace_resources import PalaceResourceProjection, ResourceBound  # noqa: E402
from lib.provenance import bytes_sha256, canonical_sha256, file_sha256  # noqa: E402


def _resource_projection(retained):
    return PalaceResourceProjection(
        wall_time_s=ResourceBound(100.0, 400.0),
        cpu_time_s=ResourceBound(100.0, 800.0),
        vector_residency_bytes=ResourceBound(retained, 1024**3),
        hierarchy_operator_solver_bytes=ResourceBound(1, 4 * 1024**3),
        retained_terminal_vectors_bytes=ResourceBound(retained, 1024**3),
        peak_rss_bytes=ResourceBound(retained, 9 * 1024**3),
        matrix_output_bytes=ResourceBound(100, 512 * 1024**2),
        logging_output_bytes=ResourceBound(100, 512 * 1024**2),
        checkpoint_write_bytes=ResourceBound(100, 512 * 1024**2),
        checkpoint_read_bytes=ResourceBound(0, 512 * 1024**2),
        uncertainty_reasons=("conservative fixture-test bounds",),
        observation_sha256=("a" * 64,),
    )


def _fixture_stdout():
    return (
        b"PALACE_MILESTONE setup_start solver=electrostatic checkpoint_enabled=0\n"
        b"PALACE_MILESTONE setup_end campaign_digest=disabled prefix_before=0 "
        b"loaded_rhs=[] new_rhs=[1,2]\n"
        b"Computing electrostatic fields for 2 terminal boundaries\n"
        b"PALACE_MILESTONE rhs_start rhs=1 terminal=1 action=solve\n"
        b" 0 KSP residual norm ||r||_B = 1.0e+00\n"
        b"PCG solver converged in 1 iterations\n"
        b"Explicit residual ||b-Ax||/||b|| = 1.0e-12 (target = 1.0e-10)\n"
        b"PALACE_MILESTONE residual_accepted rhs=1 terminal=1 source=solved value=1.0e-12\n"
        b"PALACE_MILESTONE reaction_accepted rhs=1 terminal=1 source=solved entries=2\n"
        b"PALACE_MILESTONE rhs_solved rhs=1 terminal=1 solves=1 iterations=1\n"
        b"PALACE_MILESTONE rhs_start rhs=2 terminal=2 action=solve\n"
        b" 0 KSP residual norm ||r||_B = 1.0e+00\n"
        b"PCG solver converged in 1 iterations\n"
        b"Explicit residual ||b-Ax||/||b|| = 2.0e-12 (target = 1.0e-10)\n"
        b"PALACE_MILESTONE residual_accepted rhs=2 terminal=2 source=solved value=2.0e-12\n"
        b"PALACE_MILESTONE reaction_accepted rhs=2 terminal=2 source=solved entries=2\n"
        b"PALACE_MILESTONE rhs_solved rhs=2 terminal=2 solves=1 iterations=1\n"
        b"PALACE_MILESTONE finalization_start terminal_count=2 prefix_before=0 prefix_after=0\n"
        b"PALACE_MILESTONE finalization_end prefix_before=0 prefix_after=0 total_solves=2 "
        b"total_iterations=2 checkpoint_read_bytes=0 checkpoint_written_bytes=0 "
        b"checkpoint_retained_bytes=0\n"
        b"Elapsed Time Report (s)\nPeak Memory\n"
    )


def _report(name, matrix, fixture="parallel_plate_air"):
    rung = next(item for item in RUNGS if item.name == name)
    fixture_definition = ladder_module._fixture(fixture)
    conductors = ladder_module._conductors(fixture_definition)
    outer = ladder_module._outer_bounds(
        fixture_definition, conductors, rung.outer_scale
    )
    near_size = ladder_module._near_mesh_size(fixture_definition, rung.mesh_scale)
    model_minimum, model_maximum = ladder_module._model_bounds(
        fixture_definition, conductors
    )
    model_span = max(
        high - low for low, high in zip(model_minimum, model_maximum)
    )
    far_size = max(near_size, model_span / 5.0 * rung.far_mesh_scale)
    transition_distance = max(4.0 * near_size, 0.5 * model_span)
    dielectrics = []
    if fixture_definition.dielectric_bounds_m is not None:
        dielectrics = [{
            "name": "fixture_dielectric",
            "bounds": {
                "minimum": list(fixture_definition.dielectric_bounds_m[0]),
                "maximum": list(fixture_definition.dielectric_bounds_m[1]),
            },
            "relative_permittivity": fixture_definition.relative_permittivity,
        }]
    terminal_attributes = [
        [conductor.name, 101 + index]
        for index, conductor in enumerate(conductors)
    ]
    material_attributes = [
        ["outer", 1, 1.0],
        *[[value["name"], 2 + index, value["relative_permittivity"]]
          for index, value in enumerate(dielectrics)],
    ]
    provenance = {
        "mesh_sha256": "a" * 64,
        "gmsh": {
            "version": "4.15.2",
            "python_module": "/gmsh.py",
            "python_module_sha256": "b" * 64,
            "library": "/libgmsh",
            "library_sha256": "c" * 64,
        },
        "mesh_parameters": {
            "mesh_size_near_m": near_size,
            "mesh_size_far_m": far_size,
            "transition_distance_m": transition_distance,
            "algorithm_3d": 10,
            "threads": 1,
            "msh_version": 2.2,
        },
        "outer_bounds": json.loads(json.dumps(asdict(outer))),
        "outer_permittivity": 1.0,
        "conductors": [
            json.loads(json.dumps(asdict(value))) for value in conductors
        ],
        "dielectrics": dielectrics,
        "terminal_attributes": terminal_attributes,
        "material_attributes": material_attributes,
        "ground_attribute": 9999,
        "reference_semantics": {
            "kind": "finite_outer_dirichlet_approximation",
            "convergence_ladder_required": True,
            "not_a_circuit_node": True,
        },
        "node_count": 100,
        "edge_count": 600,
        "face_count": 1000,
        "tetrahedron_count": 500,
    }
    resource = rung.resource_class
    limits = {**MESH_LIMITS[resource], **asdict(RESOURCE_LIMITS[resource])}
    reference = {
        "kind": "finite_outer_dirichlet_approximation",
        "convergence_ladder_required": True,
        "not_a_circuit_node": True,
        "outer_bounds_m": provenance["outer_bounds"],
        "outer_scale": rung.outer_scale,
    }
    mesh_identity = {
        "mesh_path": "/fixture.msh",
        "mesh_sha256": "a" * 64,
        "manifest_path": "/fixture.msh.manifest.json",
        "manifest_sha256": "d" * 64,
        "provenance_sha256": "e" * 64,
        "provenance": provenance,
    }
    config_provenance = {
        "checkpoint": None,
        "config_sha256": "f" * 64,
        "finite_reference": copy.deepcopy(reference),
        "gate_policy": "palace-electrostatic-pcb-gates-v2",
        "ground_attribute": 9999,
        "linear_tolerance": 1e-10,
        "explicit_residual_tolerance": 1e-10,
        "materials": [
            {"name": material_name, "attributes": [attribute],
             "relative_permittivity": permittivity}
            for material_name, attribute, permittivity in material_attributes
        ],
        "maximum_iterations": 500,
        "mesh_manifest_sha256": mesh_identity["manifest_sha256"],
        "mesh_sha256": mesh_identity["mesh_sha256"],
        "order": rung.order,
        "terminals": [
            {"index": index, "name": terminal_name, "attribute": attribute}
            for index, (terminal_name, attribute) in enumerate(
                terminal_attributes, 1
            )
        ],
    }
    config_identity = {
        "config_path": "/config.json",
        "config_sha256": "f" * 64,
        "manifest_path": "/config.json.manifest.json",
        "manifest_sha256": "1" * 64,
        "provenance_sha256": "2" * 64,
        "provenance": config_provenance,
    }
    return {
        "fixture": fixture,
        "scope": fixture_definition.scope,
        "build_manifest": "/build.json",
        "outer_scale": rung.outer_scale,
        "mesh_scale": rung.mesh_scale,
        "far_mesh_scale": rung.far_mesh_scale,
        "order": rung.order,
        "resource_class": resource,
        "near_mesh_size_m": near_size,
        "far_mesh_size_m": far_size,
        "transition_distance_m": transition_distance,
        "node_count": 100,
        "tetrahedron_count": 500,
        "reference_semantics": reference,
        "lifecycle": "numerically_converged_diagnostic",
        "raw_matrix_f": np.asarray(matrix, dtype=float).tolist(),
        "mesh_identity": mesh_identity,
        "config_identity": config_identity,
        "run_identity": {
            "path": "/run.json",
            "sha256": "3" * 64,
            "content_sha256": "4" * 64,
            "resource_class": resource,
            "resource_limits": limits,
            "config_manifest": config_identity["manifest_path"],
            "config_manifest_sha256": config_identity["manifest_sha256"],
            "artifacts": {
                config_identity["config_path"]: config_identity["config_sha256"],
                mesh_identity["mesh_path"]: mesh_identity["mesh_sha256"],
                mesh_identity["manifest_path"]: mesh_identity["manifest_sha256"],
            },
        },
        "content_sha256": "5" * 64,
        "rung": asdict(rung),
    }


def _write_bound_report(directory):
    directory.mkdir()
    gmsh_module = directory / "gmsh.py"
    gmsh_library = directory / "libgmsh"
    mesh_path = directory / "fixture.msh"
    gmsh_module.write_text("module")
    gmsh_library.write_text("library")
    mesh_path.write_text(
        "$MeshFormat\n2.2 0 8\n$EndMeshFormat\n"
        "$Nodes\n4\n1 0 0 0\n2 1 0 0\n3 0 1 0\n4 0 0 1\n$EndNodes\n"
        "$Elements\n1\n1 4 2 1 1 1 2 3 4\n$EndElements\n"
    )
    item = _report(
        "p2", [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    )
    provenance = item["mesh_identity"]["provenance"]
    provenance.update({
        "mesh_sha256": file_sha256(mesh_path),
        "node_count": 4,
        "edge_count": 6,
        "face_count": 4,
        "tetrahedron_count": 1,
    })
    item.update({
        "node_count": 4,
        "edge_count": 6,
        "face_count": 4,
        "tetrahedron_count": 1,
    })
    provenance["gmsh"].update({
        "python_module": str(gmsh_module),
        "python_module_sha256": file_sha256(gmsh_module),
        "library": str(gmsh_library),
        "library_sha256": file_sha256(gmsh_library),
    })
    mesh_manifest = {
        "format": "dcdc-palace-mesh-v2",
        "provenance": provenance,
        "provenance_sha256": canonical_sha256(provenance),
    }
    manifest_path = directory / "fixture.msh.manifest.json"
    manifest_path.write_text(json.dumps(mesh_manifest, sort_keys=True))
    item["mesh_identity"].update({
        "mesh_path": str(mesh_path),
        "mesh_sha256": file_sha256(mesh_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
        "provenance_sha256": mesh_manifest["provenance_sha256"],
    })
    config_path = directory / "config.json"
    config_manifest_path = write_palace_config(
        config_path,
        mesh_path=mesh_path,
        mesh_manifest_path=manifest_path,
        output_directory=directory / "postpro",
        terminals=tuple(
            PalaceTerminal(index, terminal_name, attribute)
            for index, (terminal_name, attribute) in enumerate(
                provenance["terminal_attributes"], 1
            )
        ),
        materials=tuple(
            PalaceMaterial(material_name, (attribute,), permittivity)
            for material_name, attribute, permittivity
            in provenance["material_attributes"]
        ),
        ground_attribute=9999,
        finite_reference=item["reference_semantics"],
        order=item["order"],
    )
    config_manifest = json.loads(config_manifest_path.read_text())
    config = item["config_identity"]
    config.update({
        "config_path": str(config_path),
        "config_sha256": file_sha256(config_path),
        "manifest_path": str(config_manifest_path),
        "manifest_sha256": file_sha256(config_manifest_path),
        "provenance_sha256": config_manifest["provenance_sha256"],
        "provenance": config_manifest["provenance"],
    })
    output = directory / "postpro"
    output.mkdir()
    resolved_path = output / "config_resolved.json"
    resolved_manifest = palace_module.load_palace_config_manifest(config_manifest_path)
    resolved_path.write_text(json.dumps(
        palace_module._expected_resolved_config(resolved_manifest)
    ))
    event_counts = {"Total": 1, "Estimation": 0, "LinearSolve": 2, "Solve": 0}
    event_values = {
        name: (0.01 if count else 0.0) for name, count in event_counts.items()
    }
    event_values["Total"] = 0.05
    memory_growth = {
        name: dict(event_values) for name in ("Max", "Min", "Sum")
    }
    memory = {name: 0.0005 for name in ("Average", "Max", "Min", "Total")}
    palace_metadata = {
        "ElapsedTime": {"Counts": event_counts, "Durations": event_values},
        "GitTag": "v0.17.0-g86810b19-dirty",
        "Problem": {
            "DegreesOfFreedom": 10,
            "MPISize": 1,
            "MeshElements": item["tetrahedron_count"],
            "MultigridDegreesOfFreedom": [4, 10],
        },
        "LinearSolver": {"TotalIts": 2, "TotalSolves": 2},
        "PeakMemoryGrowthMegabytes": memory_growth,
        "PeakMemoryMegabytes": memory,
        "PeakNodeMemoryGrowthMegabytes": memory_growth,
        "PeakNodeMemoryMegabytes": memory,
    }
    palace_path = output / "palace.json"
    palace_path.write_text(json.dumps(palace_metadata))
    item["downstream_matrix_f"] = item["raw_matrix_f"]
    matrices = (
        ("terminal-Craw.csv", "C_raw", item["raw_matrix_f"]),
        ("terminal-C.csv", "C", item["downstream_matrix_f"]),
    )
    for filename, label, matrix in matrices:
        lines = [f"i,{label}[i][1] (F),{label}[i][2] (F)"]
        lines.extend(
            f"{index},{row[0]:.17e},{row[1]:.17e}"
            for index, row in enumerate(matrix, 1)
        )
        (output / filename).write_text("\n".join(lines) + "\n")
    stdout = _fixture_stdout()
    stderr = b""
    stdout_sha256 = bytes_sha256(stdout)
    stderr_sha256 = bytes_sha256(stderr)
    stdout_path = directory / f"config.json.stdout.{stdout_sha256}.bin"
    stderr_path = directory / f"config.json.stderr.{stderr_sha256}.bin"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    build_path = directory / "build.json"
    build = {
        "provenance_sha256": "test-build",
        "provenance": {
            "source_commit": "86810b1909f3e05e0bf1fd78f485e59d55582b6d"
        },
    }
    build_path.write_text(json.dumps(build))
    item["build_manifest"] = str(build_path)
    executable = directory / "palace"
    executable.write_text("binary")
    executable = executable.resolve()
    mpi_launcher = shutil.which("mpirun")
    assert mpi_launcher is not None
    mpi_launcher = str(Path(mpi_launcher).resolve())
    implementation = palace_module._implementation_identity()
    binaries = {str(executable): file_sha256(executable)}
    workload = palace_module._workload_record(
        resolved_manifest,
        config_manifest_path=config_manifest_path,
        build_manifest_path=build_path,
        executable=executable,
        processes=1,
        implementation=implementation,
        binaries=binaries,
        mpi_launcher=mpi_launcher,
    )
    workload_path = directory / (
        f"config.json.workload.{workload['content_sha256']}.json"
    )
    workload_path.write_text(json.dumps(workload, indent=2, sort_keys=True) + "\n")
    execution_snapshot = palace_module.prepare_execution_snapshot(
        resolved_manifest,
        workload,
        executable=executable,
        binaries=tuple(Path(path) for path in binaries),
        mpi_launcher=mpi_launcher,
    )
    snapshot_executable = next(
        record["snapshot"] for record in execution_snapshot["inputs"]
        if record["role"] == "executable"
    )
    command = [snapshot_executable, "-np", "1", "config.json"]
    policy = palace_module._trusted_resource_policy()
    retained = workload["topology_workload"][
        "retained_terminal_vector_bytes_lower_bound"
    ]
    projection = _resource_projection(retained)
    decision = palace_module.build_palace_resource_decision(
        palace_workload=workload,
        projection=projection,
        profiles=policy["profile_objects"],
        authorized_profile_ids=policy["authorized_profile_tuple"],
        policy_id=policy["policy_id"],
        policy_sha256=policy["policy_sha256"],
        validator_sha256=policy["validator_sha256"],
        minimum_headroom_ratio=policy["minimum_headroom_ratio"],
    )
    decision_path = directory / (
        f"config.json.resource-decision.{decision['content_sha256']}.json"
    )
    decision_path.write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n")
    artifact_paths = (
        config_path, mesh_path, manifest_path, workload_path, decision_path,
        resolved_path, palace_path,
        output / "terminal-Craw.csv", output / "terminal-C.csv",
        stdout_path, stderr_path,
        *(Path(record["snapshot"]) for record in execution_snapshot["inputs"]),
    )
    artifacts = {str(path): file_sha256(path) for path in artifact_paths}
    resource_class = item["run_identity"]["resource_class"]
    process_limits = asdict(RESOURCE_LIMITS[resource_class])
    run_payload = {
        "gate_policy": "palace-electrostatic-pcb-gates-v2",
        "resource_class": resource_class,
        "resource_limits": item["run_identity"]["resource_limits"],
        "resource_enforcement": "portable_monitor_not_native_containment",
        "resource_decision": str(decision_path),
        "resource_decision_sha256": file_sha256(decision_path),
        "config_manifest": str(config_manifest_path),
        "config_manifest_sha256": file_sha256(config_manifest_path),
        "build_manifest": str(build_path),
        "build_manifest_sha256": file_sha256(build_path),
        "build_provenance_sha256": "test-build",
        "binaries": binaries,
        "implementation_files": implementation,
        "mpi_launcher": mpi_launcher,
        "mpi_launcher_sha256": file_sha256(mpi_launcher),
        "workload": str(workload_path),
        "workload_sha256": file_sha256(workload_path),
        "execution_snapshot": execution_snapshot,
        "artifacts": artifacts,
        "runtime_metadata": palace_metadata,
        "command": command,
        "execution": {
            "command": command,
            "cwd": execution_snapshot["root"],
            "returncode": 0,
            "elapsed_s": 0.1,
            "peak_rss_bytes": 1024,
            "output_bytes": len(stdout),
            "directory_growth_bytes": 100,
            "max_refined_panels": 0,
            "max_gmres_iteration": 0,
            "limit_failures": [],
            "limits": process_limits,
            "stream_events": [{
                "source": "stdout",
                "byte_offset": 0,
                "byte_count": len(stdout),
                "monotonic_ns": 1,
                "sha256": bytes_sha256(stdout),
            }],
            "monitor_events": [
                {"event": "prelaunch", "monotonic_ns": 0, "detail": None},
                {"event": "process_started", "monotonic_ns": 1,
                 "detail": "test"},
                {"event": "process_exited", "monotonic_ns": 100_000_000,
                 "detail": "0"},
            ],
            "resource_samples": [{
                "monotonic_ns": 100_000_000,
                "peak_rss_bytes": 1024,
                "output_bytes": len(stdout),
                "directory_growth_bytes": 100,
                "max_refined_panels": 0,
                "max_gmres_iteration": 0,
            }],
        },
        "residuals": [
            {"terminal": {
                "index": index, "name": terminal_name, "attribute": attribute,
             }, "relative_residual": index * 1e-12, "target": 1e-10}
            for index, (terminal_name, attribute) in enumerate(
                provenance["terminal_attributes"], 1
            )
        ],
        "failures": [],
        "lifecycle": "numerically_converged_diagnostic",
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
    }
    run_payload["execution"]["palace_progress_events"] = (
        palace_module._palace_progress_events(
            stdout, run_payload["execution"]["stream_events"]
        )
    )
    run_content = canonical_sha256(run_payload)
    run_path = directory / "run.json"
    run_path.write_text(json.dumps({
        "format": "dcdc-palace-run-v3",
        **run_payload,
        "content_sha256": run_content,
    }, sort_keys=True))
    item["run_identity"].update({
        "path": str(run_path),
        "sha256": file_sha256(run_path),
        "content_sha256": run_content,
        "config_manifest": str(config_manifest_path),
        "config_manifest_sha256": file_sha256(config_manifest_path),
        "artifacts": artifacts,
    })
    item.pop("content_sha256")
    item["content_sha256"] = canonical_sha256(item)
    report_path = directory / "report.json"
    report_path.write_text(json.dumps(item, sort_keys=True))
    item["report_path"] = str(report_path)
    item["report_sha256"] = file_sha256(report_path)
    return item


def _patch_test_build_validation(monkeypatch):
    monkeypatch.setattr(
        palace_module, "RESOURCE_AUTHORIZED_OBSERVATION_SHA256", ("a" * 64,)
    )
    monkeypatch.setattr(
        palace_module,
        "validate_palace_build_manifest",
        lambda path, *, executable: json.loads(Path(path).read_text()),
    )
    monkeypatch.setattr(
        ladder_module, "validate_palace_mesh_content", lambda path, provenance: None
    )


def test_bound_artifacts_reject_stale_or_tampered_files(tmp_path, monkeypatch):
    _patch_test_build_validation(monkeypatch)
    clean = _write_bound_report(tmp_path / "clean")
    assert _bound_artifact_failures(clean) == []
    targets = (
        ("report", "report_path"),
        ("run", ("run_identity", "path")),
        ("manifest", ("mesh_identity", "manifest_path")),
        ("mesh", ("mesh_identity", "mesh_path")),
        ("config", ("config_identity", "config_path")),
        ("config_manifest", ("config_identity", "manifest_path")),
        ("gmsh_module", ("mesh_identity", "provenance", "gmsh", "python_module")),
        ("gmsh_library", ("mesh_identity", "provenance", "gmsh", "library")),
        ("resolved", ("config_identity", "config_path")),
        ("palace_metadata", ("config_identity", "config_path")),
        ("raw_csv", ("config_identity", "config_path")),
        ("standard_csv", ("config_identity", "config_path")),
        ("stdout", ("config_identity", "config_path")),
        ("stderr", ("config_identity", "config_path")),
    )
    for name, keys in targets:
        item = _write_bound_report(tmp_path / name)
        if isinstance(keys, str):
            path = item[keys]
        else:
            value = item
            for key in keys:
                value = value[key]
            path = str(value)
        derived = {
            "resolved": "postpro/config_resolved.json",
            "palace_metadata": "postpro/palace.json",
            "raw_csv": "postpro/terminal-Craw.csv",
            "standard_csv": "postpro/terminal-C.csv",
        }
        if name in ("stdout", "stderr"):
            marker = f".{name}."
            path = next(
                artifact for artifact in item["run_identity"]["artifacts"]
                if marker in Path(artifact).name
            )
        elif name in derived:
            path = str(Path(path).parent / derived[name])
        Path(path).write_bytes(Path(path).read_bytes() + b"tampered")
        assert _bound_artifact_failures(item)


def test_malformed_outer_reports_container_rejects_without_exception():
    result = _evaluate_fixture("parallel_plate_air", None, verify_artifacts=False)
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["axes"] == {}
    assert result["uncertainty_f"] is None


@pytest.mark.parametrize("mutation", [
    "scope", "outer_bounds", "outer_permittivity", "conductor", "material",
])
def test_named_fixture_semantics_are_reconstructed_before_matrix_access(mutation):
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    reports = [_report(item.name, matrix) for item in RUNGS]
    report = next(item for item in reports if item["rung"]["name"] == "outer_mid")
    provenance = report["mesh_identity"]["provenance"]
    if mutation == "scope":
        report["scope"] = "changed"
    elif mutation == "outer_bounds":
        provenance["outer_bounds"]["maximum"][0] += 1e-6
        report["reference_semantics"]["outer_bounds_m"] = copy.deepcopy(
            provenance["outer_bounds"]
        )
        report["config_identity"]["provenance"]["finite_reference"][
            "outer_bounds_m"] = copy.deepcopy(provenance["outer_bounds"])
    elif mutation == "outer_permittivity":
        provenance["outer_permittivity"] = 2.0
        provenance["material_attributes"][0][2] = 2.0
        report["config_identity"]["provenance"]["materials"][0][
            "relative_permittivity"] = 2.0
    elif mutation == "conductor":
        provenance["conductors"][0]["rings"][0][0][0] += 1e-6
    else:
        provenance["material_attributes"][0][2] = 2.0
        provenance["outer_permittivity"] = 2.0
        report["config_identity"]["provenance"]["materials"][0][
            "relative_permittivity"] = 2.0
    report["raw_matrix_f"] = "must not be inspected"
    result = _evaluate_fixture(
        "parallel_plate_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["axes"] == {}
    assert result["uncertainty_f"] is None


def test_ladder_has_three_h_two_p_and_three_outer_rungs():
    assert len(RUNGS) == 8
    names = {item.name for item in RUNGS}
    assert names == {
        "h_coarse", "h_medium", "h_fine", "p2", "p3",
        "outer_near", "outer_mid", "outer_far",
    }
    p_rungs = [item for item in RUNGS if item.axis == "p"]
    assert all(item.mesh_scale == 0.5 and item.far_mesh_scale == 1.0
               for item in p_rungs)
    assert all(item.resource_class == "pcb_diagnostic" for item in p_rungs)
    assert all(item.resource_class == "synthetic"
               for item in RUNGS if item.axis != "p")


def test_ladder_resource_profiles_use_preregistered_limits():
    assert MESH_LIMITS["pcb_diagnostic"] == {
        "nodes": 10_000_000, "tetrahedra": 50_000_000
    }
    synthetic = RESOURCE_LIMITS["synthetic"]
    assert synthetic.wall_time_s == 5 * 60.0
    assert synthetic.peak_rss_bytes == 8 * 1024**3
    assert synthetic.output_bytes == 1024**3
    diagnostic = RESOURCE_LIMITS["pcb_diagnostic"]
    assert diagnostic.wall_time_s == 30 * 60.0
    assert diagnostic.peak_rss_bytes == 24 * 1024**3
    assert diagnostic.output_bytes == 10 * 1024**3


def test_entrywise_ladder_gate_uses_fixed_two_percent_plus_femttofarad():
    matrix = np.array([[3e-12, -2e-12], [-2e-12, 3e-12]])
    assert _entrywise_gate(matrix, matrix * 1.01)["passed"] is True
    assert _entrywise_gate(matrix, matrix * 1.10)["passed"] is False


def test_outer_row_sum_gate_accepts_monotone_or_shrinking_sequences():
    reports = [
        {"raw_matrix_f": [[3.0, -2.0], [-2.0, 4.0]]},
        {"raw_matrix_f": [[3.2, -2.0], [-2.0, 3.8]]},
        {"raw_matrix_f": [[3.3, -2.0], [-2.0, 3.9]]},
    ]
    result = _outer_row_sum_gate(reports)
    assert result["passed"] is True
    assert result["rows"][0]["monotone"] is True
    assert result["rows"][1]["shrinking_increments"] is True


@pytest.mark.parametrize("path", [
    ("mesh_identity", "mesh_sha256"),
    ("mesh_identity", "manifest_sha256"),
    ("mesh_identity", "provenance_sha256"),
    ("mesh_identity", "provenance", "gmsh", "version"),
    ("mesh_identity", "provenance", "gmsh", "python_module_sha256"),
    ("mesh_identity", "provenance", "gmsh", "library_sha256"),
    ("mesh_identity", "provenance", "mesh_parameters", "mesh_size_near_m"),
    ("mesh_identity", "provenance", "mesh_parameters", "mesh_size_far_m"),
    ("mesh_identity", "provenance", "mesh_parameters", "transition_distance_m"),
    ("mesh_identity", "provenance", "mesh_parameters", "algorithm_3d"),
    ("mesh_identity", "provenance", "mesh_parameters", "threads"),
    ("mesh_identity", "provenance", "mesh_parameters", "msh_version"),
    ("mesh_identity", "provenance", "outer_bounds", "maximum"),
    ("mesh_identity", "provenance", "conductors"),
    ("mesh_identity", "provenance", "dielectrics"),
    ("mesh_identity", "provenance", "reference_semantics"),
    ("run_identity", "resource_class"),
    ("run_identity", "resource_limits", "wall_time_s"),
    ("run_identity", "resource_limits", "peak_rss_bytes"),
    ("run_identity", "resource_limits", "output_bytes"),
    ("run_identity", "resource_limits", "refined_panels"),
    ("run_identity", "resource_limits", "gmres_iterations_per_rhs"),
    ("run_identity", "resource_limits", "nodes"),
    ("run_identity", "resource_limits", "tetrahedra"),
    ("config_identity", "provenance", "mesh_sha256"),
    ("config_identity", "provenance", "mesh_manifest_sha256"),
    ("config_identity", "provenance", "order"),
    ("run_identity", "config_manifest_sha256"),
    ("run_identity", "artifacts", "/fixture.msh"),
    ("rung", "mesh_scale"),
    ("rung", "far_mesh_scale"),
    ("rung", "outer_scale"),
    ("rung", "resource_class"),
])
def test_p_shared_mesh_and_limits_reject_every_identity_mismatch(path):
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    reports = [
        _report(item.name, matrix, "pcb_like_coplanar_air") for item in RUNGS
    ]
    p2 = next(item for item in reports if item["rung"]["name"] == "p2")
    target = p2
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "changed"
    before = canonical_sha256({"fixtures": [
        _report(item.name, matrix, "pcb_like_coplanar_air") for item in RUNGS
    ]})
    after = canonical_sha256({"fixtures": reports})
    assert before != after
    assert _p_shared_mesh_gate(reports)["passed"] is False
    result = _evaluate_fixture(
        "pcb_like_coplanar_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["uncertainty_f"] is None


@pytest.mark.parametrize("path", [
    *((name,) for name in (
        "fixture", "outer_scale", "mesh_scale", "far_mesh_scale", "order",
        "resource_class", "near_mesh_size_m", "far_mesh_size_m",
        "transition_distance_m", "node_count", "tetrahedron_count",
        "reference_semantics", "mesh_identity", "config_identity",
        "run_identity", "content_sha256",
    )),
    *(("reference_semantics", name) for name in (
        "kind", "convergence_ladder_required", "not_a_circuit_node",
        "outer_bounds_m", "outer_scale",
    )),
    *(("reference_semantics", "outer_bounds_m", name)
      for name in ("minimum", "maximum")),
    *(("config_identity", "provenance", "finite_reference", name)
      for name in (
          "kind", "convergence_ladder_required", "not_a_circuit_node",
          "outer_bounds_m", "outer_scale",
      )),
    *(("config_identity", "provenance", "finite_reference",
       "outer_bounds_m", name) for name in ("minimum", "maximum")),
    *(("mesh_identity", name) for name in (
        "mesh_path", "mesh_sha256", "manifest_path", "manifest_sha256",
        "provenance_sha256", "provenance",
    )),
    *(("config_identity", name) for name in (
        "config_path", "config_sha256", "manifest_path", "manifest_sha256",
        "provenance_sha256", "provenance",
    )),
    *(("mesh_identity", "provenance", name) for name in sorted({
        "conductors", "dielectrics", "edge_count", "face_count", "gmsh",
        "ground_attribute", "material_attributes", "mesh_parameters",
        "mesh_sha256", "node_count", "outer_bounds", "outer_permittivity",
        "reference_semantics", "terminal_attributes", "tetrahedron_count",
    })),
    *(("mesh_identity", "provenance", "gmsh", name) for name in (
        "version", "python_module", "python_module_sha256", "library",
        "library_sha256",
    )),
    *(("mesh_identity", "provenance", "mesh_parameters", name) for name in (
        "algorithm_3d", "mesh_size_far_m", "mesh_size_near_m", "msh_version",
        "threads", "transition_distance_m",
    )),
    *(("config_identity", "provenance", name) for name in (
        "config_sha256", "explicit_residual_tolerance", "finite_reference",
        "gate_policy", "ground_attribute", "linear_tolerance", "materials",
        "maximum_iterations",
        "mesh_manifest_sha256", "mesh_sha256", "order", "terminals",
    )),
    *(("run_identity", name) for name in (
        "path", "sha256", "content_sha256", "resource_class",
        "resource_limits", "config_manifest", "config_manifest_sha256",
        "artifacts",
    )),
])
def test_p_shared_mesh_rejects_same_required_omission_on_both_reports(path):
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    reports = [_report(item.name, matrix) for item in RUNGS]
    for name in ("p2", "p3"):
        report = next(item for item in reports if item["rung"]["name"] == name)
        target = report
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    for report in reports:
        report["raw_matrix_f"] = "must not be inspected"
    result = _evaluate_fixture(
        "parallel_plate_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["uncertainty_f"] is None
    assert result["axes"] == {}


def test_equal_nested_reference_extra_or_contradiction_rejects():
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    for mode in ("extra", "contradiction"):
        reports = [_report(item.name, matrix) for item in RUNGS]
        for name in ("p2", "p3"):
            report = next(item for item in reports if item["rung"]["name"] == name)
            references = (
                report["reference_semantics"],
                report["config_identity"]["provenance"]["finite_reference"],
            )
            for reference in references:
                if mode == "extra":
                    reference["unexpected"] = True
                else:
                    reference["outer_scale"] = 99.0
        result = _evaluate_fixture(
            "parallel_plate_air", reports, verify_artifacts=False
        )
        assert result["lifecycle"] == "rejected_diagnostic"
        assert result["axes"] == {}
        assert result["uncertainty_f"] is None


@pytest.mark.parametrize("control", [
    "mesh_scale", "far_mesh_scale", "outer_scale", "order", "resource_class",
])
def test_all_rungs_are_bound_to_frozen_policy_controls(control):
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    reports = [_report(item.name, matrix) for item in RUNGS]
    report = next(item for item in reports if item["rung"]["name"] == "outer_mid")
    if control == "resource_class":
        report[control] = "pcb_diagnostic"
        report["rung"][control] = "pcb_diagnostic"
        report["run_identity"][control] = "pcb_diagnostic"
        report["run_identity"]["resource_limits"] = {
            **MESH_LIMITS["pcb_diagnostic"],
            **asdict(RESOURCE_LIMITS["pcb_diagnostic"]),
        }
    else:
        changed = 2 if control == "order" else 2.0
        report[control] = changed
        report["rung"][control] = changed
        if control == "outer_scale":
            report["reference_semantics"]["outer_scale"] = changed
            report["config_identity"]["provenance"]["finite_reference"][
                "outer_scale"] = changed
        elif control == "order":
            report["config_identity"]["provenance"]["order"] = changed
    report["raw_matrix_f"] = "must not be inspected"
    result = _evaluate_fixture(
        "parallel_plate_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["axes"] == {}
    assert result["uncertainty_f"] is None


@pytest.mark.parametrize("mutation", [
    "circuit_reference", "numeric_reference", "boolean_threads",
    "none_bound", "string_bound", "boolean_bound", "string_conductor_z",
    "conductors_none", "terminals_none", "artifacts_list",
])
def test_unsafe_or_malformed_identity_rejects_without_matrix_access(mutation):
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    reports = [_report(item.name, matrix) for item in RUNGS]
    for name in ("p2", "p3"):
        report = next(item for item in reports if item["rung"]["name"] == name)
        if mutation == "circuit_reference":
            for reference in (
                    report["reference_semantics"],
                    report["config_identity"]["provenance"]["finite_reference"]):
                reference.update({
                    "kind": "circuit_node",
                    "convergence_ladder_required": False,
                    "not_a_circuit_node": False,
                })
            report["mesh_identity"]["provenance"]["reference_semantics"].update({
                "kind": "circuit_node",
                "convergence_ladder_required": False,
                "not_a_circuit_node": False,
            })
        elif mutation == "numeric_reference":
            for reference in (
                    report["reference_semantics"],
                    report["config_identity"]["provenance"]["finite_reference"],
                    report["mesh_identity"]["provenance"]["reference_semantics"]):
                reference["convergence_ladder_required"] = 1
                reference["not_a_circuit_node"] = 1
        elif mutation == "boolean_threads":
            report["mesh_identity"]["provenance"]["mesh_parameters"][
                "threads"] = True
        elif mutation in ("none_bound", "string_bound", "boolean_bound"):
            value = {"none_bound": None, "string_bound": "bad",
                     "boolean_bound": True}[mutation]
            bound = "maximum" if mutation == "boolean_bound" else "minimum"
            report["reference_semantics"]["outer_bounds_m"][bound][0] = value
            report["config_identity"]["provenance"]["finite_reference"][
                "outer_bounds_m"][bound][0] = value
            report["mesh_identity"]["provenance"]["outer_bounds"][bound][0] = value
        elif mutation == "string_conductor_z":
            report["mesh_identity"]["provenance"]["conductors"][0]["z_min"] = "bad"
        elif mutation == "conductors_none":
            report["mesh_identity"]["provenance"]["conductors"] = None
        elif mutation == "terminals_none":
            report["config_identity"]["provenance"]["terminals"] = None
        else:
            report["run_identity"]["artifacts"] = []
        report["raw_matrix_f"] = "must not be inspected"
    result = _evaluate_fixture(
        "parallel_plate_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["axes"] == {}
    assert result["uncertainty_f"] is None


def _rebind_bound_item_as_order_three(item):
    item["order"] = 3
    item["rung"] = asdict(next(rung for rung in RUNGS if rung.name == "p3"))
    config = item["config_identity"]
    config_path = Path(config["config_path"])
    config_value = json.loads(config_path.read_text())
    config_value["Solver"]["Order"] = 3
    config_path.write_text(json.dumps(config_value))
    config["config_sha256"] = file_sha256(config_path)
    config["provenance"]["config_sha256"] = config["config_sha256"]
    config["provenance"]["order"] = 3
    config_manifest = {
        "format": "dcdc-palace-config-v3",
        "provenance": config["provenance"],
        "provenance_sha256": canonical_sha256(config["provenance"]),
    }
    config_manifest_path = Path(config["manifest_path"])
    config_manifest_path.write_text(json.dumps(config_manifest, sort_keys=True))
    config["manifest_sha256"] = file_sha256(config_manifest_path)
    config["provenance_sha256"] = config_manifest["provenance_sha256"]

    run_path = Path(item["run_identity"]["path"])
    run = json.loads(run_path.read_text())
    run["config_manifest_sha256"] = config["manifest_sha256"]
    run["artifacts"][str(config_path)] = config["config_sha256"]
    old_workload_path = Path(run["workload"])
    run["artifacts"].pop(str(old_workload_path))
    old_workload_path.unlink()
    executable = Path(next(
        record["source"] for record in run["execution_snapshot"]["inputs"]
        if record["role"] == "executable"
    ))
    workload = palace_module._workload_record(
        palace_module.load_palace_config_manifest(config_manifest_path),
        config_manifest_path=config_manifest_path,
        build_manifest_path=Path(run["build_manifest"]),
        executable=executable,
        processes=1,
        implementation=run["implementation_files"],
        binaries=run["binaries"],
        mpi_launcher=run["mpi_launcher"],
    )
    workload_path = config_path.with_name(
        f"{config_path.name}.workload.{workload['content_sha256']}.json"
    )
    workload_path.write_text(json.dumps(workload, indent=2, sort_keys=True) + "\n")
    run["workload"] = str(workload_path)
    run["workload_sha256"] = file_sha256(workload_path)
    run["artifacts"][str(workload_path)] = run["workload_sha256"]
    old_decision_path = Path(run["resource_decision"])
    projection = _resource_projection(
        workload["topology_workload"][
            "retained_terminal_vector_bytes_lower_bound"
        ]
    )
    policy = palace_module._trusted_resource_policy()
    decision = palace_module.build_palace_resource_decision(
        palace_workload=workload,
        projection=projection,
        profiles=policy["profile_objects"],
        authorized_profile_ids=policy["authorized_profile_tuple"],
        policy_id=policy["policy_id"],
        policy_sha256=policy["policy_sha256"],
        validator_sha256=policy["validator_sha256"],
        minimum_headroom_ratio=policy["minimum_headroom_ratio"],
    )
    run["artifacts"].pop(str(old_decision_path))
    old_decision_path.unlink()
    decision_path = config_path.with_name(
        f"{config_path.name}.resource-decision.{decision['content_sha256']}.json"
    )
    decision_path.write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n")
    run["resource_decision"] = str(decision_path)
    run["resource_decision_sha256"] = file_sha256(decision_path)
    run["artifacts"][str(decision_path)] = run["resource_decision_sha256"]
    run.pop("content_sha256")
    run_payload = {key: value for key, value in run.items() if key != "format"}
    run["content_sha256"] = canonical_sha256(run_payload)
    run_path.write_text(json.dumps(run, sort_keys=True))
    item["run_identity"].update({
        "sha256": file_sha256(run_path),
        "content_sha256": run["content_sha256"],
        "config_manifest_sha256": run["config_manifest_sha256"],
        "artifacts": run["artifacts"],
    })
    report_path = Path(item.pop("report_path"))
    item.pop("report_sha256")
    item.pop("content_sha256")
    item["content_sha256"] = canonical_sha256(item)
    report_path.write_text(json.dumps(item, sort_keys=True))
    item["report_path"] = str(report_path)
    item["report_sha256"] = file_sha256(report_path)
    return item


def test_resolved_order_two_rejects_fully_rebound_order_three(
        tmp_path, monkeypatch):
    _patch_test_build_validation(monkeypatch)
    item = _rebind_bound_item_as_order_three(
        _write_bound_report(tmp_path / "rebound")
    )
    failures = _bound_artifact_failures(item)
    assert failures


def test_p2_report_cannot_be_relabeled_as_p3():
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    reports = [_report(item.name, matrix) for item in RUNGS]
    p2 = next(item for item in reports if item["rung"]["name"] == "p2")
    index = next(index for index, item in enumerate(reports)
                 if item["rung"]["name"] == "p3")
    reports[index] = copy.deepcopy(p2)
    reports[index]["rung"] = asdict(next(item for item in RUNGS
                                           if item.name == "p3"))
    result = _evaluate_fixture(
        "parallel_plate_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["uncertainty_f"] is None
    assert result["axes"] == {}


def test_cli_exits_nonzero_for_rejected_shared_mesh(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ladder_module,
        "run_ladders",
        lambda **kwargs: (tmp_path / "report.json", {
            "lifecycle": "rejected_diagnostic"
        }),
    )
    monkeypatch.setattr(sys, "argv", [
        "study", "--output", str(tmp_path / "out"),
        "--executable", str(tmp_path / "palace"),
        "--build-manifest", str(tmp_path / "build.json"),
    ])
    with pytest.raises(SystemExit) as caught:
        ladder_module.main()
    assert caught.value.code == 1


def test_p_shared_mesh_rejects_missing_identity_even_with_passing_matrices():
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    reports = [
        _report(item.name, matrix, "pcb_like_coplanar_air") for item in RUNGS
    ]
    p3 = next(item for item in reports if item["rung"]["name"] == "p3")
    del p3["mesh_identity"]["provenance"]
    result = _evaluate_fixture(
        "pcb_like_coplanar_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["uncertainty_f"] is None


def test_fixture_evaluation_fails_closed_on_a_rejected_rung():
    matrix = [[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]]
    reports = [
        _report(item.name, matrix, "pcb_like_coplanar_air") for item in RUNGS
    ]
    reports[-1]["lifecycle"] = "rejected_diagnostic"
    result = _evaluate_fixture(
        "pcb_like_coplanar_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "rejected_diagnostic"
    assert result["uncertainty_f"] is None


def test_parallel_plate_evaluation_passes_converged_synthetic_ladders():
    base = np.array([[1.4e-13, -1.1e-13], [-1.1e-13, 1.4e-13]])
    scales = {
        "h_coarse": 1.010,
        "h_medium": 1.004,
        "h_fine": 1.000,
        "p2": 1.003,
        "p3": 1.002,
        "outer_near": 1.012,
        "outer_mid": 1.004,
        "outer_far": 1.001,
    }
    reports = [_report(item.name, base * scales[item.name]) for item in RUNGS]
    result = _evaluate_fixture(
        "parallel_plate_air", reports, verify_artifacts=False
    )
    assert result["lifecycle"] == "numerically_converged_diagnostic"
    assert result["analytic_cross_check"]["passed"] is True
    assert result["uncertainty_f"] > 0.0
