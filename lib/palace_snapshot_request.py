#!/usr/bin/env python3
"""Public request writer for privileged Palace execution snapshots."""
from pathlib import Path

if __package__:
    from .palace import load_palace_config_manifest
    from .palace_build import validate_palace_build_manifest
    from .palace_workflow import (
        build_execution_snapshot_request,
        execution_workload_inputs,
    )
    from .provenance import exclusive_publish_json
else:
    from palace import load_palace_config_manifest
    from palace_build import validate_palace_build_manifest
    from palace_workflow import (
        build_execution_snapshot_request,
        execution_workload_inputs,
    )
    from provenance import exclusive_publish_json


def write_palace_execution_snapshot_request(
        config_manifest_path, *, executable, build_manifest_path, processes=1,
        snapshot_id=None):
    config_manifest_path = Path(config_manifest_path).resolve()
    manifest = load_palace_config_manifest(config_manifest_path)
    inputs = execution_workload_inputs(
        manifest,
        config_manifest_path=config_manifest_path,
        build_manifest_path=build_manifest_path,
        executable=executable,
        processes=processes,
        palace_file=Path(__file__).with_name("palace.py"),
        validate_build=validate_palace_build_manifest,
    )
    request = build_execution_snapshot_request(
        manifest, inputs["workload"], executable=inputs["executable"],
        binaries=inputs["binaries"], mpi_launcher=inputs["mpi_launcher"],
        snapshot_id=snapshot_id,
    )
    path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.snapshot-request."
        f"{request['content_sha256']}.json"
    )
    exclusive_publish_json(path, request)
    return path, request
