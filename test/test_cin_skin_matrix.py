"""The per-port R(f) matrix ladder: `fit_skin_ladder_matrix`.

WHY IT EXISTS. `fit_skin_ladder` fits the REDUCED loop R(f) -- one scalar curve --
so it is valid for the one reduction it was fitted to and nothing else. Measured
on fugu2-dualLS-perDev: every diagonal of `R_dc + ladder*11^T` is 30-75% below the
matching ring-band port read (C17: 5.28 vs 20.73 mOhm). It reproduces the
full-bank reduction to 0.2% because those per-port errors CANCEL in that one
reduction. Change the capacitors and the current distribution changes, so the
cancellation stops: a realistically loaded seven-port bank is -17.96% wrong in
Re(Z) at the ring and 12-52% across the band. Reactance survives, damping does
not.

The acceptance test for this fit is therefore NOT "does the reduction match" --
the scalar ladder already passes that, which is exactly how the defect hid. It is
"does the reduction match under a DIFFERENT current distribution than the one it
was fitted under".

These tests run against a real 11-frequency FastHenry band scan
(`out/fugu2-perDeev-noLeads/mesh/Zcbandscan.mat`, 15 ports, 39 kHz..84 MHz) rather
than a synthetic matrix, because the property under test is whether a real
board's per-port R(f) is representable this way at all.
"""
import itertools
import pathlib

import numpy as np
import pytest

from lib.solve_reduce import (SKIN_MATRIX_FIT_TOL, fit_skin_ladder_matrix,
                              parse_zc)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN = ROOT / "out" / "fugu2-perDeev-noLeads" / "mesh" / "Zcbandscan.mat"
JSON = ROOT / "out" / "fugu2-perDeev-noLeads" / "parasitics.json"

needs_scan = pytest.mark.skipif(
    not (SCAN.exists() and JSON.exists()),
    reason="the fugu2-perDeev-noLeads band scan is not in this working copy")


@pytest.fixture(scope="module")
def scan():
    """(freqs, R_sweep, L_sweep) for the Cin ports of the real band scan."""
    import json
    zc = parse_zc(SCAN)
    d = json.loads(JSON.read_text())
    r_dc = np.array(d["cin_matrix"]["R_dc"])
    port_r_dc = np.array(d["port_R_dc"])
    n = len(r_dc)
    # The payload carries no ref->port map, but cin_matrix.R_dc IS a submatrix of
    # port_R_dc (same solve), so recover it from the diagonal and VERIFY the block.
    idx = [int(np.argmin(np.abs(np.diag(port_r_dc) - r_dc[i, i]))) for i in range(n)]
    assert len(set(idx)) == n
    assert np.array_equal(port_r_dc[np.ix_(idx, idx)], r_dc), "port identity not established"
    fs = sorted(zc)
    sub = np.ix_(idx, idx)
    return (np.array(fs),
            np.array([zc[f].real[sub] for f in fs]),
            np.array([zc[f].imag[sub] / (2 * np.pi * f) for f in fs]))


def _fit(scan, **kw):
    fs, r_sweep, _ = scan
    payload, reason = fit_skin_ladder_matrix(fs, r_sweep, **kw)
    assert payload is not None, reason
    return payload


def _model(payload, fs, base):
    """R(f) = R_base + sum_k s_k(f) M_k, evaluated at `fs`."""
    fc = np.array(payload["corners_Hz"])
    m = np.array(payload["residues"])
    w = 2 * np.pi * np.asarray(fs, dtype=float)
    s = np.array([[(x / (2 * np.pi * c)) ** 2 / (1 + (x / (2 * np.pi * c)) ** 2)
                   for c in fc] for x in w])
    s = s - np.array([[(2 * np.pi * fs[0] / (2 * np.pi * c)) ** 2
                       / (1 + (2 * np.pi * fs[0] / (2 * np.pi * c)) ** 2) for c in fc]])
    return np.einsum("fk,kij->fij", s, m) + base


@needs_scan
def test_it_fits_the_real_per_port_sweep(scan):
    """The headline: the fit tracks every ENTRY, not just the reduction."""
    fs, r_sweep, _ = scan
    payload = _fit(scan)
    assert payload["fit"]["max_rel_err"] <= SKIN_MATRIX_FIT_TOL
    # and it is a real improvement on placing the base band alone
    assert payload["fit"]["flat_max_rel_err"] > 5 * payload["fit"]["max_rel_err"]
    got = _model(payload, fs, r_sweep[0])
    assert got.shape == r_sweep.shape
    # exact at the base band by construction: the basis is offset to vanish
    # there, because the consumer places the R_base matrix and this on top of it
    assert np.allclose(got[0], r_sweep[0]), "the fit must be exact at the base band"


@needs_scan
def test_the_fitted_matrix_is_passive_at_every_frequency(scan):
    """R(f) - R_base must be PSD: no current mode dissipates negative power.

    Guaranteed by construction -- s_k(f) >= 0 and each residue is PSD, so the sum
    is a non-negative combination of PSD matrices -- but asserted on the shipped
    numbers, because "guaranteed by construction" is what every silently broken
    invariant was called first.
    """
    fs, r_sweep, _ = scan
    payload = _fit(scan)
    for m in np.array(payload["residues"]):
        assert np.allclose(m, m.T), "a residue is not symmetric"
        assert np.linalg.eigvalsh(m).min() >= -1e-18, "a residue is not PSD"
    dense = np.geomspace(fs[0], fs[-1], 60)
    for f, r in zip(dense, _model(payload, dense, r_sweep[0])):
        ev = np.linalg.eigvalsh(0.5 * (r - r_sweep[0] + (r - r_sweep[0]).T))
        assert ev.min() >= -1e-15, f"R(f) - R_base is indefinite at {f:g} Hz"


@needs_scan
def test_it_holds_under_a_current_distribution_it_was_not_fitted_under(scan):
    """THE acceptance test, and the one the scalar ladder fails.

    The scalar ladder matches the shorted full-bank reduction to 0.2% and is
    -17.96% wrong the moment real capacitors redistribute the current, because a
    scalar cannot redistribute anything. Reduce the seven-port bank both shorted
    and loaded, against the MEASURED matrix, and require both.
    """
    fs, r_sweep, l_sweep = scan
    payload = _fit(scan)
    fit = _model(payload, fs, r_sweep[0])
    seven = list(range(7))

    def reduce_at(r, ind, f, caps):
        a = (r + 1j * 2 * np.pi * f * ind)[np.ix_(seven, seven)] + np.diag(caps)
        return 1.0 / np.sum(np.linalg.solve(a, np.ones(len(seven))))

    def cap(f, c):
        return complex(3e-3, 2 * np.pi * f * 1e-9 - 1.0 / (2 * np.pi * f * c))

    worst_short = worst_load = 0.0
    for t, f in enumerate(fs):
        if f < 1e6:                     # the band where the bank is actually read
            continue
        loaded = np.array([cap(f, c) for c in
                           itertools.islice(itertools.cycle((10e-6, 1e-6, 100e-9)), 7)])
        for caps, name in ((np.zeros(7), "short"), (loaded, "load")):
            truth = reduce_at(r_sweep[t], l_sweep[t], f, caps).real
            got = reduce_at(fit[t], l_sweep[t], f, caps).real
            err = abs(got / truth - 1.0)
            if name == "short":
                worst_short = max(worst_short, err)
            else:
                worst_load = max(worst_load, err)
    # measured 0.887% / 0.564% on this board; the scalar ladder is -17.96% loaded
    assert worst_short < 0.01, f"shorted reduction off by {worst_short*100:.2f}%"
    assert worst_load < 0.01, f"LOADED reduction off by {worst_load*100:.2f}%"


@needs_scan
def test_the_fit_error_is_measured_after_the_projection(scan):
    """Projection changes the model, so an error measured before it is not ours.

    Both numbers are reported; the one the tolerance is applied to must be the
    post-projection one.
    """
    payload = _fit(scan)
    fit = payload["fit"]
    assert "max_rel_err_before_projection" in fit
    fs, r_sweep, _ = payload and scan
    got = _model(payload, fs, r_sweep[0])
    dd = np.diagonal(r_sweep, axis1=1, axis2=2)
    scale = np.sqrt(np.einsum("fi,fj->fij", dd, dd))
    measured = float(np.max(np.abs(got - r_sweep) / scale))
    assert measured == pytest.approx(fit["max_rel_err"], rel=1e-9), (
        "the reported error does not describe the residues that ship")


@needs_scan
def test_the_tolerance_is_pinned_from_both_sides_by_pole_count(scan):
    """The gate has to bite, and here it has a real job: the fit is NOT monotone
    in pole count.

    On this board, 5 poles fits to 1.64% while 4 gives 8.8% and 6 gives 9.3% --
    once the corners outnumber what 11 swept points support, the least squares
    starts trading oscillatory residues against each other. So the two
    NEIGHBOURS of the working value bracket the tolerance, which pins it from
    both sides without inventing a payload.

    Written because the previous round of this work shipped a threshold that
    could be widened 8x with the whole suite still green: every refusal case sat
    far outside it, so they proved that something large fails and never where the
    boundary is.
    """
    fs, r_sweep, _ = scan
    good, reason = fit_skin_ladder_matrix(fs, r_sweep, n_poles=5)
    assert good is not None, reason
    assert good["fit"]["max_rel_err"] < SKIN_MATRIX_FIT_TOL
    for n_poles in (4, 6):
        payload, reason = fit_skin_ladder_matrix(fs, r_sweep, n_poles=n_poles)
        assert payload is None, (
            f"{n_poles} poles fits to "
            f"{fit_skin_ladder_matrix(fs, r_sweep, n_poles=n_poles)[0]} and was accepted")
        assert "tracks the swept per-port R(f)" in reason
        assert "after the PSD projection" in reason


@needs_scan
def test_a_falling_self_resistance_is_refused_per_port(scan):
    """Skin effect never spreads current back out, and a passive ladder cannot
    realize a falling R(f). Checked PER PORT: the scalar check runs on the
    reduction, which can rise while an individual port falls."""
    fs, r_sweep, _ = scan
    bad = r_sweep.copy()
    bad[-1][3][3] = bad[0][3][3] * 0.5
    payload, reason = fit_skin_ladder_matrix(fs, bad)
    assert payload is None and "falls with frequency" in reason


@needs_scan
def test_an_under_determined_fit_is_refused_not_flattered(scan):
    """More poles than points drives the residual to zero while R(f) between the
    points stays unconstrained -- a good-looking number for an unconstrained
    extrapolation."""
    fs, r_sweep, _ = scan
    for n_poles in (0, len(fs), len(fs) + 3):
        payload, reason = fit_skin_ladder_matrix(fs, r_sweep, n_poles=n_poles)
        assert payload is None, f"{n_poles} poles against {len(fs)} points was accepted"
        assert "under-determined" in reason or "poles" in reason


@needs_scan
def test_a_mismatched_sweep_shape_is_refused(scan):
    """Frequencies and matrices that do not describe each other."""
    fs, r_sweep, _ = scan
    payload, reason = fit_skin_ladder_matrix(fs[:-1], r_sweep)
    assert payload is None and "to match" in reason
    payload, reason = fit_skin_ladder_matrix(fs, r_sweep[:, :, :-1])
    assert payload is None and "expected" in reason


@needs_scan
def test_the_scalar_ladder_is_the_rank_one_special_case(scan):
    """A board whose rise really is a shared series R must fit rank-1 residues.

    Construct exactly that -- R_base plus one shared scalar per frequency -- and
    require the fit to recover it. This is the compatibility statement: the new
    model contains the old one rather than competing with it.
    """
    fs, r_sweep, _ = scan
    base = r_sweep[0]
    n = base.shape[0]
    ones = np.ones((n, n))
    # corner INSIDE the fitted span (the corners run freqs[0]*3 .. freqs[-1]/3),
    # and offset to vanish at the base band, exactly as the basis is
    fc = 1.8e6
    shape = np.array([(f / fc) ** 2 / (1 + (f / fc) ** 2) for f in fs])
    shared = 2.0e-3 * (shape - shape[0])
    synthetic = np.array([base + s * ones for s in shared])
    payload, reason = fit_skin_ladder_matrix(fs, synthetic)
    assert payload is not None, reason
    for m in np.array(payload["residues"]):
        if np.max(np.abs(m)) < 1e-12:
            continue
        assert np.linalg.matrix_rank(m, tol=1e-9 * np.max(np.abs(m))) == 1, (
            "a shared series resistance must fit as a rank-1 residue")
