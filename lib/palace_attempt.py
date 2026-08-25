#!/usr/bin/env python3
"""Validation for quarantined rejected Palace attempt timing evidence."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat

if __package__:
    from .palace_build import palace_build_identity
    from .palace_resources import validate_palace_workload
    from .palace_workflow import (
        execution_workload_inputs, palace_progress_events,
        validate_execution_snapshot,
    )
    from .process_monitor import validate_process_event_witness
    from .provenance import bytes_sha256, canonical_equal, canonical_sha256, file_sha256
else:
    from palace_build import palace_build_identity
    from palace_resources import validate_palace_workload
    from palace_workflow import (
        execution_workload_inputs, palace_progress_events,
        validate_execution_snapshot,
    )
    from process_monitor import validate_process_event_witness
    from provenance import bytes_sha256, canonical_equal, canonical_sha256, file_sha256


RUN_MANIFEST_FORMAT = "dcdc-palace-run-v3"
GATE_POLICY = "palace-electrostatic-pcb-gates-v2"


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate rejected Palace attempt key {key!r}")
        value[key] = item
    return value


def _read_regular(path, label):
    path = Path(path)
    descriptor = None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"rejected Palace attempt {label} is not regular")
        content = b""
        while len(content) < before.st_size:
            chunk = os.read(descriptor, before.st_size - len(content))
            if not chunk:
                raise ValueError(f"rejected Palace attempt {label} is truncated")
            content += chunk
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)):
            raise ValueError(f"rejected Palace attempt {label} changed during read")
        return content
    except OSError as error:
        raise ValueError(
            f"rejected Palace attempt {label} is unavailable or unsafe: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_sha256(path, label):
    return bytes_sha256(_read_regular(path, label))


def _parse_json(content, label):
    try:
        return json.loads(content, object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"rejected Palace attempt {label} is invalid: {error}") from error


def _load_json(path, label):
    return _parse_json(_read_regular(path, label), label)


def _load_stream(config_path, identity, source):
    digest = identity.get(f"{source}_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError(f"rejected attempt {source} digest is invalid")
    path = config_path.with_name(f"{config_path.name}.{source}.{digest}.bin")
    if (_safe_sha256(path, source) != digest
            or identity["artifacts"].get(str(path.resolve())) != digest):
        raise ValueError(f"rejected attempt {source} artifact mismatch")
    content = _read_regular(path, source)
    if bytes_sha256(content) != digest:
        raise ValueError(f"rejected attempt {source} content mismatch")
    return content


def _reconstruct_workload(
        identity, config_manifest, workload, *, allow_invalid_snapshot=False):
    if __package__:
        from . import palace as palace_module
    else:
        import palace as palace_module
    snapshot = identity.get("execution_snapshot")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("inputs"), list):
        raise ValueError("rejected Palace attempt snapshot identity is invalid")
    executable_records = [
        record for record in snapshot["inputs"]
        if isinstance(record, dict) and record.get("role") == "executable"
    ]
    if len(executable_records) != 1:
        raise ValueError("rejected Palace attempt executable identity is absent")
    executable_record = executable_records[0]
    command = identity.get("command")
    if (not isinstance(command, list) or len(command) != 4
            or any(not isinstance(value, str) for value in command)
            or command[0] != executable_record.get("snapshot")
            or command[1] != "-np"):
        raise ValueError("rejected Palace attempt command identity is invalid")
    try:
        processes = int(command[2])
    except (TypeError, ValueError) as error:
        raise ValueError("rejected Palace attempt process count is invalid") from error
    manifest = palace_module.load_palace_config_manifest(config_manifest)
    execution = identity.get("execution")
    if (command[3] != manifest.config_path.name
            or str(processes) != command[2] or processes <= 0
            or not isinstance(execution, dict)
            or execution.get("command") != command
            or execution.get("cwd") != snapshot.get("root")):
        raise ValueError("rejected Palace attempt command or cwd binding mismatch")
    inputs = execution_workload_inputs(
        manifest,
        config_manifest_path=config_manifest,
        build_manifest_path=identity.get("build_manifest", ""),
        executable=executable_record.get("source", ""),
        processes=processes,
        palace_file=Path(__file__).with_name("palace.py"),
        validate_build=palace_module.validate_palace_build_manifest,
    )
    try:
        validate_execution_snapshot(
            snapshot,
            manifest,
            workload,
            executable=inputs["executable"],
            binaries=tuple(inputs["binaries"]),
            mpi_launcher=inputs["mpi_launcher"],
        )
    except (OSError, ValueError):
        if not allow_invalid_snapshot:
            raise
        payload = {key: value for key, value in snapshot.items()
                   if key != "content_sha256"}
        base_name = (
            f".{manifest.config_path.name}.execution.{workload['content_sha256']}"
        )
        snapshot_root = Path(snapshot.get("root", "")).resolve()
        if (set(snapshot) != {
                "format", "root", "workload_sha256", "inputs", "content_sha256"}
                or snapshot.get("format") != "palace-execution-snapshot-v1"
                or snapshot.get("content_sha256") != canonical_sha256(payload)
                or snapshot.get("workload_sha256") != workload["content_sha256"]
                or snapshot_root.parent != manifest.config_path.parent.resolve()
                or (snapshot_root.name != base_name
                    and not (snapshot_root.name.startswith(f"{base_name}.attempt.")
                             and len(snapshot_root.name) == len(base_name) + 73))):
            raise ValueError("rejected Palace snapshot record identity is invalid")
    if (not canonical_equal(inputs["workload"], workload)
            or identity.get("build_manifest_sha256")
            != file_sha256(inputs["build_manifest_path"])
            or identity.get("build_provenance_sha256")
            != palace_build_identity(inputs["build"])
            or not canonical_equal(identity.get("binaries"), inputs["binaries"])
            or not canonical_equal(
                identity.get("implementation_files"), inputs["implementation"])
            or identity.get("mpi_launcher") != inputs["mpi_launcher"]
            or identity.get("mpi_launcher_sha256")
            != file_sha256(inputs["mpi_launcher"])):
        raise ValueError("rejected Palace attempt workload input binding mismatch")
    return workload, manifest, inputs, palace_module


def _attempt_projection(
        path, raw, *, outcome, workload_sha256, manifest_sha256, terminals,
        checkpoint):
    execution = raw["execution"]
    progress = execution["palace_progress_events"]
    setup = [event for event in progress if event["event"] == "setup_end"]
    if len(setup) != 1:
        raise ValueError("Palace execution witness lacks one setup boundary")
    setup_detail = setup[0]["detail"]
    setup_start = next(
        event for event in progress if event["event"] == "setup_start"
    )["detail"]
    prefix_before = setup_detail.get("prefix_before")
    loaded = setup_detail.get("loaded_rhs")
    if (type(prefix_before) is not int
            or loaded != list(range(1, prefix_before + 1))):
        raise ValueError("Palace execution witness setup prefix is invalid")
    accepted = [
        event for event in progress
        if event["event"] in {"rhs_loaded", "rhs_solved"}
    ]
    accepted_indices = [event["rhs_ordinal"] for event in accepted]
    if accepted_indices != list(range(1, len(accepted_indices) + 1)):
        raise ValueError("Palace execution witness accepted prefix is not contiguous")
    prefix_after = len(accepted_indices)
    solved = [event for event in accepted if event["event"] == "rhs_solved"]
    loaded_events = [event for event in accepted if event["event"] == "rhs_loaded"]
    loaded_count = min(prefix_before, prefix_after)
    if ([event["rhs_ordinal"] for event in loaded_events]
            != list(range(1, loaded_count + 1))
            or [event["rhs_ordinal"] for event in solved]
            != list(range(prefix_before + 1, prefix_after + 1))):
        raise ValueError("Palace execution witness loaded/solved split is invalid")
    solves = sum(event["detail"].get("solves", -1) for event in solved)
    iterations = sum(event["detail"].get("iterations", -1) for event in solved)
    if solves != len(solved) or iterations < 0:
        raise ValueError("Palace execution witness solve accounting is invalid")
    final = [event for event in progress if event["event"] == "finalization_end"]
    checkpoint_read = 0
    checkpoint_written = 0
    checkpoint_retained = 0
    if final:
        if len(final) != 1:
            raise ValueError("Palace execution witness finalization is duplicated")
        detail = final[0]["detail"]
        field = next(
            event for event in progress if event["event"] == "field_solve_started"
        )
        terminal_count = field["detail"]["terminal_count"]
        expected = {
            "prefix_before": prefix_before,
            "prefix_after": (
                terminal_count
                if setup_start["checkpoint_enabled"] else 0
            ),
            "total_solves": solves,
            "total_iterations": iterations,
        }
        if any(detail.get(name) != value for name, value in expected.items()):
            raise ValueError("Palace execution witness final accounting is inconsistent")
        checkpoint_read = detail.get("checkpoint_read_bytes")
        checkpoint_written = detail.get("checkpoint_written_bytes")
        checkpoint_retained = detail.get("checkpoint_retained_bytes")
    for value in (checkpoint_read, checkpoint_written, checkpoint_retained):
        if type(value) is not int or value < 0:
            raise ValueError("Palace execution witness checkpoint accounting is invalid")
    process_count = int(raw["command"][2])
    snapshot_input_bytes = 0
    for record in raw["execution_snapshot"]["inputs"]:
        content = _read_regular(record["snapshot"], "execution snapshot input")
        if bytes_sha256(content) != record["snapshot_sha256"]:
            raise ValueError("Palace execution snapshot input changed after validation")
        snapshot_input_bytes += len(content)
    input_read_bytes = process_count * snapshot_input_bytes
    stdout_bytes = sum(
        event["byte_count"] for event in execution["stream_events"]
        if event["source"] == "stdout"
    )
    stderr_bytes = sum(
        event["byte_count"] for event in execution["stream_events"]
        if event["source"] == "stderr"
    )
    observed_usage = {
        "wall_time_s": execution["elapsed_s"],
        "cpu_time_s": execution["elapsed_s"] * process_count,
        "iterations": iterations,
        "solves": solves,
        "bytes_written": (
            execution["output_bytes"] + execution["directory_growth_bytes"]
        ),
        "bytes_read": input_read_bytes + checkpoint_read,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "checkpoint_write_bytes": checkpoint_written,
        "checkpoint_read_bytes": checkpoint_read,
        "retained_output_bytes": max(
            0, execution["directory_growth_bytes"] - checkpoint_written,
        ),
        "retained_checkpoint_bytes": checkpoint_retained,
        "peak_rss_bytes": execution["peak_rss_bytes"],
    }
    exact_completion = outcome == "completed" and len(final) == 1
    checkpoint_enabled = next(
        event for event in progress if event["event"] == "setup_start"
    )["detail"]["checkpoint_enabled"]
    exact_names = {"wall_time_s", "stdout_bytes", "stderr_bytes"}
    if final or not checkpoint_enabled:
        exact_names |= {
            "bytes_read", "checkpoint_write_bytes", "checkpoint_read_bytes",
            "retained_checkpoint_bytes",
        }
    if exact_completion:
        exact_names |= {"iterations", "solves"}
    resource_bounds = {}
    for name, value in observed_usage.items():
        upper = value if name in exact_names else None
        if name in {"cpu_time_s", "peak_rss_bytes", "retained_output_bytes"}:
            upper = None
        resource_bounds[name] = {"lower": value, "upper": upper}
    final_artifacts = None
    if outcome == "completed":
        final_artifacts = {}
        for role, filename in (
                ("raw_matrix", "terminal-Craw.csv"),
                ("standard_matrix", "terminal-C.csv")):
            matches = [
                (artifact, digest) for artifact, digest in raw["artifacts"].items()
                if Path(artifact).name == filename
            ]
            if len(matches) != 1:
                raise ValueError("Palace completed witness final matrix identity is invalid")
            artifact, digest = matches[0]
            final_artifacts[role] = {"path": artifact, "sha256": digest}
    return {
        "run_manifest": {
            "path": str(Path(path).resolve()),
            "sha256": manifest_sha256,
            "content_sha256": raw["content_sha256"],
        },
        "workload_sha256": workload_sha256,
        "resource_decision_sha256": (
            _load_json(raw["resource_decision"], "resource decision")[
                "content_sha256"
            ]
            if raw["resource_decision"] is not None else None
        ),
        "ordered_terminals": [asdict(terminal) for terminal in terminals],
        "checkpoint_enabled": bool(setup_start["checkpoint_enabled"]),
        "checkpoint": checkpoint,
        "native_campaign_digest": setup_detail["campaign_digest"],
        "outcome": outcome,
        "prefix_before": prefix_before,
        "prefix_after": prefix_after,
        "loaded_rhs_indices": [event["rhs_ordinal"] for event in loaded_events],
        "new_rhs_indices": [event["rhs_ordinal"] for event in solved],
        "resource_bounds": resource_bounds,
        "final_artifacts": final_artifacts,
        "causal_classification": {
            "version": "palace-causal-timeout-v2",
            "monitor_initiated": outcome == "wall_timeout",
            "limit": "wall_time" if outcome == "wall_timeout" else None,
            "diagnostics_before_kill": (
                [] if outcome == "wall_timeout" else list(raw["failures"])
            ),
        },
    }


def _validate_partial_progress(
        events, terminals, *, checkpoint_expected, campaign_digest=None):
    terminal_indices = [terminal.index for terminal in terminals]
    terminal_count = len(terminal_indices)
    if len(events) < 3 or [event["event"] for event in events[:3]] != [
            "setup_start", "setup_end", "field_solve_started"]:
        raise ValueError("rejected Palace attempt lacks a complete setup prefix")
    setup_start = events[0]["detail"]
    setup_end = events[1]["detail"]
    checkpoint_enabled = setup_start.get("checkpoint_enabled")
    prefix_before = setup_end.get("prefix_before")
    if (not canonical_equal(setup_start, {
            "solver": "electrostatic", "checkpoint_enabled": checkpoint_enabled})
            or checkpoint_enabled not in (0, 1)
            or bool(checkpoint_enabled) != checkpoint_expected
            or (campaign_digest is not None
                and setup_end.get("campaign_digest") != campaign_digest)
            or type(prefix_before) is not int or not 0 <= prefix_before <= terminal_count
            or set(setup_end) != {
                "campaign_digest", "prefix_before", "loaded_rhs", "new_rhs"}
            or setup_end["loaded_rhs"] != list(range(1, prefix_before + 1))
            or setup_end["new_rhs"]
            != list(range(prefix_before + 1, terminal_count + 1))
            or (not checkpoint_enabled and (
                prefix_before != 0 or setup_end["campaign_digest"] != "disabled"))
            or not canonical_equal(
                events[2]["detail"], {"terminal_count": terminal_count})):
        raise ValueError("rejected Palace attempt setup identity is invalid")
    expected = ["setup_start", "setup_end", "field_solve_started"]
    ordinal_by_position: list[int | None] = [None, None, None]
    for ordinal in range(1, terminal_count + 1):
        loaded_rhs = ordinal <= prefix_before
        names = ["rhs_start"]
        if not loaded_rhs:
            names.extend(("rhs_started", "rhs_converged"))
        names.extend((
            "explicit_residual_completed", "residual_accepted",
            "reaction_accepted",
        ))
        if not loaded_rhs and checkpoint_enabled:
            names.append("checkpoint_published")
        names.append("rhs_loaded" if loaded_rhs else "rhs_solved")
        expected.extend(names)
        ordinal_by_position.extend([ordinal] * len(names))
    expected.extend(("finalization_start", "finalization_end"))
    ordinal_by_position.extend((None, None))
    names = [event["event"] for event in events]
    if len(names) > len(expected) or names != expected[:len(names)]:
        raise ValueError("rejected Palace attempt progress is not a valid prefix")
    converged_iterations = {}
    accepted_prefix = 0
    for position, event in enumerate(events[3:], 3):
        ordinal = ordinal_by_position[position]
        name = event["event"]
        detail = event["detail"]
        if ordinal is not None and event["rhs_ordinal"] != ordinal:
            raise ValueError("rejected Palace attempt RHS progress is reordered")
        terminal = terminal_indices[ordinal - 1] if ordinal is not None else None
        source = "loaded" if ordinal is not None and ordinal <= prefix_before else "solved"
        if name == "rhs_start" and not canonical_equal(detail, {
                "rhs": ordinal, "terminal": terminal,
                "action": "load" if source == "loaded" else "solve"}):
            raise ValueError("rejected Palace attempt RHS start is invalid")
        if name == "rhs_started" and (
                set(detail) != {"initial_preconditioned_residual"}
                or not isinstance(detail["initial_preconditioned_residual"], float)):
            raise ValueError("rejected Palace attempt KSP start is invalid")
        if name == "rhs_converged":
            iterations = detail.get("iterations")
            if set(detail) != {"iterations"} or type(iterations) is not int or iterations < 0:
                raise ValueError("rejected Palace attempt convergence is invalid")
            converged_iterations[ordinal] = iterations
        if name == "explicit_residual_completed" and (
                set(detail) != {"explicit_relative_residual", "target"}
                or any(not isinstance(detail[key], float)
                       for key in ("explicit_relative_residual", "target"))):
            raise ValueError("rejected Palace attempt explicit residual is invalid")
        if name == "residual_accepted" and (
                set(detail) != {"rhs", "terminal", "source", "value"}
                or detail["rhs"] != ordinal or detail["terminal"] != terminal
                or detail["source"] != source or not isinstance(detail["value"], float)):
            raise ValueError("rejected Palace attempt residual acceptance is invalid")
        if name == "reaction_accepted" and not canonical_equal(detail, {
                "rhs": ordinal, "terminal": terminal, "source": source,
                "entries": terminal_count}):
            raise ValueError("rejected Palace attempt reaction acceptance is invalid")
        if name == "checkpoint_published" and not canonical_equal(detail, {
                "rhs": ordinal, "terminal": terminal, "prefix_after": ordinal}):
            raise ValueError("rejected Palace attempt checkpoint prefix is invalid")
        if name in {"rhs_loaded", "rhs_solved"}:
            iterations = 0 if source == "loaded" else converged_iterations.get(ordinal)
            solves = 0 if source == "loaded" else 1
            if not canonical_equal(detail, {
                    "rhs": ordinal, "terminal": terminal,
                    "solves": solves, "iterations": iterations}):
                raise ValueError("rejected Palace attempt solve accounting is invalid")
            accepted_prefix = ordinal
        expected_after = terminal_count if checkpoint_enabled else 0
        if name == "finalization_start" and not canonical_equal(detail, {
                "terminal_count": terminal_count, "prefix_before": prefix_before,
                "prefix_after": expected_after}):
            raise ValueError("rejected Palace attempt finalization start is invalid")
        if name == "finalization_end":
            expected_final = {
                "prefix_before": prefix_before, "prefix_after": expected_after,
                "total_solves": terminal_count - prefix_before,
                "total_iterations": sum(converged_iterations.values()),
                "checkpoint_read_bytes": detail.get("checkpoint_read_bytes"),
                "checkpoint_written_bytes": detail.get("checkpoint_written_bytes"),
                "checkpoint_retained_bytes": detail.get("checkpoint_retained_bytes"),
            }
            if (not canonical_equal(detail, expected_final)
                    or any(type(detail[key]) is not int or detail[key] < 0 for key in (
                        "checkpoint_read_bytes", "checkpoint_written_bytes",
                        "checkpoint_retained_bytes"))):
                raise ValueError("rejected Palace attempt final accounting is invalid")
    return prefix_before, accepted_prefix


def validate_palace_execution_witness(path):
    supplied = Path(path)
    if supplied.is_symlink():
        raise ValueError("Palace execution witness must not be a symlink")
    path = supplied.absolute()
    content = _read_regular(path, "execution witness")
    raw = _parse_json(content, "execution witness")
    manifest_sha256 = bytes_sha256(content)
    lifecycle = raw.get("lifecycle") if isinstance(raw, dict) else None
    if lifecycle == "numerically_converged_diagnostic":
        if __package__:
            from . import palace as palace_module
        else:
            import palace as palace_module
        accepted = palace_module._validate_palace_run_manifest_unattested(
            path, _document=(raw, manifest_sha256),
        )
        return _attempt_projection(
            path, accepted["raw"], outcome="completed",
            workload_sha256=accepted["workload"]["content_sha256"],
            manifest_sha256=manifest_sha256,
            terminals=accepted["manifest"].terminals,
            checkpoint=accepted["manifest"].checkpoint,
        )
    if lifecycle == "rejected_diagnostic":
        rejected = validate_palace_attempt_manifest(
            path, _document=(raw, manifest_sha256),
        )
        return _attempt_projection(
            path, raw, outcome=rejected["causal_outcome"],
            workload_sha256=rejected["workload_sha256"],
            manifest_sha256=manifest_sha256,
            terminals=rejected["manifest"].terminals,
            checkpoint=rejected["manifest"].checkpoint,
        )
    raise ValueError("Palace execution witness lifecycle is unsupported")


def validate_palace_attempt_manifest(path, *, _document=None):
    supplied = Path(path)
    if supplied.is_symlink():
        raise ValueError("rejected Palace attempt manifest must not be a symlink")
    path = supplied.absolute()
    if _document is None:
        content = _read_regular(path, "manifest")
        raw = _parse_json(content, "manifest")
        manifest_sha256 = bytes_sha256(content)
    else:
        raw, manifest_sha256 = _document
    if not isinstance(raw, dict) or raw.get("format") != RUN_MANIFEST_FORMAT:
        raise ValueError("unsupported rejected Palace attempt format")
    identity = dict(raw)
    identity.pop("format")
    content_sha256 = identity.pop("content_sha256", None)
    if content_sha256 != canonical_sha256(identity):
        raise ValueError("rejected Palace attempt content ID mismatch")
    expected_identity_keys = {
        "artifacts", "binaries", "build_manifest", "build_manifest_sha256",
        "build_provenance_sha256", "command", "config_manifest",
        "config_manifest_sha256", "execution", "failures", "gate_policy",
        "implementation_files", "lifecycle", "mpi_launcher",
        "mpi_launcher_sha256", "residuals", "resource_class",
        "resource_enforcement", "resource_limits", "runtime_metadata",
        "stderr_sha256", "stdout_sha256", "workload", "workload_sha256",
        "resource_decision", "resource_decision_sha256", "execution_snapshot",
    }
    if set(identity) != expected_identity_keys:
        raise ValueError("rejected Palace attempt identity schema mismatch")
    if (identity.get("gate_policy") != GATE_POLICY
            or identity.get("lifecycle") != "rejected_diagnostic"
            or not isinstance(identity.get("failures"), list)
            or not identity["failures"]
            or any(not isinstance(value, str) or not value
                   for value in identity["failures"])):
        raise ValueError("Palace attempt is not quarantined rejected evidence")
    artifacts = identity.get("artifacts")
    if (not isinstance(artifacts, dict)
            or any(not isinstance(name, str) or not isinstance(digest, str)
                   or _safe_sha256(name, "artifact") != digest
                   for name, digest in artifacts.items())):
        raise ValueError("rejected Palace attempt artifact map mismatch")
    config_manifest = Path(identity.get("config_manifest", "")).resolve()
    config_path = Path(str(config_manifest).removesuffix(".manifest.json"))
    if (identity.get("config_manifest_sha256")
            != _safe_sha256(config_manifest, "config manifest")
            or artifacts.get(str(config_path))
            != _safe_sha256(config_path, "config")):
        raise ValueError("rejected Palace attempt config identity mismatch")
    workload_path = Path(identity.get("workload", "")).resolve()
    if (identity.get("workload_sha256")
            != _safe_sha256(workload_path, "workload")):
        raise ValueError("rejected Palace attempt workload identity mismatch")
    workload = validate_palace_workload(_load_json(workload_path, "workload"))
    workload, manifest, inputs, palace_module = _reconstruct_workload(
        identity, config_manifest, workload,
        allow_invalid_snapshot=any(
            failure.startswith("execution_snapshot:")
            for failure in identity["failures"]
        ),
    )
    resource_class = identity.get("resource_class")
    if resource_class not in palace_module.RESOURCE_LIMITS:
        raise ValueError("rejected Palace attempt resource class is invalid")
    expected_limits = {
        **asdict(palace_module.RESOURCE_LIMITS[resource_class]),
        **palace_module.MESH_LIMITS[resource_class],
    }
    execution = identity.get("execution")
    expected_execution_keys = {
        "command", "cwd", "returncode", "elapsed_s", "peak_rss_bytes",
        "output_bytes", "directory_growth_bytes", "max_refined_panels",
        "max_gmres_iteration", "limit_failures", "limits", "stream_events",
        "monitor_events", "resource_samples", "palace_progress_events",
    }
    if (not canonical_equal(identity.get("resource_limits"), expected_limits)
            or identity.get("resource_enforcement")
            != "portable_process_limits"
            or not isinstance(execution, dict)
            or set(execution) != expected_execution_keys
            or not canonical_equal(
                execution.get("limits"), asdict(
                    palace_module.RESOURCE_LIMITS[resource_class]))):
        raise ValueError("rejected Palace attempt resource identity mismatch")
    decision_value = identity.get("resource_decision")
    decision_digest = identity.get("resource_decision_sha256")
    if (decision_value is None) != (decision_digest is None):
        raise ValueError("rejected Palace attempt resource decision identity is partial")
    decision_path = None
    if decision_value is not None:
        decision_path = Path(decision_value).resolve()
        if (not decision_path.is_file() or decision_path.is_symlink()
                or decision_digest != file_sha256(decision_path)):
            raise ValueError("rejected Palace attempt resource decision is invalid")
        policy = palace_module._trusted_resource_policy()
        palace_module.validate_bound_resource_decision(
            _load_json(decision_path, "resource decision"),
            run_path=path,
            expected_workload=workload,
            policy=policy,
            resource_class=resource_class,
            decision_path=decision_path,
            config_path=manifest.config_path,
        )
    if manifest.checkpoint is not None and decision_path is None:
        raise ValueError("checkpointed Palace attempt lacks a resource decision")
    stdout_digest = identity["stdout_sha256"]
    stderr_digest = identity["stderr_sha256"]
    required_artifacts = {
        manifest.config_path, manifest.mesh_path, manifest.mesh_manifest_path,
        workload_path,
        config_path.with_name(f"{config_path.name}.stdout.{stdout_digest}.bin"),
        config_path.with_name(f"{config_path.name}.stderr.{stderr_digest}.bin"),
    }
    if decision_path is not None:
        required_artifacts.add(decision_path)
    snapshot_artifacts = {
        Path(item["snapshot"])
        for item in identity["execution_snapshot"]["inputs"]
    }
    missing_snapshot_artifacts = {
        candidate for candidate in snapshot_artifacts if not candidate.is_file()
    }
    if (missing_snapshot_artifacts
            and not any(failure.startswith("execution_snapshot:")
                        for failure in identity["failures"])):
        raise ValueError("rejected Palace attempt lost an unreported snapshot input")
    required_artifacts.update(snapshot_artifacts - missing_snapshot_artifacts)
    optional_artifacts = {
        manifest.output_directory / "palace.json",
        manifest.output_directory / "config_resolved.json",
    }
    snapshot_output = (
        Path(identity["execution_snapshot"]["root"])
        / manifest.output_directory.name
    )
    for directory in (manifest.output_directory, snapshot_output):
        if directory.is_dir() and not directory.is_symlink():
            for quarantine in directory.glob(".quarantine*"):
                if quarantine.is_dir() and not quarantine.is_symlink():
                    optional_artifacts.update(
                        quarantine.glob("terminal-C*.quarantined")
                    )
    expected_artifacts = {
        str(candidate.resolve()): _safe_sha256(candidate, "bound artifact")
        for candidate in required_artifacts | optional_artifacts
        if candidate.exists()
    }
    if expected_artifacts != artifacts:
        missing = sorted(set(artifacts) - set(expected_artifacts))
        unexpected = sorted(set(expected_artifacts) - set(artifacts))
        raise ValueError(
            "rejected Palace attempt artifact inventory mismatch: "
            f"unbound={missing}, omitted={unexpected}"
        )
    stdout = _load_stream(config_path, identity, "stdout")
    stderr = _load_stream(config_path, identity, "stderr")
    execution = identity.get("execution")
    if (not isinstance(execution, dict)
            or execution.get("command") != identity.get("command")):
        raise ValueError("rejected Palace attempt execution identity mismatch")
    validate_process_event_witness(execution, stdout, stderr)
    expected_progress = palace_progress_events(stdout, execution["stream_events"])
    if not canonical_equal(
            execution.get("palace_progress_events"), expected_progress):
        raise ValueError("rejected Palace attempt progress witness mismatch")
    known_events = {
        "setup_start", "setup_end", "field_solve_started", "rhs_start",
        "rhs_started", "rhs_converged", "rhs_not_converged",
        "explicit_residual_completed", "residual_accepted", "reaction_accepted",
        "checkpoint_published", "rhs_loaded", "rhs_solved",
        "finalization_start", "finalization_end",
    }
    if any(event["event"] not in known_events for event in expected_progress):
        raise ValueError("rejected Palace attempt contains an unknown milestone")
    prefix_before, accepted_prefix = _validate_partial_progress(
        expected_progress, manifest.terminals,
        checkpoint_expected=manifest.checkpoint is not None,
        campaign_digest=(
            bytes_sha256(manifest.checkpoint["native_campaign_identity"].encode())
            if manifest.checkpoint is not None else "disabled"
        ),
    )
    rhs_ordinals = [
        event["rhs_ordinal"] for event in expected_progress
        if event["event"] in {"rhs_loaded", "rhs_solved"}
    ]
    if (rhs_ordinals != list(range(1, len(rhs_ordinals) + 1))
            or accepted_prefix != len(rhs_ordinals)):
        raise ValueError("rejected Palace attempt accepted RHS sequence is not contiguous")
    limit_failures = execution.get("limit_failures")
    if not isinstance(limit_failures, list):
        raise ValueError("rejected Palace attempt limit witness is invalid")
    monitor_details = [
        event["detail"] for event in execution["monitor_events"]
        if event["event"] in {"limit_detected", "postexit_limit_detected"}
        and isinstance(event["detail"], str)
    ]
    if any(not any(failure in detail for detail in monitor_details)
           for failure in limit_failures):
        raise ValueError("rejected Palace attempt limit cause is not witnessed")
    process_failures = [
        failure for failure in identity["failures"]
        if failure.startswith("process_exit:")
    ]
    if bool(process_failures) != (execution.get("returncode") != 0):
        raise ValueError("rejected Palace attempt exit cause is inconsistent")
    diagnostic_failures = [
        failure for failure in identity["failures"]
        if failure.startswith((
            "solver_diagnostic:", "progress_validation:",
            "stream_encoding:", "execution_snapshot:",
            "output_publication:", "matrix_quarantine:",
        ))
    ]
    incomplete_artifact_names = {
        "terminal-Craw.csv", "terminal-C.csv", "palace.json",
        "config_resolved.json",
    }
    unexpected_final_artifact = any(
        Path(name).name in incomplete_artifact_names
        or (Path(name).name.startswith(("terminal-Craw.csv.", "terminal-C.csv."))
            and Path(name).name.endswith(".quarantined"))
        for name in artifacts
    )
    wall_causes = [
        failure for failure in limit_failures
        if failure.startswith("wall time exceeded ")
    ]
    monitor_names = [event["event"] for event in execution["monitor_events"]]
    wall_limit = (
        len(wall_causes) == 1 and len(limit_failures) == 1
        and "limit_detected" in monitor_names
        and "kill_initiated" in monitor_names
    )
    causal_outcome = (
        "wall_timeout"
        if wall_limit and not diagnostic_failures and not unexpected_final_artifact
        else "terminal_failure"
    )
    censored_bounds = {
        "wall_time_s": {"lower": execution["elapsed_s"], "upper": None},
        "peak_rss_bytes": {"lower": execution["peak_rss_bytes"], "upper": None},
        "logging_output_bytes": {
            "lower": execution["output_bytes"], "upper": None,
        },
        "directory_output_bytes": {
            "lower": execution["directory_growth_bytes"], "upper": None,
        },
        "reason": causal_outcome,
    }
    return {
        "lifecycle": "rejected_diagnostic",
        "content_sha256": content_sha256,
        "manifest_sha256": manifest_sha256,
        "manifest": manifest,
        "raw": raw,
        "workload_sha256": workload["content_sha256"],
        "execution": execution,
        "failures": list(identity["failures"]),
        "causal_outcome": causal_outcome,
        "accepted_prefix": len(rhs_ordinals),
        "censored_bounds": censored_bounds,
        "progress_events": expected_progress,
        "matrix_available": False,
    }
