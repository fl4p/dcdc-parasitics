#!/usr/bin/env python3
"""Run the preregistered Palace h, p, and finite-domain fixture ladders."""
import argparse
import csv
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT))

from experiments.palace_fixture_study import (  # noqa: E402
    _conductors,
    _fixture,
    _model_bounds,
    _near_mesh_size,
    _outer_bounds,
    run_study,
)
from maxwell import MaxwellMatrix, entrywise_convergence_gate  # noqa: E402
from palace import (  # noqa: E402
    MESH_LIMITS,
    RESOURCE_LIMITS,
    validate_palace_run_manifest,
)
from palace_mesh import (  # noqa: E402
    BoxBounds,
    ConductorPrism,
    DielectricBox,
    validate_palace_mesh_content,
)
from provenance import canonical_equal, canonical_sha256, file_sha256  # noqa: E402


REPORT_FORMAT = "dcdc-palace-fixture-ladders-v1"
FIXTURES = (
    "parallel_plate_air",
    "parallel_plate_enclosed_fr4",
    "pcb_like_coplanar_air",
)
ENTRY_ATOL_F = 1e-15
ENTRY_RTOL = 0.02


@dataclass(frozen=True)
class LadderRung:
    name: str
    axis: str
    mesh_scale: float
    far_mesh_scale: float
    outer_scale: float
    order: int
    resource_class: str


RUNGS = (
    LadderRung("h_coarse", "h", 0.85, 0.85, 6.0, 1, "synthetic"),
    LadderRung("h_medium", "h", 0.7, 0.7, 6.0, 1, "synthetic"),
    LadderRung("h_fine", "h", 0.6, 0.6, 6.0, 1, "synthetic"),
    LadderRung("p2", "p", 0.5, 1.0, 6.0, 2, "pcb_diagnostic"),
    LadderRung("p3", "p", 0.5, 1.0, 6.0, 3, "pcb_diagnostic"),
    LadderRung("outer_near", "outer", 1.0, 1.0, 4.0, 1, "synthetic"),
    LadderRung("outer_mid", "outer", 1.0, 1.0, 6.0, 1, "synthetic"),
    LadderRung("outer_far", "outer", 1.0, 1.0, 8.0, 1, "synthetic"),
)


def _axis_reports(reports, axis):
    by_name = {item["rung"]["name"]: item for item in reports}
    if axis == "h":
        names = ("h_coarse", "h_medium", "h_fine")
    elif axis == "p":
        names = ("p2", "p3")
    elif axis == "outer":
        names = ("outer_near", "outer_mid", "outer_far")
    else:
        raise ValueError(f"unknown ladder axis {axis!r}")
    return tuple(by_name[name] for name in names)


def _entrywise_gate(left, right):
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    if left.ndim != 2 or left.shape[0] != left.shape[1]:
        raise ValueError("convergence matrices must be square")
    names = tuple(str(index) for index in range(left.shape[0]))
    return entrywise_convergence_gate(
        MaxwellMatrix(names, left),
        MaxwellMatrix(names, right),
        atol=ENTRY_ATOL_F,
        rtol=ENTRY_RTOL,
    )


def _outer_row_sum_gate(reports):
    row_sums = np.asarray([
        np.sum(np.asarray(item["raw_matrix_f"], dtype=float), axis=1)
        for item in reports
    ])
    entries = []
    for index in range(row_sums.shape[1]):
        values = row_sums[:, index]
        increments = np.diff(values)
        monotone = bool(np.all(increments >= 0.0) or np.all(increments <= 0.0))
        shrinking = bool(abs(increments[1]) <= abs(increments[0]))
        entries.append({
            "row": index + 1,
            "values_f": values.tolist(),
            "increments_f": increments.tolist(),
            "monotone": monotone,
            "shrinking_increments": shrinking,
            "passed": monotone or shrinking,
        })
    return {"passed": all(item["passed"] for item in entries), "rows": entries}


def _parallel_plate_analytic_gate(report):
    epsilon_0 = 8.8541878128e-12
    ideal_f = epsilon_0 * 1e-6 / 100e-6
    mutual_f = -float(report["raw_matrix_f"][0][1])
    relative_excess = mutual_f / ideal_f - 1.0
    return {
        "ideal_parallel_plate_f": ideal_f,
        "extracted_mutual_f": mutual_f,
        "relative_excess": relative_excess,
        "expected_range": [0.0, 0.5],
        "passed": 0.0 <= relative_excess <= 0.5,
    }


HASH_RE = re.compile(r"[0-9a-f]{64}")
MESH_PROVENANCE_KEYS = {
    "conductors", "dielectrics", "edge_count", "face_count", "gmsh",
    "ground_attribute", "material_attributes", "mesh_parameters",
    "mesh_sha256", "node_count", "outer_bounds", "outer_permittivity",
    "reference_semantics", "terminal_attributes", "tetrahedron_count",
}


def _identity_schema_failures(item):
    failures = []
    required_report = {
        "fixture", "scope", "build_manifest", "outer_scale", "mesh_scale",
        "far_mesh_scale", "order",
        "resource_class", "near_mesh_size_m", "far_mesh_size_m",
        "transition_distance_m", "node_count", "tetrahedron_count",
        "reference_semantics", "mesh_identity", "config_identity",
        "run_identity", "content_sha256",
    }
    missing = required_report - set(item)
    if missing:
        failures.append(f"study report missing fields: {sorted(missing)}")
        return failures
    for name in ("content_sha256",):
        if not isinstance(item[name], str) or HASH_RE.fullmatch(item[name]) is None:
            failures.append(f"invalid {name}")
    mesh = item["mesh_identity"]
    mesh_keys = {
        "mesh_path", "mesh_sha256", "manifest_path", "manifest_sha256",
        "provenance_sha256", "provenance",
    }
    if not isinstance(mesh, dict) or set(mesh) != mesh_keys:
        failures.append("mesh identity schema mismatch")
        return failures
    for name in ("mesh_sha256", "manifest_sha256", "provenance_sha256"):
        if not isinstance(mesh[name], str) or HASH_RE.fullmatch(mesh[name]) is None:
            failures.append(f"invalid mesh identity {name}")
    provenance = mesh["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != MESH_PROVENANCE_KEYS:
        failures.append("mesh provenance schema mismatch")
        return failures
    gmsh = provenance["gmsh"]
    if not isinstance(gmsh, dict) or set(gmsh) != {
        "version", "python_module", "python_module_sha256", "library",
        "library_sha256",
    }:
        failures.append("Gmsh identity schema mismatch")
    parameters = provenance["mesh_parameters"]
    if not isinstance(parameters, dict) or set(parameters) != {
        "algorithm_3d", "mesh_size_far_m", "mesh_size_near_m", "msh_version",
        "threads", "transition_distance_m",
    }:
        failures.append("mesh-parameter schema mismatch")
    if (not isinstance(provenance["outer_bounds"], dict) or set(
            provenance["outer_bounds"]) != {"minimum", "maximum"}
            or any(not isinstance(provenance["outer_bounds"][name], list)
                   or len(provenance["outer_bounds"][name]) != 3
                   for name in ("minimum", "maximum"))):
        failures.append("outer-bounds schema mismatch")
    if not isinstance(provenance["reference_semantics"], dict) or set(
            provenance["reference_semantics"]) != {
                "convergence_ladder_required", "kind", "not_a_circuit_node"
            }:
        failures.append("mesh reference-semantics schema mismatch")
    for conductor in provenance["conductors"]:
        if not isinstance(conductor, dict) or set(conductor) != {
                "name", "rings", "z_min", "z_max"}:
            failures.append("conductor schema mismatch")
        elif (not isinstance(conductor["rings"], list)
              or not conductor["rings"]
              or any(not isinstance(ring, list) or len(ring) < 3
                     or any(not isinstance(point, list) or len(point) != 2
                            for point in ring)
                     for ring in conductor["rings"])):
            failures.append("conductor-ring schema mismatch")
    for dielectric in provenance["dielectrics"]:
        if not isinstance(dielectric, dict) or set(dielectric) != {
                "name", "bounds", "relative_permittivity"}:
            failures.append("dielectric schema mismatch")
        elif (not isinstance(dielectric["bounds"], dict) or set(
                dielectric["bounds"]) != {"minimum", "maximum"}
              or any(not isinstance(dielectric["bounds"][name], list)
                     or len(dielectric["bounds"][name]) != 3
                     for name in ("minimum", "maximum"))):
            failures.append("dielectric-bounds schema mismatch")
    config = item["config_identity"]
    config_keys = {
        "config_path", "config_sha256", "manifest_path", "manifest_sha256",
        "provenance_sha256", "provenance",
    }
    if not isinstance(config, dict) or set(config) != config_keys:
        failures.append("config identity schema mismatch")
        return failures
    reference = item["reference_semantics"]
    reference_keys = {
        "kind", "convergence_ladder_required", "not_a_circuit_node",
        "outer_bounds_m", "outer_scale",
    }
    if not isinstance(reference, dict) or set(reference) != reference_keys:
        failures.append("study finite-reference schema mismatch")
    elif (not isinstance(reference["outer_bounds_m"], dict)
          or set(reference["outer_bounds_m"]) != {"minimum", "maximum"}
          or any(not isinstance(reference["outer_bounds_m"][name], list)
                 or len(reference["outer_bounds_m"][name]) != 3
                 for name in ("minimum", "maximum"))):
        failures.append("study finite-reference bounds schema mismatch")
    config_provenance = config["provenance"]
    if not isinstance(config_provenance, dict) or set(config_provenance) != {
        "checkpoint", "config_sha256", "explicit_residual_tolerance",
        "finite_reference", "gate_policy", "ground_attribute",
        "linear_tolerance", "materials",
        "maximum_iterations",
        "mesh_manifest_sha256", "mesh_sha256", "order", "terminals",
    }:
        failures.append("config provenance schema mismatch")
    else:
        if config_provenance["checkpoint"] is not None:
            failures.append("fixture study does not permit checkpoint execution")
        finite_reference = config_provenance["finite_reference"]
        if (not isinstance(finite_reference, dict)
                or set(finite_reference) != reference_keys):
            failures.append("config finite-reference schema mismatch")
        for terminal in config_provenance["terminals"]:
            if not isinstance(terminal, dict) or set(terminal) != {
                    "index", "name", "attribute"}:
                failures.append("config terminal schema mismatch")
        for material in config_provenance["materials"]:
            if not isinstance(material, dict) or set(material) != {
                    "name", "attributes", "relative_permittivity"}:
                failures.append("config material schema mismatch")
    for terminal in provenance["terminal_attributes"]:
        if (not isinstance(terminal, list) or len(terminal) != 2
                or not isinstance(terminal[0], str)
                or type(terminal[1]) is not int):
            failures.append("mesh terminal-attribute schema mismatch")
    for material in provenance["material_attributes"]:
        if (not isinstance(material, list) or len(material) != 3
                or not isinstance(material[0], str)
                or type(material[1]) is not int
                or isinstance(material[2], bool)
                or not isinstance(material[2], (int, float))):
            failures.append("mesh material-attribute schema mismatch")
    run = item["run_identity"]
    if not isinstance(run, dict) or set(run) != {
        "path", "sha256", "content_sha256", "resource_class",
        "resource_limits", "config_manifest", "config_manifest_sha256",
        "artifacts",
    }:
        failures.append("run identity schema mismatch")
        return failures
    for container, names in (
        (config, ("config_sha256", "manifest_sha256", "provenance_sha256")),
        (run, ("sha256", "content_sha256", "config_manifest_sha256")),
    ):
        for name in names:
            if not isinstance(container[name], str) or HASH_RE.fullmatch(
                    container[name]) is None:
                failures.append(f"invalid identity hash {name}")
    if not isinstance(run["resource_limits"], dict) or set(
            run["resource_limits"]) != {
                "wall_time_s", "peak_rss_bytes", "output_bytes",
                "refined_panels", "gmres_iterations_per_rhs", "nodes",
                "tetrahedra",
            }:
        failures.append("resource-limit schema mismatch")
    expected_reference = {
        "kind": "finite_outer_dirichlet_approximation",
        "convergence_ladder_required": True,
        "not_a_circuit_node": True,
    }
    if (isinstance(reference, dict)
            and not canonical_equal(
                {key: reference.get(key) for key in expected_reference},
                expected_reference,
            )):
        failures.append("study finite-reference meaning is not qualification-safe")
    finite_reference = config_provenance.get("finite_reference", {})
    if (isinstance(finite_reference, dict)
            and not canonical_equal(
                {key: finite_reference.get(key) for key in expected_reference},
                expected_reference,
            )):
        failures.append("config finite-reference meaning is not qualification-safe")
    if not canonical_equal(provenance.get("reference_semantics"), expected_reference):
        failures.append("mesh finite-reference meaning is not qualification-safe")
    try:
        report_bounds = BoxBounds(**reference["outer_bounds_m"])
        mesh_bounds = BoxBounds(**provenance["outer_bounds"])
        config_bounds = BoxBounds(**finite_reference["outer_bounds_m"])
        conductors = tuple(ConductorPrism(
            conductor["name"],
            tuple(tuple(tuple(point) for point in ring)
                  for ring in conductor["rings"]),
            conductor["z_min"], conductor["z_max"],
        ) for conductor in provenance["conductors"])
        tuple(DielectricBox(
            dielectric["name"], BoxBounds(**dielectric["bounds"]),
            dielectric["relative_permittivity"],
        ) for dielectric in provenance["dielectrics"])
    except (KeyError, TypeError, ValueError) as error:
        failures.append(f"unsafe geometry or reference values: {error}")
    else:
        if not conductors or report_bounds != mesh_bounds or config_bounds != mesh_bounds:
            failures.append("geometry/reference bounds are inconsistent")
    for name in ("outer_scale", "mesh_scale", "far_mesh_scale",
                 "near_mesh_size_m", "far_mesh_size_m", "transition_distance_m"):
        value = item.get(name)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0.0):
            failures.append(f"study control {name} must be finite and positive")
    if item.get("outer_scale", 0.0) <= 1.0:
        failures.append("study outer scale must exceed one")
    for name in ("node_count", "tetrahedron_count", "order"):
        if type(item.get(name)) is not int or item[name] <= 0:
            failures.append(f"study {name} must be a positive integer")
    parameters = provenance.get("mesh_parameters", {})
    if (isinstance(parameters, dict)
            and (type(parameters.get("algorithm_3d")) is not int
                 or parameters["algorithm_3d"] != 10
                 or type(parameters.get("threads")) is not int
                 or parameters["threads"] != 1
                 or isinstance(parameters.get("msh_version"), bool)
                 or not isinstance(parameters.get("msh_version"), (int, float))
                 or parameters["msh_version"] != 2.2)):
        failures.append("mesh parameters are not qualification-safe")
    for name in ("mesh_size_far_m", "mesh_size_near_m",
                 "transition_distance_m"):
        value = parameters.get(name) if isinstance(parameters, dict) else None
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0.0):
            failures.append(f"mesh parameter {name} must be finite and positive")
    for name in (
            "node_count", "edge_count", "face_count", "tetrahedron_count",
            "ground_attribute"):
        if type(provenance.get(name)) is not int or provenance[name] <= 0:
            failures.append(f"mesh {name} must be a positive integer")
    for terminal in config_provenance.get("terminals", ()):
        if (type(terminal.get("index")) is not int or terminal["index"] <= 0
                or type(terminal.get("attribute")) is not int
                or terminal["attribute"] <= 0 or not terminal.get("name")):
            failures.append("config terminal values are invalid")
    for material in config_provenance.get("materials", ()):
        permittivity = material.get("relative_permittivity")
        attributes = material.get("attributes")
        if (not material.get("name") or not isinstance(attributes, list)
                or not attributes or any(type(value) is not int or value <= 0
                                         for value in attributes)
                or isinstance(permittivity, bool)
                or not isinstance(permittivity, (int, float))
                or not math.isfinite(permittivity) or permittivity <= 0.0):
            failures.append("config material values are invalid")
    return failures


def _frozen_fixture_semantic_failures(item):
    fixture = _fixture(item["fixture"])
    conductors = _conductors(fixture)
    outer = _outer_bounds(fixture, conductors, item["outer_scale"])
    near_size = _near_mesh_size(fixture, item["mesh_scale"])
    model_minimum, model_maximum = _model_bounds(fixture, conductors)
    model_span = max(
        high - low for low, high in zip(model_minimum, model_maximum)
    )
    far_size = max(near_size, model_span / 5.0 * item["far_mesh_scale"])
    transition_distance = max(4.0 * near_size, 0.5 * model_span)
    dielectrics = ()
    if fixture.dielectric_bounds_m is not None:
        dielectrics = (DielectricBox(
            "fixture_dielectric",
            BoxBounds(*fixture.dielectric_bounds_m),
            fixture.relative_permittivity,
        ),)
    expected = {
        "scope": fixture.scope,
        "conductors": [asdict(value) for value in conductors],
        "dielectrics": [asdict(value) for value in dielectrics],
        "outer_bounds": asdict(outer),
        "outer_permittivity": 1.0,
        "terminal_attributes": [
            [conductor.name, 101 + index]
            for index, conductor in enumerate(conductors)
        ],
        "material_attributes": [
            ["outer", 1, 1.0],
            *[
                [dielectric.name, 2 + index, dielectric.relative_permittivity]
                for index, dielectric in enumerate(dielectrics)
            ],
        ],
        "near_mesh_size_m": near_size,
        "far_mesh_size_m": far_size,
        "transition_distance_m": transition_distance,
    }
    provenance = item["mesh_identity"]["provenance"]
    actual = {
        "scope": item.get("scope"),
        "conductors": provenance["conductors"],
        "dielectrics": provenance["dielectrics"],
        "outer_bounds": provenance["outer_bounds"],
        "outer_permittivity": provenance["outer_permittivity"],
        "terminal_attributes": provenance["terminal_attributes"],
        "material_attributes": provenance["material_attributes"],
        "near_mesh_size_m": item["near_mesh_size_m"],
        "far_mesh_size_m": item["far_mesh_size_m"],
        "transition_distance_m": item["transition_distance_m"],
    }
    return [] if canonical_equal(actual, expected) else [
        "named fixture geometry/material/reference semantics differ from policy"
    ]


def _semantic_binding_failures(item):
    failures = []
    try:
        mesh = item["mesh_identity"]
        provenance = mesh["provenance"]
        config = item["config_identity"]
        config_provenance = config["provenance"]
        run = item["run_identity"]
        rung = item["rung"]
        failures.extend(_frozen_fixture_semantic_failures(item))
        expected_rung = next(
            (asdict(candidate) for candidate in RUNGS
             if candidate.name == rung.get("name")),
            None,
        )
        if expected_rung is None or not canonical_equal(rung, expected_rung):
            failures.append("rung controls differ from frozen policy")
        for name in (
                "outer_scale", "mesh_scale", "far_mesh_scale", "order",
                "resource_class"):
            if not canonical_equal(item[name], rung[name]):
                failures.append(f"rung/study mismatch: {name}")
        if not canonical_equal(item["order"], config_provenance["order"]):
            failures.append("study/config order mismatch")
        if not canonical_equal(item["resource_class"], run["resource_class"]):
            failures.append("study/run resource class mismatch")
        expected_limits = {
            **MESH_LIMITS[item["resource_class"]],
            **asdict(RESOURCE_LIMITS[item["resource_class"]]),
        }
        if not canonical_equal(run["resource_limits"], expected_limits):
            failures.append("run resource limits differ from policy")
        if mesh["mesh_sha256"] != provenance["mesh_sha256"]:
            failures.append("report/manifest mesh hash mismatch")
        if (not canonical_equal(item["node_count"], provenance["node_count"])
                or not canonical_equal(
                    item["tetrahedron_count"], provenance["tetrahedron_count"]
                )):
            failures.append("study/manifest mesh count mismatch")
        parameters = provenance["mesh_parameters"]
        for report_name, manifest_name in (
            ("near_mesh_size_m", "mesh_size_near_m"),
            ("far_mesh_size_m", "mesh_size_far_m"),
            ("transition_distance_m", "transition_distance_m"),
        ):
            if not canonical_equal(item[report_name], parameters[manifest_name]):
                failures.append(f"study/manifest mismatch: {report_name}")
        reference = item["reference_semantics"]
        if not canonical_equal(reference["outer_scale"], item["outer_scale"]):
            failures.append("finite-reference outer-scale mismatch")
        if (not canonical_equal(
                    reference["outer_bounds_m"], provenance["outer_bounds"]
                ) or not canonical_equal(
                    {name: reference[name] for name in (
                        "kind", "convergence_ladder_required", "not_a_circuit_node"
                    )},
                    provenance["reference_semantics"],
                )):
            failures.append("study/manifest reference mismatch")
        if config_provenance["config_sha256"] != config["config_sha256"]:
            failures.append("config hash cross-binding mismatch")
        if (config_provenance["mesh_sha256"] != mesh["mesh_sha256"]
                or config_provenance["mesh_manifest_sha256"]
                != mesh["manifest_sha256"]):
            failures.append("config/mesh cross-binding mismatch")
        if not canonical_equal(config_provenance["finite_reference"], reference):
            failures.append("config/reference cross-binding mismatch")
        terminals = [
            {"index": index, "name": name, "attribute": attribute}
            for index, (name, attribute) in enumerate(
                provenance["terminal_attributes"], 1
            )
        ]
        materials = [
            {"name": name, "attributes": [attribute],
             "relative_permittivity": permittivity}
            for name, attribute, permittivity in provenance["material_attributes"]
        ]
        if not canonical_equal(config_provenance["terminals"], terminals):
            failures.append("config/terminal cross-binding mismatch")
        if not canonical_equal(config_provenance["materials"], materials):
            failures.append("config/material cross-binding mismatch")
        if not canonical_equal(
                config_provenance["ground_attribute"], provenance["ground_attribute"]):
            failures.append("config/ground cross-binding mismatch")
        if (run["config_manifest"] != config["manifest_path"]
                or run["config_manifest_sha256"] != config["manifest_sha256"]):
            failures.append("run/config-manifest cross-binding mismatch")
        for path, expected in (
            (config["config_path"], config["config_sha256"]),
            (mesh["mesh_path"], mesh["mesh_sha256"]),
            (mesh["manifest_path"], mesh["manifest_sha256"]),
        ):
            if run["artifacts"].get(path) != expected:
                failures.append(f"run artifact cross-binding mismatch: {path}")
    except (KeyError, TypeError, ValueError) as error:
        failures.append(f"semantic cross-binding failed: {error}")
    return failures


def _recursive_subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _recursive_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            _recursive_subset(left, right) for left, right in zip(expected, actual)
        )
    return canonical_equal(expected, actual)


def _unbound_identity_failures(item):
    try:
        return _identity_schema_failures(item) + _semantic_binding_failures(item)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        return [f"malformed unbound identity: {error}"]


def _csv_matrix_values(path):
    with Path(path).open(newline="") as stream:
        rows = list(csv.reader(stream))
    return np.asarray([
        [float(value) for value in row[1:]] for row in rows[1:]
    ], dtype=float)


def _bound_artifact_failures(item):
    try:
        failures = _identity_schema_failures(item)
        failures.extend(_semantic_binding_failures(item))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        return [f"malformed bound identity: {error}"]
    if failures:
        return failures
    try:
        validated_run = validate_palace_run_manifest(item["run_identity"]["path"])
        if (validated_run["manifest"].order != item["order"]
                or not np.array_equal(
                    validated_run["raw_matrix"].values,
                    np.asarray(item["raw_matrix_f"], dtype=float))
                or not np.array_equal(
                    validated_run["downstream_matrix"].values,
                    np.asarray(item["downstream_matrix_f"], dtype=float))):
            failures.append("validated Palace run/report mismatch")
    except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError) as error:
        failures.append(f"Palace run revalidation failed: {error}")
    if failures:
        return failures
    try:
        report_path = Path(item["report_path"])
        if file_sha256(report_path) != item["report_sha256"]:
            failures.append("study report physical hash mismatch")
        stored_report = json.loads(report_path.read_text())
        for key, value in stored_report.items():
            if not canonical_equal(item.get(key), value):
                failures.append(f"study report stale field: {key}")
        stored_content = stored_report.pop("content_sha256")
        if canonical_sha256(stored_report) != stored_content:
            failures.append("study report content ID mismatch")
        if item["content_sha256"] != stored_content:
            failures.append("study report stale content ID")

        mesh = item["mesh_identity"]
        if file_sha256(mesh["mesh_path"]) != mesh["mesh_sha256"]:
            failures.append("mesh physical hash mismatch")
        if file_sha256(mesh["manifest_path"]) != mesh["manifest_sha256"]:
            failures.append("mesh manifest physical hash mismatch")
        stored_manifest = json.loads(Path(mesh["manifest_path"]).read_text())
        if canonical_sha256(stored_manifest["provenance"]) != stored_manifest[
                "provenance_sha256"]:
            failures.append("mesh manifest content ID mismatch")
        if (mesh["provenance_sha256"] != stored_manifest["provenance_sha256"]
                or not canonical_equal(
                    mesh["provenance"], stored_manifest["provenance"]
                )):
            failures.append("mesh identity is stale or incomplete")
        validate_palace_mesh_content(mesh["mesh_path"], mesh["provenance"])
        gmsh = mesh["provenance"]["gmsh"]
        if file_sha256(gmsh["python_module"]) != gmsh["python_module_sha256"]:
            failures.append("Gmsh module hash mismatch")
        if file_sha256(gmsh["library"]) != gmsh["library_sha256"]:
            failures.append("Gmsh library hash mismatch")

        config = item["config_identity"]
        if file_sha256(config["config_path"]) != config["config_sha256"]:
            failures.append("config physical hash mismatch")
        if file_sha256(config["manifest_path"]) != config["manifest_sha256"]:
            failures.append("config manifest physical hash mismatch")
        stored_config_manifest = json.loads(
            Path(config["manifest_path"]).read_text()
        )
        if canonical_sha256(stored_config_manifest["provenance"]) != (
                stored_config_manifest["provenance_sha256"]):
            failures.append("config manifest content ID mismatch")
        if (config["provenance_sha256"]
                != stored_config_manifest["provenance_sha256"]
                or not canonical_equal(
                    config["provenance"], stored_config_manifest["provenance"]
                )):
            failures.append("config identity is stale or incomplete")
        stored_config = json.loads(Path(config["config_path"]).read_text())
        if not canonical_equal(stored_config["Solver"]["Order"], item["order"]):
            failures.append("live config solver order mismatch")
        expected_boundaries = {
            "Ground": {"Attributes": [config["provenance"]["ground_attribute"]]},
            "Terminal": [
                {"Attributes": [terminal["attribute"]], "Index": terminal["index"]}
                for terminal in config["provenance"]["terminals"]
            ],
        }
        expected_materials = [
            {"Attributes": material["attributes"],
             "Permittivity": material["relative_permittivity"]}
            for material in config["provenance"]["materials"]
        ]
        if (not canonical_equal(stored_config["Boundaries"], expected_boundaries)
                or not canonical_equal(
                    stored_config["Domains"]["Materials"], expected_materials
                ) or not canonical_equal(
                    stored_config["Solver"]["Linear"]["Tol"],
                    config["provenance"]["linear_tolerance"],
                ) or not canonical_equal(
                    stored_config["Solver"]["Linear"]["VerificationTol"],
                    config["provenance"]["explicit_residual_tolerance"],
                ) or not canonical_equal(
                    stored_config["Solver"]["Linear"]["MaxIts"],
                    config["provenance"]["maximum_iterations"],
                )):
            failures.append("live config/provenance semantic mismatch")

        run = item["run_identity"]
        if file_sha256(run["path"]) != run["sha256"]:
            failures.append("run manifest physical hash mismatch")
        stored_run = json.loads(Path(run["path"]).read_text())
        stored_run_content = stored_run.pop("content_sha256")
        stored_run.pop("format")
        if canonical_sha256(stored_run) != stored_run_content:
            failures.append("run manifest content ID mismatch")
        if (run["content_sha256"] != stored_run_content
                or not canonical_equal(
                    run["resource_class"], stored_run["resource_class"]
                ) or not canonical_equal(
                    run["resource_limits"], stored_run["resource_limits"]
                ) or run["config_manifest"] != stored_run["config_manifest"]
                or run["config_manifest_sha256"]
                != stored_run["config_manifest_sha256"]
                or not canonical_equal(run["artifacts"], stored_run["artifacts"])):
            failures.append("run identity is stale or incomplete")

        artifacts = stored_run["artifacts"]
        for path, expected_sha256 in artifacts.items():
            if not Path(path).is_file() or file_sha256(path) != expected_sha256:
                failures.append(f"missing or tampered run artifact: {path}")
        config_path = Path(config["config_path"])
        output = config_path.parent / "postpro"
        required = {
            str(config_path),
            stored_run["workload"],
            mesh["mesh_path"],
            mesh["manifest_path"],
            str(output / "config_resolved.json"),
            str(output / "palace.json"),
            str(output / "terminal-Craw.csv"),
            str(output / "terminal-C.csv"),
        }
        if stored_run["resource_decision"] is not None:
            required.add(stored_run["resource_decision"])
        required.update(
            record["snapshot"]
            for record in stored_run["execution_snapshot"]["inputs"]
        )
        stream_paths = [
            path for path in artifacts
            if Path(path).name.startswith(f"{config_path.name}.stdout.")
            or Path(path).name.startswith(f"{config_path.name}.stderr.")
        ]
        required.update(stream_paths)
        if len(stream_paths) != 2 or set(artifacts) != required:
            failures.append("required run artifact set mismatch")

        resolved_path = output / "config_resolved.json"
        palace_path = output / "palace.json"
        raw_path = output / "terminal-Craw.csv"
        standard_path = output / "terminal-C.csv"
        resolved = json.loads(resolved_path.read_text())
        if not _recursive_subset(stored_config, resolved):
            failures.append("resolved config differs from bound input config")
        if resolved["Solver"]["Order"] != item["order"]:
            failures.append("resolved solver order mismatch")
        resolved_mesh = Path(resolved["Model"]["Mesh"])
        if not resolved_mesh.is_absolute():
            resolved_mesh = config_path.parent / resolved_mesh
        if (resolved_mesh.resolve() != Path(mesh["mesh_path"]).resolve()
                or file_sha256(resolved_mesh) != mesh["mesh_sha256"]):
            failures.append("resolved mesh identity mismatch")
        if not np.array_equal(
                _csv_matrix_values(raw_path),
                np.asarray(item["raw_matrix_f"], dtype=float)):
            failures.append("raw CSV/report matrix mismatch")
        if not np.array_equal(
                _csv_matrix_values(standard_path),
                np.asarray(item["downstream_matrix_f"], dtype=float)):
            failures.append("standard CSV/report matrix mismatch")

        palace_metadata = json.loads(palace_path.read_text())
        if not canonical_equal(palace_metadata, stored_run["runtime_metadata"]):
            failures.append("Palace metadata/run-manifest mismatch")
        problem = palace_metadata["Problem"]
        if (problem["MeshElements"] != item["tetrahedron_count"]
                or problem["DegreesOfFreedom"] <= 0):
            failures.append("Palace mesh/DOF metadata mismatch")
        command = stored_run["command"]
        mpi_index = command.index("-np")
        if problem["MPISize"] != int(command[mpi_index + 1]):
            failures.append("Palace MPI metadata mismatch")
        if palace_metadata["LinearSolver"]["TotalSolves"] != len(
                config["provenance"]["terminals"]):
            failures.append("Palace solve-count metadata mismatch")
        build_path = Path(item["build_manifest"])
        build = json.loads(build_path.read_text())
        if (file_sha256(build_path) != stored_run["build_manifest_sha256"]
                or str(build_path.resolve()) != stored_run["build_manifest"]
                or build["provenance"]["source_commit"][:8]
                not in palace_metadata["GitTag"]):
            failures.append("Palace build/runtime metadata mismatch")
        stdout = next((path for path in stream_paths if ".stdout." in path), None)
        stderr = next((path for path in stream_paths if ".stderr." in path), None)
        if (stdout is None or stderr is None
                or artifacts.get(stdout) != stored_run["stdout_sha256"]
                or artifacts.get(stderr) != stored_run["stderr_sha256"]):
            failures.append("Palace stream artifact mismatch")
    except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError,
            json.JSONDecodeError) as error:
        failures.append(f"missing or malformed bound identity: {error}")
    return failures


def _p_shared_mesh_gate(reports, *, verify_artifacts=True):
    try:
        by_name = {item["rung"]["name"]: item for item in reports}
        p2 = by_name["p2"]
        p3 = by_name["p3"]
        expected_limits = {
            **MESH_LIMITS["pcb_diagnostic"],
            **asdict(RESOURCE_LIMITS["pcb_diagnostic"]),
        }
        expected_rungs = (
            (p2, 2),
            (p3, 3),
        )
        for item, order in expected_rungs:
            identity_failures = (
                _bound_artifact_failures(item) if verify_artifacts else
                _unbound_identity_failures(item)
            )
            if identity_failures:
                return {
                    "passed": False,
                    "failure": "p rung identity/schema failure",
                    "identity_failures": identity_failures,
                }
            rung = item["rung"]
            if not canonical_equal(rung, asdict(LadderRung(
                    rung["name"], "p", 0.5, 1.0, 6.0, order,
                    "pcb_diagnostic"))):
                return {"passed": False, "failure": "p rung controls changed"}
            if not canonical_equal(item["order"], order):
                return {"passed": False, "failure": "p study order changed"}
            run = item["run_identity"]
            if (run["resource_class"] != "pcb_diagnostic"
                    or not canonical_equal(run["resource_limits"], expected_limits)):
                return {"passed": False, "failure": "p rung limits changed"}
        p2_mesh = p2["mesh_identity"]
        p3_mesh = p3["mesh_identity"]
        fields = (
            "mesh_sha256",
            "manifest_sha256",
            "provenance_sha256",
            "provenance",
        )
        mismatches = [
            field for field in fields
            if not canonical_equal(p2_mesh[field], p3_mesh[field])
        ]
        if mismatches:
            return {
                "passed": False,
                "failure": "p2/p3 shared mesh identity mismatch",
                "mismatched_fields": mismatches,
            }
        return {
            "passed": True,
            "mesh_sha256": p2_mesh["mesh_sha256"],
            "manifest_sha256": p2_mesh["manifest_sha256"],
            "provenance_sha256": p2_mesh["provenance_sha256"],
            "gmsh": p2_mesh["provenance"]["gmsh"],
            "mesh_parameters": p2_mesh["provenance"]["mesh_parameters"],
            "p2_run_content_sha256": p2["run_identity"]["content_sha256"],
            "p3_run_content_sha256": p3["run_identity"]["content_sha256"],
        }
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        return {"passed": False, "failure": f"missing p shared-mesh identity: {error}"}


def _evaluate_fixture(fixture, reports, *, verify_artifacts=True):
    if not isinstance(reports, (list, tuple)):
        return {
            "axes": {},
            "p_shared_mesh": {
                "passed": False,
                "failure": "malformed outer reports container",
            },
            "analytic_cross_check": None,
            "uncertainty_f": None,
            "failures": ["malformed outer reports container"],
            "lifecycle": "rejected_diagnostic",
        }
    failures = []
    if len(reports) != len(RUNGS):
        failures.append("missing ladder rung")
    for item in reports:
        if not isinstance(item, dict):
            failures.append("malformed ladder rung report")
            continue
        rung = item.get("rung")
        name = rung.get("name", "unknown") if isinstance(rung, dict) else "unknown"
        if item.get("fixture") != fixture:
            failures.append(f"{name}: fixture identity mismatch")
        if item.get("lifecycle") != "numerically_converged_diagnostic":
            failures.append(f"{name}: rejected rung")
        identity_failures = (
            _bound_artifact_failures(item) if verify_artifacts else
            _unbound_identity_failures(item)
        )
        failures.extend(f"{name}: {failure}" for failure in identity_failures)
    p_shared_mesh = _p_shared_mesh_gate(
        reports, verify_artifacts=verify_artifacts
    )
    if not p_shared_mesh["passed"]:
        failures.append(f"p: {p_shared_mesh['failure']}")
    axes = {}
    uncertainty = 0.0
    if not failures:
        for axis in ("h", "p", "outer"):
            axis_reports = _axis_reports(reports, axis)
            adjacent = []
            for left, right in zip(axis_reports, axis_reports[1:]):
                gate = _entrywise_gate(left["raw_matrix_f"], right["raw_matrix_f"])
                gate["from"] = left["rung"]["name"]
                gate["to"] = right["rung"]["name"]
                adjacent.append(gate)
                uncertainty = max(uncertainty, gate["max_difference_f"])
                if not gate["passed"]:
                    failures.append(
                        f"{axis}: {gate['from']} to {gate['to']} matrix convergence"
                    )
            axes[axis] = {"adjacent": adjacent, "passed": all(
                item["passed"] for item in adjacent
            )}
        outer_gate = _outer_row_sum_gate(_axis_reports(reports, "outer"))
        axes["outer"]["row_sum_convergence"] = outer_gate
        axes["outer"]["passed"] = axes["outer"]["passed"] and outer_gate["passed"]
        if not outer_gate["passed"]:
            failures.append("outer: capacitance-to-reference row sums do not converge")
    analytic = None
    if fixture == "parallel_plate_air" and not failures:
        analytic = _parallel_plate_analytic_gate(
            next(item for item in reports if item["rung"]["name"] == "h_fine")
        )
        if not analytic["passed"]:
            failures.append("parallel-plate analytic cross-check")
    return {
        "axes": axes,
        "p_shared_mesh": p_shared_mesh,
        "analytic_cross_check": analytic,
        "uncertainty_f": uncertainty if not failures else None,
        "failures": failures,
        "lifecycle": (
            "numerically_converged_diagnostic" if not failures
            else "rejected_diagnostic"
        ),
    }


def run_ladders(*, output_directory, executable, build_manifest, processes=1):
    output_directory = Path(output_directory).resolve()
    if output_directory.exists() and any(output_directory.iterdir()):
        raise ValueError("Palace ladder output directory must be empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    fixture_results = []
    for fixture in FIXTURES:
        reports = []
        for rung in RUNGS:
            try:
                path, report = run_study(
                    fixture_name=fixture,
                    output_directory=output_directory / fixture / rung.name,
                    executable=executable,
                    build_manifest=build_manifest,
                    outer_scale=rung.outer_scale,
                    mesh_scale=rung.mesh_scale,
                    order=rung.order,
                    processes=processes,
                    far_mesh_scale=rung.far_mesh_scale,
                    resource_class=rung.resource_class,
                )
            except (AttributeError, IndexError, KeyError, OSError, RuntimeError,
            TypeError, ValueError) as error:
                reports.append({
                    "fixture": fixture,
                    "lifecycle": "rejected_diagnostic",
                    "failures": [f"ladder execution: {error}"],
                    "rung": asdict(rung),
                })
            else:
                reports.append({
                    **report,
                    "report_path": str(path),
                    "report_sha256": file_sha256(path),
                    "rung": asdict(rung),
                })
        fixture_results.append({
            "fixture": fixture,
            "rungs": reports,
            **_evaluate_fixture(fixture, reports),
        })
    failures = [
        f"{item['fixture']}: {failure}"
        for item in fixture_results
        for failure in item["failures"]
    ]
    report = {
        "format": REPORT_FORMAT,
        "gate_policy": "palace-electrostatic-pcb-gates-v2",
        "implementation": str(Path(__file__).resolve()),
        "implementation_sha256": file_sha256(__file__),
        "executable": str(Path(executable).resolve()),
        "build_manifest": str(Path(build_manifest).resolve()),
        "build_manifest_sha256": file_sha256(build_manifest),
        "entrywise_allowance": {"atol_f": ENTRY_ATOL_F, "rtol": ENTRY_RTOL},
        "outer_row_sum_rule": "monotone or shrinking adjacent increments",
        "fixtures": fixture_results,
        "failures": failures,
        "lifecycle": (
            "numerically_converged_diagnostic" if not failures
            else "rejected_diagnostic"
        ),
        "scope_limit": (
            "Frozen fixture qualification only; no Fugu run, matrix, or physical model."
        ),
    }
    report["content_sha256"] = canonical_sha256(report)
    path = output_directory / f"palace-fixture-ladders.{report['content_sha256']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--processes", type=int, default=1)
    arguments = parser.parse_args()
    path, report = run_ladders(
        output_directory=arguments.output,
        executable=arguments.executable,
        build_manifest=arguments.build_manifest,
        processes=arguments.processes,
    )
    print(f"report: {path}")
    print(f"lifecycle: {report['lifecycle']}")
    if report["lifecycle"] == "rejected_diagnostic":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
