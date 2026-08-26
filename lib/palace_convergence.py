"""Acceptance for a mesh convergence ladder.

Every Palace mesh this project builds carries `convergence_ladder_required:
True` in its reference semantics, because a finite outer Dirichlet box only
approximates open space and the approximation is only trustworthy once
refinement stops moving the answer. That marker declares the obligation but
nothing in the library discharged it: the rule lived as a copy-pasted "is the
finest pair within 2% + 1 fF" in each campaign script, and that rule is wrong.

It is wrong because it reads two points out of a sequence. A ladder that
oscillates passes it the moment two adjacent rungs happen to land close
together, which is luck, not convergence. The canary produced exactly that --
99.5917, 105.4212, 102.9801, 104.6627 pF -- and the finest pair came in at
1.61%, inside the band, on a sequence that is visibly ringing rather than
settling.

Contraction alone does not save it. That same oscillating ladder has |delta|
ratios of 0.419 and 0.689, both comfortably below any sensible contraction
threshold, because a sequence bouncing around a centre with decreasing
amplitude contracts beautifully while telling you nothing about where it is
going. What separates it from a converging sequence is the *sign*: a
discretisation error that is being driven out approaches its limit from one
side, so the corrections keep pointing the same way. The two tests are only
sufficient together, and neither is redundant:

  - sign consistency alone passes a monotone sequence whose steps are growing
    (the canary's fixed-band ladder: -9047.04, -6536.29, -7267.88 fF);
  - contraction alone passes the oscillating one above.

Both requirements are suspended for a delta that is already inside the
acceptance band, since a converged sequence is entitled to jitter about its
limit at the level the band tolerates.

The gate is fail-closed and tri-state. A ladder that cannot be judged -- too
few solved rungs, a rung that varied more than one axis, a noise floor wider
than the band -- reports `unevaluable` rather than passing, because "we could
not tell" and "it converged" must never be the same answer.
"""
import math
from collections import namedtuple

# The acceptance band. A rung-to-rung change is inside it if it is small in
# relative OR absolute terms: 1 fF of movement on a small coupling should not
# fail merely because the coupling itself is small.
REL_BAND = 0.02
ABS_BAND_F = 1e-15
# How fast successive corrections must shrink. A pure h-refinement of order p
# with length ratio r contracts by r**-p; at the sqrt(2) area-halving step this
# ladder uses, even first order predicts 0.707. 0.9 leaves room for a real
# ladder to be untidy without admitting one whose steps are flat or growing.
MAX_CONTRACTION_RATIO = 0.9
# Reciprocity slack. The matrix is assembled symmetrically, so anything above
# this is a solver or extraction defect rather than round-off.
MAX_RECIPROCITY_F = 1e-18

#: One solved (or unsolved) step of a ladder, coarse to fine.
#:
#: element_size_m -- the refinement length being driven, which must strictly
#:   decrease along the ladder.
#: held -- every other mesh parameter, which must be identical across rungs;
#:   this is what makes the ladder a one-axis ladder rather than a walk.
#: trace_f -- None when the rung has not been solved.
Rung = namedtuple(
    "Rung",
    "label element_size_m trace_f positive_off_diagonals reciprocity_f held")
Rung.__new__.__defaults__ = (None, 0, 0.0, None)

LadderVerdict = namedtuple(
    "LadderVerdict",
    "state reason deltas_f contraction_ratios finest_rel finest_abs_f "
    "observed_order limit_f")


def within_band(delta_f, trace_f):
    """Is a rung-to-rung change inside the acceptance band?"""
    if not math.isfinite(delta_f) or not math.isfinite(trace_f):
        raise ValueError("convergence ladder delta or trace is not finite")
    if trace_f == 0.0:
        return abs(delta_f) <= ABS_BAND_F
    return abs(delta_f) <= ABS_BAND_F or abs(delta_f / trace_f) <= REL_BAND


def _unevaluable(reason):
    return LadderVerdict("unevaluable", reason, (), (), None, None, None, None)


def _check_ladder_shape(rungs):
    """Reasons this sequence is not a one-axis ladder at all, or None."""
    if len(rungs) < 2:
        return "a ladder needs at least two rungs to have a step"
    held = [rung.held for rung in rungs]
    if any(item is None for item in held):
        return ("rungs did not declare their held mesh parameters, so whether "
                "this is a one-axis ladder is unknown")
    if any(item != held[0] for item in held[1:]):
        differing = sorted({
            key for item in held[1:] for key in set(item) | set(held[0])
            if item.get(key) != held[0].get(key)})
        return (f"rungs disagree on held mesh parameters {differing}: more "
                f"than one axis varied, so the sequence is not a ladder and "
                f"its trend is not interpretable")
    sizes = [rung.element_size_m for rung in rungs]
    if any(not isinstance(size, (int, float)) or isinstance(size, bool)
           or not math.isfinite(size) or size <= 0.0 for size in sizes):
        return "rung element sizes are not positive finite lengths"
    if any(fine >= coarse for coarse, fine in zip(sizes, sizes[1:])):
        return ("rung element sizes do not strictly decrease, so the sequence "
                "is not ordered coarse to fine")
    return None


def check_convergence_ladder(rungs, noise_floor_f=None):
    """Judge a coarse-to-fine ladder, fail-closed and tri-state.

    Returns a LadderVerdict whose state is "converged", "not_converged", or
    "unevaluable". Only "converged" is a pass; callers must not treat
    "unevaluable" as one.
    """
    rungs = list(rungs)
    shape_problem = _check_ladder_shape(rungs)
    if shape_problem is not None:
        return _unevaluable(shape_problem)

    solved = [rung for rung in rungs if rung.trace_f is not None]
    if len(solved) != len(rungs):
        missing = [rung.label for rung in rungs if rung.trace_f is None]
        # An unsolved rung is not a rung that can be skipped: dropping it joins
        # two non-adjacent refinements into one apparent step and silently
        # changes what the deltas mean.
        return _unevaluable(f"rungs {missing} are unsolved")
    if any(not math.isfinite(rung.trace_f) for rung in solved):
        return _unevaluable("a rung produced a non-finite trace")
    # Three rungs is the minimum that carries any information about whether
    # the corrections are shrinking. Two rungs give one delta and nothing to
    # compare it against, which is precisely how the old rule passed noise.
    if len(solved) < 3:
        return _unevaluable(
            f"{len(solved)} solved rungs cannot show a trend; at least three "
            f"are needed to compare successive corrections")

    unhealthy = [rung.label for rung in solved
                 if rung.positive_off_diagonals
                 or rung.reciprocity_f > MAX_RECIPROCITY_F]
    if unhealthy:
        # Checked before the trend, deliberately: a sequence of unphysical
        # matrices can converge perfectly well onto an unphysical answer.
        return LadderVerdict(
            "not_converged",
            f"rungs {unhealthy} have positive off-diagonals or fail "
            f"reciprocity; a converged number from an unphysical matrix is "
            f"not a pass", (), (), None, None, None, None)

    traces = [rung.trace_f for rung in solved]
    deltas = tuple(fine - coarse for coarse, fine in zip(traces, traces[1:]))
    ratios = tuple(
        abs(nxt / prev) if prev else math.inf
        for prev, nxt in zip(deltas, deltas[1:]))
    finest_abs = deltas[-1]
    finest_rel = finest_abs / traces[-1] if traces[-1] else math.inf
    order = _observed_order(solved, deltas)
    limit = _aitken_limit(traces[-3:])
    verdict = LadderVerdict(
        None, None, deltas, ratios, finest_rel, finest_abs, order, limit)

    # A noise floor wider than the band makes the whole comparison unreadable:
    # a delta inside the band would be indistinguishable from two meshes that
    # happened to differ. This is unevaluable rather than a failure, because
    # the ladder may well be converged -- there is simply no way to see it at
    # this resolution.
    if noise_floor_f is not None:
        if not math.isfinite(noise_floor_f) or noise_floor_f < 0.0:
            return _unevaluable("the supplied mesh noise floor is not a "
                                "non-negative finite capacitance")
        if not within_band(noise_floor_f, traces[-1]):
            return verdict._replace(
                state="unevaluable",
                reason=(
                    f"the mesh-to-mesh noise floor of {noise_floor_f * 1e15:.2f} fF "
                    f"is wider than the {REL_BAND * 100:.0f}% + "
                    f"{ABS_BAND_F * 1e15:.0f} fF band, so no rung-to-rung "
                    f"change at this base resolution can be attributed to "
                    f"refinement rather than to re-meshing"))

    problem = _judge_trend(traces, deltas, [rung.label for rung in solved])
    if problem is not None:
        return verdict._replace(state="not_converged", reason=problem)
    return verdict._replace(
        state="converged",
        reason=(
            f"the ladder contracts with consistent sign and its finest step "
            f"of {abs(finest_abs) * 1e15:.2f} fF "
            f"({abs(finest_rel) * 100:.2f}%) is inside the band"))


def _judge_trend(values, deltas, labels):
    """Why this coarse-to-fine sequence is not converging, or None.

    Shared by the trace gate and the entrywise matrix gate so that both apply
    exactly the same rule; an entry judged more leniently than the trace would
    be a hole in the matrix gate.
    """
    for index, (prev, nxt) in enumerate(zip(deltas, deltas[1:])):
        # Both trend requirements are suspended once a correction is already
        # inside the band: a settled sequence may jitter about its limit at
        # that level without that being evidence against it. This is also what
        # keeps the entrywise gate usable, since a coupling whose corrections
        # are all sub-fF is settled no matter which way they point.
        if within_band(prev, values[index + 1]):
            continue
        if (prev > 0.0) != (nxt > 0.0) and not within_band(
                nxt, values[index + 2]):
            return (f"the correction changed sign at rung {labels[index + 2]} "
                    f"({prev * 1e15:+.2f} fF then {nxt * 1e15:+.2f} fF) while "
                    f"still outside the band: the sequence is oscillating "
                    f"about a centre, not approaching a limit from one side")
        if abs(nxt) > MAX_CONTRACTION_RATIO * abs(prev):
            return (f"the correction at rung {labels[index + 2]} did not "
                    f"contract ({prev * 1e15:+.2f} fF then {nxt * 1e15:+.2f} "
                    f"fF, ratio {abs(nxt / prev):.3f} > "
                    f"{MAX_CONTRACTION_RATIO}): refinement is not driving the "
                    f"discretisation error out")
    if not within_band(deltas[-1], values[-1]):
        relative = (abs(deltas[-1] / values[-1]) * 100.0 if values[-1]
                    else float("inf"))
        return (f"the finest step of {abs(deltas[-1]) * 1e15:.2f} fF "
                f"({relative:.2f}%) is outside the "
                f"{REL_BAND * 100:.0f}% + {ABS_BAND_F * 1e15:.0f} fF band")
    return None


MatrixLadderVerdict = namedtuple(
    "MatrixLadderVerdict",
    "state reason entry_count failed_entries worst_entry trace")


def check_matrix_convergence_ladder(rungs, matrices, noise_floor_f=None,
                                    entry_noise_floors=None):
    """Judge a ladder entry by entry, which is what the goal actually asks for.

    The gate is specified over capacitance *matrices*, but every campaign
    script applied it to the trace. A trace is a sum, and sums cancel: entries
    can be moving in opposite directions by several percent each while their
    total sits still. On Fugu that is 3403 independent couplings hiding behind
    one number.

    The absolute half of the band only makes sense here, too. One fF against a
    100 pF trace is unreachable noise; against a fF-scale coupling it is the
    whole tolerance, and it is what stops a small coupling failing merely for
    being small.

    `matrices` is one square matrix per rung, coarse to fine, aligned with
    `rungs`. Only i <= j is judged: reciprocity is checked separately per rung,
    and judging both triangles would double-report the same defect.

    `entry_noise_floors` is a matrix of per-entry re-meshing spreads, and it is
    not optional in spirit. A single scalar floor taken from the trace badly
    understates what individual couplings do, because the trace is a sum and
    the re-meshing errors in it cancel. Measured on the canary, the trace moves
    0.238% across three meshes of nominally identical resolution while 45 of
    its 171 entries move more than the whole band, the worst by 63.8%. An entry
    that noisy cannot be shown to have converged to 2% by any amount of
    refinement, so it is reported unevaluable rather than judged.
    """
    rungs = list(rungs)
    matrices = list(matrices)
    if len(matrices) != len(rungs):
        return MatrixLadderVerdict(
            "unevaluable", "a matrix was not supplied for every rung",
            0, (), None, None)
    trace = check_convergence_ladder(rungs, noise_floor_f=noise_floor_f)
    if trace.state == "unevaluable":
        # The shape, health and rung-count preconditions are ladder-wide, so
        # there is nothing an entrywise pass could add here.
        return MatrixLadderVerdict(
            "unevaluable", trace.reason, 0, (), None, trace)

    sizes = {len(matrix) for matrix in matrices}
    if len(sizes) != 1 or any(
            any(len(row) != len(matrix) for row in matrix)
            for matrix in matrices):
        return MatrixLadderVerdict(
            "unevaluable",
            f"rungs disagree on matrix shape {sorted(sizes)} or are not "
            f"square: the entries cannot be put in correspondence",
            0, (), None, trace)
    size = sizes.pop()
    if size == 0:
        # No entries means nothing was judged, and nothing judged must not
        # report as everything passing.
        return MatrixLadderVerdict(
            "unevaluable", "the matrices have no entries to judge",
            0, (), None, trace)
    if any(not math.isfinite(matrix[i][j])
           for matrix in matrices for i in range(size) for j in range(size)):
        return MatrixLadderVerdict(
            "unevaluable", "a rung produced a non-finite matrix entry",
            0, (), None, trace)

    if entry_noise_floors is not None:
        if (len(entry_noise_floors) != size
                or any(len(row) != size for row in entry_noise_floors)):
            return MatrixLadderVerdict(
                "unevaluable",
                "the per-entry noise floors do not match the matrix shape",
                0, (), None, trace)
        if any(not math.isfinite(entry_noise_floors[i][j])
               or entry_noise_floors[i][j] < 0.0
               for i in range(size) for j in range(size)):
            return MatrixLadderVerdict(
                "unevaluable",
                "a per-entry noise floor is not a non-negative finite "
                "capacitance", 0, (), None, trace)

    labels = [rung.label for rung in rungs]
    failed = []
    judged = []
    unreadable = []
    for i in range(size):
        for j in range(i, size):
            values = [matrix[i][j] for matrix in matrices]
            deltas = [fine - coarse for coarse, fine in zip(values, values[1:])]
            relative = (abs(deltas[-1] / values[-1]) if values[-1]
                        else math.inf)
            judged.append(((i, j), relative, deltas[-1]))
            if entry_noise_floors is not None and not within_band(
                    entry_noise_floors[i][j], values[-1]):
                # Checked before the trend: refining an entry this noisy tells
                # you nothing, so calling it converged or not converged would
                # both be claims the data cannot support.
                unreadable.append(((i, j), entry_noise_floors[i][j]))
                continue
            problem = _judge_trend(values, deltas, labels)
            if problem is not None:
                failed.append(((i, j), problem))
    # size >= 1 above, so there is always at least the (0, 0) entry here.
    worst = max(judged, key=lambda item: item[1])

    entries = size * (size + 1) // 2
    if unreadable:
        (i, j), floor = max(unreadable, key=lambda item: item[1])
        return MatrixLadderVerdict(
            "unevaluable",
            f"{len(unreadable)} of {entries} entries move more than the "
            f"{REL_BAND * 100:.0f}% + {ABS_BAND_F * 1e15:.0f} fF band when the "
            f"mesh is merely regenerated at the same resolution, worst "
            f"C[{i}][{j}] at {floor * 1e15:.2f} fF; no amount of refinement "
            f"can demonstrate convergence against that",
            entries, tuple(failed), worst, trace)
    if failed:
        (i, j), problem = failed[0]
        return MatrixLadderVerdict(
            "not_converged",
            f"{len(failed)} of {entries} entries have not converged; "
            f"C[{i}][{j}]: {problem}",
            entries, tuple(failed), worst, trace)
    if trace.state != "converged":
        # Belt and braces: every entry settled but the trace did not is a
        # contradiction, and the honest answer is that something is wrong
        # rather than a pass.
        return MatrixLadderVerdict(
            "not_converged",
            f"every entry converged but the trace did not ({trace.reason}), "
            f"which is contradictory; the ladder is not trustworthy",
            entries, (), worst, trace)
    return MatrixLadderVerdict(
        "converged",
        f"all {entries} entries converged; the worst finest step is "
        f"{abs(worst[2]) * 1e15:.2f} fF ({worst[1] * 100:.2f}%) at "
        f"C[{worst[0][0]}][{worst[0][1]}]",
        entries, (), worst, trace)


def _observed_order(solved, deltas):
    """Order implied by the last two corrections, against the real length
    ratio. Halving an *area* is a sqrt(2) step in length, so assuming a
    halving here would report twice the true order."""
    if len(deltas) < 2 or not deltas[-2] or not deltas[-1]:
        return None
    ratio = solved[-2].element_size_m / solved[-1].element_size_m
    if ratio <= 1.0:
        return None
    return math.log(abs(deltas[-2] / deltas[-1])) / math.log(ratio)


def _aitken_limit(traces):
    """Aitken extrapolation, which assumes a constant contraction ratio and is
    therefore only meaningful on a ladder that already passes the trend tests.
    Reported for information, never used to decide the verdict."""
    if len(traces) < 3:
        return None
    denominator = traces[0] - 2.0 * traces[1] + traces[2]
    if denominator == 0.0:
        return None
    return (traces[0] * traces[2] - traces[1] ** 2) / denominator
