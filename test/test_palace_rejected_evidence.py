#!/usr/bin/env python3
"""Rejected-evidence Palace execution regressions."""
from test_palace import (
    PalaceRunRejected, Path, _patch_process_run,
    _run_with_test_resource_policy, _write_config, bytes_sha256,
    canonical_sha256, deepcopy, file_sha256, json,
    load_palace_config_manifest, np, palace_attempt, pytest,
    validate_palace_attempt_manifest, validate_palace_execution_witness,
)


def test_unavailable_peak_memory_persists_rejected_evidence(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])

    def clear_memory(metadata):
        zero = {name: 0.0 for name in ("Average", "Max", "Min", "Total")}
        metadata["PeakMemoryMegabytes"] = zero
        metadata["PeakNodeMemoryMegabytes"] = zero

    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values,
        metadata_mutator=clear_memory,
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    validate_palace_attempt_manifest(caught.value.manifest_path)
    assert any("peak-memory collection" in failure
               for failure in caught.value.failures)
    assert not [path for path in tmp_path.rglob("terminal-C*.csv")
                if path.is_file() or path.is_symlink()]


def test_unbounded_derived_node_count_persists_rejected_evidence(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])

    def make_node_ratio_unbounded(metadata):
        metadata["PeakNodeMemoryMegabytes"] = {
            "Min": 1e-308, "Average": 1e-308,
            "Max": 1e308, "Total": 1e308,
        }

    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values,
        metadata_mutator=make_node_ratio_unbounded,
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    validate_palace_attempt_manifest(caught.value.manifest_path)
    assert any("PeakNodeMemoryMegabytes MPI totals" in failure
               for failure in caught.value.failures)
    assert not [path for path in tmp_path.rglob("terminal-C*.csv")
                if path.is_file() or path.is_symlink()]


def test_run_rejects_missing_normal_completion_markers(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, completion_markers=False
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    assert any(item.startswith("normal_completion:") for item in caught.value.failures)
    timing = validate_palace_attempt_manifest(caught.value.manifest_path)
    assert timing["lifecycle"] == "rejected_diagnostic"
    assert timing["matrix_available"] is False


def test_rejected_attempt_classifies_witnessed_wall_timeout(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, wall_timeout=True
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    timing = validate_palace_attempt_manifest(caught.value.manifest_path)
    assert timing["causal_outcome"] == "wall_timeout"
    assert timing["censored_bounds"]["reason"] == "wall_timeout"
    projection = validate_palace_execution_witness(caught.value.manifest_path)
    assert projection["outcome"] == "wall_timeout"
    assert projection["prefix_before"] == 0
    assert projection["prefix_after"] == 1
    assert projection["resource_bounds"]["solves"] == {"lower": 1, "upper": None}
    assert projection["causal_classification"]["limit"] == "wall_time"
    rejected = json.loads(caught.value.manifest_path.read_text())
    rejected_workload = json.loads(Path(rejected["workload"]).read_text())
    assert projection["workload_sha256"] == rejected_workload["content_sha256"]
    assert projection["workload_sha256"] != rejected["workload_sha256"]


def test_partial_progress_binds_terminal_roster_and_iteration_telemetry(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, wall_timeout=True
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    run = json.loads(caught.value.manifest_path.read_text())
    events = run["execution"]["palace_progress_events"]
    terminals = load_palace_config_manifest(manifest_path).terminals
    shortened = deepcopy(events)
    shortened[1]["detail"]["new_rhs"] = [1]
    with pytest.raises(ValueError, match="setup identity"):
        palace_attempt._validate_partial_progress(
            shortened, terminals, checkpoint_expected=False,
        )
    divergent = deepcopy(events)
    divergent[-1]["detail"]["iterations"] += 1
    with pytest.raises(ValueError, match="solve accounting"):
        palace_attempt._validate_partial_progress(
            divergent, terminals, checkpoint_expected=False,
        )
    checkpointed = deepcopy(run)
    checkpointed_events = checkpointed["execution"]["palace_progress_events"]
    checkpointed_events[0]["detail"]["checkpoint_enabled"] = 1
    checkpointed_events[1]["detail"]["campaign_digest"] = "c" * 64
    checkpointed_events.insert(-1, {
        "event": "checkpoint_published", "rhs_ordinal": 1,
        "stdout_byte_offset": checkpointed_events[-1]["stdout_byte_offset"] - 1,
        "observed_monotonic_ns": checkpointed_events[-1]["observed_monotonic_ns"],
        "time_semantics": "reader_receipt_upper_bound",
        "detail": {"rhs": 1, "terminal": 1, "prefix_after": 1},
    })
    palace_attempt._validate_partial_progress(
        checkpointed_events, terminals, checkpoint_expected=True,
    )
    projection = palace_attempt._attempt_projection(
        caught.value.manifest_path, checkpointed,
        outcome="wall_timeout", workload_sha256="a" * 64,
        manifest_sha256="b" * 64, terminals=terminals,
        checkpoint={
            "path": str((tmp_path / "checkpoint").resolve()),
            "native_campaign_identity": "c" * 64,
        },
    )
    for name in (
            "checkpoint_write_bytes", "checkpoint_read_bytes",
            "retained_checkpoint_bytes", "peak_rss_bytes",
            "retained_output_bytes", "cpu_time_s"):
        assert projection["resource_bounds"][name]["upper"] is None
        assert projection["resource_bounds"][name]["lower"] >= 0


def test_rejected_attempt_requires_strict_manifest_json(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, completion_markers=False
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    content = caught.value.manifest_path.read_text().rstrip()
    duplicate = tmp_path / "duplicate-run.json"
    duplicate.write_text(content[:-1] + ',"lifecycle":"rejected_diagnostic"}')
    with pytest.raises(ValueError, match="duplicate"):
        validate_palace_attempt_manifest(duplicate)


@pytest.mark.parametrize("mutation", ["second_limit", "quarantined_matrix"])
def test_wall_timeout_requires_exclusive_cause_and_no_final_matrix(
        tmp_path, monkeypatch, mutation):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, wall_timeout=True
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    run = json.loads(caught.value.manifest_path.read_text())
    if mutation == "second_limit":
        limit = run["execution"]["limits"]["peak_rss_bytes"]
        failure = f"peak memory footprint exceeded {limit} bytes"
        run["execution"]["limit_failures"].append(failure)
        run["execution"]["monitor_events"][2]["detail"] += f"; {failure}"
        run["execution"]["peak_rss_bytes"] = limit + 1
        run["execution"]["resource_samples"][-1]["peak_rss_bytes"] = limit + 1
    else:
        quarantine = tmp_path / "postpro" / ".quarantine"
        quarantine.mkdir(parents=True, exist_ok=True)
        content = b"forbidden partial matrix"
        digest = bytes_sha256(content)
        matrix = quarantine / f"terminal-Craw.csv.{digest}.quarantined"
        matrix.write_bytes(content)
        run["artifacts"][str(matrix.resolve())] = digest
    unsigned = dict(run)
    unsigned.pop("format")
    unsigned.pop("content_sha256")
    run["content_sha256"] = canonical_sha256(unsigned)
    path = tmp_path / f"timeout-{mutation}.json"
    path.write_text(json.dumps(run))
    assert validate_palace_attempt_manifest(path)["causal_outcome"] == "terminal_failure"


def test_rejected_attempt_rejects_symlinked_stream(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, completion_markers=False
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    run = json.loads(caught.value.manifest_path.read_text())
    stream = Path(next(
        name for name in run["artifacts"] if ".stdout." in name
    ))
    target = stream.with_name("stream-target.bin")
    stream.rename(target)
    stream.symlink_to(target.name)
    with pytest.raises(ValueError, match="unsafe"):
        validate_palace_attempt_manifest(caught.value.manifest_path)


def test_rejected_attempt_reconstructs_workload_from_execution_inputs(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, completion_markers=False
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    run_path = caught.value.manifest_path
    run = json.loads(run_path.read_text())
    workload = json.loads(Path(run["workload"]).read_text())
    workload["host_class"] = "fabricated-valid-host"
    unsigned_workload = dict(workload)
    unsigned_workload.pop("content_sha256")
    workload["content_sha256"] = canonical_sha256(unsigned_workload)
    rebound_path = tmp_path / "rebound-workload.json"
    rebound_path.write_text(json.dumps(workload))
    run["workload"] = str(rebound_path.resolve())
    run["workload_sha256"] = file_sha256(rebound_path)
    unsigned_run = dict(run)
    unsigned_run.pop("format")
    unsigned_run.pop("content_sha256")
    run["content_sha256"] = canonical_sha256(unsigned_run)
    rebound_run = tmp_path / "rebound-run.json"
    rebound_run.write_text(json.dumps(run))
    with pytest.raises(ValueError, match="workload|snapshot"):
        validate_palace_attempt_manifest(rebound_run)


@pytest.mark.parametrize("mutation", ["executable", "config", "extra", "cwd"])
def test_rejected_attempt_binds_exact_command_and_snapshot_cwd(
        tmp_path, monkeypatch, mutation):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, completion_markers=False
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    run = json.loads(caught.value.manifest_path.read_text())
    if mutation == "cwd":
        run["execution"]["cwd"] = str(tmp_path)
    else:
        command = list(run["command"])
        if mutation == "executable":
            command[0] = str(tmp_path / "other-palace")
        elif mutation == "config":
            command[3] = "other-config.json"
        else:
            command.append("extra")
        run["command"] = command
        run["execution"]["command"] = command
    unsigned = dict(run)
    unsigned.pop("format")
    unsigned.pop("content_sha256")
    run["content_sha256"] = canonical_sha256(unsigned)
    rebound = tmp_path / f"rebound-{mutation}.json"
    rebound.write_text(json.dumps(run))
    with pytest.raises(ValueError, match="command|cwd"):
        validate_palace_attempt_manifest(rebound)


def test_run_rejects_residuals_without_rhs_progress_milestones(
        tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, omit_rhs_milestones=True,
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    assert any(item.startswith("progress_validation:")
               for item in caught.value.failures)


def test_run_rejects_wrong_solve_counts_and_resolved_materials(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])

    def alter_metadata(value):
        value["LinearSolver"]["TotalSolves"] = 1

    def alter_resolved(value):
        value["Domains"]["Materials"][0]["Permittivity"] = 4.2

    build_manifest = _patch_process_run(
        monkeypatch,
        tmp_path,
        values,
        values,
        metadata_mutator=alter_metadata,
        resolved_mutator=alter_resolved,
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    assert any(item.startswith("completion_metadata:") for item in caught.value.failures)


def test_run_rejects_missing_completion_metadata(tmp_path, monkeypatch):
    _, manifest_path = _write_config(tmp_path)
    executable = tmp_path / "palace"
    executable.write_text("binary")
    values = np.array([[3e-12, -2e-12], [-2e-12, 4e-12]])
    build_manifest = _patch_process_run(
        monkeypatch, tmp_path, values, values, write_metadata=False
    )
    with pytest.raises(PalaceRunRejected) as caught:
        _run_with_test_resource_policy(
            manifest_path, executable=executable,
            build_manifest=build_manifest, monkeypatch=monkeypatch,
        )
    assert any(item.startswith("completion_metadata:") for item in caught.value.failures)
