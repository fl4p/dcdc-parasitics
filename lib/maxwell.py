#!/usr/bin/env python3
"""Validate and convert Maxwell capacitance matrices.

A Maxwell matrix satisfies ``Q = C @ V``. Its diagonal entries are positive,
off-diagonal entries are non-positive, and each row sum is the capacitance from
that conductor to the reference at infinity. Pairwise SPICE capacitors are
therefore ``-C[i, j]`` and reference capacitors are the row sums.
"""
from dataclasses import dataclass
import math
import re

import numpy as np


@dataclass(frozen=True)
class MaxwellMatrix:
    names: tuple[str, ...]
    values: np.ndarray

    def __post_init__(self):
        names = tuple(str(name) for name in self.names)
        values = np.array(self.values, dtype=float, copy=True)
        if values.ndim != 2 or values.shape[0] != values.shape[1]:
            raise ValueError("Maxwell matrix must be square")
        if values.shape[0] != len(names):
            raise ValueError("conductor-name count does not match matrix dimension")
        if (not names or any(not name for name in names)
                or len(set(names)) != len(names)):
            raise ValueError("conductor names must be non-empty and unique")
        values.setflags(write=False)
        object.__setattr__(self, "names", names)
        object.__setattr__(self, "values", values)


@dataclass(frozen=True)
class CapacitorBranch:
    node_a: str
    node_b: str
    farads: float


def aggregate_ideal_shorts(matrix, assignments, group_order):
    """Aggregate base conductors connected by explicit ideal shorts."""
    assignments = dict(assignments)
    group_order = tuple(str(name) for name in group_order)
    if (not group_order or len(set(group_order)) != len(group_order)
            or any(not name for name in group_order)):
        raise ValueError("ideal-short group names must be non-empty and unique")
    if set(assignments) != set(matrix.names):
        raise ValueError("ideal-short assignments must cover every conductor")
    unknown = set(assignments.values()) - set(group_order)
    if unknown:
        raise ValueError(f"ideal-short assignments contain unknown groups: {unknown}")
    incidence = np.zeros((len(matrix.names), len(group_order)), dtype=float)
    group_indices = {name: index for index, name in enumerate(group_order)}
    for row, name in enumerate(matrix.names):
        incidence[row, group_indices[assignments[name]]] = 1.0
    values = incidence.T @ matrix.values @ incidence
    return MaxwellMatrix(group_order, values)


def _matrix_tolerance(matrix, rtol, atol):
    scale = float(np.max(np.abs(matrix))) if matrix.size else 0.0
    return max(float(atol), float(rtol) * scale)


def entrywise_convergence_gate(left, right, *, atol=1e-15, rtol=0.02):
    """Compare two named raw matrices using a per-entry mixed tolerance."""
    if left.names != right.names:
        raise ValueError("convergence matrices must have identical conductor ordering")
    if (isinstance(atol, bool) or isinstance(rtol, bool)
            or not isinstance(atol, (int, float))
            or not isinstance(rtol, (int, float))):
        raise ValueError("convergence tolerances must be finite and non-negative")
    atol = float(atol)
    rtol = float(rtol)
    if not math.isfinite(atol) or not math.isfinite(rtol) or atol < 0.0 or rtol < 0.0:
        raise ValueError("convergence tolerances must be finite and non-negative")
    finite = bool(np.isfinite(left.values).all() and np.isfinite(right.values).all())
    allowance = atol + rtol * np.maximum(np.abs(left.values), np.abs(right.values))
    difference = np.abs(right.values - left.values)
    return {
        "passed": finite and bool(np.all(difference <= allowance)),
        "finite": finite,
        "max_difference_f": float(np.max(difference)) if finite else float("inf"),
        "max_allowance_f": float(np.max(allowance)) if finite else float("nan"),
        "difference_f": difference.tolist(),
        "allowance_f": allowance.tolist(),
    }


def maxwell_gate_results(matrix, *, rtol=1e-4, atol=0.0):
    """Return machine-readable gates for a symmetric downstream matrix."""
    values = matrix.values
    finite = bool(np.isfinite(values).all())
    tolerance = _matrix_tolerance(values, rtol, atol) if finite else float("nan")
    symmetry_error = (
        float(np.max(np.abs(values - values.T))) if finite else float("inf")
    )
    symmetric = 0.5 * (values + values.T) if finite else values
    diagonal_minimum = (
        float(np.min(np.diag(symmetric))) if finite else float("nan")
    )
    off_diagonal = symmetric.copy()
    if finite:
        np.fill_diagonal(off_diagonal, 0.0)
    off_diagonal_maximum = (
        float(np.max(off_diagonal)) if finite else float("nan")
    )
    row_sum_minimum = (
        float(np.min(symmetric.sum(axis=1))) if finite else float("nan")
    )
    eigenvalue_minimum = (
        float(np.linalg.eigvalsh(symmetric).min()) if finite else float("nan")
    )
    return {
        "finite": {"passed": finite, "value": finite},
        "symmetry": {
            "passed": finite and symmetry_error <= tolerance,
            "error": symmetry_error,
            "tolerance": tolerance,
        },
        "diagonal": {
            "passed": finite and diagonal_minimum > 0.0,
            "minimum": diagonal_minimum,
        },
        "off_diagonal": {
            "passed": finite and off_diagonal_maximum <= tolerance,
            "maximum": off_diagonal_maximum,
            "tolerance": tolerance,
        },
        "row_sum": {
            "passed": finite and row_sum_minimum >= -tolerance,
            "minimum": row_sum_minimum,
            "tolerance": tolerance,
        },
        "positive_semidefinite": {
            "passed": finite and eigenvalue_minimum >= -tolerance,
            "minimum_eigenvalue": eigenvalue_minimum,
            "tolerance": tolerance,
        },
    }


def _raise_failed_maxwell_gate(results):
    messages = {
        "finite": "Maxwell matrix contains non-finite values",
        "symmetry": "Maxwell matrix is not symmetric",
        "diagonal": "Maxwell matrix diagonal must be positive",
        "off_diagonal": "Maxwell matrix has a positive off-diagonal entry",
        "row_sum": "Maxwell matrix has a negative capacitance to reference",
        "positive_semidefinite": "Maxwell matrix is not positive semidefinite",
    }
    for name, result in results.items():
        if not result["passed"]:
            raise ValueError(messages[name])


def validate_maxwell(matrix, *, rtol=1e-4, atol=0.0):
    """Raise ``ValueError`` unless ``matrix`` is a passive Maxwell matrix."""
    _raise_failed_maxwell_gate(maxwell_gate_results(matrix, rtol=rtol, atol=atol))
    return matrix


def raw_maxwell_gate_results(matrix, *, reciprocity_rtol=1e-3,
                             reciprocity_atol=1e-18):
    """Return the fixed-policy gates for an unsymmetrized solver matrix."""
    values = matrix.values
    finite = bool(np.isfinite(values).all())
    diagonal_minimum = (
        float(np.min(np.diag(values))) if finite else float("nan")
    )
    off_diagonal = values.copy()
    if finite:
        np.fill_diagonal(off_diagonal, 0.0)
    off_diagonal_maximum = (
        float(np.max(off_diagonal)) if finite else float("nan")
    )
    row_sum_minimum = (
        float(np.min(values.sum(axis=1))) if finite else float("nan")
    )
    symmetric = 0.5 * (values + values.T) if finite else values
    eigenvalue_minimum = (
        float(np.linalg.eigvalsh(symmetric).min()) if finite else float("nan")
    )
    eigenvalue_tolerance = (
        max(reciprocity_atol, 1e-6 * float(np.max(np.diag(values))))
        if finite else float("nan")
    )
    couplings = []
    for row in range(len(matrix.names)):
        for column in range(row + 1, len(matrix.names)):
            forward = float(values[row, column])
            reverse = float(values[column, row])
            tolerance = reciprocity_atol + reciprocity_rtol * max(
                abs(forward), abs(reverse)
            )
            error = abs(forward - reverse)
            couplings.append({
                "row": matrix.names[row],
                "column": matrix.names[column],
                "error": error,
                "tolerance": tolerance,
                "passed": finite and error <= tolerance,
            })
    return {
        "finite": {"passed": finite, "value": finite},
        "diagonal": {
            "passed": finite and diagonal_minimum > 0.0,
            "minimum": diagonal_minimum,
        },
        "off_diagonal": {
            "passed": finite and off_diagonal_maximum <= reciprocity_atol,
            "maximum": off_diagonal_maximum,
            "tolerance": reciprocity_atol,
        },
        "row_sum": {
            "passed": finite and row_sum_minimum >= -reciprocity_atol,
            "minimum": row_sum_minimum,
            "tolerance": reciprocity_atol,
        },
        "positive_semidefinite": {
            "passed": finite and eigenvalue_minimum >= -eigenvalue_tolerance,
            "minimum_eigenvalue": eigenvalue_minimum,
            "tolerance": eigenvalue_tolerance,
        },
        "reciprocity": {
            "passed": all(item["passed"] for item in couplings),
            "couplings": couplings,
        },
    }


def symmetrize_maxwell(matrix, *, reciprocity_rtol=1e-3,
                       reciprocity_atol=1e-18):
    """Validate the raw matrix, average reciprocity noise, and validate strictly."""
    reciprocity_rtol = float(reciprocity_rtol)
    reciprocity_atol = float(reciprocity_atol)
    if (not np.isfinite(reciprocity_rtol) or reciprocity_rtol < 0.0
            or not np.isfinite(reciprocity_atol) or reciprocity_atol < 0.0):
        raise ValueError("reciprocity tolerances must be finite and non-negative")
    results = raw_maxwell_gate_results(
        matrix,
        reciprocity_rtol=reciprocity_rtol,
        reciprocity_atol=reciprocity_atol,
    )
    messages = {
        "finite": "Maxwell matrix contains non-finite values",
        "diagonal": "Maxwell matrix diagonal must be positive",
        "off_diagonal": "raw Maxwell matrix has a positive off-diagonal entry",
        "row_sum": "raw Maxwell matrix has a negative capacitance to reference",
        "positive_semidefinite": "raw Maxwell matrix is not positive semidefinite",
    }
    for name, message in messages.items():
        if not results[name]["passed"]:
            raise ValueError(message)
    if not results["reciprocity"]["passed"]:
        coupling = next(
            item for item in results["reciprocity"]["couplings"]
            if not item["passed"]
        )
        raise ValueError(
            "Maxwell coupling is not reciprocal for "
            f"{coupling['row']!r}/{coupling['column']!r}: "
            f"error {coupling['error']:g} > {coupling['tolerance']:g}"
        )
    values = matrix.values
    symmetric = MaxwellMatrix(matrix.names, 0.5 * (values + values.T))
    validate_maxwell(symmetric, rtol=0.0, atol=0.0)
    return symmetric


def _exact_reference_branches(target, accumulated, *, maximum=16):
    branches = []
    for _ in range(maximum):
        if accumulated == target:
            return branches
        if accumulated > target:
            break
        candidate = target - accumulated
        for _ in range(maximum):
            if accumulated + candidate <= target:
                break
            candidate = math.nextafter(candidate, 0.0)
        updated = accumulated + candidate
        if not math.isfinite(candidate) or candidate <= 0.0 or updated <= accumulated:
            break
        branches.append(candidate)
        accumulated = updated
    raise ValueError("nonnegative branches cannot exactly reconstruct Maxwell diagonal")


def maxwell_to_branches(matrix, *, reference_node="0"):
    """Return an exact passive branch representation of ``matrix``."""
    validate_maxwell(matrix, rtol=0.0, atol=0.0)
    if reference_node in matrix.names:
        raise ValueError("reference node must not be a matrix conductor")
    values = matrix.values
    branches = []
    accumulated = np.zeros(len(matrix.names), dtype=float)
    for row, name_a in enumerate(matrix.names):
        for col in range(row + 1, len(matrix.names)):
            capacitance = -float(values[row, col])
            if capacitance > 0.0:
                branches.append(CapacitorBranch(
                    name_a, matrix.names[col], capacitance
                ))
                accumulated[row] += capacitance
                accumulated[col] += capacitance

    for row, name in enumerate(matrix.names):
        for capacitance in _exact_reference_branches(
                float(values[row, row]), float(accumulated[row])):
            branches.append(CapacitorBranch(name, reference_node, capacitance))
    reconstructed = branches_to_maxwell(
        matrix.names, branches, reference_node=reference_node
    )
    if not np.array_equal(reconstructed.values, matrix.values):
        raise ValueError("capacitor branches do not exactly reconstruct Maxwell matrix")
    return branches


def branches_to_maxwell(names, branches, *, reference_node="0"):
    """Reconstruct a Maxwell matrix from pairwise capacitor branches."""
    names = tuple(names)
    if reference_node in names:
        raise ValueError("reference node must not be a matrix conductor")
    index = {name: idx for idx, name in enumerate(names)}
    if len(index) != len(names):
        raise ValueError("conductor names must be unique")
    values = np.zeros((len(names), len(names)), dtype=float)
    for branch in branches:
        capacitance = float(branch.farads)
        if not np.isfinite(capacitance) or capacitance < 0.0:
            raise ValueError("branch capacitance must be finite and non-negative")
        a_ref = branch.node_a == reference_node
        b_ref = branch.node_b == reference_node
        if a_ref and b_ref:
            raise ValueError("capacitor branch cannot connect reference to itself")
        if a_ref or b_ref:
            node = branch.node_b if a_ref else branch.node_a
            if node not in index:
                raise ValueError(f"unknown conductor {node!r}")
            values[index[node], index[node]] += capacitance
            continue
        if branch.node_a not in index or branch.node_b not in index:
            raise ValueError("branch references an unknown conductor")
        i = index[branch.node_a]
        j = index[branch.node_b]
        if i == j:
            raise ValueError("capacitor branch cannot connect a conductor to itself")
        values[i, i] += capacitance
        values[j, j] += capacitance
        values[i, j] -= capacitance
        values[j, i] -= capacitance
    return MaxwellMatrix(names, values)


def _spice_identifier(name):
    identifier = re.sub(r"[^A-Za-z0-9_]", "_", str(name))
    if not identifier or identifier[0].isdigit():
        identifier = f"n_{identifier}"
    return identifier


def spice_subcircuit(matrix, *, subckt_name="maxwell_capacitance",
                     reference_node="0"):
    """Render the matrix as a passive pairwise-capacitor SPICE subcircuit."""
    nodes = tuple(_spice_identifier(name) for name in matrix.names)
    reference_port = None if reference_node == "0" else _spice_identifier(reference_node)
    all_nodes = nodes + (() if reference_port is None else (reference_port,))
    if len({node.lower() for node in all_nodes}) != len(all_nodes):
        raise ValueError("node names collide after SPICE sanitization")
    subckt_name = _spice_identifier(subckt_name)
    node_map = dict(zip(matrix.names, nodes))
    lines = ["* Maxwell capacitance matrix: Q = C V"]
    for name, node in node_map.items():
        if name != node:
            lines.append(f"* conductor {name} -> {node}")
    ports = " ".join(all_nodes)
    lines.append(f".SUBCKT {subckt_name} {ports}")
    spice_reference = "0" if reference_port is None else reference_port
    for number, branch in enumerate(maxwell_to_branches(
            matrix, reference_node=reference_node), 1):
        node_a = spice_reference if branch.node_a == reference_node else node_map[branch.node_a]
        node_b = spice_reference if branch.node_b == reference_node else node_map[branch.node_b]
        lines.append(f"C{number} {node_a} {node_b} {branch.farads:.17g}")
    lines.append(f".ENDS {subckt_name}")
    return "\n".join(lines) + "\n"
