#!/usr/bin/env python3
from copy import deepcopy
import csv
import json
import os
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import palace  # noqa: E402
import palace_attempt  # noqa: E402,F401
import palace_runtime  # noqa: E402
from palace_attempt import (  # noqa: E402
    validate_palace_attempt_manifest,
    validate_palace_execution_witness,
)
from palace import (  # noqa: E402
    PalaceMaterial,
    PalaceRunRejected,
    PalaceTerminal,
    checkpoint_matrix_access,
    load_palace_config_manifest,
    _parse_palace_matrix_csv,
    run_palace,
    validate_palace_run_manifest,
    write_palace_config,
    write_palace_resource_decision,
)
from palace_matrix_access import PalaceMatrixAccess  # noqa: E402
from palace_ledger_v2 import CanonicalLedgerPublicationV2  # noqa: E402
from palace_resources import (  # noqa: E402
    PalaceResourceProjection,
    ResourceBound,
)
from process_monitor import ProcessExecution  # noqa: E402
from provenance import bytes_sha256, canonical_sha256, file_sha256  # noqa: E402


def _write_config(tmp_path, *, tolerance=1e-10,
                  explicit_residual_tolerance=None, order=1, checkpoint=None,
                  maximum_iterations=500, linear_solver_type="BoomerAMG",
                  multigrid_max_levels=None):
    mesh = tmp_path / "fixture.msh"
    mesh.write_text(
        "$MeshFormat\n2.2 0 8\n$EndMeshFormat\n"
        "$Nodes\n4\n"
        "1 0 0 0\n2 1 0 0\n3 0 1 0\n4 0 0 1\n$EndNodes\n"
        "$Elements\n1\n1 4 2 1 1 1 2 3 4\n$EndElements\n"
    )
    gmsh_module = tmp_path / "gmsh.py"
    gmsh_library = tmp_path / "libgmsh"
    gmsh_module.write_text("module")
    gmsh_library.write_text("library")
    mesh_provenance = {
        "mesh_sha256": file_sha256(mesh),
        "conductors": [
            {"name": "plate_low", "rings": [[[-0.001, -0.001],
                                                [0.001, -0.001],
                                                [0.001, 0.001],
                                                [-0.001, 0.001]]],
             "z_min": -0.0002, "z_max": -0.0001},
            {"name": "plate_high", "rings": [[[-0.001, -0.001],
                                                 [0.001, -0.001],
                                                 [0.001, 0.001],
                                                 [-0.001, 0.001]]],
             "z_min": 0.0001, "z_max": 0.0002},
        ],
        "dielectrics": [],
        "gmsh": {
            "version": "4.15.2",
            "python_module": str(gmsh_module),
            "python_module_sha256": file_sha256(gmsh_module),
            "library": str(gmsh_library),
            "library_sha256": file_sha256(gmsh_library),
        },
        "terminal_attributes": [["plate_low", 2], ["plate_high", 3]],
        "material_attributes": [["outer", 1, 1.0]],
        "ground_attribute": 4,
        "mesh_parameters": {
            "algorithm_3d": 10,
            "mesh_size_far_m": 0.001,
            "mesh_size_near_m": 0.0001,
            "msh_version": 2.2,
            "threads": 1,
            "transition_distance_m": 0.001,
        },
        "node_count": 4,
        "edge_count": 6,
        "face_count": 4,
        "tetrahedron_count": 1,
        "outer_bounds": {
            "minimum": [-0.003, -0.003, -0.003],
            "maximum": [0.003, 0.003, 0.003],
        },
        "outer_permittivity": 1.0,
        "reference_semantics": {
            "kind": "finite_outer_dirichlet_approximation",
            "convergence_ladder_required": True,
            "not_a_circuit_node": True,
        },
    }
    mesh_manifest = {
        "format": "dcdc-palace-mesh-v2",
        "provenance": mesh_provenance,
        "provenance_sha256": canonical_sha256(mesh_provenance),
    }
    mesh_manifest_path = mesh.with_suffix(".msh.manifest.json")
    mesh_manifest_path.write_text(json.dumps(mesh_manifest))
    config = tmp_path / "config.json"
    manifest = write_palace_config(
        config,
        mesh_path=mesh,
        mesh_manifest_path=mesh_manifest_path,
        output_directory=tmp_path / "postpro",
        terminals=(
            PalaceTerminal(1, "plate_low", 2),
            PalaceTerminal(2, "plate_high", 3),
        ),
        materials=(PalaceMaterial("outer", (1,), 1.0),),
        ground_attribute=4,
        finite_reference={
            "kind": "finite_outer_dirichlet_approximation",
            "convergence_ladder_required": True,
            "not_a_circuit_node": True,
            "outer_bounds_m": {
                "minimum": [-0.003, -0.003, -0.003],
                "maximum": [0.003, 0.003, 0.003],
            },
            "outer_scale": 6.0,
        },
        linear_tolerance=tolerance,
        explicit_residual_tolerance=explicit_residual_tolerance,
        order=order,
        checkpoint=checkpoint,
        maximum_iterations=maximum_iterations,
        linear_solver_type=linear_solver_type,
        multigrid_max_levels=multigrid_max_levels,
    )
    return config, manifest


def test_checkpoint_config_v3_binds_absolute_root_and_native_identity(tmp_path):
    checkpoint = {
        "path": str((tmp_path / "checkpoint-store").resolve()),
        "native_campaign_identity": "b" * 64,
    }
    config_path, manifest_path = _write_config(tmp_path, checkpoint=checkpoint)
    manifest = load_palace_config_manifest(manifest_path)
    assert manifest.checkpoint == checkpoint
    assert json.loads(config_path.read_text())["Solver"]["Electrostatic"] == {
        "Save": 0,
        "Checkpoint": {
            "Path": checkpoint["path"],
            "CampaignIdentity": checkpoint["native_campaign_identity"],
        },
    }
    resolved = palace._expected_resolved_config(manifest)
    assert resolved["Solver"]["Electrostatic"]["Checkpoint"]["Path"] == checkpoint["path"]


def test_resolved_linear_max_size_tracks_iteration_limit(tmp_path):
    _, manifest_path = _write_config(tmp_path, maximum_iterations=750)
    resolved = palace._expected_resolved_config(
        load_palace_config_manifest(manifest_path)
    )
    assert resolved["Solver"]["Linear"]["MaxSize"] == 750


def test_legacy_config_v2_remains_loadable(tmp_path):
    _, manifest_path = _write_config(tmp_path)
    value = json.loads(manifest_path.read_text())
    value["format"] = "dcdc-palace-config-v2"
    for key in ("checkpoint", "linear_solver_type", "multigrid_max_levels"):
        value["provenance"].pop(key)
    value["provenance_sha256"] = canonical_sha256(value["provenance"])
    manifest_path.write_text(json.dumps(value))
    assert load_palace_config_manifest(manifest_path).checkpoint is None


def test_explicit_residual_tolerance_is_independent_and_fail_closed(tmp_path):
    config, manifest_path = _write_config(
        tmp_path, tolerance=1e-12, explicit_residual_tolerance=1e-10
    )
    manifest = load_palace_config_manifest(manifest_path)
    assert manifest.linear_tolerance == 1e-12
    assert manifest.explicit_residual_tolerance == 1e-10
    assert json.loads(config.read_text())["Solver"]["Linear"] == {
        "KSPType": "CG",
        "MaxIts": 500,
        "Tol": 1e-12,
        "Type": "BoomerAMG",
        "VerificationTol": 1e-10,
    }
    with pytest.raises(ValueError, match="no stricter than"):
        _write_config(
            tmp_path, tolerance=1e-10, explicit_residual_tolerance=1e-12
        )


@pytest.mark.parametrize("value", [0.0, True, "1e-10", float("nan"), float("inf")])
def test_explicit_residual_tolerance_rejects_invalid_values(tmp_path, value):
    with pytest.raises(ValueError, match="explicit residual tolerance"):
        _write_config(tmp_path, explicit_residual_tolerance=value)


def test_palace_identity_binds_complete_kicad_plc_producer():
    paths = {Path(path).name for path in palace._implementation_identity()}
    assert {
        "extract_palace_mesh.py",
        "kicad_fastercap.py",
        "kicad_fastercap_schema.py",
        "kicad_palace.py",
        "kicad_palace_dump.py",
        "kicad_palace_schema.py",
        "palace_completion.py",
        "palace_plc_mesh.py",
    } <= paths


def _write_matrix(path, name, values):
    with path.open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["i", f"{name}[i][1] (F)", f"{name}[i][2] (F)"])
        for index, row in enumerate(values, 1):
            writer.writerow([f"{index:.17e}", *(f"{value:+.17e}" for value in row)])


def _write_completion_artifacts(output, config_path, *, terminal_count=2):
    manifest = load_palace_config_manifest(
        config_path.with_suffix(config_path.suffix + ".manifest.json")
    )
    (output / "config_resolved.json").write_text(json.dumps(
        palace._expected_resolved_config(manifest)
    ))
    event_counts = {
        "Total": 1,
        "Estimation": 0,
        "LinearSolve": terminal_count,
        "Solve": 0,
    }
    event_values = {
        name: (0.01 if count else 0.0) for name, count in event_counts.items()
    }
    event_values["Total"] = 0.05
    memory_growth = {
        name: dict(event_values) for name in ("Max", "Min", "Sum")
    }
    memory = {name: 0.0005 for name in ("Average", "Max", "Min", "Total")}
    (output / "palace.json").write_text(json.dumps({
        "ElapsedTime": {
            "Counts": event_counts,
            "Durations": event_values,
        },
        "GitTag": "test-build-dirty",
        "LinearSolver": {"TotalIts": 9, "TotalSolves": terminal_count},
        "PeakMemoryGrowthMegabytes": memory_growth,
        "PeakMemoryMegabytes": memory,
        "PeakNodeMemoryGrowthMegabytes": memory_growth,
        "PeakNodeMemoryMegabytes": memory,
        "Problem": {
            "DegreesOfFreedom": 4,
            "MPISize": 1,
            "MeshElements": 1,
            "MultigridDegreesOfFreedom": [4],
        },
    }))


def _successful_stdout(*, warning=""):
    return (
        f"{warning}"
        "PALACE_MILESTONE setup_start solver=electrostatic checkpoint_enabled=0\n"
        "PALACE_MILESTONE setup_end campaign_digest=disabled prefix_before=0 "
        "loaded_rhs=[] new_rhs=[1,2]\n"
        "Computing electrostatic fields for 2 terminal boundaries\n"
        "PALACE_MILESTONE rhs_start rhs=1 terminal=1 action=solve\n"
        " 0 KSP residual norm ||r||_B = 1.0e+00\n"
        "PCG solver converged in 4 iterations\n"
        "Explicit residual ||b-Ax||/||b|| = 1.00000000000000000e-12 "
        "(target = 1.00000000000000004e-10)\n"
        "PALACE_MILESTONE residual_accepted rhs=1 terminal=1 source=solved "
        "value=1.00000000000000000e-12\n"
        "PALACE_MILESTONE reaction_accepted rhs=1 terminal=1 source=solved entries=2\n"
        "PALACE_MILESTONE rhs_solved rhs=1 terminal=1 solves=1 iterations=4\n"
        "PALACE_MILESTONE rhs_start rhs=2 terminal=2 action=solve\n"
        " 0 KSP residual norm ||r||_B = 1.0e+00\n"
        "PCG solver converged in 5 iterations\n"
        "Explicit residual ||b-Ax||/||b|| = 2.00000000000000000e-12 "
        "(target = 1.00000000000000004e-10)\n"
        "PALACE_MILESTONE residual_accepted rhs=2 terminal=2 source=solved "
        "value=2.00000000000000000e-12\n"
        "PALACE_MILESTONE reaction_accepted rhs=2 terminal=2 source=solved entries=2\n"
        "PALACE_MILESTONE rhs_solved rhs=2 terminal=2 solves=1 iterations=5\n"
        "PALACE_MILESTONE finalization_start terminal_count=2 prefix_before=0 "
        "prefix_after=0\n"
        "PALACE_MILESTONE finalization_end prefix_before=0 prefix_after=0 "
        "total_solves=2 total_iterations=9 checkpoint_read_bytes=0 "
        "checkpoint_written_bytes=0 checkpoint_retained_bytes=0\n"
        "Elapsed Time Report (s)\n"
        "Peak Memory\n"
    ).encode()


def _patch_process_run(monkeypatch, tmp_path, raw, standard, *, warning="",
                       write_metadata=True, completion_markers=True,
                       omit_rhs_milestones=False, wall_timeout=False,
                       metadata_mutator=None, resolved_mutator=None):
    def fake_run(command, *, cwd, limits, environment=None):
        output = Path(cwd) / "postpro"
        output.mkdir(exist_ok=True)
        if not wall_timeout:
            _write_matrix(output / "terminal-Craw.csv", "C_raw", raw)
            _write_matrix(output / "terminal-C.csv", "C", standard)
        if write_metadata and not wall_timeout:
            _write_completion_artifacts(output, tmp_path / "config.json")
            if metadata_mutator is not None:
                path = output / "palace.json"
                value = json.loads(path.read_text())
                metadata_mutator(value)
                path.write_text(json.dumps(value))
            if resolved_mutator is not None:
                path = output / "config_resolved.json"
                value = json.loads(path.read_text())
                resolved_mutator(value)
                path.write_text(json.dumps(value))
        stdout = _successful_stdout(warning=warning)
        if wall_timeout:
            marker = b"PALACE_MILESTONE rhs_solved rhs=1 terminal=1 solves=1 iterations=4\n"
            stdout = stdout[:stdout.index(marker) + len(marker)]
        if omit_rhs_milestones:
            stdout = b"\n".join(
                line for line in stdout.splitlines()
                if b"KSP residual" not in line
                and b"PCG solver" not in line
                and b"Computing electrostatic fields" not in line
            ) + b"\n"
        if not completion_markers:
            stdout = stdout.replace(b"Elapsed Time Report (s)\nPeak Memory\n", b"")
        returncode = -15 if wall_timeout else 0
        elapsed_s = 301.0 if wall_timeout else 0.1
        exit_ns = int(elapsed_s * 1e9)
        monitor_events = [
            {"event": "prelaunch", "monotonic_ns": 0, "detail": None},
            {"event": "process_started", "monotonic_ns": 1,
             "detail": "test"},
        ]
        limit_failures = ()
        if wall_timeout:
            detail = f"wall time exceeded {limits.wall_time_s:g}s"
            limit_failures = (detail,)
            monitor_events.extend((
                {"event": "limit_detected", "monotonic_ns": 300_000_000_001,
                 "detail": detail},
                {"event": "kill_initiated", "monotonic_ns": 300_000_000_002,
                 "detail": None},
            ))
        monitor_events.append({
            "event": "process_exited", "monotonic_ns": exit_ns,
            "detail": str(returncode),
        })
        return ProcessExecution(
            tuple(command), str(cwd), returncode, stdout, b"", elapsed_s,
            1024, len(stdout), 100, 0, 0, limit_failures, limits,
            stream_events=({
                "source": "stdout",
                "byte_offset": 0,
                "byte_count": len(stdout),
                "monotonic_ns": 1,
                "sha256": bytes_sha256(stdout),
            },),
            monitor_events=tuple(monitor_events),
            resource_samples=({
                "monotonic_ns": exit_ns - 1,
                "peak_rss_bytes": 1024,
                "output_bytes": len(stdout),
                "directory_growth_bytes": 100,
                "max_refined_panels": 0,
                "max_gmres_iteration": 0,
            },),
        )

    build_manifest = tmp_path / "build-manifest.json"
    build_manifest.write_text("{}\n")
    monkeypatch.setattr(
        palace,
        "validate_palace_build_manifest",
        lambda path, *, executable: {
            "provenance_sha256": "test-build",
            "provenance": {"source_commit": "test-build-commit"},
        },
    )
    monkeypatch.setattr(palace, "run_monitored_process", fake_run)
    return build_manifest


@pytest.mark.parametrize(
    ("order", "ams_max_its", "mg_smooth_order"),
    [(1, 1, 4), (2, 2, 4), (3, 3, 6)],
)
def test_resolved_defaults_are_pinned_for_each_solver_order(
        tmp_path, order, ams_max_its, mg_smooth_order):
    _, manifest_path = _write_config(tmp_path, order=order)
    manifest = load_palace_config_manifest(manifest_path)
    resolved = palace._expected_resolved_config(manifest)
    assert resolved["Solver"]["Order"] == order
    assert resolved["Solver"]["Linear"]["AMSMaxIts"] == ams_max_its
    assert resolved["Solver"]["Linear"]["MGSmoothOrder"] == mg_smooth_order
    assert resolved["Solver"]["PartialAssemblyOrder"] == 1


def test_config_manifest_is_content_addressed_and_context_bound(tmp_path):
    config, manifest_path = _write_config(tmp_path)
    manifest = load_palace_config_manifest(manifest_path)
    assert manifest.config_path == config.resolve()
    assert manifest.mesh_path == (tmp_path / "fixture.msh").resolve()
    assert manifest.terminal_names == ("plate_low", "plate_high")
    assert manifest.finite_reference["convergence_ladder_required"] is True
    config.write_text(config.read_text() + " ")
    with pytest.raises(ValueError, match="config hash mismatch"):
        load_palace_config_manifest(manifest_path)


def test_config_manifest_rejects_self_canonical_semantic_relabeling(tmp_path):
    config_path, manifest_path = _write_config(tmp_path)
    config = json.loads(config_path.read_text())
    config["Boundaries"]["Terminal"][0]["Attributes"] = [77]
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    manifest = json.loads(manifest_path.read_text())
    manifest["provenance"]["config_sha256"] = file_sha256(config_path)
    manifest["provenance_sha256"] = canonical_sha256(manifest["provenance"])
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="semantics do not match"):
        load_palace_config_manifest(manifest_path)


@pytest.mark.parametrize(("path", "replacement"), [
    (("Model", "L0"), True),
    (("Model", "Refinement", "MaxIts"), False),
    (("Domains", "Materials", 0, "Attributes", 0), True),
    (("Domains", "Materials", 0, "Permittivity"), True),
    (("Solver", "Order"), True),
    (("Solver", "Electrostatic", "Save"), False),
    (("Boundaries", "Terminal", 0, "Index"), True),
])
def test_config_manifest_rejects_bool_number_substitution(
        tmp_path, path, replacement):
    config_path, manifest_path = _write_config(tmp_path)
    config = json.loads(config_path.read_text())
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    manifest = json.loads(manifest_path.read_text())
    manifest["provenance"]["config_sha256"] = file_sha256(config_path)
    manifest["provenance_sha256"] = canonical_sha256(manifest["provenance"])
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="semantics do not match"):
        load_palace_config_manifest(manifest_path)


def test_config_rejects_an_implicit_or_undeclared_reference(tmp_path):
    mesh = tmp_path / "fixture.msh"
    mesh.write_text("mesh\n")
    mesh_provenance = {
        "mesh_sha256": file_sha256(mesh),
        "terminal_attributes": [["A", 2]],
        "material_attributes": [["air", 1, 1.0]],
        "ground_attribute": 3,
        "node_count": 4,
        "tetrahedron_count": 1,
        "outer_bounds": {
            "minimum": [-0.003, -0.003, -0.003],
            "maximum": [0.003, 0.003, 0.003],
        },
        "reference_semantics": {
            "kind": "finite_outer_dirichlet_approximation",
            "convergence_ladder_required": True,
            "not_a_circuit_node": True,
        },
    }
    mesh_manifest_path = mesh.with_suffix(".msh.manifest.json")
    mesh_manifest_path.write_text(json.dumps({
        "format": "dcdc-palace-mesh-v2",
        "provenance": mesh_provenance,
        "provenance_sha256": canonical_sha256(mesh_provenance),
    }))
    with pytest.raises(ValueError, match="finite electrostatic reference"):
        write_palace_config(
            tmp_path / "config.json",
            mesh_path=mesh,
            mesh_manifest_path=mesh_manifest_path,
            output_directory=tmp_path / "postpro",
            terminals=(PalaceTerminal(1, "A", 2),),
            materials=(PalaceMaterial("air", (1,), 1.0),),
            ground_attribute=3,
            finite_reference={"kind": "spice_ground"},
        )


def test_palace_matrix_access_capability_cannot_be_forged():
    with pytest.raises(ValueError, match="capability is invalid"):
        PalaceMatrixAccess(
            campaign_sha256="a" * 64, head_sha256="b" * 64,
            config_sha256="c" * 64, terminal_names=("A",), matrices=(),
            execution_witness=("/run.json", "d" * 64),
            _ledger=object(), _nonce=object(),
        )


def test_matrix_capability_rejects_ledger_subclasses():
    class ForgedLedger(CanonicalLedgerPublicationV2):
        pass

    forged = object.__new__(ForgedLedger)
    with pytest.raises(ValueError, match="capability is invalid"):
        PalaceMatrixAccess(
            campaign_sha256="a" * 64, head_sha256="b" * 64,
            config_sha256="c" * 64, terminal_names=("A",), matrices=(),
            execution_witness=("/run.json", "d" * 64),
            _ledger=forged, _nonce=object(),
        )
    with pytest.raises(ValueError, match="canonical v2 ledger"):
        checkpoint_matrix_access(
            forged, None, raw_path="raw.csv", standard_path="standard.csv",
        )


def test_checkpoint_matrix_access_rejects_without_canonical_v2_ledger():
    with pytest.raises(ValueError, match="canonical v2 ledger"):
        checkpoint_matrix_access(
            None, None, raw_path="raw.csv", standard_path="standard.csv"
        )


def test_matrix_parser_rejects_terminal_reordering(tmp_path):
    _, manifest_path = _write_config(tmp_path)
    manifest = load_palace_config_manifest(manifest_path)
    matrix_path = tmp_path / "matrix.csv"
    matrix_path.write_text(
        "i,C_raw[i][2] (F),C_raw[i][1] (F)\n"
        "1,-2e-12,3e-12\n2,4e-12,-2e-12\n"
    )
    with pytest.raises(ValueError, match="identity or units mismatch"):
        _parse_palace_matrix_csv(matrix_path, manifest, matrix_name="raw")


@pytest.mark.parametrize("header", [
    "i,wrong[i][1] (F),wrong[i][2] (F)",
    "i,C_raw[i][1] (pF),C_raw[i][2] (pF)",
    "i,prefix C_raw[i][1] (F),C_raw[i][2] (F)",
    "i,C_raw[i][1] (F) suffix,C_raw[i][2] (F)",
    "i,C_raw[i][1] (F),C_raw[i][1] (F)",
])
def test_matrix_parser_rejects_wrong_identity_units_or_indices(tmp_path, header):
    _, manifest_path = _write_config(tmp_path)
    manifest = load_palace_config_manifest(manifest_path)
    matrix_path = tmp_path / "matrix.csv"
    matrix_path.write_text(f"{header}\n1,3e-12,-2e-12\n2,-2e-12,4e-12\n")
    with pytest.raises(ValueError, match="identity or units mismatch"):
        _parse_palace_matrix_csv(matrix_path, manifest, matrix_name="raw")


def _run_with_test_resource_policy(
        manifest_path, *, executable, build_manifest, monkeypatch):
    manifest = load_palace_config_manifest(manifest_path)
    inputs = palace._execution_workload_inputs(
        manifest,
        config_manifest_path=manifest_path,
        build_manifest_path=build_manifest,
        executable=executable,
        processes=1,
        palace_file=palace.__file__,
        validate_build=palace.validate_palace_build_manifest,
    )
    retained = inputs["workload"]["topology_workload"][
        "retained_terminal_vector_bytes_lower_bound"
    ]
    projection = PalaceResourceProjection(
        wall_time_s=ResourceBound(1.0, 100.0),
        cpu_time_s=ResourceBound(1.0, 100.0),
        vector_residency_bytes=ResourceBound(retained, 256 * 1024),
        hierarchy_operator_solver_bytes=ResourceBound(1, 512 * 1024),
        retained_terminal_vectors_bytes=ResourceBound(retained, 256 * 1024),
        peak_rss_bytes=ResourceBound(retained, 1024**2),
        matrix_output_bytes=ResourceBound(1, 128 * 1024),
        logging_output_bytes=ResourceBound(1, 128 * 1024),
        checkpoint_write_bytes=ResourceBound(1, 128 * 1024),
        checkpoint_read_bytes=ResourceBound(0, 128 * 1024),
        uncertainty_reasons=("conservative unit-test bounds",),
        observation_sha256=("a" * 64,),
    )
    policy = palace._trusted_resource_policy()
    decision = palace.build_palace_resource_decision(
        palace_workload=inputs["workload"],
        projection=projection,
        profiles=policy["profile_objects"],
        authorized_profile_ids=policy["authorized_profile_tuple"],
        policy_id=policy["policy_id"],
        policy_sha256=policy["policy_sha256"],
        validator_sha256=policy["validator_sha256"],
        minimum_headroom_ratio=policy["minimum_headroom_ratio"],
    )
    decision_path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.resource-decision."
        f"{decision['content_sha256']}.json"
    )
    decision_path.write_text(json.dumps(decision))
    return run_palace(
        manifest_path,
        executable=executable,
        build_manifest_path=build_manifest,
        resource_decision_path=decision_path,
    )


def test_run_accepts_only_independent_raw_and_exact_standard_matrix(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "bin" / "palace"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    (executable.parent / "palace-arm64.bin").write_bytes(b"binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])

    build_manifest = _patch_process_run(monkeypatch, tmp_path, values, values)
    run = _run_with_test_resource_policy(
        manifest_path, executable=executable, build_manifest=build_manifest,
        monkeypatch=monkeypatch,
    )
    np.testing.assert_array_equal(run.raw_matrix.values, values)
    np.testing.assert_array_equal(run.downstream_matrix.values, values)
    stored = json.loads(run.manifest_path.read_text())
    assert stored["lifecycle"] == "numerically_converged_diagnostic"
    assert stored["resource_enforcement"] == "portable_process_limits"
    workload_path = Path(stored["workload"])
    workload = json.loads(workload_path.read_text())
    assert stored["workload_sha256"] == file_sha256(workload_path)
    assert workload["format"] == "palace-workload-v1"
    assert workload["topology_workload"]["topology"] == {
        "nodes": 4, "edges": 6, "faces": 4, "tetrahedra": 1,
    }
    assert str(workload_path) in stored["artifacts"]
    validated = validate_palace_run_manifest(run.manifest_path)
    np.testing.assert_array_equal(validated["raw_matrix"].values, values)
    np.testing.assert_array_equal(validated["downstream_matrix"].values, values)
    projection = validate_palace_execution_witness(run.manifest_path)
    assert projection["outcome"] == "completed"
    assert projection["prefix_after"] == 2
    assert projection["resource_bounds"]["solves"] == {"lower": 2, "upper": 2}
    assert projection["resource_bounds"]["iterations"] == {"lower": 9, "upper": 9}
    assert projection["workload_sha256"] == workload["content_sha256"]
    assert projection["workload_sha256"] != stored["workload_sha256"]


def test_execution_uses_frozen_inputs_and_rejects_source_mutation(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values
    )
    monitored = palace.run_monitored_process

    def mutate_source(command, **arguments):
        assert Path(command[0]) != executable
        assert Path(arguments["cwd"]) != tmp_path
        assert Path(command[0]).stat().st_mode & 0o222 == 0
        assert Path(arguments["cwd"]).stat().st_mode & 0o222 == 0
        executable.write_text("mutated")
        return monitored(command, **arguments)

    monkeypatch.setattr(palace, "run_monitored_process", mutate_source)
    with pytest.raises(PalaceRunRejected, match="execution snapshot") as rejected:
        _run_with_test_resource_policy(
            manifest_path, executable=executable, build_manifest=build_manifest,
            monkeypatch=monkeypatch,
        )
    persisted = json.loads(rejected.value.manifest_path.read_text())
    assert persisted["execution_snapshot"]["format"] == (
        "palace-execution-snapshot-v1"
    )
    assert any(
        failure.startswith("execution_snapshot:")
        for failure in persisted["failures"]
    )


def test_persisted_run_rejects_rebound_workload(tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)
    identity = json.loads(run.manifest_path.read_text())
    workload_path = Path(identity["workload"])
    workload = json.loads(workload_path.read_text())
    workload["solver_controls"]["maximum_iterations"] += 1
    unsigned = dict(workload)
    unsigned.pop("content_sha256")
    workload["content_sha256"] = canonical_sha256(unsigned)
    workload_path.write_text(json.dumps(workload, indent=2, sort_keys=True) + "\n")

    def rebind(value):
        digest = file_sha256(workload_path)
        value["workload_sha256"] = digest
        value["artifacts"][str(workload_path)] = digest

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="workload"):
        validate_palace_run_manifest(run.manifest_path)


def _accepted_run(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(monkeypatch, tmp_path, values, values)
    return _run_with_test_resource_policy(
        manifest_path, executable=executable, build_manifest=build_manifest,
        monkeypatch=monkeypatch,
    )


def test_resource_decision_rejects_nonidentical_config_and_mesh_manifests(
        tmp_path, monkeypatch):
    observation_directory = tmp_path / "observation"
    observation_directory.mkdir()
    observation_run = _accepted_run(observation_directory, monkeypatch)
    target_directory = tmp_path / "target"
    target_directory.mkdir()
    _, manifest_path = _write_config(target_directory)
    raw = np.array([[3e-12, -1e-12], [-1e-12, 4e-12]])
    _patch_process_run(monkeypatch, target_directory, raw, raw.copy())
    executable = observation_directory / "palace"
    build_manifest = observation_directory / "build-manifest.json"
    with pytest.raises(ValueError, match="not comparable"):
        write_palace_resource_decision(
            manifest_path,
            executable=executable,
            build_manifest_path=build_manifest,
            completed_run_manifest_paths=(observation_run.manifest_path,),
        )
    assert not list(target_directory.glob("*.resource-decision.*.json"))


def test_user_space_run_needs_no_resource_authority(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values
    )
    run = run_palace(
        manifest_path,
        executable=executable,
        build_manifest_path=build_manifest,
    )
    stored = json.loads(run.manifest_path.read_text())
    assert stored["resource_decision"] is None
    assert stored["resource_class"] == "synthetic"
    witness = validate_palace_execution_witness(run.manifest_path)
    assert witness["resource_decision_sha256"] is None


def test_decisionless_rejected_run_remains_validated_evidence(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, warning="Warning! rejected\n",
    )
    with pytest.raises(PalaceRunRejected) as caught:
        run_palace(
            manifest_path, executable=executable,
            build_manifest_path=build_manifest,
        )
    rejected = validate_palace_attempt_manifest(caught.value.manifest_path)
    witness = validate_palace_execution_witness(caught.value.manifest_path)
    assert rejected["matrix_available"] is False
    assert witness["resource_decision_sha256"] is None


def test_checkpointed_run_requires_campaign_authority(tmp_path, monkeypatch):
    checkpoint = {
        "path": str((tmp_path / "checkpoint-store").resolve()),
        "native_campaign_identity": "a" * 64,
    }
    _, manifest_path = _write_config(tmp_path, checkpoint=checkpoint)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(monkeypatch, tmp_path, values, values)
    with pytest.raises(
            ValueError, match="resource decision and canonical campaign attempt"):
        run_palace(
            manifest_path, executable=executable,
            build_manifest_path=build_manifest,
        )
    assert not (tmp_path / "postpro").exists()


def test_checkpoint_completion_metadata_counts_only_new_solves(tmp_path):
    checkpoint = {
        "path": str((tmp_path / "checkpoint-store").resolve()),
        "native_campaign_identity": "a" * 64,
    }
    config_path, _ = _write_config(tmp_path, checkpoint=checkpoint)
    output = tmp_path / "postpro"
    output.mkdir()
    _write_completion_artifacts(output, config_path)
    metadata_path = output / "palace.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["ElapsedTime"]["Counts"]["LinearSolve"] = 1
    metadata["LinearSolver"] = {"TotalIts": 5, "TotalSolves": 1}
    metadata_path.write_text(json.dumps(metadata))

    _, _, validated = palace._validate_completion_metadata(
        output, load_palace_config_manifest(config_path.with_suffix(
            config_path.suffix + ".manifest.json"
        )), processes=1,
    )
    assert validated["LinearSolver"]["TotalSolves"] == 1


def test_post_snapshot_oserror_persists_rejection_and_quarantines(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(monkeypatch, tmp_path, values, values)
    def invalidate_snapshot(record, *args, **kwargs):
        Path(record["inputs"][0]["snapshot"]).unlink()
        raise FileNotFoundError("snapshot input disappeared")

    monkeypatch.setattr(
        palace, "validate_execution_snapshot", invalidate_snapshot,
    )
    with pytest.raises(PalaceRunRejected) as caught:
        run_palace(
            manifest_path, executable=executable,
            build_manifest_path=build_manifest,
        )
    rejected = validate_palace_attempt_manifest(caught.value.manifest_path)
    assert rejected["matrix_available"] is False
    assert any(failure.startswith("execution_snapshot:")
               for failure in caught.value.failures)
    snapshot_root = Path(
        json.loads(caught.value.manifest_path.read_text())["execution_snapshot"]["root"]
    )
    assert not [path for path in snapshot_root.rglob("terminal-C*.csv")
                if path.is_file() or path.is_symlink()]
    assert len(list(snapshot_root.rglob("terminal-C*.quarantined"))) == 2


def test_hostile_quarantine_directory_cannot_escape_rejection(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, warning="Warning! rejected\n",
    )
    fake_run = palace.run_monitored_process

    def hostile_run(*args, **kwargs):
        result = fake_run(*args, **kwargs)
        hostile = Path(kwargs["cwd"]) / "postpro" / ".quarantine"
        hostile.mkdir()
        (hostile / "attacker").write_text("occupied")
        return result

    monkeypatch.setattr(palace, "run_monitored_process", hostile_run)
    with pytest.raises(PalaceRunRejected) as caught:
        run_palace(
            manifest_path, executable=executable,
            build_manifest_path=build_manifest,
        )
    assert caught.value.manifest_path.is_file()
    assert len(list(tmp_path.rglob("terminal-C*.quarantined"))) == 4
    assert not [path for path in tmp_path.rglob("terminal-C*.csv") if path.is_file() or path.is_symlink()]


def test_quarantine_oserror_purges_live_matrices_before_rejection(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, warning="Warning! rejected\n",
    )

    def unavailable(*_args, **_kwargs):
        raise OSError("injected quarantine failure")

    monkeypatch.setattr(palace_runtime, "quarantine_matrix_files", unavailable)
    with pytest.raises(PalaceRunRejected) as caught:
        run_palace(
            manifest_path, executable=executable,
            build_manifest_path=build_manifest,
        )
    rejected = validate_palace_attempt_manifest(caught.value.manifest_path)
    assert rejected["matrix_available"] is False
    assert not [path for path in tmp_path.rglob("terminal-C*.csv") if path.is_file() or path.is_symlink()]
    assert any(failure.startswith("matrix_quarantine:")
               for failure in caught.value.failures)


def test_partial_quarantine_cleanup_failure_binds_surviving_evidence(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, warning="Warning! rejected\n",
    )
    real_append = palace_runtime._append_capture
    calls = 0

    def fail_second_capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second capture failure")
        return real_append(*args, **kwargs)

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(palace_runtime, "_append_capture", fail_second_capture)
    monkeypatch.setattr(palace_runtime.shutil, "rmtree", fail_cleanup)
    with pytest.raises(PalaceRunRejected) as caught:
        run_palace(
            manifest_path, executable=executable,
            build_manifest_path=build_manifest,
        )
    validate_palace_attempt_manifest(caught.value.manifest_path)
    artifacts = json.loads(
        caught.value.manifest_path.read_text()
    )["artifacts"]
    assert any(path.endswith(".quarantined") for path in artifacts)
    assert not [path for path in tmp_path.rglob("terminal-C*.csv")
                if path.is_file() or path.is_symlink()]


def test_runtime_git_tag_must_bind_build_source(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])

    def alter_metadata(value):
        value["GitTag"] = "diagnostic-runtime-tag"

    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values,
        metadata_mutator=alter_metadata,
    )
    with pytest.raises(PalaceRunRejected) as caught:
        run_palace(
            manifest_path, executable=executable,
            build_manifest_path=build_manifest,
        )
    assert any("runtime Git/build identity mismatch" in failure
               for failure in caught.value.failures)


def test_completed_observation_produces_finite_resource_decision(
        tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)
    raw = json.loads(run.manifest_path.read_text())
    decision_path = write_palace_resource_decision(
        raw["config_manifest"],
        executable=next(
            item["source"] for item in raw["execution_snapshot"]["inputs"]
            if item["role"] == "executable"
        ),
        build_manifest_path=raw["build_manifest"],
        completed_run_manifest_paths=(run.manifest_path,),
    )
    decision = json.loads(decision_path.read_text())
    assert decision["selected_profile_id"] == "synthetic"
    projection = decision["projection"]
    assert all(bound["upper"] is not None for name, bound in projection.items()
               if name not in {"uncertainty_reasons", "observation_sha256"})


def test_missing_completed_observation_creates_no_resource_decision(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    build_manifest = tmp_path / "build-manifest.json"
    build_manifest.write_text("{}\n")
    monkeypatch.setattr(
        palace,
        "validate_palace_build_manifest",
        lambda path, *, executable: {
            "provenance_sha256": "test-build",
            "provenance": {"source_commit": "test-build-commit"},
        },
    )
    with pytest.raises(ValueError, match="completed observation"):
        write_palace_resource_decision(
            manifest_path,
            executable=executable,
            build_manifest_path=build_manifest,
            completed_run_manifest_paths=(),
        )
    assert not list(tmp_path.glob("*.resource-decision.*.json"))
    assert not (tmp_path / "postpro").exists()


def _rewrite_run_identity(path, mutate):
    value = json.loads(path.read_text())
    mutate(value)
    value.pop("content_sha256")
    identity = {key: item for key, item in value.items() if key != "format"}
    value["content_sha256"] = canonical_sha256(identity)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _rebind_stdout_event(value, stdout):
    events = value["execution"]["stream_events"]
    assert len(events) == 1 and events[0]["source"] == "stdout"
    events[0]["byte_count"] = len(stdout)
    events[0]["sha256"] = bytes_sha256(stdout)
    value["execution"]["resource_samples"][-1]["output_bytes"] = len(stdout)
    value["execution"]["palace_progress_events"] = (
        palace._palace_progress_events(stdout, events)
    )


def test_completed_progress_rejects_duplicate_or_reordered_rhs_boundaries():
    stdout = _successful_stdout()
    stream_events = [{
        "source": "stdout", "byte_offset": 0, "byte_count": len(stdout),
        "monotonic_ns": 1, "sha256": bytes_sha256(stdout),
    }]
    events = palace._palace_progress_events(stdout, stream_events)
    palace._validate_completed_palace_progress(
        events, 2, checkpoint_enabled=False,
    )
    with pytest.raises(ValueError, match="differs from the manifest"):
        palace._validate_completed_palace_progress(
            events, 2, checkpoint_enabled=True,
        )
    rebound = list(events)
    rebound.insert(3, events[2])
    with pytest.raises(ValueError, match="sequence is incomplete"):
        palace._validate_completed_palace_progress(rebound, 2)
    with pytest.raises(ValueError, match="campaign identity"):
        palace._validate_completed_palace_progress(
            events, 2, checkpoint_enabled=False,
            campaign_digest="f" * 64,
        )
    wrong_terminal = deepcopy(events)
    first_start = next(event for event in wrong_terminal
                       if event["event"] == "rhs_start")
    first_start["detail"]["terminal"] = 999
    with pytest.raises(ValueError, match="RHS start"):
        palace._validate_completed_palace_progress(
            wrong_terminal, 2, checkpoint_enabled=False,
            campaign_digest="disabled", terminal_indices=(1, 2),
        )


def test_progress_timestamp_uses_chunk_containing_final_marker_byte():
    stdout = (
        b"Computing electrostatic fields for 2 terminal boundar"
        b"ies\n"
    )
    split = len(stdout) - 4
    events = [
        {
            "source": "stdout", "byte_offset": 0, "byte_count": split,
            "monotonic_ns": 1, "sha256": bytes_sha256(stdout[:split]),
        },
        {
            "source": "stdout", "byte_offset": split,
            "byte_count": len(stdout) - split, "monotonic_ns": 100,
            "sha256": bytes_sha256(stdout[split:]),
        },
    ]
    progress = palace._palace_progress_events(stdout, events)
    assert progress[0]["observed_monotonic_ns"] == 100


def test_persisted_run_records_and_revalidates_palace_progress(
        tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)
    identity = json.loads(run.manifest_path.read_text())
    progress = identity["execution"]["palace_progress_events"]
    completed = [
        event for event in progress
        if event["event"] == "explicit_residual_completed"
    ]
    assert [event["rhs_ordinal"] for event in completed] == [1, 2]
    assert all(
        event["time_semantics"] == "reader_receipt_upper_bound"
        for event in completed
    )
    validate_palace_run_manifest(run.manifest_path)

    def rebind(value):
        value["execution"]["palace_progress_events"][0]["rhs_ordinal"] = 9

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="progress-event witness"):
        validate_palace_run_manifest(run.manifest_path)


def test_persisted_run_rejects_rebound_forbidden_resolved_settings(
        tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)
    resolved = tmp_path / "postpro/config_resolved.json"
    value = json.loads(resolved.read_text())
    value["Problem"]["Type"] = "Magnetostatic"
    value["Model"]["Refinement"]["MaxIts"] = 7
    value["Solver"]["Device"] = "GPU"
    value["Solver"]["Linear"]["KSPType"] = "GMRES"
    resolved.write_text(json.dumps(value))

    def rebind(identity):
        identity["artifacts"][str(resolved)] = file_sha256(resolved)

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="resolved config"):
        validate_palace_run_manifest(run.manifest_path)


@pytest.mark.parametrize(("setting_path", "changed"), [
    (("Solver", "Electrostatic", "Save"), 1),
    (("Problem", "Verbose"), 1),
    (("Problem", "OutputFormats", "Paraview"), False),
    (("Model", "CleanUnusedElements"), False),
    (("Model", "Refinement", "Tol"), 0.02),
    (("Solver", "PartialAssemblyOrder"), 2),
    (("Solver", "Linear", "AMSMaxIts"), 2),
    (("Solver", "Linear", "MGSmoothOrder"), 6),
    (("Solver", "Linear", "VerificationTol"), 1e-9),
    (("Solver", "QuadratureOrderExtra"), 1),
    (("Solver", "Linear", "InitialGuess"), False),
])
def test_persisted_run_rejects_rebound_resolved_default(
        tmp_path, monkeypatch, setting_path, changed):
    run = _accepted_run(tmp_path, monkeypatch)
    resolved = tmp_path / "postpro/config_resolved.json"
    value = json.loads(resolved.read_text())
    target = value
    for key in setting_path[:-1]:
        target = target[key]
    target[setting_path[-1]] = changed
    resolved.write_text(json.dumps(value))

    def rebind(identity):
        identity["artifacts"][str(resolved)] = file_sha256(resolved)

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="resolved config"):
        validate_palace_run_manifest(run.manifest_path)


def test_persisted_run_rejects_rebound_incomplete_completion_metadata(
        tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)
    metadata = tmp_path / "postpro/palace.json"
    value = json.loads(metadata.read_text())
    value["ElapsedTime"]["Counts"]["Total"] = 0
    metadata.write_text(json.dumps(value))

    def rebind(identity):
        identity["artifacts"][str(metadata)] = file_sha256(metadata)
        identity["runtime_metadata"] = value

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="runtime"):
        validate_palace_run_manifest(run.manifest_path)


@pytest.mark.parametrize("metadata_path", [
    ("ElapsedTime", "Counts", "Total"),
    ("LinearSolver", "TotalIts"),
    ("Problem", "DegreesOfFreedom"),
])
def test_persisted_run_rejects_rebound_boolean_runtime_metadata(
        tmp_path, monkeypatch, metadata_path):
    run = _accepted_run(tmp_path, monkeypatch)
    metadata = tmp_path / "postpro/palace.json"
    value = json.loads(metadata.read_text())
    target = value
    for key in metadata_path[:-1]:
        target = target[key]
    target[metadata_path[-1]] = True
    metadata.write_text(json.dumps(value))

    def rebind(identity):
        identity["artifacts"][str(metadata)] = file_sha256(metadata)
        identity["runtime_metadata"] = value

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="runtime"):
        validate_palace_run_manifest(run.manifest_path)


@pytest.mark.parametrize(("field", "replacement"), [
    ("elapsed_s", 0.0),
    ("peak_rss_bytes", 1024.0),
    ("output_bytes", 160.0),
    ("directory_growth_bytes", 100.0),
    ("max_refined_panels", 0.0),
    ("max_gmres_iteration", 0.0),
])
def test_persisted_run_rejects_rebound_execution_scalar_substitution(
        tmp_path, monkeypatch, field, replacement):
    run = _accepted_run(tmp_path, monkeypatch)

    def rebind(identity):
        identity["execution"][field] = replacement

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="execution"):
        validate_palace_run_manifest(run.manifest_path)


def test_persisted_run_rejects_rehashed_resource_summary_without_samples(
        tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)

    def rebind(identity):
        identity["execution"]["elapsed_s"] = 0.06
        identity["execution"]["peak_rss_bytes"] = 600
        identity["execution"]["directory_growth_bytes"] = 0

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="resource summary|elapsed time"):
        validate_palace_run_manifest(run.manifest_path)


@pytest.mark.parametrize(("metadata_path", "replacement"), [
    (("ElapsedTime", "Durations", "Solve"), 0),
    (("PeakMemoryMegabytes", "Max"), 1),
    (("PeakNodeMemoryGrowthMegabytes", "Max", "Solve"), 0),
])
def test_persisted_run_rejects_rebound_runtime_scalar_substitution(
        tmp_path, monkeypatch, metadata_path, replacement):
    run = _accepted_run(tmp_path, monkeypatch)
    metadata = tmp_path / "postpro/palace.json"
    value = json.loads(metadata.read_text())
    target = value
    for key in metadata_path[:-1]:
        target = target[key]
    target[metadata_path[-1]] = replacement
    metadata.write_text(json.dumps(value))

    def rebind(identity):
        identity["artifacts"][str(metadata)] = file_sha256(metadata)
        identity["runtime_metadata"] = value

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="runtime"):
        validate_palace_run_manifest(run.manifest_path)


@pytest.mark.parametrize("contradiction", [
    "growth_order", "count_duration", "mpi_total", "rank_peak_rss",
])
def test_persisted_run_rejects_rebound_runtime_contradiction(
        tmp_path, monkeypatch, contradiction):
    run = _accepted_run(tmp_path, monkeypatch)
    metadata = tmp_path / "postpro/palace.json"
    value = json.loads(metadata.read_text())
    if contradiction == "growth_order":
        value["PeakMemoryGrowthMegabytes"]["Min"]["Solve"] = 0.02
    elif contradiction == "count_duration":
        value["ElapsedTime"]["Durations"]["LinearSolve"] = 0.0
    elif contradiction == "mpi_total":
        value["PeakMemoryMegabytes"]["Total"] = 0.001
    else:
        for key in ("Average", "Max", "Min", "Total"):
            value["PeakMemoryMegabytes"][key] = 2.0
    metadata.write_text(json.dumps(value))

    def rebind(identity):
        identity["artifacts"][str(metadata)] = file_sha256(metadata)
        identity["runtime_metadata"] = value

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="runtime|RSS"):
        validate_palace_run_manifest(run.manifest_path)


@pytest.mark.parametrize("field", [
    "lifecycle", "execution", "residuals", "residual_terminal_index",
    "implementation_files", "build_provenance_sha256",
])
def test_persisted_run_rejects_rebound_run_witness_fields(
        tmp_path, monkeypatch, field):
    run = _accepted_run(tmp_path, monkeypatch)

    def rebind(identity):
        if field == "lifecycle":
            identity[field] = "rejected_diagnostic"
        elif field == "execution":
            identity[field]["returncode"] = 1
        elif field == "residuals":
            identity[field][0]["relative_residual"] = 9e-11
        elif field == "residual_terminal_index":
            identity["residuals"][0]["terminal"]["index"] = True
        elif field == "implementation_files":
            identity[field][next(iter(identity[field]))] = "0" * 64
        else:
            identity[field] = "changed-build"

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError):
        validate_palace_run_manifest(run.manifest_path)


def test_persisted_run_rejects_rebound_negative_residual(tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)
    identity = json.loads(run.manifest_path.read_text())
    old_path = Path(next(
        path for path in identity["artifacts"] if ".stdout." in Path(path).name
    ))
    stdout = old_path.read_bytes().replace(
        b"= 1.00000000000000000e-12",
        b"= -1.00000000000000000e-12",
        1,
    )
    stdout_sha256 = palace.bytes_sha256(stdout)
    new_path = old_path.with_name(f"config.json.stdout.{stdout_sha256}.bin")
    new_path.write_bytes(stdout)
    old_path.unlink()

    def rebind(value):
        value["artifacts"].pop(str(old_path))
        value["artifacts"][str(new_path)] = file_sha256(new_path)
        value["stdout_sha256"] = stdout_sha256
        value["execution"]["output_bytes"] = len(stdout)
        _rebind_stdout_event(value, stdout)
        value["residuals"][0]["relative_residual"] = -1e-12

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="explicit residual gate"):
        validate_palace_run_manifest(run.manifest_path)


def test_persisted_run_rejects_rebound_residual_target(tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)
    identity = json.loads(run.manifest_path.read_text())
    old_path = Path(next(
        path for path in identity["artifacts"] if ".stdout." in Path(path).name
    ))
    stdout = old_path.read_bytes().replace(
        b"target = 1.00000000000000004e-10",
        b"target = 1.00000000000000006e-09",
        1,
    )
    stdout_sha256 = palace.bytes_sha256(stdout)
    new_path = old_path.with_name(f"config.json.stdout.{stdout_sha256}.bin")
    new_path.write_bytes(stdout)
    old_path.unlink()

    def rebind(value):
        value["artifacts"].pop(str(old_path))
        value["artifacts"][str(new_path)] = file_sha256(new_path)
        value["stdout_sha256"] = stdout_sha256
        value["execution"]["output_bytes"] = len(stdout)
        _rebind_stdout_event(value, stdout)
        value["residuals"][0]["target"] = 1e-9

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="explicit residual gate"):
        validate_palace_run_manifest(run.manifest_path)


def test_persisted_run_rejects_rebound_warning_stream(tmp_path, monkeypatch):
    run = _accepted_run(tmp_path, monkeypatch)
    identity = json.loads(run.manifest_path.read_text())
    old_path = Path(next(
        path for path in identity["artifacts"] if ".stdout." in Path(path).name
    ))
    stdout = old_path.read_bytes() + b"Warning! rebound warning\n"
    stdout_sha256 = palace.bytes_sha256(stdout)
    new_path = old_path.with_name(f"config.json.stdout.{stdout_sha256}.bin")
    new_path.write_bytes(stdout)
    old_path.unlink()

    def rebind(value):
        value["artifacts"].pop(str(old_path))
        value["artifacts"][str(new_path)] = file_sha256(new_path)
        value["stdout_sha256"] = stdout_sha256
        value["execution"]["output_bytes"] = len(stdout)
        _rebind_stdout_event(value, stdout)

    _rewrite_run_identity(run.manifest_path, rebind)
    with pytest.raises(ValueError, match="warning or failure"):
        validate_palace_run_manifest(run.manifest_path)


def test_run_rejects_nonreciprocal_raw_matrix_before_standard_copy(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    raw = np.array([[3e-12, -2e-12], [-1e-12, 4e-12]])
    standard = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])

    build_manifest = _patch_process_run(monkeypatch, tmp_path, raw, standard)
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    assert "raw_matrix_gate: reciprocity" in caught.value.failures


def test_run_rejects_native_ansi_palace_warning(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch,
        tmp_path,
        values,
        values,
        warning="\x1b[38;2;255;255;000m--> Warning!\x1b[0m\n",
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    assert any("solver_diagnostic: --> Warning!" == item for item in caught.value.failures)
