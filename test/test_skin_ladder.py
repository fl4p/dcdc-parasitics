#!/usr/bin/env python3
"""Foster RL skin-ladder fit for the frequency-dependent commutation-loop copper.

The extractor already sweeps 39 kHz..84 MHz and used to throw away everything but the ~5 MHz
plateau, so every consumer got copper R frozen at ONE band. On Fugu2 that reads -70% at the
SW ring. `fit_skin_ladder` turns the swept R(f) into a series ladder the deck can place.

The guards are calibrated here against the inputs they exist to catch: a sweep that never
reached the ring, an R(f) that falls, and a fit too poor to trust.
"""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from lib import solve_reduce as sr  # noqa: E402

FREQS = [39e3, 84e3, 181e3, 390e3, 840e3, 1.81e6, 3.9e6, 8.4e6, 18.1e6, 39e6, 84e6]
# the measured Fugu2 loop (out/fugu2-perDeev-noLeads), in ohms
R_MEAS = [1.411e-3, 1.616e-3, 1.830e-3, 2.029e-3, 2.207e-3, 2.489e-3,
          3.134e-3, 4.153e-3, 4.963e-3, 5.341e-3, 5.477e-3]
# what the deck's frequency-FLAT network reduces to at each of those frequencies
R_FLAT = [1.361e-3, 1.444e-3, 1.519e-3, 1.551e-3, 1.559e-3, 1.561e-3,
          1.562e-3, 1.562e-3, 1.562e-3, 1.562e-3, 1.562e-3]


def test_fit_tracks_the_swept_loop_r():
    poles, diag = sr.fit_skin_ladder(FREQS, R_MEAS, R_FLAT, n_poles=5)
    # the frozen-R deck is ~70% low at the ring; the ladder closes it to a few percent
    assert diag["flat_max_rel_err"] > 0.6
    assert diag["max_rel_err"] < 0.03
    assert 3e-3 < diag["R_sum"] < 5e-3          # ~= R_meas(84 MHz) - R_flat


def test_every_pole_is_passive():
    """NNLS cannot return a negative R_k — and a negative R_k (or L_k) in the deck would be an
    ACTIVE element that manufactures energy at the ring. Assert the property, not the solver."""
    poles, _ = sr.fit_skin_ladder(FREQS, R_MEAS, R_FLAT, n_poles=5)
    assert poles
    for p in poles:
        assert p["R"] > 0 and p["L"] > 0 and p["fc_Hz"] > 0


def test_ladder_is_zero_at_dc_and_full_at_hf():
    """The ladder must add NOTHING at the conduction band (or it would silently re-base the
    conduction budget) and its full sum(R_k) at the ring."""
    poles, _ = sr.fit_skin_ladder(FREQS, R_MEAS, R_FLAT, n_poles=5)

    def ladder_R(f):
        w = 2 * np.pi * f
        return sum(p["R"] * (w * p["L"] / p["R"]) ** 2 / (1 + (w * p["L"] / p["R"]) ** 2)
                   for p in poles)

    r_sum = sum(p["R"] for p in poles)
    assert ladder_R(1.0) < 1e-9 * r_sum          # DC: every L_k shorts its R_k
    assert ladder_R(1e9) == pytest.approx(r_sum, rel=1e-3)   # HF: every L_k is an open


def test_a_sweep_that_never_reached_the_ring_gets_no_ladder():
    """Absence of evidence must not encode absence of the problem: a sweep stopping at 5 MHz
    has NOT measured the ring-band copper, so it gets a REASON, not an extrapolated ladder."""
    zc = _fake_zc(fmax=5e6)
    payload, reason = sr._cin_skin_payload(zc, [0, 1], _L(), _Rdc(), 39e3)
    assert payload is None
    assert "below the" in reason and "ring band" in reason


def test_a_falling_r_of_f_is_refused():
    """Copper R never falls with frequency. If the reduction says it does, a passive series
    ladder cannot realize it — refuse rather than emit NNLS's best non-negative approximation
    of an impossible curve."""
    freqs = np.array(FREQS)
    r_meas = np.array(R_MEAS)
    r_flat = np.array(R_FLAT)
    # calibrate: make the rise collapse at the top of the band
    bad = r_meas.copy()
    bad[-3:] = r_meas[3]
    zc = _fake_zc(r_curve=bad)
    payload, reason = sr._cin_skin_payload(zc, [0, 1], _L(), _Rdc(), 39e3)
    assert payload is None
    assert "not monotone" in reason


def test_a_sweep_too_narrow_for_interior_corners_is_refused():
    """The killer case: NNLS drives the residual to ~1% by placing pole corners OUTSIDE the
    measured band, where nothing constrains them. A good-looking fit_ok on an unconstrained
    extrapolation is exactly the false PASS this guard exists for."""
    zc = {f: _Rdc() * (1 + i) + 1j * 2 * np.pi * f * _L()
          for i, f in enumerate((40e6, 50e6, 84e6))}       # 2.1x span: no interior
    payload, reason = sr._cin_skin_payload(zc, [0, 1], _L(), _Rdc(), 40e6)
    assert payload is None
    assert "too narrow to place pole corners" in reason


def test_more_poles_than_data_points_is_capped():
    """n_poles > len(freqs) is under-determined: the residual goes to ~0 while R(f) BETWEEN the
    points is unconstrained. Cap the poles at the data rather than ship a flattering fit."""
    freqs = FREQS[:4]
    poles, diag = sr.fit_skin_ladder(freqs, R_MEAS[:4], R_FLAT[:4], n_poles=12)
    assert diag["n_poles"] <= len(freqs) - 1
    assert len(poles) <= len(freqs) - 1


def test_fit_ok_flags_a_ladder_too_poor_to_trust():
    """One pole cannot track a 3-decade skin transition (it over-states the mid-band ~2x).
    The payload must SAY the fit is bad rather than ship it as if it were good."""
    _, diag = sr.fit_skin_ladder(FREQS, R_MEAS, R_FLAT, n_poles=1)
    assert diag["max_rel_err"] > sr.SKIN_FIT_TOL


# ---- helpers: a 2-port sweep whose effective loop R follows a prescribed curve -------------

def _L():
    return np.array([[5e-9, 1e-9], [1e-9, 5e-9]])


def _Rdc():
    return np.array([[1.4e-3, 0.1e-3], [0.1e-3, 1.4e-3]])


def _fake_zc(fmax=84e6, r_curve=None):
    """Two identical coupled ports whose R rises with frequency like real copper."""
    freqs = [f for f in FREQS if f <= fmax]
    L = _L()
    zc = {}
    for i, f in enumerate(freqs):
        if r_curve is not None:
            scale = r_curve[i] / R_MEAS[0]
        else:
            scale = R_MEAS[i] / R_MEAS[0]
        zc[f] = _Rdc() * scale + 1j * 2 * np.pi * f * L
    return zc
