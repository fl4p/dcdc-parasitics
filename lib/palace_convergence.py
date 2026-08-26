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

    for index, (prev, nxt) in enumerate(zip(deltas, deltas[1:])):
        # Both trend requirements are suspended once a correction is already
        # inside the band: a settled sequence may jitter about its limit at
        # that level without that being evidence against it.
        if within_band(prev, traces[index + 1]):
            continue
        if (prev > 0.0) != (nxt > 0.0) and not within_band(
                nxt, traces[index + 2]):
            return verdict._replace(
                state="not_converged",
                reason=(
                    f"the correction changed sign at rung "
                    f"{solved[index + 2].label} "
                    f"({prev * 1e15:+.2f} fF then {nxt * 1e15:+.2f} fF) while "
                    f"still outside the band: the sequence is oscillating "
                    f"about a centre, not approaching a limit from one side"))
        if abs(nxt) > MAX_CONTRACTION_RATIO * abs(prev):
            return verdict._replace(
                state="not_converged",
                reason=(
                    f"the correction at rung {solved[index + 2].label} did not "
                    f"contract ({prev * 1e15:+.2f} fF then {nxt * 1e15:+.2f} "
                    f"fF, ratio {abs(nxt / prev):.3f} > "
                    f"{MAX_CONTRACTION_RATIO}): refinement is not driving the "
                    f"discretisation error out"))

    if not within_band(finest_abs, traces[-1]):
        return verdict._replace(
            state="not_converged",
            reason=(
                f"the finest step of {abs(finest_abs) * 1e15:.2f} fF "
                f"({abs(finest_rel) * 100:.2f}%) is outside the "
                f"{REL_BAND * 100:.0f}% + {ABS_BAND_F * 1e15:.0f} fF band"))
    return verdict._replace(
        state="converged",
        reason=(
            f"the ladder contracts with consistent sign and its finest step "
            f"of {abs(finest_abs) * 1e15:.2f} fF "
            f"({abs(finest_rel) * 100:.2f}%) is inside the band"))


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
