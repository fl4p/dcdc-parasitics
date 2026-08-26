#!/usr/bin/env python3

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from palace_convergence import (  # noqa: E402
    ABS_BAND_F,
    MAX_CONTRACTION_RATIO,
    REL_BAND,
    Rung,
    check_convergence_ladder,
    within_band,
)

HELD = {"max_planar_area_m2": 1e-6, "max_vertical_step_m": 1e-4}


def _ladder(traces, sizes=None, held=HELD, **kwargs):
    sizes = sizes or [1e-4 / (2.0 ** (index * 0.5))
                      for index in range(len(traces))]
    return [
        Rung(f"r{index}", size, trace, held=held, **kwargs)
        for index, (size, trace) in enumerate(zip(sizes, traces))
    ]


def _geometric(start, limit, ratio, count):
    """A textbook converging ladder: corrections shrinking by a constant ratio,
    all pointing the same way."""
    return [limit + (start - limit) * ratio ** index for index in range(count)]


def _from_deltas(first, deltas):
    """Traces built from an explicit correction sequence, so a test can fix the
    contraction ratio without the band test also firing."""
    traces = [first]
    for delta in deltas:
        traces.append(traces[-1] + delta)
    return traces


def test_a_geometric_ladder_inside_the_band_converges():
    verdict = check_convergence_ladder(
        _ladder(_geometric(120e-12, 100e-12, 0.05, 5)))
    assert verdict.state == "converged", verdict.reason
    assert abs(verdict.finest_rel) <= REL_BAND
    assert verdict.limit_f == pytest.approx(100e-12, rel=1e-6)


# -- known-bad calibration -------------------------------------------------
#
# Both sequences below are real measurements from the simple-hb canary on
# corrected v3 geometry, not constructions. The old finest-pair rule passed the
# first and the obvious contraction-only fix passes it too, which is why both
# are pinned here.

# 12.9M-tet scaled graded ladder, edge element 100 -> 70.7 -> 50 -> 35.4 um.
OSCILLATING_PF = [99.5917, 105.4212, 102.9801, 104.6627]
# Fixed-band graded ladder, edge element 282.8 -> 200 -> 141.4 -> 100 um.
NON_CONTRACTING_PF = [122.4429, 113.3958, 106.8595, 99.5917]


def test_the_oscillating_canary_ladder_is_refused():
    verdict = check_convergence_ladder(
        _ladder([value * 1e-12 for value in OSCILLATING_PF]))
    assert verdict.state == "not_converged", verdict.reason
    assert "sign" in verdict.reason


def test_the_oscillating_ladder_would_have_passed_the_finest_pair_rule():
    """The defect this module exists to fix: the finest two rungs of a visibly
    ringing sequence land inside the band, so a two-point rule calls it
    converged."""
    traces = [value * 1e-12 for value in OSCILLATING_PF]
    finest = traces[-1] - traces[-2]
    assert within_band(finest, traces[-1])


def test_the_oscillating_ladder_also_contracts_so_contraction_alone_is_not_enough():
    """Its corrections shrink by 0.419 then 0.689 while flipping sign, so a
    contraction test with no sign requirement passes it as well."""
    traces = [value * 1e-12 for value in OSCILLATING_PF]
    deltas = [b - a for a, b in zip(traces, traces[1:])]
    ratios = [abs(b / a) for a, b in zip(deltas, deltas[1:])]
    assert all(ratio < MAX_CONTRACTION_RATIO for ratio in ratios)
    assert len({delta > 0 for delta in deltas}) == 2


def test_the_non_contracting_canary_ladder_is_refused():
    verdict = check_convergence_ladder(
        _ladder([value * 1e-12 for value in NON_CONTRACTING_PF]))
    assert verdict.state == "not_converged", verdict.reason
    assert "contract" in verdict.reason


def test_the_non_contracting_ladder_has_one_sign_so_sign_alone_is_not_enough():
    """Its corrections all point the same way; only the growing magnitude
    gives it away."""
    traces = [value * 1e-12 for value in NON_CONTRACTING_PF]
    deltas = [b - a for a, b in zip(traces, traces[1:])]
    assert len({delta > 0 for delta in deltas}) == 1
    assert abs(deltas[-1]) > abs(deltas[-2])


# -- monotonicity: worse input must not flip back to a pass ----------------


@pytest.mark.parametrize("scale", [1.0, 2.0, 10.0, 1e3, 1e6])
def test_a_ladder_whose_steps_grow_is_refused_at_every_scale(scale):
    traces = [100e-12, 110e-12, 110e-12 + 10e-12 * scale]
    verdict = check_convergence_ladder(_ladder(traces))
    assert verdict.state == "not_converged", verdict.reason


@pytest.mark.parametrize("ratio", [0.5, 0.85, 0.95, 1.05, 1.5])
def test_the_contraction_threshold_is_monotone_in_the_ratio(ratio):
    """Corrections well outside the band contracting at exactly `ratio`, then a
    final one inside it. The band test therefore cannot fire and the verdict
    turns only on the contraction ratio. The exact threshold value is left
    untested on purpose: `abs(nxt) > ratio * abs(prev)` is decided in floating
    point there and pinning it would test round-off, not the rule."""
    trace = 100e-12
    big = 0.10 * trace
    verdict = check_convergence_ladder(_ladder(_from_deltas(
        trace, [-big, -big * ratio, -big * ratio ** 2, -1e-16])))
    if ratio < MAX_CONTRACTION_RATIO:
        assert verdict.state == "converged", verdict.reason
    else:
        assert verdict.state != "converged", verdict.reason


def test_a_ladder_that_does_not_move_at_all_is_converged():
    """The degenerate case: refinement changes nothing, so every correction is
    zero and inside the band. Contraction is undefined here and must not be
    read as a failure."""
    verdict = check_convergence_ladder(_ladder([100e-12] * 4))
    assert verdict.state == "converged", verdict.reason
    assert verdict.deltas_f == (0.0, 0.0, 0.0)


def test_a_settled_ladder_may_jitter_inside_the_band():
    """Once corrections are inside the band the sign and contraction
    requirements are suspended, so round-off-level wobble is not a failure."""
    trace = 100e-12
    verdict = check_convergence_ladder(_ladder(
        [trace, trace + 0.5 * REL_BAND * trace, trace, trace + 1e-16]))
    assert verdict.state == "converged", verdict.reason


# -- unevaluable input must never read as a pass ---------------------------


def test_two_rungs_cannot_be_judged():
    verdict = check_convergence_ladder(_ladder([100e-12, 100e-12]))
    assert verdict.state == "unevaluable"
    assert "three" in verdict.reason


def test_an_unsolved_rung_makes_the_ladder_unevaluable():
    rungs = _ladder([100e-12, 100e-12, 100e-12, 100e-12])
    rungs[1] = rungs[1]._replace(trace_f=None)
    verdict = check_convergence_ladder(rungs)
    assert verdict.state == "unevaluable"
    assert "unsolved" in verdict.reason


def test_a_non_finite_trace_is_unevaluable():
    verdict = check_convergence_ladder(
        _ladder([100e-12, 100e-12, float("nan")]))
    assert verdict.state == "unevaluable"


def test_rungs_that_disagree_on_a_held_parameter_are_not_a_ladder():
    rungs = _ladder(_geometric(120e-12, 100e-12, 0.05, 4))
    rungs[2] = rungs[2]._replace(
        held={**HELD, "max_planar_area_m2": 5e-7})
    verdict = check_convergence_ladder(rungs)
    assert verdict.state == "unevaluable"
    assert "max_planar_area_m2" in verdict.reason


def test_rungs_that_did_not_declare_held_parameters_are_unevaluable():
    verdict = check_convergence_ladder(
        _ladder(_geometric(120e-12, 100e-12, 0.05, 4), held=None))
    assert verdict.state == "unevaluable"


def test_rungs_out_of_coarse_to_fine_order_are_unevaluable():
    traces = _geometric(120e-12, 100e-12, 0.05, 4)
    verdict = check_convergence_ladder(
        _ladder(traces, sizes=[1e-4, 2e-4, 3e-4, 4e-4]))
    assert verdict.state == "unevaluable"
    assert "coarse to fine" in verdict.reason


def test_an_empty_ladder_is_unevaluable():
    assert check_convergence_ladder([]).state == "unevaluable"


# -- matrix health --------------------------------------------------------


def test_a_positive_off_diagonal_fails_even_on_a_converged_sequence():
    rungs = _ladder(_geometric(120e-12, 100e-12, 0.05, 4))
    rungs[1] = rungs[1]._replace(positive_off_diagonals=3)
    verdict = check_convergence_ladder(rungs)
    assert verdict.state == "not_converged"
    assert "unphysical" in verdict.reason


def test_a_reciprocity_breakdown_fails_even_on_a_converged_sequence():
    rungs = _ladder(_geometric(120e-12, 100e-12, 0.05, 4))
    rungs[-1] = rungs[-1]._replace(reciprocity_f=1e-15)
    verdict = check_convergence_ladder(rungs)
    assert verdict.state == "not_converged"


# -- noise floor ----------------------------------------------------------


def test_a_noise_floor_wider_than_the_band_makes_a_pass_unevaluable():
    """If re-meshing at fixed resolution moves the answer more than the band,
    a within-band step cannot be attributed to refinement."""
    verdict = check_convergence_ladder(
        _ladder(_geometric(120e-12, 100e-12, 0.05, 5)), noise_floor_f=3e-12)
    assert verdict.state == "unevaluable"
    assert "noise floor" in verdict.reason


def test_a_noise_floor_inside_the_band_leaves_the_verdict_alone():
    verdict = check_convergence_ladder(
        _ladder(_geometric(120e-12, 100e-12, 0.05, 5)), noise_floor_f=1e-16)
    assert verdict.state == "converged", verdict.reason


@pytest.mark.parametrize("floor", [-1e-15, float("nan"), float("inf")])
def test_a_nonsensical_noise_floor_is_unevaluable(floor):
    verdict = check_convergence_ladder(
        _ladder(_geometric(120e-12, 100e-12, 0.05, 5)), noise_floor_f=floor)
    assert verdict.state == "unevaluable"


# -- reported quantities --------------------------------------------------


def test_the_observed_order_uses_the_real_length_ratio():
    """Areas halve, so lengths shrink by sqrt(2); assuming a halving would
    report twice the true order."""
    sizes = [1e-4 / (2.0 ** (index * 0.5)) for index in range(4)]
    order = 2.0
    traces = [100e-12 + 1e-12 * size ** order / sizes[0] ** order
              for size in sizes]
    verdict = check_convergence_ladder(_ladder(traces, sizes=sizes))
    assert verdict.observed_order == pytest.approx(order, rel=1e-6)


def test_the_band_is_relative_or_absolute():
    assert within_band(0.9 * ABS_BAND_F, 1e-18)
    assert not within_band(2.0 * ABS_BAND_F, 1e-18)
    assert within_band(0.5 * REL_BAND * 100e-12, 100e-12)
    assert not within_band(2.0 * REL_BAND * 100e-12, 100e-12)


def test_a_non_finite_delta_raises_rather_than_reading_as_in_band():
    with pytest.raises(ValueError):
        within_band(float("nan"), 100e-12)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# -- entrywise matrix gate -------------------------------------------------


from palace_convergence import check_matrix_convergence_ladder  # noqa: E402


def _matrices(entries):
    """entries: {(i, j): [value per rung]} -> one symmetric matrix per rung."""
    size = 1 + max(max(pair) for pair in entries)
    count = len(next(iter(entries.values())))
    out = []
    for index in range(count):
        matrix = [[0.0] * size for _ in range(size)]
        for (i, j), values in entries.items():
            matrix[i][j] = matrix[j][i] = values[index]
        out.append(matrix)
    return out


def _converging(limit, start, count):
    return [limit + (start - limit) * 0.05 ** index for index in range(count)]


def test_a_converged_matrix_passes():
    entries = {
        (0, 0): _converging(50e-12, 60e-12, 4),
        (0, 1): _converging(-8e-12, -9e-12, 4),
        (1, 1): _converging(50e-12, 60e-12, 4),
    }
    verdict = check_matrix_convergence_ladder(
        _ladder([sum(v[i] for k, v in entries.items() if k[0] == k[1])
                 for i in range(4)]), _matrices(entries))
    assert verdict.state == "converged", verdict.reason
    assert verdict.entry_count == 3


def test_entries_cancelling_in_a_converged_trace_are_caught():
    """The reason the gate must be entrywise: two couplings drifting in
    opposite directions by 5% a rung leave the trace perfectly still."""
    drift = [50e-12, 52.5e-12, 55e-12, 57.5e-12]
    entries = {
        (0, 0): drift,
        (1, 1): [110e-12 - value for value in drift],
        (0, 1): _converging(-8e-12, -9e-12, 4),
    }
    traces = [matrix[0][0] + matrix[1][1] for matrix in _matrices(entries)]
    # The trace is constant to within round-off while each entry moves 5% a
    # rung, so the trace gate passes it and sees nothing at all.
    assert max(traces) - min(traces) < 1e-25
    rungs = _ladder(traces)
    assert check_convergence_ladder(rungs).state == "converged"

    verdict = check_matrix_convergence_ladder(rungs, _matrices(entries))
    assert verdict.state == "not_converged", verdict.reason
    assert len(verdict.failed_entries) == 2


def test_a_small_coupling_is_not_failed_merely_for_being_small():
    """Sub-fF corrections are inside the absolute half of the band, so the
    trend tests are suspended and a tiny noisy coupling still passes."""
    entries = {
        (0, 0): _converging(50e-12, 60e-12, 4),
        (1, 1): _converging(50e-12, 60e-12, 4),
        (0, 1): [-2e-15, -2.3e-15, -2.1e-15, -2.4e-15],
    }
    traces = [m[0][0] + m[1][1] for m in _matrices(entries)]
    verdict = check_matrix_convergence_ladder(
        _ladder(traces), _matrices(entries))
    assert verdict.state == "converged", verdict.reason


def test_a_mismatched_matrix_count_is_unevaluable():
    rungs = _ladder(_geometric(120e-12, 100e-12, 0.05, 4))
    entries = {(0, 0): _converging(50e-12, 60e-12, 4)}
    verdict = check_matrix_convergence_ladder(rungs, _matrices(entries)[:2])
    assert verdict.state == "unevaluable"


def test_matrices_of_differing_shape_are_unevaluable():
    rungs = _ladder(_geometric(120e-12, 100e-12, 0.05, 3))
    matrices = [[[1e-12]], [[1e-12]], [[1e-12, 0.0], [0.0, 1e-12]]]
    verdict = check_matrix_convergence_ladder(rungs, matrices)
    assert verdict.state == "unevaluable"
    assert "shape" in verdict.reason


def test_empty_matrices_are_unevaluable_not_a_pass():
    rungs = _ladder(_geometric(120e-12, 100e-12, 0.05, 3))
    verdict = check_matrix_convergence_ladder(rungs, [[], [], []])
    assert verdict.state == "unevaluable"


def test_a_non_finite_entry_is_unevaluable():
    rungs = _ladder(_geometric(120e-12, 100e-12, 0.05, 3))
    matrices = _matrices({(0, 0): [1e-12, 1e-12, 1e-12]})
    matrices[1][0][0] = float("nan")
    verdict = check_matrix_convergence_ladder(rungs, matrices)
    assert verdict.state == "unevaluable"


def test_a_ladder_wide_precondition_failure_short_circuits():
    rungs = _ladder(_geometric(120e-12, 100e-12, 0.05, 4))
    rungs[2] = rungs[2]._replace(held={**HELD, "max_planar_area_m2": 5e-7})
    entries = {(0, 0): _converging(50e-12, 60e-12, 4)}
    verdict = check_matrix_convergence_ladder(rungs, _matrices(entries))
    assert verdict.state == "unevaluable"
    assert "max_planar_area_m2" in verdict.reason


def test_entries_too_noisy_to_read_are_unevaluable_not_converged():
    """Measured on the canary: re-meshing at nominally identical resolution
    moves 45 of 171 entries more than the whole band. Refining an entry that
    noisy proves nothing either way."""
    entries = {
        (0, 0): _converging(50e-12, 60e-12, 4),
        (1, 1): _converging(50e-12, 60e-12, 4),
        (0, 1): _converging(-8e-12, -9e-12, 4),
    }
    rungs = _ladder([m[0][0] + m[1][1] for m in _matrices(entries)])
    matrices = _matrices(entries)
    assert check_matrix_convergence_ladder(
        rungs, matrices).state == "converged"

    floors = [[0.0, 0.0], [0.0, 0.0]]
    floors[0][1] = floors[1][0] = 0.6 * abs(entries[(0, 1)][-1])
    verdict = check_matrix_convergence_ladder(
        rungs, matrices, entry_noise_floors=floors)
    assert verdict.state == "unevaluable", verdict.reason
    assert "regenerated at the same resolution" in verdict.reason


def test_a_quiet_entry_noise_floor_leaves_the_verdict_alone():
    entries = {
        (0, 0): _converging(50e-12, 60e-12, 4),
        (1, 1): _converging(50e-12, 60e-12, 4),
        (0, 1): _converging(-8e-12, -9e-12, 4),
    }
    rungs = _ladder([m[0][0] + m[1][1] for m in _matrices(entries)])
    floors = [[1e-16, 1e-16], [1e-16, 1e-16]]
    verdict = check_matrix_convergence_ladder(
        rungs, _matrices(entries), entry_noise_floors=floors)
    assert verdict.state == "converged", verdict.reason


def test_noise_floors_of_the_wrong_shape_are_unevaluable():
    entries = {(0, 0): _converging(50e-12, 60e-12, 4)}
    rungs = _ladder([m[0][0] for m in _matrices(entries)])
    verdict = check_matrix_convergence_ladder(
        rungs, _matrices(entries), entry_noise_floors=[[0.0, 0.0], [0.0, 0.0]])
    assert verdict.state == "unevaluable"
    assert "shape" in verdict.reason


@pytest.mark.parametrize("bad", [-1e-15, float("nan"), float("inf")])
def test_a_nonsensical_entry_noise_floor_is_unevaluable(bad):
    entries = {(0, 0): _converging(50e-12, 60e-12, 4)}
    rungs = _ladder([m[0][0] for m in _matrices(entries)])
    verdict = check_matrix_convergence_ladder(
        rungs, _matrices(entries), entry_noise_floors=[[bad]])
    assert verdict.state == "unevaluable"


def test_a_noisy_entry_does_not_mask_a_failure_elsewhere():
    """The unevaluable entries are reported, but the entries that did fail the
    trend are still carried on the verdict rather than discarded."""
    entries = {
        (0, 0): [50e-12, 55e-12, 60e-12, 65e-12],
        (1, 1): _converging(50e-12, 60e-12, 4),
        (0, 1): _converging(-8e-12, -9e-12, 4),
    }
    rungs = _ladder([m[0][0] + m[1][1] for m in _matrices(entries)])
    floors = [[0.0, 0.0], [0.0, 0.0]]
    floors[0][1] = floors[1][0] = 0.6 * abs(entries[(0, 1)][-1])
    verdict = check_matrix_convergence_ladder(
        rungs, _matrices(entries), entry_noise_floors=floors)
    assert verdict.state == "unevaluable"
    assert (0, 0) in [pair for pair, _ in verdict.failed_entries]
