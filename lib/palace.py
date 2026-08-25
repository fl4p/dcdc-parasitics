#!/usr/bin/env python3
"""Fail-closed Palace electrostatic configuration, execution, and result parsing."""
from dataclasses import asdict, dataclass
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil

import numpy as np

if __package__:
    from .maxwell import MaxwellMatrix
    from .palace_matrix_gates import matrix_gate_failures as _matrix_gate_failures
    from .palace_matrix_access import (
        PalaceMatrixAccess, checkpoint_matrix_access as checkpoint_matrix_access,
        read_attested_matrix as _read_attested_matrix)
    from .palace_completion import (
        completed_observation_validator as _completed_observation_validator,
        validate_completion_metadata as _validate_completion_metadata_impl,
    )
    from .palace_ledger_v2 import (
        CanonicalLedgerPublicationV2, abort_campaign_attempt_on_error,
    )
    from .palace_runtime import (
        native_campaign_digest as _native_campaign_digest,
        quarantine_or_purge_matrix_files as _quarantine_or_purge_matrix_files,
        validate_execution_runtime_binding as _validate_execution_runtime_binding,
        validate_runtime_build_identity as _validate_runtime_build_identity,
        validate_runtime_progress_accounting as _validate_runtime_progress_accounting,
    )
    from .palace_build import palace_build_identity, validate_palace_build_manifest
    from .palace_mesh import BoxBounds, validate_palace_mesh_manifest
    from .palace_resources import (
        build_palace_resource_decision,
        validate_palace_resource_decision,
        validate_palace_workload,
    )
    from .palace_workflow import (
        LINEAR_SOLVER_TYPES as _LINEAR_SOLVER_TYPES,
        binary_paths as _binary_paths,
        execution_workload_inputs as _execution_workload_inputs,
        implementation_identity,
        palace_config_payload as _palace_config_payload,
        palace_progress_events as _palace_progress_events,
        prepare_execution_snapshot, projection_from_completed_runs,
        publish_snapshot_output, trusted_resource_policy,
        validate_bound_resource_decision,
        validate_completed_palace_progress as _validate_completed_palace_progress,
        validate_execution_snapshot,
        workload_record as _workload_record,
    )
    from .process_monitor import (
        ProcessLimits,
        run_monitored_process,
        validate_process_event_witness,
    )
    from .provenance import (
        bytes_sha256, canonical_equal, canonical_sha256,
        exclusive_publish_bytes, exclusive_publish_json, file_sha256, strict_json_file,
    )
else:
    from maxwell import MaxwellMatrix
    from palace_matrix_gates import matrix_gate_failures as _matrix_gate_failures
    from palace_matrix_access import (
        PalaceMatrixAccess, checkpoint_matrix_access as checkpoint_matrix_access,
        read_attested_matrix as _read_attested_matrix)
    from palace_completion import (
        completed_observation_validator as _completed_observation_validator,
        validate_completion_metadata as _validate_completion_metadata_impl,
    )
    from palace_ledger_v2 import (
        CanonicalLedgerPublicationV2, abort_campaign_attempt_on_error,
    )
    from palace_runtime import (
        native_campaign_digest as _native_campaign_digest,
        quarantine_or_purge_matrix_files as _quarantine_or_purge_matrix_files,
        validate_execution_runtime_binding as _validate_execution_runtime_binding,
        validate_runtime_build_identity as _validate_runtime_build_identity,
        validate_runtime_progress_accounting as _validate_runtime_progress_accounting,
    )
    from palace_build import palace_build_identity, validate_palace_build_manifest
    from palace_mesh import BoxBounds, validate_palace_mesh_manifest
    from palace_resources import (
        build_palace_resource_decision,
        validate_palace_resource_decision,
        validate_palace_workload,
    )
    from palace_workflow import (
        LINEAR_SOLVER_TYPES as _LINEAR_SOLVER_TYPES,
        binary_paths as _binary_paths,
        execution_workload_inputs as _execution_workload_inputs,
        implementation_identity,
        palace_config_payload as _palace_config_payload,
        palace_progress_events as _palace_progress_events,
        prepare_execution_snapshot, projection_from_completed_runs,
        publish_snapshot_output, trusted_resource_policy,
        validate_bound_resource_decision,
        validate_completed_palace_progress as _validate_completed_palace_progress,
        validate_execution_snapshot,
        workload_record as _workload_record,
    )
    from process_monitor import (
        ProcessLimits,
        run_monitored_process,
        validate_process_event_witness,
    )
    from provenance import (
        bytes_sha256, canonical_equal, canonical_sha256,
        exclusive_publish_bytes, exclusive_publish_json, file_sha256, strict_json_file,
    )


GATE_POLICY = "palace-electrostatic-pcb-gates-v2"
CONFIG_MANIFEST_FORMAT = "dcdc-palace-config-v3"
LEGACY_CONFIG_MANIFEST_FORMAT = "dcdc-palace-config-v2"
RUN_MANIFEST_FORMAT = "dcdc-palace-run-v3"
EXPLICIT_RESIDUAL_RE = re.compile(
    r"Explicit residual \|\|b-Ax\|\|/\|\|b\|\| = (?P<residual>[0-9.eE+-]+) \(target = (?P<target>[0-9.eE+-]+)\)")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FAILURE_RE = re.compile(
    r"(?:\bwarning\s*[!:]|\berror\s*[!:]|mfem abort|did not converge|"
    r"verification failed)", re.IGNORECASE)
NORMAL_COMPLETION_MARKERS = ("Elapsed Time Report (s)", "Peak Memory")
MESH_LIMITS = {"synthetic": {"nodes": 1_000_000, "tetrahedra": 5_000_000}, "pcb_diagnostic": {"nodes": 10_000_000, "tetrahedra": 50_000_000}}
RESOURCE_LIMITS = {
    "synthetic": ProcessLimits(300.0, 8 * 1024**3, 1024**3, 2**63 - 1, 2**31 - 1),
    "pcb_diagnostic": ProcessLimits(30 * 60.0, 24 * 1024**3, 10 * 1024**3, 2**63 - 1, 2**31 - 1),
}


def _trusted_resource_policy():
    return trusted_resource_policy(
        RESOURCE_LIMITS, MESH_LIMITS,
        validator_path=Path(__file__).with_name("palace_resources.py"),
    )


@dataclass(frozen=True)
class PalaceTerminal:
    index: int
    name: str
    attribute: int

    def __post_init__(self):
        if not isinstance(self.index, int) or isinstance(self.index, bool) or self.index <= 0:
            raise ValueError("terminal index must be a positive integer")
        if (not isinstance(self.name, str) or not self.name
                or len(self.name) > 100):
            raise ValueError("terminal name must be non-empty and at most 100 characters")
        if type(self.attribute) is not int or self.attribute <= 0:
            raise ValueError("terminal attribute must be a positive integer")


@dataclass(frozen=True)
class PalaceMaterial:
    name: str
    attributes: tuple[int, ...]
    relative_permittivity: float

    def __post_init__(self):
        attributes = tuple(self.attributes)
        if (not isinstance(self.name, str) or not self.name or not attributes
                or len(set(attributes)) != len(attributes)
                or any(type(value) is not int or value <= 0 for value in attributes)):
            raise ValueError("material name and positive unique attributes are required")
        if (isinstance(self.relative_permittivity, bool)
                or not isinstance(self.relative_permittivity, (int, float))):
            raise ValueError("material permittivity must be finite and positive")
        permittivity = float(self.relative_permittivity)
        if not np.isfinite(permittivity) or permittivity <= 0.0:
            raise ValueError("material permittivity must be finite and positive")
        object.__setattr__(self, "attributes", attributes)
        object.__setattr__(self, "relative_permittivity", permittivity)


@dataclass(frozen=True)
class PalaceConfigManifest:
    config_path: Path
    mesh_path: Path
    mesh_manifest_path: Path
    output_directory: Path
    terminals: tuple[PalaceTerminal, ...]
    materials: tuple[PalaceMaterial, ...]
    ground_attribute: int
    linear_tolerance: float
    explicit_residual_tolerance: float
    maximum_iterations: int
    order: int
    linear_solver_type: str
    multigrid_max_levels: int | None
    finite_reference: dict
    checkpoint: dict | None
    mesh_provenance: dict
    config: dict
    raw: dict

    @property
    def terminal_names(self):
        return tuple(terminal.name for terminal in self.terminals)


@dataclass(frozen=True)
class PalaceResidual:
    terminal: PalaceTerminal
    relative_residual: float
    target: float


@dataclass(frozen=True)
class PalaceRun:
    command: tuple[str, ...]
    stdout: str
    stderr: str
    raw_matrix: MaxwellMatrix
    downstream_matrix: MaxwellMatrix
    residuals: tuple[PalaceResidual, ...]
    manifest_path: Path


class PalaceRunRejected(RuntimeError):
    def __init__(self, message, *, failures, manifest_path):
        super().__init__(message)
        self.failures = tuple(failures)
        self.manifest_path = Path(manifest_path)


def write_palace_config(path, *, mesh_path, mesh_manifest_path, output_directory,
                        terminals, materials, ground_attribute, finite_reference, order=1,
                        linear_tolerance=1e-10, explicit_residual_tolerance=None,
                        maximum_iterations=500, checkpoint=None,
                        linear_solver_type="BoomerAMG", multigrid_max_levels=None):
    path = Path(path).resolve()
    mesh_path = Path(mesh_path).resolve()
    mesh_manifest_path = Path(mesh_manifest_path).resolve()
    output_directory = Path(output_directory).resolve()
    terminals = tuple(terminals)
    materials = tuple(materials)
    finite_reference = json.loads(json.dumps(dict(finite_reference), allow_nan=False))
    if checkpoint is not None:
        checkpoint = dict(checkpoint)
        native_identity = checkpoint.get("native_campaign_identity")
        checkpoint_path = Path(checkpoint.get("path", ""))
        if (set(checkpoint) != {"path", "native_campaign_identity"}
                or not checkpoint_path.is_absolute()
                or checkpoint_path.is_symlink()
                or not isinstance(native_identity, str) or len(native_identity) != 64
                or any(character not in "0123456789abcdef"
                       for character in native_identity)):
            raise ValueError("Palace checkpoint identity is invalid")
        checkpoint = {
            "path": str(checkpoint_path.resolve()),
            "native_campaign_identity": native_identity,
        }
    if set(finite_reference) != {
            "kind", "convergence_ladder_required", "not_a_circuit_node",
            "outer_bounds_m", "outer_scale"}:
        raise ValueError("finite electrostatic reference schema mismatch")
    try:
        BoxBounds(**finite_reference["outer_bounds_m"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"finite electrostatic reference bounds are invalid: {error}") from error
    outer_scale = finite_reference["outer_scale"]
    if (finite_reference["kind"] != "finite_outer_dirichlet_approximation"
            or finite_reference["convergence_ladder_required"] is not True
            or finite_reference["not_a_circuit_node"] is not True
            or isinstance(outer_scale, bool)
            or not isinstance(outer_scale, (int, float))
            or not np.isfinite(outer_scale) or outer_scale <= 1.0):
        raise ValueError("finite electrostatic reference is not qualification-safe")
    if not mesh_path.is_file():
        raise ValueError("Palace mesh does not exist")
    mesh_manifest = validate_palace_mesh_manifest(
        mesh_manifest_path, mesh_path=mesh_path
    )
    mesh_provenance = mesh_manifest["provenance"]
    if not terminals or tuple(item.index for item in terminals) != tuple(
            range(1, len(terminals) + 1)):
        raise ValueError("terminal indices must be contiguous from one")
    if len({item.name for item in terminals}) != len(terminals):
        raise ValueError("terminal names must be unique")
    if len({item.attribute for item in terminals}) != len(terminals):
        raise ValueError("terminal attributes must be unique")
    if not materials:
        raise ValueError("at least one Palace material is required")
    material_attributes = [value for item in materials for value in item.attributes]
    if len(set(material_attributes)) != len(material_attributes):
        raise ValueError("material attributes must be globally unique")
    if type(ground_attribute) is not int or ground_attribute <= 0:
        raise ValueError("ground attribute must be a positive integer")
    if ground_attribute in {item.attribute for item in terminals}:
        raise ValueError("ground and terminal attributes must be distinct")
    if tuple(tuple(item) for item in mesh_provenance.get("terminal_attributes", ())) != tuple(
            (item.name, item.attribute) for item in terminals):
        raise ValueError("Palace terminals do not match the mesh manifest")
    if tuple(tuple(item) for item in mesh_provenance.get("material_attributes", ())) != tuple(
            (item.name, item.attributes[0], item.relative_permittivity)
            for item in materials
    ) or any(len(item.attributes) != 1 for item in materials):
        raise ValueError("Palace materials do not match the mesh manifest")
    if mesh_provenance.get("ground_attribute") != ground_attribute:
        raise ValueError("Palace ground does not match the mesh manifest")
    if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
        raise ValueError("finite-element order must be a positive integer")
    if (isinstance(linear_tolerance, bool)
            or not isinstance(linear_tolerance, (int, float))):
        raise ValueError("linear tolerance must lie between zero and one")
    linear_tolerance = float(linear_tolerance)
    if not np.isfinite(linear_tolerance) or not 0.0 < linear_tolerance < 1.0:
        raise ValueError("linear tolerance must lie between zero and one")
    if explicit_residual_tolerance is None:
        explicit_residual_tolerance = linear_tolerance
    if (isinstance(explicit_residual_tolerance, bool)
            or not isinstance(explicit_residual_tolerance, (int, float))):
        raise ValueError("explicit residual tolerance must lie between zero and one")
    explicit_residual_tolerance = float(explicit_residual_tolerance)
    if (not np.isfinite(explicit_residual_tolerance)
            or not 0.0 < explicit_residual_tolerance < 1.0
            or linear_tolerance > explicit_residual_tolerance):
        raise ValueError(
            "explicit residual tolerance must be no stricter than the solver tolerance"
        )
    if (not isinstance(maximum_iterations, int) or isinstance(maximum_iterations, bool)
            or maximum_iterations <= 0):
        raise ValueError("maximum iterations must be a positive integer")
    if (type(linear_solver_type) is not str
            or linear_solver_type not in _LINEAR_SOLVER_TYPES):
        raise ValueError("unsupported Palace linear solver type")
    if not (multigrid_max_levels is None
            or (type(multigrid_max_levels) is int and multigrid_max_levels == 1)):
        raise ValueError("multigrid max levels supports only None or 1")
    if (finite_reference.get("kind") != "finite_outer_dirichlet_approximation"
            or not finite_reference.get("not_a_circuit_node")):
        raise ValueError("finite electrostatic reference must be declared explicitly")
    if not finite_reference.get("convergence_ladder_required"):
        raise ValueError("finite electrostatic reference requires a domain-size ladder")
    if finite_reference.get("outer_bounds_m") != mesh_provenance.get("outer_bounds"):
        raise ValueError("finite electrostatic reference does not match mesh bounds")
    if mesh_provenance.get("reference_semantics") != {
            "kind": "finite_outer_dirichlet_approximation",
            "convergence_ladder_required": True,
            "not_a_circuit_node": True,
    }:
        raise ValueError("mesh reference semantics are not qualification-safe")

    try:
        relative_mesh = mesh_path.relative_to(path.parent)
        relative_output = output_directory.relative_to(path.parent)
    except ValueError as error:
        raise ValueError("mesh and output must remain inside the Palace run directory") from error
    if relative_mesh.parts[0] == ".." or relative_output.parts[0] == "..":
        raise ValueError("Palace paths cannot escape the run directory")

    config = _palace_config_payload(
        relative_mesh=relative_mesh,
        relative_output=relative_output,
        terminals=terminals,
        materials=materials,
        ground_attribute=ground_attribute,
        order=order,
        linear_tolerance=linear_tolerance,
        explicit_residual_tolerance=explicit_residual_tolerance,
        maximum_iterations=maximum_iterations,
        checkpoint=checkpoint,
        linear_solver_type=linear_solver_type,
        multigrid_max_levels=multigrid_max_levels,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    provenance = {
        "gate_policy": GATE_POLICY,
        "config_sha256": file_sha256(path),
        "mesh_sha256": file_sha256(mesh_path),
        "mesh_manifest_sha256": file_sha256(mesh_manifest_path),
        "terminals": [asdict(item) for item in terminals],
        "materials": [asdict(item) for item in materials],
        "ground_attribute": ground_attribute,
        "linear_tolerance": linear_tolerance,
        "explicit_residual_tolerance": explicit_residual_tolerance,
        "maximum_iterations": maximum_iterations,
        "order": order,
        "linear_solver_type": linear_solver_type,
        "multigrid_max_levels": multigrid_max_levels,
        "finite_reference": finite_reference,
        "checkpoint": checkpoint,
    }
    manifest = {
        "format": CONFIG_MANIFEST_FORMAT,
        "provenance": provenance,
        "provenance_sha256": canonical_sha256(provenance),
    }
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path


def load_palace_config_manifest(path):
    path = Path(path).resolve()
    raw = strict_json_file(path, label="Palace config manifest")
    if (not isinstance(raw, dict) or raw.get("format") not in {
            CONFIG_MANIFEST_FORMAT, LEGACY_CONFIG_MANIFEST_FORMAT}):
        raise ValueError("unsupported Palace config manifest format")
    provenance = raw.get("provenance")
    if (not isinstance(provenance, dict)
            or raw.get("provenance_sha256") != canonical_sha256(provenance)):
        raise ValueError("Palace config provenance hash mismatch")
    expected_provenance = {
        "config_sha256", "explicit_residual_tolerance", "finite_reference",
        "gate_policy", "ground_attribute", "linear_tolerance", "materials",
        "maximum_iterations", "mesh_manifest_sha256", "mesh_sha256", "order",
        "terminals",
    }
    if raw["format"] == CONFIG_MANIFEST_FORMAT:
        expected_provenance.add("checkpoint")
    if "linear_solver_type" in provenance:
        expected_provenance.add("linear_solver_type")
    if "multigrid_max_levels" in provenance:
        expected_provenance.add("multigrid_max_levels")
    if set(provenance) != expected_provenance:
        raise ValueError("Palace config provenance schema mismatch")
    linear_solver_type = provenance.get("linear_solver_type", "BoomerAMG")
    if (type(linear_solver_type) is not str
            or linear_solver_type not in _LINEAR_SOLVER_TYPES):
        raise ValueError("unsupported Palace linear solver type")
    multigrid_max_levels = provenance.get("multigrid_max_levels")
    if not (multigrid_max_levels is None
            or (type(multigrid_max_levels) is int and multigrid_max_levels == 1)):
        raise ValueError("multigrid max levels supports only None or 1")
    if provenance["gate_policy"] != GATE_POLICY:
        raise ValueError("Palace config gate policy mismatch")
    tolerance = provenance["linear_tolerance"]
    explicit_tolerance = provenance["explicit_residual_tolerance"]
    if (type(provenance["ground_attribute"]) is not int
            or provenance["ground_attribute"] <= 0
            or type(provenance["order"]) is not int or provenance["order"] <= 0
            or type(provenance["maximum_iterations"]) is not int
            or provenance["maximum_iterations"] <= 0
            or isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or not np.isfinite(tolerance) or not 0.0 < tolerance < 1.0
            or isinstance(explicit_tolerance, bool)
            or not isinstance(explicit_tolerance, (int, float))
            or not np.isfinite(explicit_tolerance)
            or not 0.0 < explicit_tolerance < 1.0
            or tolerance > explicit_tolerance):
        raise ValueError("Palace config numeric controls are invalid")
    config_path = Path(str(path).removesuffix(".manifest.json"))
    config = strict_json_file(config_path, label="Palace config")
    if file_sha256(config_path) != provenance.get("config_sha256"):
        raise ValueError("Palace config hash mismatch")
    mesh_path = (config_path.parent / config["Model"]["Mesh"]).resolve()
    mesh_manifest_path = mesh_path.with_suffix(mesh_path.suffix + ".manifest.json")
    output_directory = (config_path.parent / config["Problem"]["Output"]).resolve()
    if file_sha256(mesh_path) != provenance.get("mesh_sha256"):
        raise ValueError("Palace mesh hash mismatch")
    mesh_manifest = validate_palace_mesh_manifest(mesh_manifest_path, mesh_path=mesh_path)
    if file_sha256(mesh_manifest_path) != provenance.get("mesh_manifest_sha256"):
        raise ValueError("Palace mesh manifest hash mismatch")
    terminals = tuple(PalaceTerminal(**item) for item in provenance["terminals"])
    materials = tuple(
        PalaceMaterial(
            item["name"], tuple(item["attributes"]), item["relative_permittivity"]
        )
        for item in provenance["materials"]
    )
    mesh_provenance = dict(mesh_manifest["provenance"])
    try:
        relative_mesh = mesh_path.relative_to(config_path.parent)
        relative_output = output_directory.relative_to(config_path.parent)
    except ValueError as error:
        raise ValueError("Palace config paths escape the run directory") from error
    checkpoint = provenance.get("checkpoint")
    if checkpoint is not None:
        checkpoint_path = Path(checkpoint.get("path", ""))
        native_identity = checkpoint.get("native_campaign_identity")
        if (not isinstance(checkpoint, dict)
                or set(checkpoint) != {"path", "native_campaign_identity"}
                or not checkpoint_path.is_absolute() or checkpoint_path.is_symlink()
                or not isinstance(native_identity, str) or len(native_identity) != 64
                or any(character not in "0123456789abcdef"
                       for character in native_identity)):
            raise ValueError("Palace checkpoint identity is invalid")
    expected_config = _palace_config_payload(
        relative_mesh=relative_mesh,
        relative_output=relative_output,
        terminals=terminals,
        materials=materials,
        ground_attribute=provenance["ground_attribute"],
        order=provenance["order"],
        linear_tolerance=provenance["linear_tolerance"],
        explicit_residual_tolerance=provenance["explicit_residual_tolerance"],
        maximum_iterations=provenance["maximum_iterations"],
        checkpoint=checkpoint,
        linear_solver_type=linear_solver_type,
        multigrid_max_levels=multigrid_max_levels,
    )
    if not canonical_equal(config, expected_config):
        raise ValueError("Palace config semantics do not match its manifest")
    if tuple(tuple(item) for item in mesh_provenance.get("terminal_attributes", ())) != tuple(
            (item.name, item.attribute) for item in terminals):
        raise ValueError("Palace config terminals do not match its mesh")
    if tuple(tuple(item) for item in mesh_provenance.get("material_attributes", ())) != tuple(
            (item.name, item.attributes[0], item.relative_permittivity)
            for item in materials
    ) or any(len(item.attributes) != 1 for item in materials):
        raise ValueError("Palace config materials do not match its mesh")
    if mesh_provenance.get("ground_attribute") != provenance["ground_attribute"]:
        raise ValueError("Palace config ground does not match its mesh")
    finite_reference = provenance["finite_reference"]
    if (not isinstance(finite_reference, dict) or set(finite_reference) != {
            "kind", "convergence_ladder_required", "not_a_circuit_node",
            "outer_bounds_m", "outer_scale"}):
        raise ValueError("Palace finite reference schema mismatch")
    try:
        BoxBounds(**finite_reference["outer_bounds_m"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Palace finite reference bounds are invalid: {error}") from error
    outer_scale = finite_reference["outer_scale"]
    if (finite_reference["kind"] != "finite_outer_dirichlet_approximation"
            or finite_reference["not_a_circuit_node"] is not True
            or finite_reference["convergence_ladder_required"] is not True
            or finite_reference["outer_bounds_m"] != mesh_provenance.get("outer_bounds")
            or isinstance(outer_scale, bool)
            or not isinstance(outer_scale, (int, float))
            or not np.isfinite(outer_scale) or outer_scale <= 1.0):
        raise ValueError("Palace finite reference does not match its mesh")
    finite_reference = dict(finite_reference)
    return PalaceConfigManifest(
        config_path=config_path,
        mesh_path=mesh_path,
        mesh_manifest_path=mesh_manifest_path,
        output_directory=output_directory,
        terminals=terminals,
        materials=materials,
        ground_attribute=provenance["ground_attribute"],
        linear_tolerance=provenance["linear_tolerance"],
        explicit_residual_tolerance=provenance["explicit_residual_tolerance"],
        maximum_iterations=provenance["maximum_iterations"],
        order=provenance["order"],
        linear_solver_type=linear_solver_type,
        multigrid_max_levels=multigrid_max_levels,
        finite_reference=finite_reference,
        checkpoint=checkpoint,
        mesh_provenance=mesh_provenance,
        config=config,
        raw=raw,
    )


def _parse_palace_matrix_content(content, manifest, *, matrix_name):
    if type(content) is not bytes:
        raise ValueError(f"invalid Palace {matrix_name} CSV content")
    expected_indices = tuple(item.index for item in manifest.terminals)
    try:
        rows = list(csv.reader(io.StringIO(content.decode("utf-8"), newline="")))
    except (UnicodeError, csv.Error) as error:
        raise ValueError(f"invalid Palace {matrix_name} CSV: {error}") from error
    if len(rows) != len(expected_indices) + 1:
        raise ValueError(f"Palace {matrix_name} matrix row count mismatch")
    matrix_labels = {"raw": "C_raw", "standard": "C"}
    if matrix_name not in matrix_labels:
        raise ValueError("unknown Palace matrix identity")
    expected_header = (
        "i",
        *(f"{matrix_labels[matrix_name]}[i][{index}] (F)" for index in expected_indices),
    )
    header = tuple(value.strip() for value in rows[0])
    if header != expected_header:
        raise ValueError(f"Palace {matrix_name} matrix identity or units mismatch")
    values = np.empty((len(expected_indices), len(expected_indices)), dtype=float)
    for row_index, row in enumerate(rows[1:]):
        if len(row) != len(expected_indices) + 1:
            raise ValueError(f"Palace {matrix_name} matrix column count mismatch")
        try:
            index_value = float(row[0])
            values[row_index] = [float(value) for value in row[1:]]
        except ValueError as error:
            raise ValueError(f"Palace {matrix_name} matrix contains invalid numbers") from error
        if index_value != expected_indices[row_index]:
            raise ValueError(f"Palace {matrix_name} terminal rows are reordered")
    if not np.isfinite(values).all():
        raise ValueError(f"Palace {matrix_name} matrix contains non-finite values")
    return MaxwellMatrix(manifest.terminal_names, values)


def _parse_palace_matrix_csv(path, manifest, *, matrix_name):
    path = Path(path)
    if path.is_symlink() or ".quarantine" in path.parts:
        raise ValueError(f"Palace {matrix_name} matrix is quarantined or unsafe")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError(f"invalid Palace {matrix_name} CSV: {error}") from error
    return _parse_palace_matrix_content(
        content, manifest, matrix_name=matrix_name,
    )


def parse_palace_matrix_csv(path, manifest, *, matrix_name, matrix_access):
    if type(matrix_access) is not PalaceMatrixAccess:
        raise ValueError("Palace matrix access requires the bound attested campaign")
    content = _read_attested_matrix(
        path, manifest, matrix_name=matrix_name, matrix_access=matrix_access,
    )
    return _parse_palace_matrix_content(
        content, manifest, matrix_name=matrix_name,
    )


def _parse_residuals(stdout, manifest):
    matches = tuple(EXPLICIT_RESIDUAL_RE.finditer(stdout))
    if len(matches) != len(manifest.terminals):
        raise ValueError("explicit-residual count does not match terminal count")
    residuals = []
    for terminal, match in zip(manifest.terminals, matches):
        residual = float(match.group("residual"))
        target = float(match.group("target"))
        if (not np.isfinite((residual, target)).all()
                or residual < 0.0 or target <= 0.0
                or target != manifest.explicit_residual_tolerance
                or residual > target):
            raise ValueError(f"terminal {terminal.name} failed the explicit residual gate")
        residuals.append(PalaceResidual(terminal, residual, target))
    return tuple(residuals)


def _load_json_artifact(path, *, label):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Palace {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"invalid Palace {label}: expected an object")
    return value


def _expected_resolved_config(manifest):
    return {
        "Boundaries": manifest.config["Boundaries"],
        "Domains": {
            "Materials": [
                {
                    "Attributes": list(material.attributes),
                    "Conductivity": 0.0,
                    "LondonDepth": 0.0,
                    "LossTan": 0.0,
                    "Permeability": 1.0,
                    "Permittivity": material.relative_permittivity,
                }
                for material in manifest.materials
            ],
        },
        "Model": {
            "AddInterfaceBoundaryElements": True,
            "CleanUnusedElements": True,
            "CrackDisplacementFactor": 1e-12,
            "CrackInternalBoundaryElements": True,
            "ExportPrerefinedMesh": False,
            "L0": 1.0,
            "MakeHexahedral": False,
            "MakeSimplex": False,
            "Mesh": manifest.config["Model"]["Mesh"],
            "RefineCrackElements": True,
            "Refinement": {
                "MaxIts": 0,
                "MaxNCLevels": 1,
                "MaxSize": 0,
                "MaximumImbalance": 1.1,
                "Nonconformal": True,
                "SaveAdaptIterations": True,
                "SaveAdaptMesh": False,
                "SerialUniformLevels": 0,
                "Tol": 0.01,
                "UniformLevels": 0,
                "UpdateFraction": 0.7,
            },
            "RemoveCurvature": False,
            "ReorderElements": False,
            "ReorientTetMesh": False,
        },
        "Problem": {
            "Output": manifest.config["Problem"]["Output"],
            "OutputFormats": {"GridFunction": False, "Paraview": True},
            "Type": "Electrostatic",
            "Verbose": 2,
        },
        "Solver": {
            "Device": "CPU",
            "Electrostatic": {
                "Save": 0,
                **({"Checkpoint": {
                    "Path": manifest.checkpoint["path"],
                    "CampaignIdentity": manifest.checkpoint["native_campaign_identity"],
                }} if manifest.checkpoint else {}),
            },
            "Linear": {
                "AMGAggressiveCoarsening": True,
                "AMSMaxIts": manifest.order,
                "AMSSingularOperator": False,
                "AMSVectorInterpolation": False,
                "ColumnOrdering": "Default",
                "ComplexCoarseSolve": False,
                "DivFreeMaxIts": 1000,
                "DivFreeTol": 1e-12,
                "DropSmallEntries": False,
                "EstimatorMG": False,
                "EstimatorMaxIts": 10000,
                "EstimatorTol": 1e-6,
                "GSOrthogonalization": "MGS",
                "InitialGuess": True,
                "KSPType": "CG",
                "MGAuxiliarySmoother": False,
                "MGCoarsenType": "Logarithmic",
                "MGCycleIts": 1,
                "MGMaxLevels": (
                    100 if manifest.multigrid_max_levels is None
                    else manifest.multigrid_max_levels),
                "MGSmoothChebyshev4th": True,
                "MGSmoothEigScaleMax": 1.0,
                "MGSmoothEigScaleMin": 0.0,
                "MGSmoothIts": 1,
                "MGSmoothOrder": max(4, 2 * manifest.order),
                "MGUseMesh": True,
                "MaxIts": manifest.maximum_iterations,
                "MaxSize": manifest.maximum_iterations,
                "PCMatReal": False,
                "PCMatShifted": False,
                "PCSide": "Default",
                "ReorderingReuse": True,
                "STRUMPACKButterflyLevels": 1,
                "STRUMPACKCompressionTol": 0.001,
                "STRUMPACKCompressionType": "None",
                "STRUMPACKLossyPrecision": 16,
                "SuperLU3DCommunicator": False,
                "Tol": manifest.linear_tolerance,
                "Type": manifest.linear_solver_type,
                "VerificationTol": manifest.explicit_residual_tolerance,
            },
            "Order": manifest.order,
            "PartialAssemblyOrder": 1,
            "QuadratureOrderExtra": 0,
            "QuadratureOrderJacobian": False,
        },
    }


def _validate_completion_metadata(output_directory, manifest, *, processes):
    return _validate_completion_metadata_impl(
        output_directory, manifest, processes=processes,
        load_json_artifact=_load_json_artifact,
        expected_resolved_config=_expected_resolved_config,
    )


def _implementation_identity():
    return implementation_identity(Path(__file__))


def write_palace_resource_decision(
        config_manifest_path, *, executable, build_manifest_path,
        completed_run_manifest_paths, completed_matrix_accesses=None, processes=1):
    manifest = load_palace_config_manifest(config_manifest_path)
    inputs = _execution_workload_inputs(
        manifest,
        config_manifest_path=config_manifest_path,
        build_manifest_path=build_manifest_path,
        executable=executable,
        processes=processes,
        palace_file=__file__,
        validate_build=validate_palace_build_manifest,
    )
    policy = _trusted_resource_policy()
    completed_run_manifest_paths, validate_observation = (
        _completed_observation_validator(
            completed_run_manifest_paths, completed_matrix_accesses,
            validate_run=validate_palace_run_manifest,
        )
    )
    projection = projection_from_completed_runs(
        inputs["workload"], completed_run_manifest_paths,
        validate_run=validate_observation,
        validate_workload=validate_palace_workload,
    )
    decision = build_palace_resource_decision(
        palace_workload=inputs["workload"],
        projection=projection,
        profiles=policy["profile_objects"],
        authorized_profile_ids=policy["authorized_profile_tuple"],
        policy_id=policy["policy_id"],
        policy_sha256=policy["policy_sha256"],
        validator_sha256=policy["validator_sha256"],
        minimum_headroom_ratio=policy["minimum_headroom_ratio"],
    )
    path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.resource-decision."
        f"{decision['content_sha256']}.json"
    )
    exclusive_publish_json(path, decision)
    return path


def _validate_palace_run_manifest_unattested(path, *, _document=None):
    path = Path(path).resolve()
    if _document is None:
        _document = (strict_json_file(path, label="Palace run manifest"), file_sha256(path))
    raw, manifest_sha256 = _document
    if not isinstance(raw, dict) or raw.get("format") != RUN_MANIFEST_FORMAT:
        raise ValueError("unsupported Palace run manifest format")
    identity = dict(raw)
    identity.pop("format")
    content_sha256 = identity.pop("content_sha256", None)
    if content_sha256 != canonical_sha256(identity):
        raise ValueError("Palace run manifest content ID mismatch")
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
        raise ValueError("Palace run manifest schema mismatch")
    if identity.get("gate_policy") != GATE_POLICY:
        raise ValueError("Palace run gate policy mismatch")
    manifest_path = Path(identity["config_manifest"]).resolve()
    manifest = load_palace_config_manifest(manifest_path)
    if file_sha256(manifest_path) != identity.get("config_manifest_sha256"):
        raise ValueError("Palace run config-manifest hash mismatch")
    resource_class = identity.get("resource_class")
    if resource_class not in RESOURCE_LIMITS:
        raise ValueError("Palace run resource class is invalid")
    mesh_limits = MESH_LIMITS[resource_class]
    expected_resource_limits = {
        **asdict(RESOURCE_LIMITS[resource_class]),
        **mesh_limits,
    }
    if not canonical_equal(identity.get("resource_limits"), expected_resource_limits):
        raise ValueError("Palace run resource limits mismatch")
    if (manifest.mesh_provenance["node_count"] > mesh_limits["nodes"]
            or manifest.mesh_provenance["tetrahedron_count"]
            > mesh_limits["tetrahedra"]):
        raise ValueError("Palace run mesh exceeds resource limits")
    snapshot = identity.get("execution_snapshot")
    snapshot_inputs = snapshot.get("inputs") if isinstance(snapshot, dict) else None
    executable_inputs = [
        item for item in snapshot_inputs or ()
        if isinstance(item, dict) and item.get("role") == "executable"
    ]
    command = identity.get("command")
    if (len(executable_inputs) != 1
            or not isinstance(command, list) or len(command) != 4
            or any(not isinstance(value, str) for value in command)
            or command[0] != executable_inputs[0].get("snapshot")
            or command[1] != "-np" or command[3] != manifest.config_path.name):
        raise ValueError("Palace run command is invalid")
    try:
        processes = int(command[2])
    except (TypeError, ValueError) as error:
        raise ValueError("Palace run process count is invalid") from error
    if processes <= 0 or str(processes) != command[2]:
        raise ValueError("Palace run process count is invalid")
    executable = Path(executable_inputs[0].get("source", "")).resolve()
    if not executable.is_file():
        raise ValueError("Palace run executable is missing")
    build_manifest_path = Path(identity["build_manifest"]).resolve()
    build = validate_palace_build_manifest(
        build_manifest_path, executable=executable
    )
    if (file_sha256(build_manifest_path) != identity.get("build_manifest_sha256")
            or palace_build_identity(build)
            != identity.get("build_provenance_sha256")):
        raise ValueError("Palace run build identity mismatch")
    expected_binaries = {
        str(binary): file_sha256(binary) for binary in _binary_paths(executable)
    }
    if identity.get("binaries") != expected_binaries:
        raise ValueError("Palace run binary identity mismatch")
    implementation = _implementation_identity()
    if identity.get("implementation_files") != implementation:
        raise ValueError("Palace run implementation identity mismatch")
    mpi_launcher = shutil.which("mpirun")
    expected_launcher = str(Path(mpi_launcher).resolve()) if mpi_launcher else None
    expected_launcher_hash = file_sha256(mpi_launcher) if mpi_launcher else None
    if (identity.get("mpi_launcher") != expected_launcher
            or identity.get("mpi_launcher_sha256") != expected_launcher_hash):
        raise ValueError("Palace run launcher identity mismatch")
    workload_path = Path(identity.get("workload", "")).resolve()
    if (not workload_path.is_file()
            or identity.get("workload_sha256") != file_sha256(workload_path)):
        raise ValueError("Palace run workload artifact identity mismatch")
    try:
        workload = validate_palace_workload(strict_json_file(
            workload_path, label="Palace workload"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Palace run workload is invalid: {error}") from error
    expected_workload = _workload_record(
        manifest,
        config_manifest_path=manifest_path,
        build_manifest_path=build_manifest_path,
        executable=executable,
        processes=processes,
        implementation=implementation,
        binaries=expected_binaries,
        mpi_launcher=expected_launcher,
    )
    if workload != expected_workload:
        raise ValueError("Palace run workload does not match execution inputs")
    validate_execution_snapshot(
        snapshot,
        manifest,
        workload,
        executable=executable,
        binaries=tuple(expected_binaries),
        mpi_launcher=expected_launcher,
    )
    expected_workload_name = (
        f"{manifest.config_path.name}.workload.{workload['content_sha256']}.json"
    )
    if (workload_path.parent != manifest.config_path.parent
            or workload_path.name != expected_workload_name):
        raise ValueError("Palace run workload path is not content addressed")
    decision_value = identity.get("resource_decision")
    decision_sha256 = identity.get("resource_decision_sha256")
    if (decision_value is None) != (decision_sha256 is None):
        raise ValueError("Palace run resource decision identity is partial")
    if manifest.checkpoint is not None and decision_value is None:
        raise ValueError("checkpointed Palace run lacks a resource decision")
    decision_path = None
    if decision_value is not None:
        decision_path = Path(decision_value).resolve()
        if (not decision_path.is_file()
                or decision_sha256 != file_sha256(decision_path)):
            raise ValueError("Palace run resource decision artifact mismatch")
        try:
            raw_decision = strict_json_file(decision_path, label="Palace resource decision")
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid Palace run resource decision: {error}") from error
        policy = _trusted_resource_policy()
        validate_bound_resource_decision(
            raw_decision,
            run_path=path,
            expected_workload=workload,
            policy=policy,
            resource_class=resource_class,
            decision_path=decision_path,
            config_path=manifest.config_path,
        )
    if identity.get("resource_enforcement") != "portable_process_limits":
        raise ValueError("Palace run resource-enforcement identity mismatch")
    if (identity.get("lifecycle") != "numerically_converged_diagnostic"
            or identity.get("failures") != []):
        raise ValueError("Palace run lifecycle is not accepted")
    execution = identity.get("execution")
    expected_execution_limits = asdict(RESOURCE_LIMITS[resource_class])
    expected_execution_keys = {
        "command", "cwd", "returncode", "elapsed_s", "peak_rss_bytes",
        "output_bytes", "directory_growth_bytes", "max_refined_panels",
        "max_gmres_iteration", "limit_failures", "limits",
        "stream_events", "monitor_events", "resource_samples",
        "palace_progress_events",
    }
    if (not isinstance(execution, dict) or set(execution) != expected_execution_keys
            or execution.get("command") != command
            or execution.get("cwd") != snapshot["root"]
            or type(execution.get("returncode")) is not int
            or execution.get("returncode") != 0
            or execution.get("limit_failures") != []
            or not canonical_equal(execution.get("limits"), expected_execution_limits)):
        raise ValueError("Palace run execution witness is invalid")
    bounded_values = (
        ("elapsed_s", float, RESOURCE_LIMITS[resource_class].wall_time_s, False),
        ("peak_rss_bytes", int, RESOURCE_LIMITS[resource_class].peak_rss_bytes, True),
        ("output_bytes", int, RESOURCE_LIMITS[resource_class].output_bytes, True),
        ("directory_growth_bytes", int,
         RESOURCE_LIMITS[resource_class].output_bytes, True),
        ("max_refined_panels", int,
         RESOURCE_LIMITS[resource_class].refined_panels, True),
        ("max_gmres_iteration", int,
         RESOURCE_LIMITS[resource_class].gmres_iterations_per_rhs, True),
    )
    for name, expected_type, maximum, allow_zero in bounded_values:
        value = execution.get(name)
        minimum = 0 if allow_zero else 0.0
        if (type(value) is not expected_type or not np.isfinite(value)
                or value < minimum or (not allow_zero and value == minimum)
                or value > maximum):
            raise ValueError(f"Palace run execution {name} is invalid")

    stdout_sha256 = identity.get("stdout_sha256")
    stderr_sha256 = identity.get("stderr_sha256")
    stdout_path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.stdout.{stdout_sha256}.bin"
    )
    stderr_path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.stderr.{stderr_sha256}.bin"
    )
    output_directory = (
        Path(snapshot["root"]) / manifest.output_directory.name
        if manifest.checkpoint is not None else manifest.output_directory
    )
    raw_path = output_directory / "terminal-Craw.csv"
    standard_path = output_directory / "terminal-C.csv"
    palace_path = output_directory / "palace.json"
    resolved_path = output_directory / "config_resolved.json"
    required_artifacts = [
        manifest.config_path, manifest.mesh_path, manifest.mesh_manifest_path,
        workload_path, raw_path, standard_path, palace_path, resolved_path,
        stdout_path, stderr_path,
    ]
    if decision_path is not None:
        required_artifacts.append(decision_path)
    required_artifacts.extend(
        Path(item["snapshot"]) for item in snapshot["inputs"]
    )
    expected_artifacts = {
        str(artifact.resolve()): file_sha256(artifact)
        for artifact in required_artifacts
    }
    if not canonical_equal(identity.get("artifacts"), expected_artifacts):
        raise ValueError("Palace run artifact map mismatch")
    stdout_bytes = stdout_path.read_bytes()
    stderr_bytes = stderr_path.read_bytes()
    if (bytes_sha256(stdout_bytes) != stdout_sha256
            or bytes_sha256(stderr_bytes) != stderr_sha256
            or execution["output_bytes"] != len(stdout_bytes) + len(stderr_bytes)):
        raise ValueError("Palace run stream identity mismatch")
    validate_process_event_witness(execution, stdout_bytes, stderr_bytes)
    expected_progress = _palace_progress_events(
        stdout_bytes, execution["stream_events"]
    )
    if not canonical_equal(
            execution.get("palace_progress_events"), expected_progress):
        raise ValueError("Palace run progress-event witness mismatch")
    progress_summary = _validate_completed_palace_progress(
        expected_progress, len(manifest.terminals),
        checkpoint_enabled=manifest.checkpoint is not None,
        campaign_digest=_native_campaign_digest(manifest),
        terminal_indices=(terminal.index for terminal in manifest.terminals),
    )
    try:
        stdout = stdout_bytes.decode("utf-8", errors="strict")
        stderr = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"Palace run streams are not UTF-8: {error}") from error
    normalized_stream = ANSI_ESCAPE_RE.sub("", stdout + "\n" + stderr)
    if FAILURE_RE.search(normalized_stream):
        raise ValueError("Palace run contains a warning or failure diagnostic")
    if any(marker not in normalized_stream for marker in NORMAL_COMPLETION_MARKERS):
        raise ValueError("Palace run lacks normal-completion markers")
    residuals = _parse_residuals(stdout, manifest)
    expected_residuals = [
        {
            "terminal": asdict(item.terminal),
            "relative_residual": item.relative_residual,
            "target": item.target,
        }
        for item in residuals
    ]
    if not canonical_equal(identity.get("residuals"), expected_residuals):
        raise ValueError("Palace run residual witness mismatch")
    _, _, metadata = _validate_completion_metadata(
        output_directory, manifest, processes=processes
    )
    _validate_execution_runtime_binding(execution, metadata)
    _validate_runtime_build_identity(metadata, build)
    _validate_runtime_progress_accounting(metadata, progress_summary)
    if not canonical_equal(identity.get("runtime_metadata"), metadata):
        raise ValueError("Palace run metadata witness mismatch")
    raw_matrix = _parse_palace_matrix_csv(raw_path, manifest, matrix_name="raw")
    downstream_matrix = _parse_palace_matrix_csv(
        standard_path, manifest, matrix_name="standard"
    )
    matrix_failures = _matrix_gate_failures(raw_matrix, downstream_matrix)
    if matrix_failures:
        raise ValueError("Palace run matrix gate failed: " + "; ".join(matrix_failures))
    return {
        "manifest": manifest, "workload": workload,
        "manifest_sha256": manifest_sha256,
        "raw": raw,
        "stdout": stdout,
        "stderr": stderr,
        "residuals": residuals,
        "metadata": metadata,
        "raw_matrix": raw_matrix,
        "downstream_matrix": downstream_matrix,
    }


def validate_palace_run_manifest(path, *, matrix_access=None):
    validated = _validate_palace_run_manifest_unattested(path)
    manifest = validated["manifest"]
    if manifest.checkpoint is None:
        if matrix_access is not None:
            raise ValueError("ordinary Palace run does not accept campaign matrix access")
        return validated
    snapshot_root = Path(validated["raw"]["execution_snapshot"]["root"])
    output = snapshot_root / manifest.output_directory.name
    validated["raw_matrix"] = parse_palace_matrix_csv(
        output / "terminal-Craw.csv", manifest, matrix_name="raw",
        matrix_access=matrix_access,
    )
    validated["downstream_matrix"] = parse_palace_matrix_csv(
        output / "terminal-C.csv", manifest, matrix_name="standard",
        matrix_access=matrix_access,
    )
    return validated


@abort_campaign_attempt_on_error
def run_palace(config_manifest_path, *, executable, build_manifest_path, processes=1,
               resource_class=None, resource_decision_path=None,
               campaign_ledger=None, attempt_id=None):
    manifest = load_palace_config_manifest(config_manifest_path)
    inputs = _execution_workload_inputs(
        manifest,
        config_manifest_path=config_manifest_path,
        build_manifest_path=build_manifest_path,
        executable=executable,
        processes=processes,
        palace_file=__file__,
        validate_build=validate_palace_build_manifest,
    )
    executable = inputs["executable"]
    build_manifest_path = inputs["build_manifest_path"]
    build_manifest = inputs["build"]
    binaries = inputs["binaries"]
    implementation = inputs["implementation"]
    mpi_launcher = inputs["mpi_launcher"]
    workload = inputs["workload"]
    resource_decision = None
    policy = None
    if resource_decision_path is not None:
        resource_decision_path = Path(resource_decision_path).resolve()
        raw_decision = strict_json_file(
            resource_decision_path, label="Palace resource decision")
        policy = _trusted_resource_policy()
        resource_decision = validate_palace_resource_decision(
            raw_decision,
            expected_workload=workload,
            trusted_profiles=policy["profile_objects"],
            trusted_authorized_profile_ids=policy["authorized_profile_tuple"],
            trusted_policy_id=policy["policy_id"],
            trusted_policy_sha256=policy["policy_sha256"],
            trusted_validator_sha256=policy["validator_sha256"],
            trusted_minimum_headroom_ratio=policy["minimum_headroom_ratio"],
        )
        expected_name = (
            f"{manifest.config_path.name}.resource-decision."
            f"{resource_decision['content_sha256']}.json"
        )
        if (resource_decision_path.parent != manifest.config_path.parent
                or resource_decision_path.name != expected_name):
            raise ValueError("Palace resource decision path is not content addressed")
        selected = resource_decision["selected_profile_id"]
        if resource_class is not None and resource_class != selected:
            raise ValueError("caller resource class differs from trusted decision")
        resource_class = selected
    node_count = manifest.mesh_provenance.get("node_count")
    tetrahedron_count = manifest.mesh_provenance.get("tetrahedron_count")
    if (type(node_count) is not int or type(tetrahedron_count) is not int
            or node_count <= 0 or tetrahedron_count <= 0):
        raise ValueError("Palace mesh manifest lacks positive resource counts")
    if resource_class is None:
        resource_class = next((
            name for name, limits in MESH_LIMITS.items()
            if node_count <= limits["nodes"]
            and tetrahedron_count <= limits["tetrahedra"]
        ), None)
    if resource_class not in RESOURCE_LIMITS:
        raise ValueError("unknown Palace resource class")
    mesh_limits = MESH_LIMITS[resource_class]
    if node_count > mesh_limits["nodes"] or tetrahedron_count > mesh_limits["tetrahedra"]:
        raise ValueError("Palace mesh exceeds its resource-class element cap")
    if manifest.checkpoint is None and manifest.output_directory.exists():
        raise ValueError("Palace output directory must not exist before a run")
    workload_path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.workload.{workload['content_sha256']}.json"
    )
    if (campaign_ledger is None) != (attempt_id is None):
        raise ValueError("Palace campaign ledger and attempt ID must be supplied together")
    if manifest.checkpoint is not None:
        if (resource_decision is None
                or type(campaign_ledger) is not CanonicalLedgerPublicationV2):
            raise ValueError(
                "checkpointed Palace runs require a resource decision and canonical campaign attempt"
            )
    elif campaign_ledger is not None:
        raise ValueError("campaign accounting requires checkpointing")
    if workload_path.exists() or workload_path.is_symlink():
        if not canonical_equal(
                strict_json_file(workload_path, label="Palace workload"), workload):
            raise ValueError("Palace content-addressed workload differs")
    else:
        try:
            exclusive_publish_json(workload_path, workload)
        except FileExistsError:
            if not canonical_equal(
                    strict_json_file(workload_path, label="Palace workload"), workload):
                raise ValueError("Palace content-addressed workload differs")
    snapshot_instance = (
        canonical_sha256({"attempt_id": attempt_id})
        if manifest.checkpoint is not None else None
    )
    execution_snapshot = prepare_execution_snapshot(
        manifest,
        workload,
        executable=executable,
        binaries=tuple(Path(path) for path in binaries),
        mpi_launcher=mpi_launcher,
        instance_id=snapshot_instance,
    )
    if campaign_ledger is not None:
        assert resource_decision is not None and policy is not None
        CanonicalLedgerPublicationV2.register_attempt(
            campaign_ledger, attempt_id, resource_decision=resource_decision,
            execution_snapshot=execution_snapshot, trusted_policy=policy,
        )
    snapshot_inputs = {
        item["role"]: item for item in execution_snapshot["inputs"]
    }
    snapshot_executable = snapshot_inputs["executable"]["snapshot"]
    snapshot_root = Path(execution_snapshot["root"])
    run_output_directory = (
        snapshot_root / manifest.output_directory.name
        if manifest.checkpoint is not None else manifest.output_directory
    )
    command = (
        snapshot_executable, "-np", str(processes), manifest.config_path.name
    )
    environment = dict(os.environ)
    environment["PATH"] = (
        str(snapshot_root / "bin") + os.pathsep + environment.get("PATH", "")
    )
    environment["OMP_NUM_THREADS"] = "1"
    monitor_kwargs = {}
    if manifest.checkpoint is not None:
        monitor_kwargs["additional_output_paths"] = (
            manifest.checkpoint["path"],
        )
    execution = run_monitored_process(
        command,
        cwd=snapshot_root,
        limits=RESOURCE_LIMITS[resource_class],
        environment=environment,
        **monitor_kwargs,
    )
    failures = list(execution.limit_failures)
    snapshot_failure = None
    try:
        validate_execution_snapshot(
            execution_snapshot, manifest, workload, executable=executable,
            binaries=tuple(Path(path) for path in binaries),
            mpi_launcher=mpi_launcher,
        )
    except (OSError, ValueError) as error:
        snapshot_failure = str(error)
    if snapshot_failure is None and manifest.checkpoint is None:
        try:
            publish_snapshot_output(snapshot_root, run_output_directory)
        except (OSError, ValueError) as error:
            failures.append(f"output_publication: {error}")
    try:
        stdout = execution.stdout.decode("utf-8", errors="strict")
        stderr = execution.stderr.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        failures.append(f"stream_encoding: {error}")
        stdout = execution.stdout.decode("utf-8", errors="replace")
        stderr = execution.stderr.decode("utf-8", errors="replace")
    if snapshot_failure is not None:
        failures.append(f"execution_snapshot: {snapshot_failure}")
    if execution.returncode != 0:
        failures.append(f"process_exit: exit code {execution.returncode}")
    normalized_stream = ANSI_ESCAPE_RE.sub("", stdout + "\n" + stderr)
    diagnostics = [line.strip() for line in normalized_stream.splitlines()
                   if FAILURE_RE.search(line)]
    failures.extend(f"solver_diagnostic: {line}" for line in diagnostics)
    for marker in NORMAL_COMPLETION_MARKERS:
        if marker not in normalized_stream:
            failures.append(f"normal_completion: missing {marker!r}")
    try:
        residuals = _parse_residuals(stdout, manifest)
    except ValueError as error:
        failures.append(f"residual_validation: {error}")
        residuals = ()

    palace_metadata_path = run_output_directory / "palace.json"
    resolved_config_path = run_output_directory / "config_resolved.json"
    runtime_metadata = None
    progress_summary = None
    try:
        palace_metadata_path, resolved_config_path, runtime_metadata = (
            _validate_completion_metadata(
                run_output_directory, manifest, processes=processes
            )
        )
        _validate_execution_runtime_binding(asdict(execution), runtime_metadata)
        _validate_runtime_build_identity(runtime_metadata, build_manifest)
    except ValueError as error:
        failures.append(f"completion_metadata: {error}")

    try:
        progress_events = _palace_progress_events(
            execution.stdout, list(execution.stream_events),
        )
    except ValueError as error:
        failures.append(f"progress_validation: {error}")
        progress_events = []
    else:
        try:
            progress_summary = _validate_completed_palace_progress(
                progress_events, len(manifest.terminals),
                checkpoint_enabled=manifest.checkpoint is not None,
                campaign_digest=_native_campaign_digest(manifest),
                terminal_indices=(terminal.index for terminal in manifest.terminals),
            )
            if runtime_metadata is not None:
                _validate_runtime_progress_accounting(
                    runtime_metadata, progress_summary,
                )
        except ValueError as error:
            wall_limit = any(value.startswith("wall time exceeded ")
                             for value in execution.limit_failures)
            progress_failure = "progress_incomplete" if wall_limit else "progress_validation"
            failures.append(f"{progress_failure}: {error}")

    raw_path = run_output_directory / "terminal-Craw.csv"
    standard_path = run_output_directory / "terminal-C.csv"
    raw_matrix = downstream_matrix = None
    try:
        raw_matrix = _parse_palace_matrix_csv(raw_path, manifest, matrix_name="raw")
        downstream_matrix = _parse_palace_matrix_csv(
            standard_path, manifest, matrix_name="standard"
        )
        failures.extend(_matrix_gate_failures(raw_matrix, downstream_matrix))
    except ValueError as error:
        failures.append(f"matrix_validation: {error}")

    snapshot_quarantined = quarantine_survivors = ()
    if failures:
        if run_output_directory.is_dir():
            quarantined, error = _quarantine_or_purge_matrix_files(
                run_output_directory, (raw_path, standard_path),
            )
            raw_path, standard_path, *quarantine_survivors = quarantined
            if error is not None:
                failures.append(f"matrix_quarantine: {error}")
        if manifest.checkpoint is None:
            snapshot_output = snapshot_root / manifest.output_directory.name
            snapshot_quarantined, error = _quarantine_or_purge_matrix_files(
                snapshot_output,
                (
                    snapshot_output / "terminal-Craw.csv",
                    snapshot_output / "terminal-C.csv",
                ),
            )
            if error is not None:
                failures.append(f"matrix_quarantine: {error}")
    stdout_sha256 = bytes_sha256(execution.stdout)
    stderr_sha256 = bytes_sha256(execution.stderr)
    stdout_path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.stdout.{stdout_sha256}.bin"
    )
    stderr_path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.stderr.{stderr_sha256}.bin"
    )
    exclusive_publish_bytes(stdout_path, execution.stdout)
    exclusive_publish_bytes(stderr_path, execution.stderr)
    artifacts = {}
    artifact_candidates = [
        manifest.config_path,
        manifest.mesh_path,
        manifest.mesh_manifest_path,
        workload_path,
        raw_path,
        standard_path,
        palace_metadata_path,
        resolved_config_path,
        stdout_path,
        stderr_path,
    ]
    if resource_decision_path is not None:
        artifact_candidates.append(resource_decision_path)
    artifact_candidates.extend(
        Path(item["snapshot"]) for item in execution_snapshot["inputs"]
    )
    artifact_candidates.extend((*quarantine_survivors, *snapshot_quarantined))
    for artifact in artifact_candidates:
        if artifact.is_file():
            artifacts[str(artifact.resolve())] = file_sha256(artifact)
    execution_record = asdict(execution)
    execution_record.pop("stdout")
    execution_record.pop("stderr")
    execution_record["palace_progress_events"] = progress_events
    identity = {
        "gate_policy": GATE_POLICY,
        "config_manifest": str(Path(config_manifest_path).resolve()),
        "config_manifest_sha256": file_sha256(config_manifest_path),
        "build_manifest": str(build_manifest_path),
        "build_manifest_sha256": file_sha256(build_manifest_path),
        "build_provenance_sha256": palace_build_identity(build_manifest),
        "binaries": binaries,
        "implementation_files": implementation,
        "mpi_launcher": mpi_launcher,
        "mpi_launcher_sha256": file_sha256(mpi_launcher),
        "workload": str(workload_path.resolve()),
        "workload_sha256": file_sha256(workload_path),
        "execution_snapshot": execution_snapshot,
        "resource_decision": (
            str(resource_decision_path) if resource_decision_path is not None
            else None
        ),
        "resource_decision_sha256": (
            file_sha256(resource_decision_path)
            if resource_decision_path is not None else None
        ),
        "artifacts": artifacts,
        "command": list(command),
        "resource_class": resource_class,
        "resource_limits": {
            **asdict(RESOURCE_LIMITS[resource_class]),
            **mesh_limits,
        },
        "resource_enforcement": "portable_process_limits",
        "execution": execution_record,
        "runtime_metadata": runtime_metadata,
        "residuals": [
            {
                "terminal": asdict(item.terminal),
                "relative_residual": item.relative_residual,
                "target": item.target,
            }
            for item in residuals
        ],
        "failures": failures,
        "lifecycle": (
            "numerically_converged_diagnostic" if not failures else "rejected_diagnostic"
        ),
    }
    identity["stdout_sha256"] = stdout_sha256
    identity["stderr_sha256"] = stderr_sha256
    identity["content_sha256"] = canonical_sha256(identity)
    run_manifest_path = manifest.config_path.with_name(
        f"{manifest.config_path.name}.run.{identity['lifecycle']}."
        f"{identity['content_sha256']}.json"
    )
    run_manifest = {"format": RUN_MANIFEST_FORMAT, **identity}
    exclusive_publish_json(run_manifest_path, run_manifest)
    matrix_access = None
    if campaign_ledger is not None:
        assert manifest.checkpoint is not None
        CanonicalLedgerPublicationV2.finish_attempt(
            campaign_ledger, attempt_id, run_manifest_path=run_manifest_path,
            checkpoint_root=manifest.checkpoint["path"],
        )
        if not failures:
            matrix_access = checkpoint_matrix_access(
                campaign_ledger, manifest,
                raw_path=raw_path, standard_path=standard_path,
            )
    if failures:
        raise PalaceRunRejected(
            "; ".join(failures), failures=failures, manifest_path=run_manifest_path
        )
    assert raw_matrix is not None and downstream_matrix is not None
    if manifest.checkpoint is not None:
        assert matrix_access is not None
        raw_matrix = parse_palace_matrix_csv(
            raw_path, manifest, matrix_name="raw", matrix_access=matrix_access,
        )
        downstream_matrix = parse_palace_matrix_csv(
            standard_path, manifest, matrix_name="standard",
            matrix_access=matrix_access,
        )
    return PalaceRun(
        command=command,
        stdout=stdout,
        stderr=stderr,
        raw_matrix=raw_matrix,
        downstream_matrix=downstream_matrix,
        residuals=residuals,
        manifest_path=run_manifest_path,
    )
