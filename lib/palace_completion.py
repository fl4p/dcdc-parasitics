#!/usr/bin/env python3
"""Validate Palace completion metadata and resolved solver identity."""
from pathlib import Path

import numpy as np

if __package__:
    from .palace_resources import PalaceTopologyWorkload
    from .palace_runtime import validate_runtime_metadata_schema
    from .provenance import canonical_sha256
else:
    from palace_resources import PalaceTopologyWorkload
    from palace_runtime import validate_runtime_metadata_schema
    from provenance import canonical_sha256


def completed_observation_validator(paths, accesses, *, validate_run):
    paths = tuple(paths)
    if not paths:
        raise ValueError("at least one completed observation run is required")
    accesses = (None,) * len(paths) if accesses is None else tuple(accesses)
    if len(accesses) != len(paths):
        raise ValueError("completed observation matrix-access count differs")
    authorized = {
        str(Path(path).resolve()): access
        for path, access in zip(paths, accesses, strict=True)
    }
    if len(authorized) != len(paths):
        raise ValueError("completed observation paths must be unique")

    def validate(path):
        key = str(Path(path).resolve())
        if key not in authorized:
            raise ValueError("completed observation path is not authorized")
        return validate_run(path, matrix_access=authorized[key])

    return paths, validate


def validate_completion_metadata(
        output_directory, manifest, *, processes, load_json_artifact,
        expected_resolved_config):
    palace_path = output_directory / "palace.json"
    resolved_path = output_directory / "config_resolved.json"
    metadata = load_json_artifact(palace_path, label="runtime metadata")
    resolved = load_json_artifact(resolved_path, label="resolved config")
    validate_runtime_metadata_schema(metadata)
    try:
        counts = metadata["ElapsedTime"]["Counts"]
        linear = metadata["LinearSolver"]
        problem = metadata["Problem"]
        git_tag = metadata["GitTag"]
    except (KeyError, TypeError) as error:
        raise ValueError("Palace runtime metadata is incomplete") from error
    terminal_count = len(manifest.terminals)
    linear_solves = counts.get("LinearSolve")
    total_solves = linear.get("TotalSolves")
    expected_solves = terminal_count if manifest.checkpoint is None else total_solves
    required_exact_ints = (
        (counts.get("Total"), 1),
        (linear_solves, expected_solves),
        (counts.get("Estimation"), 0),
        (counts.get("Solve"), 0),
        (problem.get("MPISize"), processes),
        (problem.get("MeshElements"), manifest.mesh_provenance["tetrahedron_count"]),
    )
    total_iterations = linear.get("TotalIts")
    degrees_of_freedom = problem.get("DegreesOfFreedom")
    topology = PalaceTopologyWorkload(
        node_count=manifest.mesh_provenance["node_count"],
        edge_count=manifest.mesh_provenance["edge_count"],
        face_count=manifest.mesh_provenance["face_count"],
        tetrahedron_count=manifest.mesh_provenance["tetrahedron_count"],
        order=manifest.order,
        terminal_count=terminal_count,
        process_count=processes,
    )
    expected_hierarchy = topology.h1_hierarchy
    if manifest.multigrid_max_levels == 1:
        expected_hierarchy = expected_hierarchy[-1:]
    runtime_hierarchy = problem.get("MultigridDegreesOfFreedom")
    if (type(total_solves) is not int
            or not 0 <= total_solves <= terminal_count
            or any(type(value) is not int or value != expected
                   for value, expected in required_exact_ints)
            or type(total_iterations) is not int
            or not 0 <= total_iterations <= total_solves * manifest.maximum_iterations
            or type(degrees_of_freedom) is not int
            or degrees_of_freedom != expected_hierarchy[-1]
            or type(runtime_hierarchy) is not list
            or runtime_hierarchy != list(expected_hierarchy)
            or not isinstance(git_tag, str) or not git_tag):
        raise ValueError("Palace runtime metadata fails completion or identity checks")
    try:
        resolved_identity = canonical_sha256(resolved)
        expected_identity = canonical_sha256(expected_resolved_config(manifest))
    except (TypeError, ValueError) as error:
        raise ValueError("Palace resolved config contains invalid values") from error
    if resolved_identity != expected_identity:
        raise ValueError("Palace resolved config does not match the requested model")
    rank_memory = metadata["PeakMemoryMegabytes"]
    node_memory = metadata["PeakNodeMemoryMegabytes"]
    if rank_memory["Average"] <= 0.0 or node_memory["Average"] <= 0.0:
        raise ValueError("Palace runtime peak-memory collection is unavailable")
    if not np.isclose(
            rank_memory["Total"], rank_memory["Average"] * processes,
            rtol=1e-12, atol=0.0):
        raise ValueError(
            "Palace runtime PeakMemoryMegabytes MPI totals are inconsistent")
    node_count = node_memory["Total"] / node_memory["Average"]
    node_total_regressed = (
        node_memory["Total"] < rank_memory["Total"]
        and not np.isclose(
            node_memory["Total"], rank_memory["Total"],
            rtol=1e-12, atol=0.0)
    )
    if (not np.isfinite(node_count)
            or not np.isclose(node_count, round(node_count), rtol=0.0, atol=1e-9)
            or not 1 <= round(node_count) <= processes
            or node_total_regressed):
        raise ValueError(
            "Palace runtime PeakNodeMemoryMegabytes MPI totals are inconsistent")
    return palace_path, resolved_path, metadata
