#!/usr/bin/env python3
"""Pure workload identities and resource bounds for Palace electrostatics."""
from copy import deepcopy
from dataclasses import asdict, dataclass
from math import comb, isfinite
import sys

if __package__:
    from .provenance import canonical_equal, canonical_sha256
else:
    from provenance import canonical_equal, canonical_sha256


TOPOLOGY_WORKLOAD_FORMAT = "dcdc-palace-topology-workload-v1"
RESOURCE_DECISION_FORMAT = "dcdc-palace-topology-resource-decision-v1"
WORKLOAD_FORMAT = "palace-workload-v1"
PALACE_RESOURCE_DECISION_FORMAT = "palace-resource-decision-v1"
SUPPORTED_ORDERS = (1, 2, 3)
PROJECTION_BOUND_NAMES = (
    "wall_time_s", "cpu_time_s", "vector_residency_bytes",
    "hierarchy_operator_solver_bytes", "retained_terminal_vectors_bytes",
    "peak_rss_bytes", "matrix_output_bytes", "logging_output_bytes",
    "checkpoint_write_bytes", "checkpoint_read_bytes",
)


def _positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _minimum_shadow_size(cardinality, uniformity):
    remaining = cardinality
    maximum = 0
    shadow = 0
    for rank in range(uniformity, 0, -1):
        candidate = rank - 1
        while comb(candidate + 1, rank) <= remaining:
            candidate += 1
        if candidate >= rank:
            remaining -= comb(candidate, rank)
            shadow += comb(candidate, rank - 1)
            maximum = candidate
        elif maximum:
            maximum -= 1
    if remaining != 0:
        raise ValueError("simplicial shadow derivation failed")
    return shadow


def tetrahedral_h1_hierarchy(
        node_count, edge_count, face_count, tetrahedron_count, order):
    counts = tuple(
        _positive_int(value, label)
        for value, label in (
            (node_count, "node count"),
            (edge_count, "edge count"),
            (face_count, "face count"),
            (tetrahedron_count, "tetrahedron count"),
            (order, "finite-element order"),
        )
    )
    nodes, edges, faces, tetrahedra, order = counts
    if (nodes < 4 or edges < 6 or faces < 4
            or nodes > 4 * tetrahedra
            or edges > 6 * tetrahedra
            or faces > 4 * tetrahedra
            or edges > comb(nodes, 2)
            or faces > comb(nodes, 3)
            or tetrahedra > comb(nodes, 4)
            or faces < _minimum_shadow_size(tetrahedra, 4)
            or edges < _minimum_shadow_size(faces, 3)
            or nodes < _minimum_shadow_size(edges, 2)):
        raise ValueError("tetrahedral topology counts are combinatorially impossible")
    return tuple(
        nodes
        + (level - 1) * edges
        + comb(level - 1, 2) * faces
        + comb(level - 1, 3) * tetrahedra
        for level in range(1, order + 1)
    )


@dataclass(frozen=True)
class PalaceTopologyWorkload:
    node_count: int
    edge_count: int
    face_count: int
    tetrahedron_count: int
    order: int
    terminal_count: int
    process_count: int

    def __post_init__(self):
        for name, value in asdict(self).items():
            _positive_int(value, name.replace("_", " "))

    @property
    def h1_hierarchy(self):
        return tetrahedral_h1_hierarchy(
            self.node_count,
            self.edge_count,
            self.face_count,
            self.tetrahedron_count,
            self.order,
        )

    @property
    def retained_terminal_vector_bytes_lower_bound(self):
        return 2 * 8 * self.terminal_count * self.h1_hierarchy[-1]

    def record(self):
        hierarchy = self.h1_hierarchy
        payload = {
            "format": TOPOLOGY_WORKLOAD_FORMAT,
            "topology": {
                "nodes": self.node_count,
                "edges": self.edge_count,
                "faces": self.face_count,
                "tetrahedra": self.tetrahedron_count,
            },
            "order": self.order,
            "terminal_count": self.terminal_count,
            "process_count": self.process_count,
            "h1_true_dofs_by_level": list(hierarchy),
            "h1_true_dofs_finest": hierarchy[-1],
            "h1_true_dofs_hierarchy_sum": sum(hierarchy),
            "h1_true_dofs_hierarchy_peak": max(hierarchy),
            "retained_terminal_vector_bytes_lower_bound": (
                self.retained_terminal_vector_bytes_lower_bound
            ),
        }
        return {**payload, "content_sha256": canonical_sha256(payload)}


@dataclass(frozen=True)
class ResourceBound:
    lower: float
    upper: float | None

    def __post_init__(self):
        if (isinstance(self.lower, bool)
                or not isinstance(self.lower, (int, float))
                or not isfinite(float(self.lower))
                or float(self.lower) < 0.0):
            raise ValueError("resource lower bound must be finite and nonnegative")
        if self.upper is not None and (
                isinstance(self.upper, bool)
                or not isinstance(self.upper, (int, float))
                or not isfinite(float(self.upper))
                or float(self.upper) <= 0.0
                or float(self.upper) < float(self.lower)):
            raise ValueError("resource upper bound must be finite and not below lower")


@dataclass(frozen=True)
class PalaceResourceProjection:
    wall_time_s: ResourceBound
    cpu_time_s: ResourceBound
    vector_residency_bytes: ResourceBound
    hierarchy_operator_solver_bytes: ResourceBound
    retained_terminal_vectors_bytes: ResourceBound
    peak_rss_bytes: ResourceBound
    matrix_output_bytes: ResourceBound
    logging_output_bytes: ResourceBound
    checkpoint_write_bytes: ResourceBound
    checkpoint_read_bytes: ResourceBound
    uncertainty_reasons: tuple[str, ...]
    observation_sha256: tuple[str, ...]

    def __post_init__(self):
        if (not isinstance(self.uncertainty_reasons, tuple)
                or not self.uncertainty_reasons
                or any(not isinstance(value, str) or not value
                       for value in self.uncertainty_reasons)):
            raise ValueError("resource uncertainty reasons must be explicit")
        hashes = tuple(self.observation_sha256)
        if (not hashes or len(set(hashes)) != len(hashes)
                or any(not isinstance(value, str) or len(value) != 64
                       or any(character not in "0123456789abcdef"
                              for character in value)
                       for value in hashes)):
            raise ValueError("resource observations must be unique SHA-256 values")


@dataclass(frozen=True)
class PalaceResourceProfile:
    profile_id: str
    rank: int
    wall_time_s: float
    cpu_time_s: float
    peak_rss_bytes: int
    output_bytes: int
    nodes: int
    tetrahedra: int

    def __post_init__(self):
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise ValueError("resource profile ID must be non-empty")
        _positive_int(self.rank, "resource profile rank")
        for name in ("wall_time_s", "cpu_time_s"):
            value = getattr(self, name)
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not isfinite(float(value)) or float(value) <= 0.0):
                raise ValueError(f"profile {name} must be finite and positive")
        for name in (
                "peak_rss_bytes", "output_bytes", "nodes", "tetrahedra"):
            _positive_int(getattr(self, name), name.replace("_", " "))


def select_resource_profile(
        workload, projection, profiles, authorized_profile_ids, *,
        minimum_headroom_ratio):
    if not isinstance(workload, PalaceTopologyWorkload):
        raise ValueError("Palace topology workload is required")
    if not isinstance(projection, PalaceResourceProjection):
        raise ValueError("Palace resource projection is required")
    profiles = tuple(profiles)
    if (not profiles or any(not isinstance(item, PalaceResourceProfile)
                            for item in profiles)
            or len({item.profile_id for item in profiles}) != len(profiles)
            or len({item.rank for item in profiles}) != len(profiles)):
        raise ValueError("resource profile registry is invalid")
    authorized = tuple(authorized_profile_ids)
    known = {item.profile_id for item in profiles}
    if (not authorized or len(set(authorized)) != len(authorized)
            or any(item not in known for item in authorized)):
        raise ValueError("authorized resource profiles are invalid")
    if (isinstance(minimum_headroom_ratio, bool)
            or not isinstance(minimum_headroom_ratio, (int, float))
            or not isfinite(float(minimum_headroom_ratio))
            or float(minimum_headroom_ratio) <= 1.0):
        raise ValueError("minimum resource headroom must be finite and exceed one")
    uppers = {
        name: getattr(projection, name).upper
        for name in PROJECTION_BOUND_NAMES
    }
    if any(value is None for value in uppers.values()):
        raise ValueError("every modeled resource needs a finite upper bound")
    if (projection.retained_terminal_vectors_bytes.lower
            < workload.retained_terminal_vector_bytes_lower_bound
            or projection.peak_rss_bytes.lower
            < projection.retained_terminal_vectors_bytes.lower):
        raise ValueError("memory bounds omit retained terminal vectors")
    memory_upper = sum(uppers[name] for name in (
        "vector_residency_bytes", "hierarchy_operator_solver_bytes",
        "retained_terminal_vectors_bytes",
    ))
    if uppers["peak_rss_bytes"] < memory_upper:
        raise ValueError("peak RSS upper bound omits a memory component")
    output_upper = sum(uppers[name] for name in (
        "matrix_output_bytes", "logging_output_bytes", "checkpoint_write_bytes",
    ))
    headroom = float(minimum_headroom_ratio)
    candidates = [
        profile for profile in profiles
        if profile.profile_id in authorized
        and profile.nodes >= workload.node_count
        and profile.tetrahedra >= workload.tetrahedron_count
        and profile.wall_time_s >= uppers["wall_time_s"] * headroom
        and profile.cpu_time_s >= uppers["cpu_time_s"] * headroom
        and profile.peak_rss_bytes >= uppers["peak_rss_bytes"] * headroom
        and profile.output_bytes >= output_upper * headroom
        and profile.output_bytes >= uppers["checkpoint_read_bytes"] * headroom
    ]
    if not candidates:
        raise ValueError("no authorized resource profile has complete-workload headroom")
    return min(candidates, key=lambda item: item.rank)


def resource_decision_record(
        workload, projection, profiles, authorized_profile_ids, *, policy_id,
        minimum_headroom_ratio):
    if not isinstance(policy_id, str) or not policy_id:
        raise ValueError("trusted resource policy ID is required")
    selected = select_resource_profile(
        workload,
        projection,
        profiles,
        authorized_profile_ids,
        minimum_headroom_ratio=minimum_headroom_ratio,
    )
    registry = [
        asdict(item) for item in sorted(profiles, key=lambda item: item.rank)
    ]
    payload = {
        "format": RESOURCE_DECISION_FORMAT,
        "policy_id": policy_id,
        "profile_registry": registry,
        "profile_registry_sha256": canonical_sha256(registry),
        "authorized_profile_ids": list(authorized_profile_ids),
        "minimum_headroom_ratio": float(minimum_headroom_ratio),
        "workload": workload.record(),
        "projection": {
            **{
                name: asdict(getattr(projection, name))
                for name in PROJECTION_BOUND_NAMES
            },
            "uncertainty_reasons": list(projection.uncertainty_reasons),
            "observation_sha256": list(projection.observation_sha256),
        },
        "selected_profile_id": selected.profile_id,
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}


def build_palace_resource_decision(
        *, palace_workload, projection, profiles, authorized_profile_ids,
        policy_id, policy_sha256, validator_sha256, minimum_headroom_ratio):
    workload = validate_palace_workload(palace_workload)
    topology = workload["topology_workload"]
    counts = topology["topology"]
    topology_workload = PalaceTopologyWorkload(
        node_count=counts["nodes"],
        edge_count=counts["edges"],
        face_count=counts["faces"],
        tetrahedron_count=counts["tetrahedra"],
        order=topology["order"],
        terminal_count=topology["terminal_count"],
        process_count=topology["process_count"],
    )
    if not isinstance(policy_id, str) or not policy_id:
        raise ValueError("trusted resource policy ID is required")
    policy_sha256 = _sha256(policy_sha256, "resource policy SHA-256")
    validator_sha256 = _sha256(validator_sha256, "validator SHA-256")
    selected = select_resource_profile(
        topology_workload,
        projection,
        profiles,
        authorized_profile_ids,
        minimum_headroom_ratio=minimum_headroom_ratio,
    )
    registry = [
        asdict(item) for item in sorted(profiles, key=lambda item: item.rank)
    ]
    payload = {
        "format": PALACE_RESOURCE_DECISION_FORMAT,
        "policy_id": policy_id,
        "policy_sha256": policy_sha256,
        "validator_sha256": validator_sha256,
        "profile_registry": registry,
        "profile_registry_sha256": canonical_sha256(registry),
        "authorized_profile_ids": list(authorized_profile_ids),
        "minimum_headroom_ratio": float(minimum_headroom_ratio),
        "workload": workload,
        "projection": {
            **{
                name: asdict(getattr(projection, name))
                for name in PROJECTION_BOUND_NAMES
            },
            "uncertainty_reasons": list(projection.uncertainty_reasons),
            "observation_sha256": list(projection.observation_sha256),
        },
        "selected_profile_id": selected.profile_id,
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}


def validate_palace_resource_decision(
        record, *, expected_workload, trusted_profiles,
        trusted_authorized_profile_ids, trusted_policy_id,
        trusted_policy_sha256, trusted_validator_sha256,
        trusted_minimum_headroom_ratio):
    if not isinstance(record, dict) or set(record) != {
            "format", "policy_id", "policy_sha256", "validator_sha256",
            "profile_registry", "profile_registry_sha256",
            "authorized_profile_ids", "minimum_headroom_ratio", "workload",
            "projection", "selected_profile_id", "content_sha256"}:
        raise ValueError("Palace resource decision schema mismatch")
    if record["format"] != PALACE_RESOURCE_DECISION_FORMAT:
        raise ValueError("Palace resource decision format mismatch")
    projection = record["projection"]
    if not isinstance(projection, dict) or set(projection) != {
            *PROJECTION_BOUND_NAMES, "uncertainty_reasons", "observation_sha256"}:
        raise ValueError("Palace resource projection schema mismatch")
    try:
        parsed_projection = PalaceResourceProjection(
            **{
                name: ResourceBound(**projection[name])
                for name in PROJECTION_BOUND_NAMES
            },
            uncertainty_reasons=tuple(projection["uncertainty_reasons"]),
            observation_sha256=tuple(projection["observation_sha256"]),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"Palace resource projection is invalid: {error}") from error
    rebuilt = build_palace_resource_decision(
        palace_workload=expected_workload,
        projection=parsed_projection,
        profiles=trusted_profiles,
        authorized_profile_ids=trusted_authorized_profile_ids,
        policy_id=trusted_policy_id,
        policy_sha256=trusted_policy_sha256,
        validator_sha256=trusted_validator_sha256,
        minimum_headroom_ratio=trusted_minimum_headroom_ratio,
    )
    if not canonical_equal(record, rebuilt):
        raise ValueError("Palace resource decision differs from trusted policy")
    return deepcopy(record)


def _sha256(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _finite_probability(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0.0 < float(value) < 1.0):
        raise ValueError(f"{label} must lie strictly between zero and one")
    return float(value)


def validate_topology_workload(record):
    if not isinstance(record, dict) or set(record) != {
            "format", "topology", "order", "terminal_count", "process_count",
            "h1_true_dofs_by_level", "h1_true_dofs_finest",
            "h1_true_dofs_hierarchy_sum", "h1_true_dofs_hierarchy_peak",
            "retained_terminal_vector_bytes_lower_bound", "content_sha256"}:
        raise ValueError("Palace topology workload schema mismatch")
    if record["format"] != TOPOLOGY_WORKLOAD_FORMAT:
        raise ValueError("Palace topology workload format mismatch")
    topology = record["topology"]
    if not isinstance(topology, dict) or set(topology) != {
            "nodes", "edges", "faces", "tetrahedra"}:
        raise ValueError("Palace topology workload counts are malformed")
    try:
        workload = PalaceTopologyWorkload(
            node_count=topology["nodes"], edge_count=topology["edges"],
            face_count=topology["faces"], tetrahedron_count=topology["tetrahedra"],
            order=record["order"], terminal_count=record["terminal_count"],
            process_count=record["process_count"],
        )
        expected = workload.record()
    except ValueError as error:
        raise ValueError(f"Palace topology workload derivation mismatch: {error}") from error
    if not canonical_equal(record, expected):
        raise ValueError("Palace topology workload derivation mismatch")
    return deepcopy(record)


def build_palace_workload(*, topology_workload, mesh_sha256,
                          mesh_manifest_sha256, config_sha256,
                          config_manifest_sha256, build_manifest_sha256,
                          solver_binary_sha256, mpi_launcher_sha256,
                          implementation_sha256, runtime_binaries, host_class,
                          linear_tolerance, explicit_residual_tolerance,
                          maximum_iterations):
    topology = validate_topology_workload(topology_workload)
    if topology["order"] not in SUPPORTED_ORDERS:
        raise ValueError("Palace workload finite-element order is unsupported")
    if not isinstance(host_class, str) or not host_class or len(host_class) > 100:
        raise ValueError("Palace workload host class is invalid")
    maximum_iterations = _positive_int(maximum_iterations, "maximum iterations")
    if not isinstance(runtime_binaries, list) or not runtime_binaries:
        raise ValueError("Palace workload runtime binary inventory is missing")
    normalized_binaries = []
    names = set()
    for record in runtime_binaries:
        if (not isinstance(record, dict) or set(record) != {"name", "sha256"}
                or not isinstance(record["name"], str) or not record["name"]
                or "/" in record["name"] or record["name"] in names):
            raise ValueError("Palace workload runtime binary inventory is invalid")
        names.add(record["name"])
        normalized_binaries.append({
            "name": record["name"],
            "sha256": _sha256(record["sha256"], "runtime binary SHA-256"),
        })
    normalized_binaries.sort(key=lambda record: record["name"])
    if solver_binary_sha256 not in {
            record["sha256"] for record in normalized_binaries}:
        raise ValueError("Palace workload executable is absent from binary inventory")
    payload = {
        "format": WORKLOAD_FORMAT,
        "mesh_sha256": _sha256(mesh_sha256, "mesh SHA-256"),
        "mesh_manifest_sha256": _sha256(
            mesh_manifest_sha256, "mesh manifest SHA-256",
        ),
        "config_sha256": _sha256(config_sha256, "config SHA-256"),
        "config_manifest_sha256": _sha256(
            config_manifest_sha256, "config manifest SHA-256",
        ),
        "build_manifest_sha256": _sha256(
            build_manifest_sha256, "build manifest SHA-256",
        ),
        "solver_binary_sha256": _sha256(
            solver_binary_sha256, "solver binary SHA-256",
        ),
        "mpi_launcher_sha256": _sha256(
            mpi_launcher_sha256, "MPI launcher SHA-256",
        ),
        "implementation_sha256": _sha256(
            implementation_sha256, "implementation SHA-256",
        ),
        "runtime_binaries": normalized_binaries,
        "host_class": host_class,
        "numeric_abi": {
            "scalar": "binary64",
            "byte_order": sys.byteorder,
            "bytes_per_scalar": 8,
        },
        "solver_controls": {
            "order": topology["order"],
            "terminal_count": topology["terminal_count"],
            "process_count": topology["process_count"],
            "linear_tolerance": _finite_probability(
                linear_tolerance, "linear tolerance",
            ),
            "explicit_residual_tolerance": _finite_probability(
                explicit_residual_tolerance, "explicit residual tolerance",
            ),
            "maximum_iterations": maximum_iterations,
        },
        "topology_workload": topology,
    }
    if (payload["solver_controls"]["linear_tolerance"]
            > payload["solver_controls"]["explicit_residual_tolerance"]):
        raise ValueError("Palace workload solver tolerance exceeds verification tolerance")
    return {**payload, "content_sha256": canonical_sha256(payload)}


def validate_palace_workload(record):
    if not isinstance(record, dict) or set(record) != {
            "format", "mesh_sha256", "mesh_manifest_sha256", "config_sha256",
            "config_manifest_sha256", "build_manifest_sha256",
            "solver_binary_sha256", "mpi_launcher_sha256",
            "implementation_sha256", "runtime_binaries", "host_class", "numeric_abi",
            "solver_controls", "topology_workload", "content_sha256"}:
        raise ValueError("Palace workload schema mismatch")
    if record["format"] != WORKLOAD_FORMAT:
        raise ValueError("Palace workload format mismatch")
    controls = record["solver_controls"]
    if not isinstance(controls, dict) or set(controls) != {
            "order", "terminal_count", "process_count", "linear_tolerance",
            "explicit_residual_tolerance", "maximum_iterations"}:
        raise ValueError("Palace workload solver controls are malformed")
    topology = validate_topology_workload(record["topology_workload"])
    rebuilt = build_palace_workload(
        topology_workload=topology,
        mesh_sha256=record["mesh_sha256"],
        mesh_manifest_sha256=record["mesh_manifest_sha256"],
        config_sha256=record["config_sha256"],
        config_manifest_sha256=record["config_manifest_sha256"],
        build_manifest_sha256=record["build_manifest_sha256"],
        solver_binary_sha256=record["solver_binary_sha256"],
        mpi_launcher_sha256=record["mpi_launcher_sha256"],
        implementation_sha256=record["implementation_sha256"],
        runtime_binaries=record["runtime_binaries"],
        host_class=record["host_class"],
        linear_tolerance=controls["linear_tolerance"],
        explicit_residual_tolerance=controls["explicit_residual_tolerance"],
        maximum_iterations=controls["maximum_iterations"],
    )
    if not canonical_equal(record.get("numeric_abi"), rebuilt["numeric_abi"]):
        raise ValueError("Palace workload numeric ABI mismatch")
    if not canonical_equal(record, rebuilt):
        raise ValueError("Palace workload identity or derivation mismatch")
    return deepcopy(record)
