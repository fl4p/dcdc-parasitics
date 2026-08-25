#!/usr/bin/env python3
"""Palace solver-control and runtime-memory regressions."""
from test_palace import (
    _write_completion_artifacts, _write_config, canonical_sha256, json,
    load_palace_config_manifest, palace, pytest,
)
from palace_completion import completed_observation_validator
from palace_resources import PalaceTopologyWorkload


def test_direct_linear_solver_type_round_trips(tmp_path):
    config, manifest_path = _write_config(tmp_path, linear_solver_type="SuperLU")
    manifest = load_palace_config_manifest(manifest_path)
    assert manifest.linear_solver_type == "SuperLU"
    assert json.loads(config.read_text())["Solver"]["Linear"]["Type"] == "SuperLU"
    resolved = palace._expected_resolved_config(manifest)
    assert resolved["Solver"]["Linear"]["Type"] == "SuperLU"
    provenance = json.loads(manifest_path.read_text())["provenance"]
    assert provenance["linear_solver_type"] == "SuperLU"


def test_single_level_multigrid_round_trips(tmp_path):
    config, manifest_path = _write_config(
        tmp_path, linear_solver_type="SuperLU", multigrid_max_levels=1)
    manifest = load_palace_config_manifest(manifest_path)
    assert manifest.multigrid_max_levels == 1
    assert json.loads(config.read_text())["Solver"]["Linear"]["MGMaxLevels"] == 1
    resolved = palace._expected_resolved_config(manifest)
    assert resolved["Solver"]["Linear"]["MGMaxLevels"] == 1


def test_completed_observation_validator_supplies_matrix_capability(tmp_path):
    path = tmp_path / "run.json"
    capability = object()
    calls = []

    def validate(candidate, *, matrix_access):
        calls.append((candidate, matrix_access))
        return "validated"

    paths, validator = completed_observation_validator(
        (path,), (capability,), validate_run=validate,
    )
    assert paths == (path,)
    assert validator(path) == "validated"
    assert calls == [(path, capability)]
    with pytest.raises(ValueError, match="count differs"):
        completed_observation_validator(
            (path,), (), validate_run=validate,
        )
    with pytest.raises(ValueError, match="must be unique"):
        completed_observation_validator(
            (path, path), (None, None), validate_run=validate,
        )
    _, validator = completed_observation_validator(
        (path,), (capability,), validate_run=validate,
    )
    with pytest.raises(ValueError, match="not authorized"):
        validator(tmp_path / "other.json")


def test_single_level_runtime_hierarchy_requires_only_finest_space(tmp_path):
    config, manifest_path = _write_config(
        tmp_path, order=3, multigrid_max_levels=1,
    )
    output = tmp_path / "postpro"
    output.mkdir()
    _write_completion_artifacts(output, config)
    manifest = load_palace_config_manifest(manifest_path)
    topology = PalaceTopologyWorkload(
        node_count=manifest.mesh_provenance["node_count"],
        edge_count=manifest.mesh_provenance["edge_count"],
        face_count=manifest.mesh_provenance["face_count"],
        tetrahedron_count=manifest.mesh_provenance["tetrahedron_count"],
        order=manifest.order,
        terminal_count=len(manifest.terminals),
        process_count=1,
    )
    assert len(topology.h1_hierarchy) > 1
    metadata_path = output / "palace.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["Problem"]["DegreesOfFreedom"] = topology.h1_hierarchy[-1]
    metadata["Problem"]["MultigridDegreesOfFreedom"] = [
        topology.h1_hierarchy[-1]
    ]
    metadata_path.write_text(json.dumps(metadata))
    palace._validate_completion_metadata(output, manifest, processes=1)
    metadata["Problem"]["MultigridDegreesOfFreedom"] = list(
        topology.h1_hierarchy
    )
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="completion or identity"):
        palace._validate_completion_metadata(output, manifest, processes=1)


def test_default_multigrid_emits_no_level_override(tmp_path):
    config, manifest_path = _write_config(tmp_path)
    assert "MGMaxLevels" not in json.loads(config.read_text())["Solver"]["Linear"]
    manifest = load_palace_config_manifest(manifest_path)
    assert manifest.multigrid_max_levels is None
    resolved = palace._expected_resolved_config(manifest)
    assert resolved["Solver"]["Linear"]["MGMaxLevels"] == 100


@pytest.mark.parametrize("value", [0, 2, True, "1", 1.0])
def test_multigrid_max_levels_rejects_invalid_values(tmp_path, value):
    with pytest.raises(ValueError, match="multigrid max levels"):
        _write_config(tmp_path, multigrid_max_levels=value)


@pytest.mark.parametrize("value", ["GMRES", "boomeramg", "", None, True, 1])
def test_linear_solver_type_rejects_unknown_values(tmp_path, value):
    with pytest.raises(ValueError, match="linear solver type"):
        _write_config(tmp_path, linear_solver_type=value)


def test_manifest_without_linear_solver_key_defaults_to_boomeramg(tmp_path):
    _, manifest_path = _write_config(tmp_path)
    value = json.loads(manifest_path.read_text())
    value["provenance"].pop("linear_solver_type")
    value["provenance_sha256"] = canonical_sha256(value["provenance"])
    manifest_path.write_text(json.dumps(value))
    assert load_palace_config_manifest(manifest_path).linear_solver_type == "BoomerAMG"


def test_manifest_rejects_solver_type_relabeling(tmp_path):
    _, manifest_path = _write_config(tmp_path, linear_solver_type="SuperLU")
    value = json.loads(manifest_path.read_text())
    value["provenance"]["linear_solver_type"] = "MUMPS"
    value["provenance_sha256"] = canonical_sha256(value["provenance"])
    manifest_path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="semantics do not match"):
        load_palace_config_manifest(manifest_path)


def test_completion_metadata_peak_node_memory_uses_node_semantics(tmp_path):
    config_path, _ = _write_config(tmp_path)
    output = tmp_path / "postpro"
    output.mkdir()
    _write_completion_artifacts(output, config_path)
    manifest = load_palace_config_manifest(config_path.with_suffix(
        config_path.suffix + ".manifest.json"
    ))
    metadata_path = output / "palace.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["Problem"]["MPISize"] = 4
    metadata["PeakMemoryMegabytes"] = {
        "Min": 0.0004, "Max": 0.0006, "Average": 0.0005, "Total": 0.002,
    }
    metadata["PeakNodeMemoryMegabytes"] = {
        "Min": 0.002, "Max": 0.002, "Average": 0.002, "Total": 0.002,
    }
    metadata_path.write_text(json.dumps(metadata))
    palace._validate_completion_metadata(output, manifest, processes=4)
    metadata["PeakNodeMemoryMegabytes"] = {
        "Min": 0.0024, "Max": 0.0024, "Average": 0.0024, "Total": 0.0024,
    }
    metadata_path.write_text(json.dumps(metadata))
    palace._validate_completion_metadata(output, manifest, processes=4)

    for node_memory in (
            {"Min": 0.001, "Max": 0.001, "Average": 0.001, "Total": 0.001},
            {"Min": 0.0005, "Max": 0.001, "Average": 0.0008, "Total": 0.002},
            {"Min": 0.0004, "Max": 0.0005, "Average": 0.0004, "Total": 0.002},
    ):
        metadata["PeakNodeMemoryMegabytes"] = node_memory
        metadata_path.write_text(json.dumps(metadata))
        with pytest.raises(
                ValueError, match="PeakNodeMemoryMegabytes MPI totals"):
            palace._validate_completion_metadata(output, manifest, processes=4)


def test_completion_metadata_rejects_unavailable_peak_memory(tmp_path):
    config_path, _ = _write_config(tmp_path)
    output = tmp_path / "postpro"
    output.mkdir()
    _write_completion_artifacts(output, config_path)
    manifest = load_palace_config_manifest(config_path.with_suffix(
        config_path.suffix + ".manifest.json"
    ))
    metadata_path = output / "palace.json"
    metadata = json.loads(metadata_path.read_text())
    zero = {name: 0.0 for name in ("Average", "Max", "Min", "Total")}
    metadata["PeakMemoryMegabytes"] = zero
    metadata["PeakNodeMemoryMegabytes"] = zero
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="peak-memory collection"):
        palace._validate_completion_metadata(output, manifest, processes=1)
