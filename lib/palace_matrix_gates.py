#!/usr/bin/env python3
"""Qualification gates for Palace electrostatic matrices."""
import numpy as np

if __package__:
    from .maxwell import (
        maxwell_gate_results,
        maxwell_to_branches,
        raw_maxwell_gate_results,
    )
else:
    from maxwell import (
        maxwell_gate_results,
        maxwell_to_branches,
        raw_maxwell_gate_results,
    )


def matrix_gate_failures(raw_matrix, downstream_matrix):
    failures = []
    raw_results = raw_maxwell_gate_results(raw_matrix)
    failures.extend(
        f"raw_matrix_gate: {name}"
        for name, result in raw_results.items() if not result["passed"]
    )
    if not np.array_equal(downstream_matrix.values, downstream_matrix.values.T):
        failures.append("standard Palace matrix is not exactly symmetric")
    if not np.allclose(
            downstream_matrix.values, raw_matrix.values, rtol=1e-3, atol=1e-18):
        failures.append(
            "standard energy matrix disagrees with independent reaction-charge matrix"
        )
    downstream_results = maxwell_gate_results(
        downstream_matrix, rtol=0.0, atol=0.0
    )
    failures.extend(
        f"downstream_matrix_gate: {name}"
        for name, result in downstream_results.items() if not result["passed"]
    )
    try:
        maxwell_to_branches(
            downstream_matrix, reference_node="electrostatic_infinity"
        )
    except ValueError as error:
        failures.append(f"matrix_validation: {error}")
    return failures
