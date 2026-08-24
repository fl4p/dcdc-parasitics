#!/usr/bin/env python3
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

import palace_reservation  # noqa: E402
from palace_reservation import derive_attempt_reservation  # noqa: E402
from palace_resources import (  # noqa: E402
    PalaceResourceProfile,
    PalaceResourceProjection,
    PalaceTopologyWorkload,
    ResourceBound,
    build_palace_resource_decision,
    build_palace_workload,
)
from provenance import canonical_sha256, file_sha256  # noqa: E402


def fixture(tmp_path):
    inputs = {}
    for name, content in {
            "config.json": b"config", "mesh.msh": b"mesh",
            "palace": b"binary", "mpirun": b"launcher"}.items():
        path = tmp_path / name
        path.write_bytes(content)
        inputs[name] = path
    topology = PalaceTopologyWorkload(
        node_count=4, edge_count=6, face_count=4, tetrahedron_count=1,
        order=2, terminal_count=2, process_count=1,
    ).record()
    workload = build_palace_workload(
        topology_workload=topology,
        mesh_sha256=file_sha256(inputs["mesh.msh"]),
        mesh_manifest_sha256="2" * 64,
        config_sha256=file_sha256(inputs["config.json"]),
        config_manifest_sha256="4" * 64,
        build_manifest_sha256="5" * 64,
        solver_binary_sha256=file_sha256(inputs["palace"]),
        mpi_launcher_sha256=file_sha256(inputs["mpirun"]),
        implementation_sha256="8" * 64,
        runtime_binaries=[{
            "name": "palace", "sha256": file_sha256(inputs["palace"]),
        }],
        host_class="test-host", linear_tolerance=1e-12,
        explicit_residual_tolerance=1e-10, maximum_iterations=100,
    )
    projection = PalaceResourceProjection(
        wall_time_s=ResourceBound(1.0, 10.0),
        cpu_time_s=ResourceBound(2.0, 20.0),
        vector_residency_bytes=ResourceBound(1, 100),
        hierarchy_operator_solver_bytes=ResourceBound(1, 200),
        retained_terminal_vectors_bytes=ResourceBound(1000, 1000),
        peak_rss_bytes=ResourceBound(1000, 2000),
        matrix_output_bytes=ResourceBound(10, 100),
        logging_output_bytes=ResourceBound(20, 200),
        checkpoint_write_bytes=ResourceBound(30, 300),
        checkpoint_read_bytes=ResourceBound(40, 400),
        uncertainty_reasons=("test bound",),
        observation_sha256=("a" * 64,),
    )
    profile = PalaceResourceProfile(
        profile_id="test", rank=1, wall_time_s=100.0, cpu_time_s=200.0,
        peak_rss_bytes=10_000, output_bytes=10_000,
        nodes=100, tetrahedra=100,
    )
    policy_id = "test-policy"
    policy_sha256 = "b" * 64
    validator_sha256 = "c" * 64
    decision = build_palace_resource_decision(
        palace_workload=workload, projection=projection, profiles=(profile,),
        authorized_profile_ids=("test",), policy_id=policy_id,
        policy_sha256=policy_sha256, validator_sha256=validator_sha256,
        minimum_headroom_ratio=1.1,
    )
    policy = {
        "profile_objects": (profile,),
        "authorized_profile_tuple": ("test",),
        "policy_id": policy_id,
        "policy_sha256": policy_sha256,
        "validator_sha256": validator_sha256,
        "minimum_headroom_ratio": 1.1,
        "authorized_static_projection_tuple": (
            canonical_sha256(decision["projection"]),
        ),
    }
    records = []
    for role, name in (
            ("config", "config.json"), ("mesh", "mesh.msh"),
            ("executable", "palace"), ("mpi_launcher", "mpirun")):
        path = inputs[name]
        records.append({
            "role": role,
            "source": str(path),
            "source_sha256": file_sha256(path),
            "snapshot": str(path),
            "snapshot_sha256": file_sha256(path),
        })
    snapshot_payload = {
        "format": "palace-execution-snapshot-v1",
        "root": str(tmp_path),
        "workload_sha256": workload["content_sha256"],
        "inputs": records,
    }
    snapshot = {
        **snapshot_payload,
        "content_sha256": canonical_sha256(snapshot_payload),
    }
    return inputs, workload, decision, policy, snapshot


def test_attempt_reservation_derives_complete_finite_accounting(
        tmp_path, monkeypatch):
    inputs, workload, decision, policy, snapshot = fixture(tmp_path)
    monkeypatch.setattr(
        palace_reservation, "validate_execution_snapshot_privilege_boundary",
        lambda value: True,
    )
    result = derive_attempt_reservation(
        decision, snapshot, expected_workload=workload,
        trusted_policy=policy,
    )
    input_bytes = sum(path.stat().st_size for path in inputs.values())
    assert result["input_bytes_per_process"] == input_bytes
    assert result["decision_sha256"] == decision["content_sha256"]
    assert result["reservation"] == {
        "wall_time_s": 10.0,
        "cpu_time_s": 20.0,
        "iterations": 200,
        "solves": 2,
        "bytes_written": 600,
        "bytes_read": input_bytes + 400,
        "stdout_bytes": 200,
        "stderr_bytes": 200,
        "checkpoint_write_bytes": 300,
        "checkpoint_read_bytes": 400,
        "retained_output_bytes": 300,
        "retained_checkpoint_bytes": 300,
        "peak_rss_bytes": 2000,
    }


def test_attempt_reservation_rejects_tampered_snapshot_input(
        tmp_path, monkeypatch):
    inputs, workload, decision, policy, snapshot = fixture(tmp_path)
    monkeypatch.setattr(
        palace_reservation, "validate_execution_snapshot_privilege_boundary",
        lambda value: True,
    )
    inputs["mesh.msh"].write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        derive_attempt_reservation(
            decision, snapshot, expected_workload=workload,
            trusted_policy=policy,
        )


def test_attempt_reservation_rejects_unauthorized_rebuilt_projection(
        tmp_path, monkeypatch):
    _, workload, decision, policy, snapshot = fixture(tmp_path)
    monkeypatch.setattr(
        palace_reservation, "validate_execution_snapshot_privilege_boundary",
        lambda value: True,
    )
    changed = dict(decision)
    changed["projection"] = {
        **decision["projection"],
        "wall_time_s": {"lower": 1.0, "upper": 9.0},
    }
    unsigned = dict(changed)
    unsigned.pop("content_sha256")
    changed["content_sha256"] = canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="projection is not authorized"):
        derive_attempt_reservation(
            changed, snapshot, expected_workload=workload,
            trusted_policy=policy,
        )


def test_attempt_reservation_rejects_self_rehashed_policy_substitution(
        tmp_path, monkeypatch):
    _, workload, decision, policy, snapshot = fixture(tmp_path)
    monkeypatch.setattr(
        palace_reservation, "validate_execution_snapshot_privilege_boundary",
        lambda value: True,
    )
    changed = dict(decision)
    changed["selected_profile_id"] = "forged"
    unsigned = dict(changed)
    unsigned.pop("content_sha256")
    changed["content_sha256"] = canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="differs from trusted policy"):
        derive_attempt_reservation(
            changed, snapshot, expected_workload=workload,
            trusted_policy=policy,
        )
