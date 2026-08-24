#!/usr/bin/env python3
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from maxwell import (  # noqa: E402
    CapacitorBranch,
    MaxwellMatrix,
    _exact_reference_branches,
    aggregate_ideal_shorts,
    branches_to_maxwell,
    entrywise_convergence_gate,
    maxwell_to_branches,
    spice_subcircuit,
    symmetrize_maxwell,
    validate_maxwell,
)


def test_ideal_short_aggregation_is_exact_and_passive():
    base = MaxwellMatrix(
        ("a1", "a2", "b"),
        1e-12 * np.array([
            [4.0, -1.0, -2.0],
            [-1.0, 3.0, -1.0],
            [-2.0, -1.0, 5.0],
        ]),
    )
    grouped = aggregate_ideal_shorts(
        base, {"a1": "a", "a2": "a", "b": "b"}, ("a", "b")
    )
    np.testing.assert_allclose(
        grouped.values,
        1e-12 * np.array([[5.0, -3.0], [-3.0, 5.0]]),
        rtol=0.0,
        atol=1e-27,
    )
    validate_maxwell(grouped, rtol=0.0, atol=0.0)
    with pytest.raises(ValueError, match="cover every conductor"):
        aggregate_ideal_shorts(base, {"a1": "a"}, ("a",))
    with pytest.raises(ValueError, match="unknown groups"):
        aggregate_ideal_shorts(
            base, {"a1": "a", "a2": "a", "b": "missing"}, ("a", "b")
        )


def test_entrywise_convergence_gate_uses_named_mixed_tolerance():
    left = MaxwellMatrix(("A", "B"), [[2e-12, -1e-12], [-1e-12, 3e-12]])
    within = MaxwellMatrix(("A", "B"), [[2.03e-12, -1.01e-12], [-1.01e-12, 3e-12]])
    outside = MaxwellMatrix(("A", "B"), [[2.1e-12, -1e-12], [-1e-12, 3e-12]])
    assert entrywise_convergence_gate(left, within)["passed"]
    assert not entrywise_convergence_gate(
        left, outside, atol=0.0, rtol=0.02
    )["passed"]
    with pytest.raises(ValueError, match="identical conductor ordering"):
        entrywise_convergence_gate(
            left, MaxwellMatrix(("B", "A"), within.values)
        )
    with pytest.raises(ValueError, match="finite and non-negative"):
        entrywise_convergence_gate(left, within, rtol=float("nan"))


def _two_conductor_matrix():
    pair = 2e-12
    a_to_ref = 1e-12
    b_to_ref = 3e-12
    return MaxwellMatrix(
        ("SW", "CHASSIS"),
        np.array([
            [pair + a_to_ref, -pair],
            [-pair, pair + b_to_ref],
        ]),
    )


def test_maxwell_branch_conversion_reconstructs_matrix():
    matrix = _two_conductor_matrix()
    branches = maxwell_to_branches(matrix)
    reconstructed = branches_to_maxwell(matrix.names, branches)
    np.testing.assert_array_equal(reconstructed.values, matrix.values)
    branch_values = {
        (branch.node_a, branch.node_b): branch.farads for branch in branches
    }
    assert branch_values == pytest.approx({
        ("SW", "CHASSIS"): 2e-12,
        ("SW", "0"): 1e-12,
        ("CHASSIS", "0"): 3e-12,
    })


def test_fastercap_scale_reciprocity_error_is_symmetrized():
    matrix = MaxwellMatrix(
        ("plate_low", "plate_high"),
        np.array([[2.88532e-12, -2.45165e-12],
                  [-2.45277e-12, 2.88557e-12]]),
    )
    symmetric = symmetrize_maxwell(matrix)
    np.testing.assert_array_equal(symmetric.values, symmetric.values.T)
    assert symmetric.values[0, 1] == pytest.approx(-2.45221e-12)


@pytest.mark.parametrize("values", [
    [[1.0, -0.004], [-0.0039, 1.0]],
    [[1.0, -0.009], [-0.000001, 1.0]],
])
def test_nonreciprocal_couplings_are_rejected(values):
    with pytest.raises(ValueError, match="not reciprocal"):
        symmetrize_maxwell(MaxwellMatrix(("A", "B"), values))


def test_raw_row_sum_gate_runs_before_reciprocity_averaging():
    matrix = MaxwellMatrix(
        ("A", "B"),
        [[1.000998e-12, -1.001e-12], [-1.000e-12, 1.1e-12]],
    )
    with pytest.raises(ValueError, match="raw Maxwell.*negative capacitance"):
        symmetrize_maxwell(matrix)


def test_text_rounding_asymmetry_is_accepted_and_symmetrized():
    matrix = MaxwellMatrix(
        ("A", "B"),
        np.array([[8.26757e-11, -2.73819e-11],
                  [-2.73807e-11, 8.26804e-11]]),
    )
    validate_maxwell(matrix)
    symmetric = symmetrize_maxwell(matrix)
    branches = maxwell_to_branches(symmetric)
    pair = next(branch for branch in branches if branch.node_b == "B")
    assert pair.farads == pytest.approx(2.73813e-11)


@pytest.mark.parametrize("values, message", [
    ([[1.0, -0.2], [-0.4, 1.0]], "not symmetric"),
    ([[1.0, 0.1], [0.1, 1.0]], "positive off-diagonal"),
    ([[1.0, -1.2], [-1.2, 1.0]], "negative capacitance to reference"),
])
def test_invalid_maxwell_matrices_are_rejected(values, message):
    with pytest.raises(ValueError, match=message):
        validate_maxwell(MaxwellMatrix(("A", "B"), values))


@pytest.mark.parametrize("values", [
    [[1.0, 1e-20], [1e-20, 1.0]],
    [[1.0, -1.000000000000001], [-1.000000000000001, 1.0]],
])
def test_branch_conversion_does_not_clip_tiny_passivity_violations(values):
    with pytest.raises(ValueError):
        maxwell_to_branches(MaxwellMatrix(("A", "B"), values))


@pytest.mark.parametrize("values", [
    [[1.03410509941043762e-13, -3.50559161513152861e-14],
     [-3.50559161513152861e-14, 1.09749990115516135e-13]],
    [[9.57833329581417113e-14, -3.22505903772622417e-14],
     [-3.22505903772622417e-14, 1.02109016906418289e-13]],
])
def test_exact_parallel_reference_branches_reconstruct_roundoff_cases(values):
    matrix = MaxwellMatrix(("A", "B"), values)
    branches = maxwell_to_branches(matrix)
    reconstructed = branches_to_maxwell(matrix.names, branches)
    np.testing.assert_array_equal(reconstructed.values, matrix.values)
    reference = [branch for branch in branches if branch.node_b == "0"]
    assert 2 <= len(reference) <= 32
    assert all(np.isfinite(branch.farads) and branch.farads > 0.0
               for branch in reference)


def test_exact_branch_conversion_handles_zero_reference_and_multiple_conductors():
    zero_reference = MaxwellMatrix(("A", "B"), [[1.0, -1.0], [-1.0, 1.0]])
    assert all(branch.node_b != "0" for branch in maxwell_to_branches(zero_reference))
    source = (
        CapacitorBranch("C", "A", 0.137),
        CapacitorBranch("B", "C", 0.073),
        CapacitorBranch("A", "B", 0.219),
        CapacitorBranch("C", "0", 0.011),
        CapacitorBranch("A", "0", 0.031),
        CapacitorBranch("B", "0", 0.017),
    )
    matrix = branches_to_maxwell(("A", "B", "C"), source)
    rebuilt = maxwell_to_branches(matrix)
    np.testing.assert_array_equal(
        branches_to_maxwell(matrix.names, rebuilt).values, matrix.values
    )
    for name in matrix.names:
        references = [
            branch for branch in rebuilt
            if branch.node_a == name and branch.node_b == "0"
        ]
        assert len(references) <= 16


def test_exact_reference_branch_search_fails_closed():
    with pytest.raises(ValueError, match="cannot exactly reconstruct"):
        _exact_reference_branches(1.0, 2.0)


def test_spice_subcircuit_emits_parallel_reference_roundoff_branches():
    matrix = MaxwellMatrix(
        ("A", "B"),
        [[1.03410509941043762e-13, -3.50559161513152861e-14],
         [-3.50559161513152861e-14, 1.09749990115516135e-13]],
    )
    text = spice_subcircuit(matrix)
    assert text.count(" A 0 ") == 2
    assert text.count(" B 0 ") == 2


def test_spice_subcircuit_preserves_all_branches():
    text = spice_subcircuit(_two_conductor_matrix(), subckt_name="fugu_cm")
    assert ".SUBCKT fugu_cm SW CHASSIS" in text
    assert "C1 SW CHASSIS 2e-12" in text
    assert "C2 SW 0 1.0000000000000002e-12" in text
    assert "C3 CHASSIS 0 3.0000000000000005e-12" in text
    assert text.endswith(".ENDS fugu_cm\n")


@pytest.mark.parametrize("names", [
    ("PE-strap", "PE/strap"),
    ("chassis", "CHASSIS"),
])
def test_spice_name_collision_is_rejected(names):
    matrix = MaxwellMatrix(names, np.eye(2))
    with pytest.raises(ValueError, match="collide"):
        spice_subcircuit(matrix)


def test_custom_spice_reference_is_exposed_as_a_port():
    text = spice_subcircuit(_two_conductor_matrix(), reference_node="return-path")
    assert ".SUBCKT maxwell_capacitance SW CHASSIS return_path" in text
    assert "C2 SW return_path 1.0000000000000002e-12" in text
    assert "C3 CHASSIS return_path 3.0000000000000005e-12" in text


def test_empty_conductor_name_is_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        MaxwellMatrix(("A", ""), np.eye(2))


def test_reference_node_cannot_alias_a_conductor():
    matrix = _two_conductor_matrix()
    with pytest.raises(ValueError, match="reference node"):
        maxwell_to_branches(matrix, reference_node="SW")
    with pytest.raises(ValueError, match="reference node"):
        branches_to_maxwell(matrix.names, (), reference_node="SW")
