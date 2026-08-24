#!/usr/bin/env python3
"""Trusted per-attempt reservation derivation for Palace campaigns."""
import hashlib
import math
import os
from pathlib import Path
import stat

if __package__:
    from .palace_campaign import _validate_accounting
    from .palace_resources import validate_palace_resource_decision, validate_palace_workload
    from .palace_workflow import (
        validate_prelaunch_resource_authority,
        validate_privileged_execution_snapshot_authority,
    )
    from .provenance import canonical_sha256
else:
    from palace_campaign import _validate_accounting
    from palace_resources import validate_palace_resource_decision, validate_palace_workload
    from palace_workflow import (
        validate_prelaunch_resource_authority,
        validate_privileged_execution_snapshot_authority,
    )
    from provenance import canonical_sha256


FORMAT = "palace-attempt-reservation-v1"


def _snapshot_size_and_hash(path):
    path = Path(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Palace reservation snapshot input is not regular")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or size != before.st_size):
            raise ValueError("Palace reservation snapshot input changed during read")
        return size, digest.hexdigest()
    except OSError as error:
        raise ValueError(
            f"Palace reservation snapshot input is unavailable: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def derive_attempt_reservation(
        decision, snapshot, *, expected_workload, trusted_policy):
    workload = validate_palace_workload(expected_workload)
    decision = validate_palace_resource_decision(
        decision,
        expected_workload=workload,
        trusted_profiles=trusted_policy["profile_objects"],
        trusted_authorized_profile_ids=trusted_policy["authorized_profile_tuple"],
        trusted_policy_id=trusted_policy["policy_id"],
        trusted_policy_sha256=trusted_policy["policy_sha256"],
        trusted_validator_sha256=trusted_policy["validator_sha256"],
        trusted_minimum_headroom_ratio=trusted_policy["minimum_headroom_ratio"],
    )
    validate_prelaunch_resource_authority(decision, trusted_policy)
    validate_privileged_execution_snapshot_authority(snapshot, workload)
    expected_hashes = [
        workload["config_sha256"],
        workload["mesh_sha256"],
        workload["mpi_launcher_sha256"],
        *(record["sha256"] for record in workload["runtime_binaries"]),
    ]
    actual_hashes = []
    input_bytes = 0
    seen_paths = set()
    for record in snapshot["inputs"]:
        if (not isinstance(record, dict) or set(record) != {
                "role", "source", "source_sha256", "snapshot", "snapshot_sha256"}
                or record["snapshot"] in seen_paths
                or record["source_sha256"] != record["snapshot_sha256"]):
            raise ValueError("Palace reservation snapshot input identity is invalid")
        seen_paths.add(record["snapshot"])
        size, digest = _snapshot_size_and_hash(record["snapshot"])
        if digest != record["snapshot_sha256"]:
            raise ValueError("Palace reservation snapshot input hash mismatch")
        input_bytes += size
        actual_hashes.append(digest)
    if sorted(actual_hashes) != sorted(expected_hashes):
        raise ValueError("Palace reservation snapshot inputs differ from workload")
    projection = decision["projection"]
    uppers = {}
    for name in (
            "wall_time_s", "cpu_time_s", "peak_rss_bytes",
            "matrix_output_bytes", "logging_output_bytes",
            "checkpoint_write_bytes", "checkpoint_read_bytes"):
        bound = projection[name]
        upper = bound.get("upper") if isinstance(bound, dict) else None
        if (upper is None or isinstance(upper, bool)
                or not isinstance(upper, (int, float))
                or not math.isfinite(float(upper)) or float(upper) < 0.0):
            raise ValueError("Palace reservation requires finite projection uppers")
        uppers[name] = upper
    process_count = workload["solver_controls"]["process_count"]
    terminal_count = workload["solver_controls"]["terminal_count"]
    maximum_iterations = workload["solver_controls"]["maximum_iterations"]
    matrix_bytes = math.ceil(uppers["matrix_output_bytes"])
    logging_bytes = math.ceil(uppers["logging_output_bytes"])
    checkpoint_write = math.ceil(uppers["checkpoint_write_bytes"])
    checkpoint_read = math.ceil(uppers["checkpoint_read_bytes"])
    reservation = _validate_accounting({
        "wall_time_s": float(uppers["wall_time_s"]),
        "cpu_time_s": float(uppers["cpu_time_s"]),
        "iterations": terminal_count * maximum_iterations,
        "solves": terminal_count,
        "bytes_written": matrix_bytes + logging_bytes + checkpoint_write,
        "bytes_read": process_count * input_bytes + checkpoint_read,
        "stdout_bytes": logging_bytes,
        "stderr_bytes": logging_bytes,
        "checkpoint_write_bytes": checkpoint_write,
        "checkpoint_read_bytes": checkpoint_read,
        "retained_output_bytes": matrix_bytes + logging_bytes,
        "retained_checkpoint_bytes": checkpoint_write,
        "peak_rss_bytes": math.ceil(uppers["peak_rss_bytes"]),
    })
    payload = {
        "format": FORMAT,
        "decision_sha256": decision["content_sha256"],
        "snapshot_sha256": snapshot["content_sha256"],
        "workload_sha256": workload["content_sha256"],
        "input_bytes_per_process": input_bytes,
        "process_count": process_count,
        "reservation": reservation,
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}
