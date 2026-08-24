#!/usr/bin/env python3
from copy import deepcopy
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from palace_resources import (  # noqa: E402
    PalaceResourceProfile,
    PalaceResourceProjection,
    PalaceTopologyWorkload,
    ResourceBound,
    build_palace_resource_decision,
    build_palace_workload,
    resource_decision_record,
    select_resource_profile,
    tetrahedral_h1_hierarchy,
    validate_palace_resource_decision,
    validate_palace_workload,
)
from provenance import canonical_sha256  # noqa: E402


def workload_record():
    topology = PalaceTopologyWorkload(
        node_count=22526, edge_count=134624, face_count=210289,
        tetrahedron_count=98178, order=2, terminal_count=18,
        process_count=1,
    ).record()
    return build_palace_workload(
        topology_workload=topology,
        mesh_sha256="1" * 64,
        mesh_manifest_sha256="2" * 64,
        config_sha256="3" * 64,
        config_manifest_sha256="4" * 64,
        build_manifest_sha256="5" * 64,
        solver_binary_sha256="6" * 64,
        mpi_launcher_sha256="7" * 64,
        implementation_sha256="8" * 64,
        runtime_binaries=[{"name": "palace", "sha256": "6" * 64}],
        host_class="macos-arm64-native",
        linear_tolerance=1e-12,
        explicit_residual_tolerance=1e-10,
        maximum_iterations=750,
    )


def test_tetrahedral_h1_hierarchy_matches_single_tetrahedron():
    assert tetrahedral_h1_hierarchy(4, 6, 4, 1, 4) == (4, 10, 20, 35)


def test_tetrahedral_hierarchy_rejects_impossible_simplicial_shadow():
    with pytest.raises(ValueError, match="combinatorially impossible"):
        tetrahedral_h1_hierarchy(5, 6, 4, 2, 2)


def test_topology_workload_matches_observed_simple_hb_p2_unknowns():
    workload = PalaceTopologyWorkload(
        node_count=22526,
        edge_count=134624,
        face_count=210289,
        tetrahedron_count=98178,
        order=2,
        terminal_count=18,
        process_count=1,
    )
    record = workload.record()
    assert record["h1_true_dofs_by_level"] == [22526, 157150]
    assert record["h1_true_dofs_finest"] == 157150
    assert record["h1_true_dofs_hierarchy_sum"] == 179676
    assert record["retained_terminal_vector_bytes_lower_bound"] == 45259200
    content_sha256 = record.pop("content_sha256")
    assert content_sha256 == canonical_sha256(record)


def test_complete_workload_is_strict_content_addressed_and_revalidated():
    record = workload_record()
    assert validate_palace_workload(record) == record
    assert record["topology_workload"]["h1_true_dofs_by_level"] == [22526, 157150]
    assert record["solver_controls"] == {
        "order": 2, "terminal_count": 18, "process_count": 1,
        "linear_tolerance": 1e-12,
        "explicit_residual_tolerance": 1e-10,
        "maximum_iterations": 750,
    }


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value.update(extra=True), "schema"),
    (lambda value: value.__setitem__("mesh_sha256", "f" * 64), "identity"),
    (lambda value: value["topology_workload"]["topology"].__setitem__("edges", 4),
     "derivation"),
    (lambda value: value["solver_controls"].__setitem__("order", 3), "identity"),
    (lambda value: value["numeric_abi"].__setitem__("bytes_per_scalar", 4),
     "numeric ABI"),
])
def test_complete_workload_rejects_unknown_rebound_and_derived_drift(mutation, match):
    record = deepcopy(workload_record())
    mutation(record)
    with pytest.raises(ValueError, match=match):
        validate_palace_workload(record)


def test_complete_workload_rejects_unsupported_order_and_boolean_controls():
    topology = PalaceTopologyWorkload(
        node_count=4, edge_count=6, face_count=4, tetrahedron_count=1,
        order=4, terminal_count=2, process_count=1,
    ).record()
    arguments = {
        "topology_workload": topology,
        "mesh_sha256": "1" * 64, "mesh_manifest_sha256": "2" * 64,
        "config_sha256": "3" * 64, "config_manifest_sha256": "4" * 64,
        "build_manifest_sha256": "5" * 64, "solver_binary_sha256": "6" * 64,
        "mpi_launcher_sha256": "7" * 64, "implementation_sha256": "8" * 64,
        "runtime_binaries": [{"name": "palace", "sha256": "6" * 64}],
        "host_class": "host", "linear_tolerance": 1e-12,
        "explicit_residual_tolerance": 1e-10, "maximum_iterations": 10,
    }
    with pytest.raises(ValueError, match="unsupported"):
        build_palace_workload(**arguments)
    arguments["topology_workload"] = workload_record()["topology_workload"]
    arguments["maximum_iterations"] = True
    with pytest.raises(ValueError, match="positive integer"):
        build_palace_workload(**arguments)


def _profiles():
    return (
        PalaceResourceProfile(
            "synthetic", 1, 300.0, 2400.0, 8 * 1024**3, 1024**3,
            1_000_000, 5_000_000,
        ),
        PalaceResourceProfile(
            "pcb_diagnostic", 2, 1800.0, 14_400.0, 24 * 1024**3, 10 * 1024**3,
            10_000_000, 50_000_000,
        ),
        PalaceResourceProfile(
            "extended", 3, 3600.0, 28_800.0, 32 * 1024**3, 20 * 1024**3,
            20_000_000, 100_000_000,
        ),
    )


def _topology_workload():
    return PalaceTopologyWorkload(
        22526, 134624, 210289, 98178, 2, 18, 1
    )


def _complete_projection(workload, *, wall_upper: float | None = 2500.0,
                         retained_lower=None):
    retained = workload.retained_terminal_vector_bytes_lower_bound
    retained_lower = retained if retained_lower is None else retained_lower
    return PalaceResourceProjection(
        wall_time_s=ResourceBound(1.0, wall_upper),
        cpu_time_s=ResourceBound(1.0, 5000.0),
        vector_residency_bytes=ResourceBound(retained, 512 * 1024**2),
        hierarchy_operator_solver_bytes=ResourceBound(1, 1024**3),
        retained_terminal_vectors_bytes=ResourceBound(
            retained_lower, 512 * 1024**2,
        ),
        peak_rss_bytes=ResourceBound(retained, 2 * 1024**3),
        matrix_output_bytes=ResourceBound(1, 256 * 1024),
        logging_output_bytes=ResourceBound(1, 256 * 1024),
        checkpoint_write_bytes=ResourceBound(1, 256 * 1024),
        checkpoint_read_bytes=ResourceBound(0, 256 * 1024),
        uncertainty_reasons=("conservative test bounds",),
        observation_sha256=("a" * 64,),
    )


def test_censored_projection_cannot_select_a_profile():
    workload = _topology_workload()
    projection = _complete_projection(workload, wall_upper=None)
    with pytest.raises(ValueError, match="finite upper bound"):
        select_resource_profile(
            workload, projection, _profiles(),
            ("synthetic", "pcb_diagnostic", "extended"),
            minimum_headroom_ratio=1.1,
        )


def test_smallest_authorized_profile_requires_complete_headroom():
    workload = _topology_workload()
    projection = _complete_projection(workload)
    selected = select_resource_profile(
        workload, projection, _profiles(),
        ("synthetic", "pcb_diagnostic", "extended"),
        minimum_headroom_ratio=1.1,
    )
    assert selected.profile_id == "extended"
    decision = resource_decision_record(
        workload, projection, _profiles(),
        ("synthetic", "pcb_diagnostic", "extended"),
        policy_id="source-controlled-test-policy",
        minimum_headroom_ratio=1.1,
    )
    assert decision["selected_profile_id"] == "extended"
    content_sha256 = decision.pop("content_sha256")
    assert content_sha256 == canonical_sha256(decision)


def test_complete_resource_decision_is_bound_to_trusted_policy_and_workload():
    topology = _topology_workload()
    projection = _complete_projection(topology)
    arguments = {
        "palace_workload": workload_record(),
        "projection": projection,
        "profiles": _profiles(),
        "authorized_profile_ids": (
            "synthetic", "pcb_diagnostic", "extended"
        ),
        "policy_id": "source-controlled-test-policy",
        "policy_sha256": "b" * 64,
        "validator_sha256": "c" * 64,
        "minimum_headroom_ratio": 1.1,
    }
    decision = build_palace_resource_decision(**arguments)
    assert decision["selected_profile_id"] == "extended"
    assert validate_palace_resource_decision(
        decision,
        expected_workload=arguments["palace_workload"],
        trusted_profiles=arguments["profiles"],
        trusted_authorized_profile_ids=arguments["authorized_profile_ids"],
        trusted_policy_id=arguments["policy_id"],
        trusted_policy_sha256=arguments["policy_sha256"],
        trusted_validator_sha256=arguments["validator_sha256"],
        trusted_minimum_headroom_ratio=1.1,
    ) == decision

    rebound = deepcopy(decision)
    rebound["authorized_profile_ids"] = ["extended"]
    rebound["content_sha256"] = canonical_sha256({
        key: value for key, value in rebound.items()
        if key != "content_sha256"
    })
    with pytest.raises(ValueError, match="trusted policy"):
        validate_palace_resource_decision(
            rebound,
            expected_workload=arguments["palace_workload"],
            trusted_profiles=arguments["profiles"],
            trusted_authorized_profile_ids=arguments[
                "authorized_profile_ids"
            ],
            trusted_policy_id=arguments["policy_id"],
            trusted_policy_sha256=arguments["policy_sha256"],
            trusted_validator_sha256=arguments["validator_sha256"],
            trusted_minimum_headroom_ratio=1.1,
        )


def test_terminal_vector_residency_is_a_required_rss_lower_bound():
    projection = _complete_projection(
        _topology_workload(), wall_upper=2.0, retained_lower=1,
    )
    with pytest.raises(ValueError, match="retained terminal vectors"):
        select_resource_profile(
            _topology_workload(), projection, _profiles(),
            ("synthetic", "pcb_diagnostic"),
            minimum_headroom_ratio=1.1,
        )


def test_resource_registry_rejects_ambiguous_ranks():
    profiles = (*_profiles()[:2], PalaceResourceProfile(
        "other", 2, 3600.0, 28_800.0, 32 * 1024**3, 20 * 1024**3,
        20_000_000, 100_000_000,
    ))
    workload = _topology_workload()
    projection = _complete_projection(workload, wall_upper=2.0)
    with pytest.raises(ValueError, match="registry"):
        select_resource_profile(
            workload, projection, profiles,
            tuple(item.profile_id for item in profiles),
            minimum_headroom_ratio=1.1,
        )


@pytest.mark.parametrize("field,value", [
    ("node_count", True),
    ("edge_count", 0),
    ("face_count", -1),
    ("tetrahedron_count", 1.0),
    ("order", False),
    ("terminal_count", 0),
    ("process_count", "1"),
])
def test_topology_workload_rejects_invalid_integers(field, value):
    values = {
        "node_count": 4,
        "edge_count": 6,
        "face_count": 4,
        "tetrahedron_count": 1,
        "order": 2,
        "terminal_count": 2,
        "process_count": 1,
    }
    values[field] = value
    with pytest.raises(ValueError, match="positive integer"):
        PalaceTopologyWorkload(**values)
