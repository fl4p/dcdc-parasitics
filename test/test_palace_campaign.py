#!/usr/bin/env python3
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import palace  # noqa: E402
import palace_campaign  # noqa: E402
from palace_campaign import (  # noqa: E402
    CheckpointCampaignLedger,
    build_campaign_identity,
    build_campaign_identity_v2,
    charge_attempt_reservation_v2,
    checkpoint_validator_sha256,
    load_campaign_identity_v2,
    new_native_campaign_identity,
    reconcile_campaign_attempt_v2,
    validate_campaign_identity_v2,
)
from palace import parse_palace_matrix_csv  # noqa: E402
from palace_head_authority import CanonicalHeadAuthority  # noqa: E402
import palace_ledger_v2  # noqa: E402
from palace_ledger_v2 import (  # noqa: E402
    CanonicalLedgerPublicationV2,
    ENTRY_FORMAT,
    _zero_accounting,
    validate_head_v2,
)
from palace_matrix_access import checkpoint_matrix_access  # noqa: E402
from palace_resources import PalaceTopologyWorkload, build_palace_workload  # noqa: E402
from provenance import canonical_sha256, file_sha256  # noqa: E402


AUTHORITY_KEY = b"test-checkpoint-authority-key-32-bytes-minimum"


def workload(*, config_sha256="3" * 64,
             config_manifest_sha256="4" * 64):
    topology = PalaceTopologyWorkload(
        node_count=4, edge_count=6, face_count=4, tetrahedron_count=1,
        order=2, terminal_count=2, process_count=1,
    ).record()
    return build_palace_workload(
        topology_workload=topology,
        mesh_sha256="1" * 64, mesh_manifest_sha256="2" * 64,
        config_sha256=config_sha256,
        config_manifest_sha256=config_manifest_sha256,
        build_manifest_sha256="5" * 64, solver_binary_sha256="6" * 64,
        mpi_launcher_sha256="7" * 64, implementation_sha256="8" * 64,
        runtime_binaries=[{"name": "palace", "sha256": "6" * 64}],
        host_class="test-host", linear_tolerance=1e-12,
        explicit_residual_tolerance=1e-10, maximum_iterations=100,
    )


def campaign(*, attempts=3, wall_time_s=100.0):
    integer_caps = {
        "iterations": 1000,
        "solves": 10,
        "bytes_written": 100_000,
        "bytes_read": 100_000,
        "stdout_bytes": 10_000,
        "stderr_bytes": 10_000,
        "checkpoint_write_bytes": 100_000,
        "checkpoint_read_bytes": 100_000,
        "retained_output_bytes": 100_000,
        "retained_checkpoint_bytes": 100_000,
        "peak_rss_bytes": 100_000,
    }
    return build_campaign_identity(
        workload=workload(),
        ordered_terminals=[
            {"index": 1, "name": "A", "attribute": 10},
            {"index": 2, "name": "B", "attribute": 11},
        ],
        checkpoint_format_sha256="9" * 64,
        resource_policy_sha256="a" * 64,
        cumulative_caps={
            "attempts": attempts,
            "wall_time_s": wall_time_s,
            "cpu_time_s": 200.0,
            **integer_caps,
        },
    )


def campaign_v2(tmp_path, *, workload_value=None):
    workload_value = workload_value or workload()
    root_path = tmp_path / "canonical-authority"
    root_path.mkdir()
    authority = CanonicalHeadAuthority(
        root_path,
        authority_id="test-authority-v1",
        authority_key=b"campaign v2 external authority key" * 2,
        client_uid=os.getuid() + 100_000,
    )
    finest = workload_value["topology_workload"]["h1_true_dofs_finest"]
    return build_campaign_identity_v2(
        workload=workload_value,
        ordered_terminals=[
            {"index": 1, "name": "A", "attribute": 10},
            {"index": 2, "name": "B", "attribute": 11},
        ],
        native_campaign_identity="b" * 64,
        checkpoint_validator_digest=checkpoint_validator_sha256(),
        checkpoint_partition={
            "process_count": 1,
            "global_true_dofs": finest,
            "local_true_dofs": [finest],
        },
        canonical_head_authority=authority,
        resource_policy_sha256="a" * 64,
        cumulative_caps=campaign()["cumulative_caps"],
    ), workload_value, authority


def test_campaign_v2_rejects_duck_typed_canonical_authority(tmp_path):
    _, workload_value, authority = campaign_v2(tmp_path)

    class ForgedAuthority:
        identity = authority.identity

    finest = workload_value["topology_workload"]["h1_true_dofs_finest"]
    with pytest.raises(ValueError, match="canonical authority"):
        build_campaign_identity_v2(
            workload=workload_value,
            ordered_terminals=[
                {"index": 1, "name": "A", "attribute": 10},
                {"index": 2, "name": "B", "attribute": 11},
            ],
            native_campaign_identity="b" * 64,
            checkpoint_validator_digest=checkpoint_validator_sha256(),
            checkpoint_partition={
                "process_count": 1,
                "global_true_dofs": finest,
                "local_true_dofs": [finest],
            },
            canonical_head_authority=ForgedAuthority(),
            resource_policy_sha256="a" * 64,
            cumulative_caps=campaign()["cumulative_caps"],
        )


def accounting(*, wall=10.0, solves=1):
    return {
        "wall_time_s": wall,
        "cpu_time_s": wall,
        "iterations": solves * 2,
        "solves": solves,
        "bytes_written": 300,
        "bytes_read": 100,
        "stdout_bytes": 100,
        "stderr_bytes": 0,
        "checkpoint_write_bytes": 200,
        "checkpoint_read_bytes": 0,
        "retained_output_bytes": 100,
        "retained_checkpoint_bytes": 200,
        "peak_rss_bytes": 1000,
    }


def projection_v2(value, workload_value, checkpoint_root="/tmp/checkpoint", **overrides):
    result = {
        "run_manifest": {
            "path": "/tmp/run.json", "sha256": "1" * 64,
            "content_sha256": "2" * 64,
        },
        "workload_sha256": workload_value["content_sha256"],
        "resource_decision_sha256": "d" * 64,
        "ordered_terminals": deepcopy(value["ordered_terminals"]),
        "checkpoint_enabled": True,
        "checkpoint": {
            "path": str(Path(checkpoint_root).resolve()),
            "native_campaign_identity": value["native_campaign_identity"],
        },
        "native_campaign_digest": hashlib.sha256(
            value["native_campaign_identity"].encode()
        ).hexdigest(),
        "outcome": "wall_timeout",
        "prefix_before": 0,
        "prefix_after": 1,
        "loaded_rhs_indices": [],
        "new_rhs_indices": [1],
        "resource_bounds": {
            name: {"lower": 0, "upper": None} for name in accounting()
        },
        "causal_classification": {
            "version": "palace-causal-timeout-v2",
            "monitor_initiated": True, "limit": "wall_time",
            "diagnostics_before_kill": [],
        },
        "final_artifacts": None,
    }
    result.update(overrides)
    return result


def causal(outcome):
    if outcome == "wall_timeout":
        return {
            "version": "palace-causal-timeout-v1",
            "monitor_initiated": True,
            "limit": "wall_time",
            "diagnostics_before_kill": [],
        }
    return {
        "version": "palace-causal-timeout-v1",
        "monitor_initiated": False,
        "limit": None,
        "diagnostics_before_kill": (
            ["solver failure"] if outcome == "terminal_failure" else []
        ),
    }


def artifacts(tmp_path, ledger, attempt, prefix_before, prefix_after,
              outcome, actual, causal_value):
    witnesses = []
    for role, size in (
            ("stdout", actual["stdout_bytes"]),
            ("stderr", actual["stderr_bytes"]),
            ("event_log", 2),
            ("resource_telemetry", 2)):
        path = tmp_path / f"{attempt}-{role}.bin"
        path.write_bytes(b"x" * size)
        witnesses.append({
            "role": role,
            "path": str(path.absolute()),
            "sha256": file_sha256(path),
            "bytes": size,
        })
    checkpoint_files = []
    if prefix_after:
        checkpoint = tmp_path / f"{attempt}-checkpoint.bin"
        checkpoint.write_bytes(b"c" * actual["retained_checkpoint_bytes"])
        checkpoint_files.append({
            "path": str(checkpoint.absolute()),
            "sha256": file_sha256(checkpoint),
            "bytes": checkpoint.stat().st_size,
        })
    inventory_payload = {
        "format": "palace-checkpoint-inventory-v1",
        "campaign_sha256": ledger.expected_campaign_sha256,
        "prefix": prefix_after,
        "terminal_indices": list(range(1, prefix_after + 1)),
        "files": checkpoint_files,
        "retained_bytes": sum(item["bytes"] for item in checkpoint_files),
    }
    inventory = tmp_path / f"{attempt}-inventory.json"
    inventory.write_text(json.dumps({
        **inventory_payload,
        "content_sha256": canonical_sha256(inventory_payload),
    }, sort_keys=True) + "\n")
    run_payload = {
        "format": "palace-checkpoint-attempt-v1",
        "campaign_sha256": ledger.expected_campaign_sha256,
        "attempt_id": attempt,
        "prefix_before": prefix_before,
        "prefix_after": prefix_after,
        "outcome": outcome,
        "loaded_rhs_indices": list(range(1, prefix_before + 1)),
        "new_rhs_indices": list(range(prefix_before + 1, prefix_after + 1)),
        "accounting": actual,
        "causal_classification": causal_value,
        "checkpoint_inventory_sha256": file_sha256(inventory),
        "witnesses": witnesses,
    }
    run = tmp_path / f"{attempt}-run.json"
    run.write_text(json.dumps({
        **run_payload,
        "content_sha256": canonical_sha256(run_payload),
    }, sort_keys=True) + "\n")
    return run, inventory


def create_ledger(tmp_path, value=None):
    value = campaign() if value is None else value
    return CheckpointCampaignLedger.create(
        tmp_path / "campaign",
        value,
        authority_key=AUTHORITY_KEY,
        trusted_campaign_sha256=value["content_sha256"],
    )


def register(ledger, attempt, prefix, reservation=None):
    ledger.register_attempt(
        attempt,
        prefix_before=prefix,
        resource_decision_sha256="b" * 64,
        resource_reservation=accounting(wall=20.0) if reservation is None else reservation,
    )


def finish(ledger, tmp_path, attempt, outcome, prefix, actual=None):
    prefix_before = ledger.validate()["head"]["prefix"]
    actual = accounting() if actual is None else actual
    if prefix == 0:
        actual = {**actual, "retained_checkpoint_bytes": 0}
    causal_value = causal(outcome)
    run, inventory = artifacts(
        tmp_path, ledger, attempt, prefix_before, prefix, outcome,
        actual, causal_value,
    )
    ledger.finish_attempt(
        attempt,
        outcome=outcome,
        prefix_after=prefix,
        run_manifest_path=run,
        checkpoint_inventory_path=inventory,
        accounting=actual,
        causal_classification=causal_value,
    )


def test_native_campaign_identity_generation_is_unique_and_canonical():
    values = {new_native_campaign_identity() for _ in range(16)}
    assert len(values) == 16
    assert all(len(value) == 64 and value == value.lower() for value in values)


def test_campaign_v2_binds_checkpoint_partition_and_external_authority(tmp_path):
    value, workload_value, authority = campaign_v2(tmp_path)
    assert validate_campaign_identity_v2(value, expected_campaign_sha256=value["content_sha256"], expected_workload=workload_value, expected_authority_identity=authority.identity) == value
    assert value["checkpoint_partition"]["process_count"] == 1
    assert value["canonical_head_authority"]["authority_id"] == "test-authority-v1"


@pytest.mark.parametrize("field", ["process_count", "global_true_dofs"])
def test_campaign_v2_rejects_bool_partition_rebinding(tmp_path, field):
    value, workload_value, authority = campaign_v2(tmp_path)
    value["checkpoint_partition"][field] = True
    unsigned = dict(value)
    unsigned.pop("content_sha256")
    value["content_sha256"] = canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="partition"):
        validate_campaign_identity_v2(value, expected_campaign_sha256=value["content_sha256"], expected_workload=workload_value, expected_authority_identity=authority.identity)


def test_campaign_v2_builder_rejects_untrusted_validator(tmp_path):
    value, _, authority = campaign_v2(tmp_path)
    with pytest.raises(ValueError, match="validator is not trusted"):
        build_campaign_identity_v2(
            workload=workload(), ordered_terminals=value["ordered_terminals"],
            native_campaign_identity="b" * 64,
            checkpoint_validator_digest="9" * 64,
            checkpoint_partition=value["checkpoint_partition"],
            canonical_head_authority=authority,
            resource_policy_sha256="a" * 64,
            cumulative_caps=value["cumulative_caps"],
        )


def test_campaign_v2_builder_rejects_partition_workload_mismatch(tmp_path):
    value, _, authority = campaign_v2(tmp_path)
    partition = dict(value["checkpoint_partition"])
    partition["global_true_dofs"] += 1
    partition["local_true_dofs"] = [partition["global_true_dofs"]]
    root = authority
    with pytest.raises(ValueError, match="partition"):
        build_campaign_identity_v2(
            workload=workload(), ordered_terminals=value["ordered_terminals"],
            native_campaign_identity="b" * 64,
            checkpoint_validator_digest=checkpoint_validator_sha256(),
            checkpoint_partition=partition,
            canonical_head_authority=root,
            resource_policy_sha256="a" * 64,
            cumulative_caps=value["cumulative_caps"],
        )


def test_campaign_v2_rejects_valid_partition_rehash_against_workload(tmp_path):
    value, workload_value, authority = campaign_v2(tmp_path)
    finest = value["checkpoint_partition"]["global_true_dofs"]
    value["checkpoint_partition"] = {
        "process_count": 2,
        "global_true_dofs": finest,
        "local_true_dofs": [finest, 0],
    }
    unsigned = dict(value)
    unsigned.pop("content_sha256")
    value["content_sha256"] = canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="differs from workload"):
        validate_campaign_identity_v2(value, expected_campaign_sha256=value["content_sha256"], expected_workload=workload_value, expected_authority_identity=authority.identity)


def test_campaign_v2_rejects_rehashed_roster_without_external_authorization(tmp_path):
    value, workload_value, authority = campaign_v2(tmp_path)
    trusted_digest = value["content_sha256"]
    value["ordered_terminals"][0]["name"] = "forged"
    unsigned = dict(value)
    unsigned.pop("content_sha256")
    value["content_sha256"] = canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="content hash mismatch"):
        validate_campaign_identity_v2(
            value, expected_campaign_sha256=trusted_digest,
            expected_workload=workload_value,
            expected_authority_identity=authority.identity,
        )


def test_campaign_v2_rejects_authority_instance_substitution(tmp_path):
    value, workload_value, authority = campaign_v2(tmp_path)
    other_root = tmp_path / "other-authority"
    other_root.mkdir()
    other = CanonicalHeadAuthority(
        other_root, authority_id="other", authority_key=b"other key" * 8,
        client_uid=os.getuid() + 100_000,
    )
    value["canonical_head_authority"] = other.identity
    unsigned = dict(value)
    unsigned.pop("content_sha256")
    value["content_sha256"] = canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="canonical authority"):
        validate_campaign_identity_v2(value, expected_campaign_sha256=value["content_sha256"], expected_workload=workload_value, expected_authority_identity=authority.identity)


def test_campaign_v2_strict_loader_rejects_duplicate_json(tmp_path):
    value, workload_value, authority = campaign_v2(tmp_path)
    path = tmp_path / "campaign-v2.json"
    content = json.dumps(value)
    path.write_text(content[:-1] + ',"format":"duplicate"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_campaign_identity_v2(path, expected_campaign_sha256=value["content_sha256"], expected_workload=workload_value, expected_authority_identity=authority.identity)


def test_campaign_v2_loader_rejects_rehashed_stale_validator(tmp_path):
    value, workload_value, authority = campaign_v2(tmp_path)
    value["checkpoint_contract"]["validator_sha256"] = "0" * 64
    unsigned = dict(value)
    unsigned.pop("content_sha256")
    value["content_sha256"] = canonical_sha256(unsigned)
    path = tmp_path / "campaign-v2.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="validator identity is stale"):
        load_campaign_identity_v2(
            path, expected_campaign_sha256=value["content_sha256"],
            expected_workload=workload_value,
            expected_authority_identity=authority.identity,
        )


@pytest.mark.parametrize("mutation", ["symlink", "oversized"])
def test_campaign_v2_strict_loader_rejects_unsafe_files(tmp_path, mutation):
    value, workload_value, authority = campaign_v2(tmp_path)
    path = tmp_path / "campaign-v2.json"
    if mutation == "symlink":
        target = tmp_path / "campaign-target.json"
        target.write_text(json.dumps(value))
        path.symlink_to(target.name)
    else:
        path.write_bytes(b"{" + b" " * (1024 * 1024 + 1) + b"}")
    with pytest.raises(ValueError, match="unsafe|bounded"):
        load_campaign_identity_v2(path, expected_campaign_sha256=value["content_sha256"], expected_workload=workload_value, expected_authority_identity=authority.identity)


def test_campaign_v2_reconciles_witness_and_native_checkpoint(
        tmp_path, monkeypatch):
    value, workload_value, authority = campaign_v2(tmp_path)
    projection = projection_v2(value, workload_value, checkpoint_root=tmp_path / "checkpoint")
    inventory = {
        "format": "palace-checkpoint-inventory-v2",
        "prefix": 1,
        "retained_bytes": 1234,
        "content_sha256": "3" * 64,
    }
    monkeypatch.setattr(
        palace_campaign, "validate_palace_execution_witness",
        lambda path: deepcopy(projection),
    )
    native_arguments = {}
    def validate_native(*args, **kwargs):
        native_arguments.update(kwargs)
        return deepcopy(inventory)
    monkeypatch.setattr(
        palace_campaign, "validate_native_checkpoint", validate_native,
    )
    result = reconcile_campaign_attempt_v2(value, expected_campaign_sha256=value["content_sha256"], expected_workload=workload_value, expected_authority_identity=authority.identity, run_manifest_path=tmp_path / "run.json", checkpoint_root=tmp_path / "checkpoint")
    assert result["prefix_after"] == 1
    assert result["checkpoint_inventory"] == inventory
    assert native_arguments["campaign_identity"] == value["native_campaign_identity"]
    assert native_arguments["ordered_terminal_indices"] == [1, 2]
    assert result["resource_bounds"]["retained_checkpoint_bytes"] == {
        "lower": 1234, "upper": 1234,
    }
    reservation = accounting(wall=20.0, solves=2)
    reservation["retained_checkpoint_bytes"] = 2000
    charged = charge_attempt_reservation_v2(result, reservation)
    assert charged["retained_checkpoint_bytes"] == 1234
    assert charged["bytes_read"] == reservation["bytes_read"]


@pytest.mark.parametrize("mutation", ["native", "roster", "disabled"])
def test_campaign_v2_reconciliation_rejects_execution_identity_mismatch(
        tmp_path, monkeypatch, mutation):
    value, workload_value, authority = campaign_v2(tmp_path)
    projection = projection_v2(value, workload_value, checkpoint_root=tmp_path / "checkpoint")
    if mutation == "native":
        projection["native_campaign_digest"] = "0" * 64
    elif mutation == "roster":
        projection["ordered_terminals"][0]["attribute"] = 99
    else:
        projection["checkpoint_enabled"] = False
    monkeypatch.setattr(
        palace_campaign, "validate_palace_execution_witness",
        lambda path: deepcopy(projection),
    )
    with pytest.raises(ValueError, match="attempt identity mismatch"):
        reconcile_campaign_attempt_v2(
            value, expected_campaign_sha256=value["content_sha256"],
            expected_workload=workload_value,
            expected_authority_identity=authority.identity,
            run_manifest_path=tmp_path / "run.json",
            checkpoint_root=tmp_path / "checkpoint",
        )


def test_campaign_v2_reconciliation_rejects_checkpoint_root_substitution(
        tmp_path, monkeypatch):
    value, workload_value, authority = campaign_v2(tmp_path)
    projection = projection_v2(
        value, workload_value, checkpoint_root=tmp_path / "authorized",
    )
    monkeypatch.setattr(
        palace_campaign, "validate_palace_execution_witness",
        lambda path: deepcopy(projection),
    )
    with pytest.raises(ValueError, match="attempt identity mismatch"):
        reconcile_campaign_attempt_v2(
            value, expected_campaign_sha256=value["content_sha256"],
            expected_workload=workload_value,
            expected_authority_identity=authority.identity,
            run_manifest_path=tmp_path / "run.json",
            checkpoint_root=tmp_path / "substituted",
        )


def test_campaign_v2_reconciliation_preserves_loaded_checkpoint_prefix(
        tmp_path, monkeypatch):
    value, workload_value, authority = campaign_v2(tmp_path)
    projection = projection_v2(
        value, workload_value, checkpoint_root=tmp_path / "checkpoint",
        prefix_before=1, prefix_after=0,
        loaded_rhs_indices=[], new_rhs_indices=[],
    )
    inventory = {"prefix": 1, "retained_bytes": 100}
    monkeypatch.setattr(
        palace_campaign, "validate_palace_execution_witness",
        lambda path: deepcopy(projection),
    )
    monkeypatch.setattr(
        palace_campaign, "validate_native_checkpoint",
        lambda *args, **kwargs: deepcopy(inventory),
    )
    result = reconcile_campaign_attempt_v2(
        value, expected_campaign_sha256=value["content_sha256"],
        expected_workload=workload_value,
        expected_authority_identity=authority.identity,
        run_manifest_path=tmp_path / "run.json",
        checkpoint_root=tmp_path / "checkpoint",
    )
    assert result["observed_prefix_after"] == 0
    assert result["prefix_after"] == 1


def registration_transition(campaign, predecessor, attempt_id="attempt-1"):
    reservation = _zero_accounting()
    payload = {
        "format": ENTRY_FORMAT,
        "campaign_sha256": campaign["content_sha256"],
        "sequence": predecessor["sequence"] + 1,
        "previous_entry_sha256": predecessor["entry_sha256"],
        "event": "attempt_registered",
        "attempt_id": attempt_id,
        "prefix_before": predecessor["prefix"],
        "resource_decision_sha256": "d" * 64,
        "resource_reservation": reservation,
    }
    entry = {**payload, "content_sha256": canonical_sha256(payload)}
    successor_payload = {
        **predecessor,
        "sequence": entry["sequence"],
        "entry_sha256": entry["content_sha256"],
        "active_attempt": attempt_id,
    }
    successor_payload.pop("content_sha256")
    successor = {
        **successor_payload,
        "content_sha256": canonical_sha256(successor_payload),
    }
    return entry, successor


def registration_recovery(entry):
    return {
        "resource_decision_sha256": entry["resource_decision_sha256"],
        "resource_reservation": entry["resource_reservation"],
    }


def ledger_reconciliation(campaign, root, *, outcome, before, after, witness):
    bounds = {
        name: {"lower": value, "upper": value}
        for name, value in _zero_accounting().items()
    }
    raw_matrix = root / "raw.csv"
    standard_matrix = root / "standard.csv"
    if outcome == "completed":
        raw_matrix.write_text(
            "i,C_raw[i][1] (F),C_raw[i][2] (F)\n"
            "1,2e-12,-1e-12\n2,-1e-12,2e-12\n"
        )
        standard_matrix.write_text(
            "i,C[i][1] (F),C[i][2] (F)\n"
            "1,2e-12,-1e-12\n2,-1e-12,2e-12\n"
        )
    payload = {
        "format": "palace-checkpoint-attempt-reconciliation-v2",
        "campaign_sha256": campaign["content_sha256"],
        "execution_witness": {
            "path": str((root / f"{witness}.json").resolve()),
            "sha256": witness[0] * 64,
            "content_sha256": witness[-1] * 64,
        },
        "workload_sha256": campaign["workload_sha256"],
        "resource_decision_sha256": "d" * 64,
        "outcome": outcome,
        "prefix_before": before,
        "observed_prefix_after": after,
        "prefix_after": after,
        "loaded_rhs_indices": list(range(1, before + 1)),
        "new_rhs_indices": list(range(before + 1, after + 1)),
        "resource_bounds": bounds,
        "causal_classification": {
            "version": "palace-causal-timeout-v1",
            "monitor_initiated": outcome == "wall_timeout",
            "limit": "wall_time" if outcome == "wall_timeout" else None,
            "diagnostics_before_kill": [],
        },
        "checkpoint_root": str((root / "checkpoint").resolve()),
        "checkpoint_inventory": {
            "prefix": after,
            "retained_bytes": 0,
            "content_sha256": "c" * 64,
        },
        "final_artifacts": (
            {
                "raw_matrix": {
                    "path": str(raw_matrix.resolve()),
                    "sha256": file_sha256(raw_matrix),
                },
                "standard_matrix": {
                    "path": str(standard_matrix.resolve()),
                    "sha256": file_sha256(standard_matrix),
                },
            }
            if outcome == "completed" else None
        ),
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}


def test_campaign_v2_register_and_finish_replay_derive_all_state(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ab",
    )
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    registration = ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    assert registration["event"] == "attempt_registered"
    completion = ledger.finish_attempt(
        "attempt-1", run_manifest_path=tmp_path / "ab.json",
        checkpoint_root=tmp_path / "checkpoint",
    )
    assert completion["event"] == "attempt_finished"
    head = json.loads(ledger.head_path.read_text())
    assert head["prefix"] == 1
    assert head["attempts_finished"] == 1
    assert head["active_attempt"] is None
    assert head["terminal"] is False
    ledger.close()
    reopened = CanonicalLedgerPublicationV2(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    assert reopened._head() == head
    with pytest.raises(ValueError, match="terminal replay"):
        reopened.matrix_access_record()


def test_campaign_v2_exact_ledger_rejects_helper_override(tmp_path):
    campaign, workload_value, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload_value,
    )
    with pytest.raises(AttributeError, match="immutable"):
        ledger._validate_local_state = lambda: {}
    with pytest.raises(AttributeError, match="immutable"):
        ledger._external_matches = lambda _head: None


def test_campaign_v2_public_registration_uses_trusted_derivation(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    observed = {}

    def derive(decision, snapshot, *, expected_workload, trusted_policy):
        observed.update({
            "decision": decision,
            "snapshot": snapshot,
            "workload": expected_workload,
            "policy": trusted_policy,
        })
        return {
            "decision_sha256": "d" * 64,
            "reservation": _zero_accounting(),
        }

    monkeypatch.setattr(palace_ledger_v2, "derive_attempt_reservation", derive)
    entry = ledger.register_attempt(
        "attempt-1", resource_decision={"decision": True},
        execution_snapshot={"snapshot": True},
        trusted_policy={"policy": True},
    )
    assert entry["resource_decision_sha256"] == "d" * 64
    assert observed == {
        "decision": {"decision": True},
        "snapshot": {"snapshot": True},
        "workload": workload,
        "policy": {"policy": True},
    }
    with pytest.raises(ValueError, match="active attempt"):
        ledger.register_attempt(
            "attempt-1", resource_decision={"decision": True},
            execution_snapshot={"snapshot": True},
            trusted_policy={"policy": True},
        )


def test_campaign_v2_registration_rejects_reservation_beyond_cap(tmp_path):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    reservation = _zero_accounting()
    reservation["wall_time_s"] = 101.0
    with pytest.raises(ValueError, match="cumulative capacity"):
        ledger._register_attempt(
            "attempt-oversized", resource_decision_sha256="d" * 64,
            resource_reservation=reservation,
        )
    assert not list(ledger.entries.iterdir())
    assert not ledger.pending_path.exists()


def test_campaign_v2_creation_recovers_exact_preregistered_local_state(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    real_register = CanonicalHeadAuthority.ensure_registered
    calls = 0

    def interrupt_registration(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash before authority registration")
        return real_register(*args, **kwargs)

    monkeypatch.setattr(
        CanonicalHeadAuthority, "ensure_registered", interrupt_registration,
    )
    with pytest.raises(OSError, match="before authority registration"):
        CanonicalLedgerPublicationV2.create(
            tmp_path / "ledger", campaign, authority=authority,
            expected_campaign_sha256=campaign["content_sha256"],
            expected_workload=workload,
        )
    monkeypatch.setattr(CanonicalHeadAuthority, "ensure_registered", real_register)
    recovered = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    assert recovered._head()["sequence"] == 0


def test_campaign_v2_finish_rejects_observation_beyond_reservation(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ef",
    )
    reconciliation["resource_bounds"]["wall_time_s"] = {
        "lower": 1.0, "upper": 1.0,
    }
    unsigned = dict(reconciliation)
    unsigned.pop("content_sha256")
    reconciliation["content_sha256"] = canonical_sha256(unsigned)
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    with pytest.raises(ValueError, match="exceeded its reservation"):
        ledger.finish_attempt(
            "attempt-1", run_manifest_path=tmp_path / "ef.json",
            checkpoint_root=tmp_path / "checkpoint",
        )
    head = json.loads(ledger.head_path.read_text())
    assert head["active_attempt"] == "attempt-1"
    assert head["sequence"] == 1


def test_campaign_v2_finish_rejects_resource_decision_substitution(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ab",
    )
    reconciliation["resource_decision_sha256"] = "e" * 64
    unsigned = dict(reconciliation)
    unsigned.pop("content_sha256")
    reconciliation["content_sha256"] = canonical_sha256(unsigned)
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    with pytest.raises(ValueError, match="resource decision was substituted"):
        ledger.finish_attempt(
            "attempt-1", run_manifest_path=tmp_path / "ab.json",
            checkpoint_root=tmp_path / "checkpoint",
        )
    assert ledger._head()["active_attempt"] == "attempt-1"


def test_campaign_v2_rejects_malformed_and_reused_attempt_ids(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    with pytest.raises(ValueError, match="does not extend"):
        ledger._register_attempt(
            "../bad", resource_decision_sha256="d" * 64,
            resource_reservation=_zero_accounting(),
        )
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="cd",
    )
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    ledger.finish_attempt(
        "attempt-1", run_manifest_path=tmp_path / "cd.json",
        checkpoint_root=tmp_path / "checkpoint",
    )
    with pytest.raises(ValueError, match="attempt ID was reused"):
        ledger._register_attempt(
            "attempt-1", resource_decision_sha256="d" * 64,
            resource_reservation=_zero_accounting(),
        )


def test_campaign_v2_rejects_reused_execution_witness_before_publication(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    first = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ab",
    )
    reconciliations = {"first.json": first, "ab.json": first}
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(
            reconciliations[Path(kwargs["run_manifest_path"]).name]
        ),
    )
    ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    ledger.finish_attempt(
        "attempt-1", run_manifest_path=tmp_path / "first.json",
        checkpoint_root=tmp_path / "checkpoint",
    )
    second = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=1, after=1,
        witness="cd",
    )
    second["execution_witness"]["sha256"] = first["execution_witness"]["sha256"]
    second_unsigned = dict(second)
    second_unsigned.pop("content_sha256")
    second["content_sha256"] = canonical_sha256(second_unsigned)
    ledger._register_attempt(
        "attempt-2", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    reconciliations.update({"second.json": second, "cd.json": second})
    with pytest.raises(ValueError, match="execution witness was reused"):
        ledger.finish_attempt(
            "attempt-2", run_manifest_path=tmp_path / "second.json",
            checkpoint_root=tmp_path / "checkpoint",
        )
    assert ledger._head()["active_attempt"] == "attempt-2"


def test_campaign_v2_pending_registration_rejects_reused_attempt_id(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ab",
    )
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    ledger.finish_attempt(
        "attempt-1", run_manifest_path=tmp_path / "ab.json",
        checkpoint_root=tmp_path / "checkpoint",
    )
    predecessor = ledger._head()
    entry, successor = registration_transition(
        campaign, predecessor, "attempt-1",
    )
    pending_payload = {
        "format": palace_ledger_v2.PENDING_FORMAT,
        "campaign_sha256": campaign["content_sha256"],
        "predecessor_head": predecessor,
        "successor_head": successor,
        "entry": entry,
    }
    pending = {
        **pending_payload,
        "content_sha256": canonical_sha256(pending_payload),
    }
    palace_ledger_v2.exclusive_publish_json(ledger.pending_path, pending)
    with pytest.raises(ValueError, match="attempt ID was reused"):
        ledger._reconcile_pending_transition(
            expected_registration=registration_recovery(entry),
        )
    assert authority.read(campaign["content_sha256"])["head_sha256"] == predecessor[
        "content_sha256"
    ]


def test_campaign_v2_pending_completion_rejects_reused_execution_witness(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    first = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ab",
    )
    reconciliations = {"first.json": first, "ab.json": first}
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(
            reconciliations[Path(kwargs["run_manifest_path"]).name]
        ),
    )
    ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    ledger.finish_attempt(
        "attempt-1", run_manifest_path=tmp_path / "first.json",
        checkpoint_root=tmp_path / "checkpoint",
    )
    ledger._register_attempt(
        "attempt-2", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    second = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=1, after=1,
        witness="cd",
    )
    second["execution_witness"]["sha256"] = first["execution_witness"]["sha256"]
    second_unsigned = dict(second)
    second_unsigned.pop("content_sha256")
    second["content_sha256"] = canonical_sha256(second_unsigned)
    reconciliations["cd.json"] = second
    state = ledger._validate_local_state()
    predecessor = state["head"]
    entry_payload = {
        "format": ENTRY_FORMAT,
        "campaign_sha256": campaign["content_sha256"],
        "sequence": predecessor["sequence"] + 1,
        "previous_entry_sha256": predecessor["entry_sha256"],
        "event": "attempt_finished",
        "attempt_id": "attempt-2",
        "reconciliation": second,
        "charged_accounting": _zero_accounting(),
    }
    entry = {
        **entry_payload,
        "content_sha256": canonical_sha256(entry_payload),
    }
    successor = ledger._completion_successor(
        predecessor, entry, state["active_registration"],
    )
    pending_payload = {
        "format": palace_ledger_v2.PENDING_FORMAT,
        "campaign_sha256": campaign["content_sha256"],
        "predecessor_head": predecessor,
        "successor_head": successor,
        "entry": entry,
    }
    pending = {
        **pending_payload,
        "content_sha256": canonical_sha256(pending_payload),
    }
    palace_ledger_v2.exclusive_publish_json(ledger.pending_path, pending)
    with pytest.raises(ValueError, match="execution witness was reused"):
        ledger._reconcile_pending_transition()
    assert authority.read(campaign["content_sha256"])["head_sha256"] == predecessor[
        "content_sha256"
    ]


@pytest.mark.parametrize("partial", ["empty", "entries", "lock", "campaign"])
def test_campaign_v2_creation_recovers_partial_initialization(tmp_path, partial):
    campaign, workload, authority = campaign_v2(tmp_path)
    root = tmp_path / "ledger"
    root.mkdir()
    if partial in {"entries", "lock", "campaign"}:
        (root / "entries").mkdir()
    if partial in {"lock", "campaign"}:
        (root / "writer.lock").touch()
    if partial == "campaign":
        palace_ledger_v2.exclusive_publish_json(root / "campaign.json", campaign)
    ledger = CanonicalLedgerPublicationV2.create(
        root, campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    assert ledger._head()["sequence"] == 0


@pytest.mark.parametrize("mutation", ["campaign", "head", "entry"])
def test_campaign_v2_creation_rejects_nonpristine_local_state(tmp_path, mutation):
    campaign, workload, authority = campaign_v2(tmp_path)
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "entries").mkdir()
    (root / "writer.lock").touch()
    if mutation == "campaign":
        palace_ledger_v2.exclusive_publish_json(
            root / "campaign.json", {"content_sha256": "0" * 64},
        )
    elif mutation == "head":
        palace_ledger_v2.exclusive_publish_json(root / "campaign.json", campaign)
        head = palace_ledger_v2.initial_head_v2(campaign)
        head["sequence"] = 1
        palace_ledger_v2.exclusive_publish_json(root / "head.json", head)
    else:
        (root / "entries" / "unexpected.json").write_text("{}")
    with pytest.raises(ValueError, match="initialization"):
        CanonicalLedgerPublicationV2.create(
            root, campaign, authority=authority,
            expected_campaign_sha256=campaign["content_sha256"],
            expected_workload=workload,
        )


def test_campaign_v2_completed_attempt_binds_terminal_reconciliation(
        tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n")
    manifest_raw = {"content_sha256": "e" * 64}
    manifest_bytes = (
        json.dumps(manifest_raw, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    workload_value = workload(
        config_sha256=file_sha256(config_path),
        config_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    campaign, expected_workload, authority = campaign_v2(
        tmp_path, workload_value=workload_value,
    )
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=expected_workload,
    )
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="completed", before=0, after=2,
        witness="cd",
    )
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    ledger._register_attempt(
        "attempt-final", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    completion = ledger.finish_attempt(
        "attempt-final", run_manifest_path=tmp_path / "cd.json",
        checkpoint_root=tmp_path / "checkpoint",
    )
    head = json.loads(ledger.head_path.read_text())
    assert head["terminal"] is True
    assert head["prefix"] == 2
    assert head["final_attempt_sha256"] == completion["reconciliation"][
        "content_sha256"
    ]
    record = ledger.matrix_access_record()
    assert record["head_sha256"] == head["content_sha256"]
    manifest = SimpleNamespace(
        config_path=config_path,
        terminals=(
            SimpleNamespace(index=1, name="A", attribute=10),
            SimpleNamespace(index=2, name="B", attribute=11),
        ),
        terminal_names=("A", "B"),
        raw=manifest_raw,
    )
    access = checkpoint_matrix_access(
        ledger, manifest,
        raw_path=tmp_path / "raw.csv",
        standard_path=tmp_path / "standard.csv",
    )
    assert access.campaign_sha256 == campaign["content_sha256"]
    assert access.terminal_names == ("A", "B")
    parsed = parse_palace_matrix_csv(
        tmp_path / "raw.csv", manifest,
        matrix_name="raw", matrix_access=access,
    )
    assert parsed.names == ("A", "B")
    raw_content = (tmp_path / "raw.csv").read_text()
    real_parse_content = palace._parse_palace_matrix_content

    def replace_after_attested_read(content, value, *, matrix_name):
        (tmp_path / "raw.csv").write_text("replaced-after-read\n")
        return real_parse_content(content, value, matrix_name=matrix_name)

    monkeypatch.setattr(
        palace, "_parse_palace_matrix_content", replace_after_attested_read,
    )
    parsed = parse_palace_matrix_csv(
        tmp_path / "raw.csv", manifest,
        matrix_name="raw", matrix_access=access,
    )
    assert parsed.names == ("A", "B")
    monkeypatch.setattr(palace, "_parse_palace_matrix_content", real_parse_content)
    (tmp_path / "raw.csv").write_text(raw_content)
    raw_link = tmp_path / "raw-link.csv"
    raw_link.symlink_to(tmp_path / "raw.csv")
    with pytest.raises(ValueError, match="artifact identity"):
        checkpoint_matrix_access(
            ledger, manifest, raw_path=raw_link,
            standard_path=tmp_path / "standard.csv",
        )
    config_path.write_text('{"tampered": true}\n')
    with pytest.raises(ValueError, match="config identity"):
        parse_palace_matrix_csv(
            tmp_path / "raw.csv", manifest,
            matrix_name="raw", matrix_access=access,
        )
    config_path.write_text("{}\n")
    tampered_manifest = SimpleNamespace(
        **{**manifest.__dict__, "raw": {"content_sha256": "f" * 64}}
    )
    with pytest.raises(ValueError, match="config identity"):
        checkpoint_matrix_access(
            ledger, tampered_manifest,
            raw_path=tmp_path / "raw.csv",
            standard_path=tmp_path / "standard.csv",
        )
    reordered_manifest = SimpleNamespace(
        **{
            **manifest.__dict__,
            "terminals": tuple(reversed(manifest.terminals)),
        }
    )
    with pytest.raises(ValueError, match="config identity"):
        checkpoint_matrix_access(
            ledger, reordered_manifest,
            raw_path=tmp_path / "raw.csv",
            standard_path=tmp_path / "standard.csv",
        )
    (tmp_path / "raw.csv").write_text("tampered\n")
    with pytest.raises(ValueError, match="artifact identity"):
        checkpoint_matrix_access(
            ledger, manifest,
            raw_path=tmp_path / "raw.csv",
            standard_path=tmp_path / "standard.csv",
        )
    with pytest.raises(ValueError, match="registration"):
        ledger._register_attempt(
            "too-late", resource_decision_sha256="d" * 64,
            resource_reservation=_zero_accounting(),
        )


def test_campaign_v2_publication_reconciles_crash_after_external_cas(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    predecessor = validate_head_v2(
        json.loads(ledger.head_path.read_text()), campaign,
    )
    entry, successor = registration_transition(campaign, predecessor)
    real_replace = os.replace

    def interrupt_local_head(source, target, *args, **kwargs):
        if Path(source) == ledger.next_head_path and Path(target) == ledger.head_path:
            raise OSError("injected crash after external CAS")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr("palace_ledger_v2.os.replace", interrupt_local_head)
    with pytest.raises(OSError, match="injected crash"):
        ledger._publish_transition(entry, successor)
    assert authority.read(campaign["content_sha256"])["head_sha256"] == successor[
        "content_sha256"
    ]
    assert json.loads(ledger.head_path.read_text())["content_sha256"] == predecessor[
        "content_sha256"
    ]
    monkeypatch.setattr("palace_ledger_v2.os.replace", real_replace)
    ledger.close()
    ledger = CanonicalLedgerPublicationV2(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    assert ledger._reconcile_pending_transition(expected_registration=registration_recovery(entry))["content_sha256"] == successor["content_sha256"]
    assert not ledger.pending_path.exists()


def test_campaign_v2_publication_recovers_pending_before_entry(tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    predecessor = json.loads(ledger.head_path.read_text())
    entry, successor = registration_transition(campaign, predecessor)
    real_publish = palace_ledger_v2.exclusive_publish_json

    def interrupt_entry(path, value):
        if Path(path).parent == ledger.entries:
            raise OSError("injected crash before entry publication")
        return real_publish(path, value)

    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", interrupt_entry)
    with pytest.raises(OSError, match="before entry publication"):
        ledger._publish_transition(entry, successor)
    assert ledger.pending_path.exists()
    assert not list(ledger.entries.iterdir())
    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", real_publish)
    ledger.close()
    ledger = CanonicalLedgerPublicationV2(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    with pytest.raises(ValueError, match="lacks trusted derivation"):
        ledger.recover_pending_attempt()
    monkeypatch.setattr(
        palace_ledger_v2, "derive_attempt_reservation",
        lambda *args, **kwargs: {
            "decision_sha256": entry["resource_decision_sha256"],
            "reservation": entry["resource_reservation"],
        },
    )
    assert ledger.recover_pending_attempt(
        resource_decision={}, execution_snapshot={}, trusted_policy={},
    )["content_sha256"] == successor["content_sha256"]
