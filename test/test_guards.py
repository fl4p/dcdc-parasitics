#!/usr/bin/env python3
"""Guard tests: the checks that must FAIL LOUD instead of degrading silently.

Each of these was a real anti-monotone false PASS — a path that, when it could
not evaluate its input, returned the value meaning "fine". They are pinned here
so they cannot regress back into silence.
"""
import json
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))

import sweep  # noqa: E402


def _parasitics(**kw):
    """A parasitics.json in a temp out-dir; return the dir."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "parasitics.json"), "w") as f:
        json.dump(kw, f)
    return d


def _one_run(out_dir, monkeypatch, rc=0):
    """Drive sweep.one_run against a canned parasitics.json (no subprocess)."""
    class _R:
        returncode = rc
    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _R())
    log = os.path.join(out_dir, "run.log")
    return sweep.one_run("py", "--pitch", "1.0", out_dir, [], log)[2]


def test_sweep_survives_null_r_hs(monkeypatch):
    """r_hs/r_ls are legitimately null (--allow-missing-gate-ports). That must
    print as n/a, not raise TypeError out of the executor and lose the sweep."""
    d = _parasitics(L_loop=2.6e-9, r_hs=None, r_ls=None)
    res = _one_run(d, monkeypatch)
    assert "error" not in res
    assert res["L_nH"] == pytest.approx(2.6)
    assert res["r_hs_mOhm"] is None and res["r_ls_mOhm"] is None
    assert sweep._fmt(res["r_hs_mOhm"]) == "n/a"


def test_sweep_refuses_a_missing_L_loop(monkeypatch):
    """A run with no L_loop is an ERROR, never a plausible 0.000 nH data point."""
    d = _parasitics(r_hs=1e-3)
    res = _one_run(d, monkeypatch)
    assert "error" in res and "L_loop" in res["error"]
    assert "L_nH" not in res


def test_sweep_reports_a_real_L(monkeypatch):
    d = _parasitics(L_loop=3.24e-9, r_hs=2.0e-3, r_ls=2.5e-3)
    res = _one_run(d, monkeypatch)
    assert res["L_nH"] == pytest.approx(3.24)
    assert res["r_hs_mOhm"] == pytest.approx(2.0)


def _parse(argv):
    import extract_parasitics as ep
    return ep.parse_args(argv)


def _base_argv(out):
    return ["b.kicad_pcb", "--sw", "SW", "--gnd", "GND", "-o", out]


def test_hf_freq_below_plateau_is_refused():
    """pick_plateau() takes the swept frequency NEAREST --plateau, so a sweep that
    stops below the plateau silently reports an off-plateau L as the plateau L."""
    with pytest.raises(SystemExit):
        _parse(_base_argv("/tmp/o") + ["--hf-freq", "1e6", "--plateau", "5e6"])


def test_hf_freq_at_or_above_plateau_is_accepted():
    a = _parse(_base_argv("/tmp/o") + ["--hf-freq", "5e6", "--plateau", "5e6"])
    assert a.hf_freq == 5e6
    a = _parse(_base_argv("/tmp/o") + ["--hf-freq", "1e8", "--plateau", "5e6"])
    assert a.hf_freq == 1e8


def test_lf_freq_above_plateau_is_refused():
    """The plateau must be inside the band on BOTH sides: a sweep that STARTS
    above it reads the bottom edge and calls it the plateau L."""
    with pytest.raises(SystemExit):
        _parse(_base_argv("/tmp/o") + ["--lf-freq", "1e7", "--plateau", "5e6"])


class _Resp:
    def __init__(self, b):
        self.b = b

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sidecar_fetch(monkeypatch, outcome):
    """Drive _load_altium_sidecar's URL path against a canned HTTP outcome."""
    import urllib.error

    import extract_parasitics as ep

    def fake(*a, **k):
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)

    monkeypatch.setattr(ep.urllib.request, "urlopen", fake)
    return ep._load_altium_sidecar(
        "https://example.com/b.kicad_pcb", "/tmp/b.kicad_pcb", tempfile.mkdtemp())


def test_absent_remote_sidecar_means_ordinary_board(monkeypatch):
    import urllib.error
    r = _sidecar_fetch(monkeypatch,
                       urllib.error.HTTPError("u", 404, "nf", None, None))
    assert r is None   # a definite 404: this board is simply not an Altium import


@pytest.mark.parametrize("outcome", [
    "http_503",
    "network_down",
])
def test_inconclusive_sidecar_fetch_is_unverified_not_absent(monkeypatch, outcome):
    """A failed fetch must NOT read as 'no sidecar' — that would silently drop the
    PROVISIONAL banner off an Altium reconstruction whenever the network hiccups."""
    import urllib.error
    exc = (urllib.error.HTTPError("u", 503, "err", None, None)
           if outcome == "http_503" else urllib.error.URLError("dns fail"))
    r = _sidecar_fetch(monkeypatch, exc)
    assert r is not None
    assert any("UNVERIFIED, not absent" in w for w in r["warnings"])


def test_non_dict_sidecar_does_not_crash(monkeypatch):
    r = _sidecar_fetch(monkeypatch, b"[]")
    assert r is not None and any("not a JSON object" in w for w in r["warnings"])


def _banner(**ai):
    import emit
    return "\n".join(emit._altium_banner({"meta": {"altium_import": ai}}))


def test_unreadable_sidecar_does_not_claim_a_relayer():
    """A degenerate sidecar carries no relayer; naming one asserts a conversion
    that was never observed."""
    b = _banner(warnings=["sidecar present but unreadable"], provenance="sidecar")
    assert "relayer=`unknown`" in b
    assert "partial" not in b


def test_stale_sidecar_is_flagged_in_the_report():
    """Provenance for a board that has since changed must not read as fact."""
    b = _banner(relayer="faithful", stale=True, provenance="sidecar",
                vb_pour_synthesized={"area_mm2": 14.65})
    assert "STALE" in b
    b2 = _banner(relayer="faithful", stale=None, provenance="sidecar")
    assert "could NOT be verified" in b2
    # a sidecar that matches the board makes no such claim
    b3 = _banner(relayer="faithful", stale=False, provenance="sidecar")
    assert "STALE" not in b3 and "could NOT be verified" not in b3


def test_only_fb_warning_reaches_the_spice_lib():
    """The .lib is what the loss consumer reads; a 2-layer diagnostic L_loop must
    not reach it unflagged."""
    import emit
    p = dict(L_loop=2.6e-9, R_loop=4e-3, L_gate_hs=1e-9, L_gate_ls=1e-9,
             R_gate_hs=0.0, R_gate_ls=0.0, csi_hs=0.1e-9, csi_ls=0.1e-9,
             freq_Hz=5e6, r_hs=1e-3, r_ls=1e-3,
             topo={"hs": {"refs": ["Q1"], "kelvin": False, "gate": "HG"},
                   "ls": {"refs": ["Q2"], "kelvin": False, "gate": "LG"}},
             meta={"only_fb": True, "pitch": 1.0})
    text, warn = emit.subckt(p)
    assert any("DCDC_ONLY_FB" in w for w in warn)
    assert "DCDC_ONLY_FB" in text          # rides on the .lib header too
    p["meta"]["only_fb"] = False
    text2, warn2 = emit.subckt(p)
    assert not any("DCDC_ONLY_FB" in w for w in warn2)
