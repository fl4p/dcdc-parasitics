#!/usr/bin/env python3
"""Palace progress and trusted resource-decision workflow helpers."""
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import stat

import psutil

if __package__:
    from .palace_build import validate_palace_build_manifest
    from .palace_resources import (
        PalaceResourceProfile,
        PalaceResourceProjection,
        PalaceTopologyWorkload,
        ResourceBound,
        build_palace_workload,
        validate_palace_resource_decision,
    )
    from .provenance import (
        bytes_sha256, canonical_sha256, exclusive_publish_bytes, file_sha256,
    )
else:
    from palace_build import validate_palace_build_manifest
    from palace_resources import (
        PalaceResourceProfile,
        PalaceResourceProjection,
        PalaceTopologyWorkload,
        ResourceBound,
        build_palace_workload,
        validate_palace_resource_decision,
    )
    from provenance import (
        bytes_sha256, canonical_sha256, exclusive_publish_bytes, file_sha256,
    )


RESOURCE_POLICY_ID = "dcdc-palace-authorized-resource-policy-v2"
RESOURCE_MINIMUM_HEADROOM_RATIO = 1.1
RESOURCE_OBSERVATION_WALL_FACTOR = 4.0
RESOURCE_OBSERVATION_RSS_FACTOR = 2.0
RESOURCE_OBSERVATION_OUTPUT_FACTOR = 4.0
RESOURCE_AUTHORIZED_OBSERVATION_SHA256 = ()
RESOURCE_AUTHORIZED_STATIC_PROJECTION_SHA256 = ()

def palace_config_payload(
        *, relative_mesh, relative_output, terminals, materials,
        ground_attribute, order, linear_tolerance,
        explicit_residual_tolerance, maximum_iterations, checkpoint=None):
    electrostatic: dict[str, object] = {"Save": 0}
    if checkpoint is not None:
        electrostatic["Checkpoint"] = {
            "Path": checkpoint["path"],
            "CampaignIdentity": checkpoint["native_campaign_identity"],
        }
    return {
        "Problem": {
            "Type": "Electrostatic", "Verbose": 2,
            "Output": relative_output.as_posix(),
        },
        "Model": {
            "Mesh": relative_mesh.as_posix(), "L0": 1.0,
            "Refinement": {"MaxIts": 0},
        },
        "Domains": {"Materials": [{
            "Attributes": list(material.attributes),
            "Permittivity": material.relative_permittivity,
        } for material in materials]},
        "Boundaries": {
            "Ground": {"Attributes": [ground_attribute]},
            "Terminal": [
                {"Index": terminal.index, "Attributes": [terminal.attribute]}
                for terminal in terminals
            ],
        },
        "Solver": {
            "Order": order, "Device": "CPU", "Electrostatic": electrostatic,
            "Linear": {
                "Type": "BoomerAMG", "KSPType": "CG",
                "Tol": linear_tolerance,
                "VerificationTol": explicit_residual_tolerance,
                "MaxIts": maximum_iterations,
            },
        },
    }


PALACE_PROGRESS_PATTERNS = (
    ("native_milestone", re.compile(
        rb"PALACE_MILESTONE (?P<milestone>[a-z_]+) (?P<detail>[^\r\n]+)"
    )),
    ("field_solve_started", re.compile(
        rb"Computing electrostatic fields for (?P<count>\d+) terminal boundaries"
    )),
    ("rhs_started", re.compile(
        rb"^\s*0 KSP residual norm \|\|r\|\|_B = (?P<value>[0-9.eE+-]+)",
        re.MULTILINE,
    )),
    ("rhs_converged", re.compile(
        rb"PCG solver converged in (?P<iterations>\d+) iterations"
    )),
    ("rhs_not_converged", re.compile(
        rb"PCG solver did NOT converge in (?P<iterations>\d+) iterations"
    )),
    ("explicit_residual_completed", re.compile(
        rb"Explicit residual \|\|b-Ax\|\|/\|\|b\|\| = "
        rb"(?P<residual>[0-9.eE+-]+) \(target = (?P<target>[0-9.eE+-]+)\)"
    )),
)


def implementation_identity(palace_file):
    palace_file = Path(palace_file).resolve()
    directory = palace_file.parent
    paths = (
        palace_file,
        directory / "palace_attempt.py",
        directory / "palace_build.py",
        directory / "palace_campaign.py",
        directory / "palace_mesh.py",
        directory / "palace_plc_mesh.py",
        directory / "kicad_fastercap.py",
        directory / "kicad_fastercap_schema.py",
        directory / "kicad_palace.py",
        directory / "kicad_palace_dump.py",
        directory / "kicad_palace_schema.py",
        directory.parent / "extract_palace_mesh.py",
        directory / "maxwell.py",
        directory / "palace_matrix_gates.py",
        directory / "process_monitor.py",
        directory / "provenance.py",
        directory / "palace_resources.py",
        directory / "palace_workflow.py",
    )
    if any(not path.is_file() for path in paths):
        raise ValueError("Palace qualification implementation is incomplete")
    return {str(path): file_sha256(path) for path in paths}


def workload_record(
        manifest, *, config_manifest_path, build_manifest_path, executable,
        processes, implementation, binaries, mpi_launcher):
    provenance = manifest.mesh_provenance
    topology = PalaceTopologyWorkload(
        node_count=provenance["node_count"],
        edge_count=provenance["edge_count"],
        face_count=provenance["face_count"],
        tetrahedron_count=provenance["tetrahedron_count"],
        order=manifest.order,
        terminal_count=len(manifest.terminals),
        process_count=processes,
    ).record()
    return build_palace_workload(
        topology_workload=topology,
        mesh_sha256=file_sha256(manifest.mesh_path),
        mesh_manifest_sha256=file_sha256(manifest.mesh_manifest_path),
        config_sha256=file_sha256(manifest.config_path),
        config_manifest_sha256=file_sha256(config_manifest_path),
        build_manifest_sha256=file_sha256(build_manifest_path),
        solver_binary_sha256=binaries[str(executable)],
        mpi_launcher_sha256=file_sha256(mpi_launcher),
        implementation_sha256=canonical_sha256(implementation),
        runtime_binaries=[
            {"name": Path(path).name, "sha256": digest}
            for path, digest in sorted(binaries.items())
        ],
        host_class=(
            f"{platform.system().lower()}-{platform.machine().lower()}-"
            f"{platform.processor().lower() or 'unknown-cpu'}-"
            f"cpu{os.cpu_count() or 0}-"
            f"mem{psutil.virtual_memory().total}-native"
        ),
        linear_tolerance=manifest.linear_tolerance,
        explicit_residual_tolerance=manifest.explicit_residual_tolerance,
        maximum_iterations=manifest.maximum_iterations,
    )


def execution_workload_inputs(
        manifest, *, config_manifest_path, build_manifest_path, executable,
        processes, palace_file, validate_build=validate_palace_build_manifest):
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise ValueError("Palace executable does not exist")
    if (type(processes) is not int or processes <= 0
            or processes > (os.cpu_count() or 1)):
        raise ValueError("Palace process count must fit the trusted CPU envelope")
    build_manifest_path = Path(build_manifest_path).resolve()
    build = validate_build(
        build_manifest_path, executable=executable
    )
    binaries = {str(path): file_sha256(path) for path in binary_paths(executable)}
    implementation = implementation_identity(palace_file)
    mpi_launcher = shutil.which("mpirun")
    if mpi_launcher is None:
        raise ValueError("Palace MPI launcher is unavailable")
    mpi_launcher = str(Path(mpi_launcher).resolve())
    workload = workload_record(
        manifest,
        config_manifest_path=Path(config_manifest_path).resolve(),
        build_manifest_path=build_manifest_path,
        executable=executable,
        processes=processes,
        implementation=implementation,
        binaries=binaries,
        mpi_launcher=mpi_launcher,
    )
    return {
        "executable": executable,
        "build_manifest_path": build_manifest_path,
        "build": build,
        "binaries": binaries,
        "implementation": implementation,
        "mpi_launcher": mpi_launcher,
        "workload": workload,
    }


def prepare_execution_snapshot(
        manifest, workload, *, executable, binaries, mpi_launcher):
    root = manifest.config_path.parent / (
        f".{manifest.config_path.name}.execution.{workload['content_sha256']}"
    )
    os.mkdir(root, mode=0o700)
    bin_directory = root / "bin"
    os.mkdir(bin_directory, mode=0o700)
    sources = [
        ("config", manifest.config_path, root / manifest.config_path.name, False),
        ("mesh", manifest.mesh_path, root / manifest.mesh_path.name, False),
    ]
    for source in binaries:
        role = "executable" if Path(source) == Path(executable) else "solver_binary"
        sources.append((role, Path(source), bin_directory / Path(source).name, True))
    sources.append((
        "mpi_launcher", Path(mpi_launcher),
        bin_directory / Path(mpi_launcher).name, True,
    ))
    bound_hashes = {
        "config": workload["config_sha256"],
        "mesh": workload["mesh_sha256"],
        "executable": workload["solver_binary_sha256"],
        "mpi_launcher": workload["mpi_launcher_sha256"],
    }
    runtime_binaries = {
        record["name"]: record["sha256"]
        for record in workload["runtime_binaries"]
    }
    records = []
    for role, source, snapshot, executable_bit in sources:
        content = source.read_bytes()
        expected_hash = (
            runtime_binaries.get(source.name)
            if role in {"executable", "solver_binary"}
            else bound_hashes.get(role)
        )
        if expected_hash is None or bytes_sha256(content) != expected_hash:
            raise ValueError(f"Palace {role} changed after workload derivation")
        exclusive_publish_bytes(snapshot, content)
        snapshot.chmod(0o500 if executable_bit else 0o400)
        records.append({
            "role": role,
            "source": str(source.resolve()),
            "source_sha256": file_sha256(source),
            "snapshot": str(snapshot.resolve()),
            "snapshot_sha256": file_sha256(snapshot),
        })
    output_directory = root / manifest.output_directory.name
    os.mkdir(output_directory, mode=0o700)
    bin_directory.chmod(0o500)
    root.chmod(0o500)
    payload = {
        "format": "palace-execution-snapshot-v1",
        "root": str(root.resolve()),
        "workload_sha256": workload["content_sha256"],
        "inputs": records,
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}


def validate_execution_snapshot_privilege_boundary(record):
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise ValueError("Palace execution snapshots require a POSIX privilege boundary")
    root = Path(record.get("root", "")).absolute()
    input_paths = [Path(item.get("snapshot", "")).absolute()
                   for item in record.get("inputs", ())]
    try:
        if any(path.relative_to(root) is None for path in input_paths):
            raise ValueError
    except ValueError as error:
        raise ValueError("Palace execution snapshot input escapes its root") from error
    ancestry = []
    cursor = root
    while True:
        ancestry.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    paths = [*ancestry, root / "bin", *input_paths]
    execution_uid = os.geteuid()
    for path in paths:
        try:
            metadata = path.lstat()
        except (OSError, ValueError) as error:
            raise ValueError(
                "Palace execution snapshot privilege boundary is unavailable"
            ) from error
        if (stat.S_ISLNK(metadata.st_mode) or metadata.st_uid == execution_uid
                or stat.S_IMODE(metadata.st_mode) & 0o222):
            raise ValueError(
                "Palace execution snapshot is mutable by the execution UID"
            )
    return True


def validate_execution_snapshot(
        record, manifest, workload, *, executable, binaries, mpi_launcher):
    if not isinstance(record, dict) or set(record) != {
            "format", "root", "workload_sha256", "inputs", "content_sha256"}:
        raise ValueError("Palace execution snapshot schema mismatch")
    payload = {key: value for key, value in record.items()
               if key != "content_sha256"}
    if (record["format"] != "palace-execution-snapshot-v1"
            or record["content_sha256"] != canonical_sha256(payload)
            or record["workload_sha256"] != workload["content_sha256"]):
        raise ValueError("Palace execution snapshot identity mismatch")
    root = manifest.config_path.parent / (
        f".{manifest.config_path.name}.execution.{workload['content_sha256']}"
    )
    if (Path(record["root"]).resolve() != root.resolve()
            or stat.S_IMODE(root.stat().st_mode) != 0o500
            or stat.S_IMODE((root / "bin").stat().st_mode) != 0o500):
        raise ValueError("Palace execution snapshot root mismatch")
    expected_sources = [
        ("config", manifest.config_path, root / manifest.config_path.name),
        ("mesh", manifest.mesh_path, root / manifest.mesh_path.name),
    ]
    bin_directory = root / "bin"
    for source in binaries:
        role = "executable" if Path(source) == Path(executable) else "solver_binary"
        expected_sources.append((role, Path(source), bin_directory / Path(source).name))
    expected_sources.append((
        "mpi_launcher", Path(mpi_launcher), bin_directory / Path(mpi_launcher).name
    ))
    expected = []
    for role, source, snapshot in expected_sources:
        expected_mode = 0o500 if role in {"executable", "solver_binary", "mpi_launcher"} else 0o400
        if (not source.is_file() or not snapshot.is_file()
                or stat.S_IMODE(snapshot.stat().st_mode) != expected_mode):
            raise ValueError("Palace execution snapshot input is missing or writable")
        source_sha256 = file_sha256(source)
        snapshot_sha256 = file_sha256(snapshot)
        bound_hashes = {
            "config": workload["config_sha256"],
            "mesh": workload["mesh_sha256"],
            "executable": workload["solver_binary_sha256"],
            "mpi_launcher": workload["mpi_launcher_sha256"],
        }
        runtime_binaries = {
            record["name"]: record["sha256"]
            for record in workload["runtime_binaries"]
        }
        expected_hash = (
            runtime_binaries.get(source.name)
            if role in {"executable", "solver_binary"}
            else bound_hashes.get(role)
        )
        if (source_sha256 != snapshot_sha256
                or expected_hash is None or source_sha256 != expected_hash):
            raise ValueError("Palace execution snapshot differs from bound workload")
        expected.append({
            "role": role,
            "source": str(source.resolve()),
            "source_sha256": source_sha256,
            "snapshot": str(snapshot.resolve()),
            "snapshot_sha256": snapshot_sha256,
        })
    if record["inputs"] != expected:
        raise ValueError("Palace execution snapshot inputs mismatch")
    return record


def publish_snapshot_output(snapshot_root, output_directory):
    source = Path(snapshot_root) / Path(output_directory).name
    if not source.is_dir():
        return
    output_directory = Path(output_directory)
    os.mkdir(output_directory, mode=0o700)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = output_directory / relative
        if path.is_symlink():
            raise ValueError("Palace snapshot output contains a symlink")
        if path.is_dir():
            os.mkdir(target, mode=0o700)
        elif path.is_file():
            exclusive_publish_bytes(target, path.read_bytes())
        else:
            raise ValueError("Palace snapshot output contains a special file")


def binary_paths(executable):
    executable = Path(executable).resolve()
    paths = [executable]
    paths.extend(
        candidate.resolve()
        for candidate in sorted(executable.parent.glob("palace-*.bin"))
        if candidate.is_file()
    )
    return tuple(dict.fromkeys(paths))


def requires_pcb_resource_decision(manifest):
    source_identity = manifest.mesh_provenance.get("source_identity")
    return (
        isinstance(source_identity, dict)
        and source_identity.get("kind") == "kicad_volume_dump"
    )


def trusted_resource_policy(
        resource_limits, mesh_limits, *, validator_path,
        authorized_observation_sha256=RESOURCE_AUTHORIZED_OBSERVATION_SHA256,
        authorized_static_projection_sha256=(
            RESOURCE_AUTHORIZED_STATIC_PROJECTION_SHA256)):
    profiles = tuple(
        PalaceResourceProfile(
            profile_id=name,
            rank=rank,
            wall_time_s=resource_limits[name].wall_time_s,
            cpu_time_s=(
                resource_limits[name].wall_time_s * (os.cpu_count() or 1)
            ),
            peak_rss_bytes=resource_limits[name].peak_rss_bytes,
            output_bytes=resource_limits[name].output_bytes,
            nodes=mesh_limits[name]["nodes"],
            tetrahedra=mesh_limits[name]["tetrahedra"],
        )
        for rank, name in enumerate(resource_limits, 1)
    )
    authorized = tuple(profile.profile_id for profile in profiles)
    payload = {
        "policy_id": RESOURCE_POLICY_ID,
        "profiles": [asdict(profile) for profile in profiles],
        "authorized_profile_ids": list(authorized),
        "minimum_headroom_ratio": RESOURCE_MINIMUM_HEADROOM_RATIO,
        "authorized_observation_sha256": list(
            authorized_observation_sha256
        ),
        "authorized_static_projection_sha256": list(
            authorized_static_projection_sha256
        ),
        "completed_observation_projection": {
            "wall_factor": RESOURCE_OBSERVATION_WALL_FACTOR,
            "wall_fixed_s": 1.0,
            "rss_factor": RESOURCE_OBSERVATION_RSS_FACTOR,
            "rss_fixed_bytes": 64 * 1024**2,
            "output_factor": RESOURCE_OBSERVATION_OUTPUT_FACTOR,
            "output_fixed_bytes": 1024**2,
        },
    }
    return {
        **payload,
        "policy_sha256": canonical_sha256(payload),
        "validator_sha256": file_sha256(validator_path),
        "profile_objects": profiles,
        "authorized_profile_tuple": authorized,
        "authorized_observation_tuple": tuple(
            authorized_observation_sha256
        ),
        "authorized_static_projection_tuple": tuple(
            authorized_static_projection_sha256
        ),
    }


def validate_prelaunch_resource_authority(decision, policy):
    projection_sha256 = canonical_sha256(decision.get("projection"))
    if projection_sha256 not in policy["authorized_static_projection_tuple"]:
        raise ValueError("Palace prelaunch resource projection is not authorized")
    return projection_sha256


def validate_bound_resource_decision(
        raw_decision, *, run_path, expected_workload, policy, resource_class,
        decision_path, config_path):
    trusted_observation = file_sha256(run_path) in (
        policy["authorized_observation_tuple"]
    )
    static_projection = canonical_sha256(raw_decision.get("projection")) in (
        policy["authorized_static_projection_tuple"]
    )
    if trusted_observation:
        decision = raw_decision
        unsigned_decision = dict(decision)
        decision_content = unsigned_decision.pop("content_sha256", None)
        if decision_content != canonical_sha256(unsigned_decision):
            raise ValueError("trusted observation decision hash mismatch")
    else:
        decision = validate_palace_resource_decision(
            raw_decision,
            expected_workload=expected_workload,
            trusted_profiles=policy["profile_objects"],
            trusted_authorized_profile_ids=policy["authorized_profile_tuple"],
            trusted_policy_id=policy["policy_id"],
            trusted_policy_sha256=policy["policy_sha256"],
            trusted_validator_sha256=policy["validator_sha256"],
            trusted_minimum_headroom_ratio=policy["minimum_headroom_ratio"],
        )
        if (not static_projection
                and any(value not in policy["authorized_observation_tuple"]
                        for value in decision["projection"][
                            "observation_sha256"])):
            raise ValueError("Palace resource projection is not authorized")
    expected_name = (
        f"{config_path.name}.resource-decision."
        f"{decision['content_sha256']}.json"
    )
    if (decision_path.parent != config_path.parent
            or decision_path.name != expected_name
            or decision["selected_profile_id"] != resource_class):
        raise ValueError("Palace run resource decision binding mismatch")
    return decision


def _native_milestone(match):
    name = match.group("milestone").decode("ascii")
    text = match.group("detail").decode("ascii")
    detail = {}
    for token in text.split(" "):
        if token.count("=") != 1:
            raise ValueError("Palace native milestone detail is malformed")
        key, raw = token.split("=", 1)
        if not key or key in detail:
            raise ValueError("Palace native milestone detail key is invalid")
        if raw.startswith("[") and raw.endswith("]"):
            body = raw[1:-1]
            value = [] if not body else [int(item) for item in body.split(",")]
        elif raw in ("load", "solve", "loaded", "solved", "electrostatic",
                     "disabled") or key == "campaign_digest":
            value = raw
        elif key == "value":
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("Palace native milestone value is nonfinite")
        else:
            value = int(raw)
        detail[key] = value
    return name, detail


def palace_progress_events(stdout, stream_events):
    stdout_chunks = [
        event for event in stream_events if event.get("source") == "stdout"
    ]

    def observed_time(offset):
        for event in stdout_chunks:
            start = event["byte_offset"]
            if start <= offset < start + event["byte_count"]:
                return event["monotonic_ns"]
        raise ValueError("Palace progress marker is outside stdout event coverage")

    matches = sorted(
        (match.start(), match.end() - 1, name, match)
        for name, pattern in PALACE_PROGRESS_PATTERNS
        for match in pattern.finditer(stdout)
    )
    events = []
    completed_rhs = 0
    active_rhs = None
    for offset, completion_offset, name, match in matches:
        detail = {}
        rhs_ordinal = None
        if name == "native_milestone":
            name, detail = _native_milestone(match)
            rhs_ordinal = detail.get("rhs")
        elif name == "field_solve_started":
            detail["terminal_count"] = int(match.group("count"))
        elif name == "rhs_started":
            if active_rhs is None:
                active_rhs = completed_rhs + 1
            rhs_ordinal = active_rhs
            detail["initial_preconditioned_residual"] = float(
                match.group("value")
            )
        elif name in ("rhs_converged", "rhs_not_converged"):
            if active_rhs is None:
                active_rhs = completed_rhs + 1
            rhs_ordinal = active_rhs
            detail["iterations"] = int(match.group("iterations"))
        else:
            if active_rhs is None:
                active_rhs = completed_rhs + 1
            rhs_ordinal = active_rhs
            detail.update({
                "explicit_relative_residual": float(match.group("residual")),
                "target": float(match.group("target")),
            })
            completed_rhs += 1
            active_rhs = None
        events.append({
            "event": name,
            "rhs_ordinal": rhs_ordinal,
            "stdout_byte_offset": offset,
            "observed_monotonic_ns": observed_time(completion_offset),
            "time_semantics": "reader_receipt_upper_bound",
            "detail": detail,
        })
    return events


def validate_completed_palace_progress(events, terminal_count):
    cursor = 0

    def take(name):
        nonlocal cursor
        if cursor >= len(events) or events[cursor]["event"] != name:
            raise ValueError("Palace accepted-run progress sequence is incomplete")
        event = events[cursor]
        cursor += 1
        return event

    setup_start = take("setup_start")["detail"]
    if (setup_start.get("solver") != "electrostatic"
            or setup_start.get("checkpoint_enabled") not in (0, 1)
            or set(setup_start) != {"solver", "checkpoint_enabled"}):
        raise ValueError("Palace setup-start milestone is invalid")
    setup_end = take("setup_end")["detail"]
    if set(setup_end) != {
            "campaign_digest", "prefix_before", "loaded_rhs", "new_rhs"}:
        raise ValueError("Palace setup-end milestone is invalid")
    prefix = setup_end["prefix_before"]
    if (type(prefix) is not int or not 0 <= prefix <= terminal_count
            or setup_end["loaded_rhs"] != list(range(1, prefix + 1))
            or setup_end["new_rhs"] != list(range(prefix + 1, terminal_count + 1))
            or (setup_start["checkpoint_enabled"] == 0
                and (prefix != 0 or setup_end["campaign_digest"] != "disabled"))):
        raise ValueError("Palace setup milestone prefix is inconsistent")
    field = take("field_solve_started")
    if field["detail"] != {"terminal_count": terminal_count}:
        raise ValueError("Palace progress terminal count is inconsistent")
    total_iterations = 0
    for ordinal in range(1, terminal_count + 1):
        action = "load" if ordinal <= prefix else "solve"
        started = take("rhs_start")
        if (started["rhs_ordinal"] != ordinal
                or started["detail"].get("action") != action
                or set(started["detail"]) != {"rhs", "terminal", "action"}):
            raise ValueError("Palace native RHS start is inconsistent")
        if action == "solve":
            if take("rhs_started")["rhs_ordinal"] != ordinal:
                raise ValueError("Palace KSP RHS ordering is inconsistent")
            converged = take("rhs_converged")
            if converged["rhs_ordinal"] != ordinal:
                raise ValueError("Palace KSP convergence ordering is inconsistent")
            total_iterations += converged["detail"]["iterations"]
        if take("explicit_residual_completed")["rhs_ordinal"] != ordinal:
            raise ValueError("Palace residual text ordering is inconsistent")
        residual = take("residual_accepted")
        reaction = take("reaction_accepted")
        if (residual["rhs_ordinal"] != ordinal
                or reaction["rhs_ordinal"] != ordinal
                or residual["detail"].get("source")
                != ("loaded" if action == "load" else "solved")
                or reaction["detail"].get("entries") != terminal_count):
            raise ValueError("Palace native RHS acceptance is inconsistent")
        if action == "solve" and setup_start["checkpoint_enabled"]:
            checkpoint = take("checkpoint_published")
            if (checkpoint["rhs_ordinal"] != ordinal
                    or checkpoint["detail"].get("prefix_after") != ordinal):
                raise ValueError("Palace checkpoint prefix is inconsistent")
        finished = take("rhs_loaded" if action == "load" else "rhs_solved")
        if (finished["rhs_ordinal"] != ordinal
                or finished["detail"].get("solves") != (0 if action == "load" else 1)
                or (action == "load" and finished["detail"].get("iterations") != 0)):
            raise ValueError("Palace native RHS accounting is inconsistent")
    final_start = take("finalization_start")["detail"]
    final_end = take("finalization_end")["detail"]
    expected_after = terminal_count if setup_start["checkpoint_enabled"] else 0
    if (cursor != len(events)
            or final_start != {
                "terminal_count": terminal_count,
                "prefix_before": prefix,
                "prefix_after": expected_after,
            }
            or final_end.get("prefix_before") != prefix
            or final_end.get("prefix_after") != expected_after
            or final_end.get("total_solves") != terminal_count - prefix
            or final_end.get("total_iterations") != total_iterations
            or set(final_end) != {
                "prefix_before", "prefix_after", "total_solves",
                "total_iterations", "checkpoint_read_bytes",
                "checkpoint_written_bytes", "checkpoint_retained_bytes"}):
        raise ValueError("Palace finalization milestone is inconsistent")
    offsets = [event["stdout_byte_offset"] for event in events]
    timestamps = [event["observed_monotonic_ns"] for event in events]
    if offsets != sorted(offsets) or timestamps != sorted(timestamps):
        raise ValueError("Palace progress event order is inconsistent")


def _projection_workload_identity(workload):
    return {
        key: value for key, value in workload.items()
        if key != "content_sha256"
    }


def projection_from_completed_runs(
        expected_workload, run_manifest_paths, *, validate_run,
        validate_workload):
    paths = tuple(sorted(Path(path).resolve() for path in run_manifest_paths))
    if not paths:
        raise ValueError("at least one completed observation run is required")
    elapsed = []
    rss = []
    output = []
    observation_sha256 = []
    expected_identity = _projection_workload_identity(expected_workload)
    for path in paths:
        observation = validate_run(path)
        raw = observation["raw"]
        workload_path = Path(raw["workload"]).resolve()
        observed_workload = validate_workload(json.loads(workload_path.read_text()))
        if _projection_workload_identity(observed_workload) != expected_identity:
            raise ValueError("resource observation workload is not comparable")
        execution = raw["execution"]
        elapsed.append(execution["elapsed_s"])
        rss.append(execution["peak_rss_bytes"])
        output.append(
            execution["output_bytes"] + execution["directory_growth_bytes"]
        )
        observation_sha256.append(file_sha256(path))
    topology = expected_workload["topology_workload"]
    retained = topology["retained_terminal_vector_bytes_lower_bound"]
    elapsed_max = max(elapsed)
    rss_max = max(max(rss), retained)
    output_max = max(output)
    return PalaceResourceProjection(
        wall_time_s=ResourceBound(
            elapsed_max,
            elapsed_max * RESOURCE_OBSERVATION_WALL_FACTOR + 1.0,
        ),
        cpu_time_s=ResourceBound(0.0, None),
        vector_residency_bytes=ResourceBound(
            16 * topology["h1_true_dofs_finest"], None,
        ),
        hierarchy_operator_solver_bytes=ResourceBound(0, None),
        retained_terminal_vectors_bytes=ResourceBound(retained, retained),
        peak_rss_bytes=ResourceBound(
            rss_max,
            math.ceil(rss_max * RESOURCE_OBSERVATION_RSS_FACTOR)
            + 64 * 1024**2,
        ),
        matrix_output_bytes=ResourceBound(0, None),
        logging_output_bytes=ResourceBound(
            output_max,
            math.ceil(output_max * RESOURCE_OBSERVATION_OUTPUT_FACTOR)
            + 1024**2,
        ),
        checkpoint_write_bytes=ResourceBound(0, None),
        checkpoint_read_bytes=ResourceBound(0, None),
        uncertainty_reasons=(
            "CPU time is absent from the legacy observation witness",
            "operator, solver, matrix, and checkpoint component bounds are absent",
        ),
        observation_sha256=tuple(observation_sha256),
    )
