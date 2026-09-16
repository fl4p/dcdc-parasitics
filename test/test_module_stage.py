"""Guards for the INTEGRATED-MODULE declaration (lib/module_stage.py).

Pure python -- no pcbnew, no FastHenry -- so every guard that decides WHAT a
module extraction reports is testable under system python. The board-side guards
(refdes/pad/net existence, and the two topological checks that the pad-plane
closure really sits on the Cin loop) live in fet_discovery/kicad_geom and are
exercised by the known-bad calibration runs recorded in docs/.

Guard-checklist mapping (~/.claude/CLAUDE.md):
  1 unevaluable input  -> test_closure_is_required, test_declared_requires_*
  2 monotonicity       -> n/a (this is a declaration validator, not a scorer)
  3 preconditions      -> test_roundtrip_is_idempotent (the CLI re-parses)
  6 provenance         -> test_declared_requires_source, test_manifest_*
  8 fix vs mute        -> test_ideal_refuses_nonzero_internal_nh (the value cannot
                          be quietly dropped to make a config "work")
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import module_stage as ms  # noqa: E402


def _ideal(**kw):
    base = dict(ref="U1", vin_pads=["3"], gnd_pads=["10"],
                internal_closure="ideal_pad_plane")
    base.update(kw)
    return base


def test_absent_block_is_none():
    assert ms.parse_spec(None) is None
    assert ms.parse_spec({}) is None
    assert ms.parse_spec("") is None


def test_minimal_ideal_declaration():
    got = ms.parse_spec(_ideal())
    assert got["ref"] == "U1"
    assert got["internal_closure"] == ms.IDEAL
    assert got["internal_nh"] == 0.0
    assert got["internal_source"] is None
    assert got["allow_proximity_bond"] is False


def test_closure_is_required():
    """The whole point: an UNDECLARED internal path must never default to anything."""
    with pytest.raises(ms.ModuleError) as e:
        ms.parse_spec(dict(ref="U1", vin_pads=["3"], gnd_pads=["10"]))
    assert "internal_closure" in str(e.value)
    assert "no default" in str(e.value)


def test_unknown_closure_rejected():
    with pytest.raises(ms.ModuleError) as e:
        ms.parse_spec(_ideal(internal_closure="estimate"))
    assert ms.IDEAL in str(e.value) and ms.DECLARED in str(e.value)


def test_unknown_key_is_a_hard_error():
    """A typo'd key is a LOST declaration, and the lost one may be the closure."""
    with pytest.raises(ms.ModuleError) as e:
        ms.parse_spec(_ideal(**{"internal-closure": "ideal_pad_plane"}))
    assert "unknown key" in str(e.value)


def test_ref_required():
    for bad in (None, "", "   ", 7):
        with pytest.raises(ms.ModuleError):
            ms.parse_spec(dict(ref=bad, internal_closure=ms.IDEAL))


def test_declared_requires_internal_nh():
    with pytest.raises(ms.ModuleError) as e:
        ms.parse_spec(_ideal(internal_closure=ms.DECLARED))
    assert "internal_nh" in str(e.value)


def test_declared_requires_source():
    with pytest.raises(ms.ModuleError) as e:
        ms.parse_spec(_ideal(internal_closure=ms.DECLARED, internal_nh=1.5))
    assert "internal_source" in str(e.value)


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), True, "1.5"])
def test_declared_internal_nh_must_be_positive_and_finite(bad):
    with pytest.raises(ms.ModuleError):
        ms.parse_spec(_ideal(internal_closure=ms.DECLARED, internal_nh=bad,
                             internal_source="x"))


def test_declared_roundtrip():
    got = ms.parse_spec(_ideal(internal_closure=ms.DECLARED, internal_nh=1.25,
                               internal_source="SNVSCS7E fig 8-3, read 2026-09-16"))
    assert got["internal_nh"] == 1.25
    assert ms.internal_henries(got) == pytest.approx(1.25e-9)
    assert "SNVSCS7E" in ms.basis_note(got)


def test_ideal_refuses_nonzero_internal_nh():
    """Refused, not ignored: a silently-dropped value is how a config comes to
    describe something other than what ran."""
    with pytest.raises(ms.ModuleError) as e:
        ms.parse_spec(_ideal(internal_nh=2.0))
    assert "silently ignored" in str(e.value)


def test_ideal_accepts_explicit_zero_for_idempotency():
    # parse_spec's own OUTPUT carries internal_nh=0.0, and the CLI path re-parses
    # it. If 0 were refused, every CLI module run would fail on its own normal form.
    once = ms.parse_spec(_ideal())
    twice = ms.parse_spec(once)
    assert once == twice


def test_ideal_refuses_orphan_source():
    with pytest.raises(ms.ModuleError):
        ms.parse_spec(_ideal(internal_source="somewhere"))


def test_ideal_internal_henries_is_exactly_zero():
    assert ms.internal_henries(ms.parse_spec(_ideal())) == 0.0


@pytest.mark.parametrize("bad", ["3", 3, [], ["3", "3"], [None], [True]])
def test_pad_list_shapes(bad):
    with pytest.raises(ms.ModuleError):
        ms.parse_spec(_ideal(vin_pads=bad))


def test_pad_numbers_normalize_to_strings():
    got = ms.parse_spec(_ideal(vin_pads=[3], gnd_pads=["10"]))
    assert got["vin_pads"] == ["3"] and got["gnd_pads"] == ["10"]


def test_omitted_pads_stay_none_for_autodiscovery():
    got = ms.parse_spec(dict(ref="U1", internal_closure=ms.IDEAL))
    assert got["vin_pads"] is None and got["gnd_pads"] is None


def test_allow_proximity_bond_must_be_boolean():
    with pytest.raises(ms.ModuleError):
        ms.parse_spec(_ideal(allow_proximity_bond="yes"))
    assert ms.parse_spec(_ideal(allow_proximity_bond=True))["allow_proximity_bond"]


def test_to_arg_roundtrip_and_early_validation():
    wire = ms.to_arg(_ideal())
    assert ms.parse_spec(wire) == ms.parse_spec(_ideal())
    assert ms.to_arg(None) == ""
    with pytest.raises(ms.ModuleError):
        ms.to_arg(dict(ref="U1"))          # must fail in the PARENT, pre-subprocess


def test_bad_json_wire_form_is_named_as_such():
    with pytest.raises(ms.ModuleError) as e:
        ms.parse_spec("{not json")
    assert "JSON" in str(e.value)


def test_basis_note_always_says_board_copper_only():
    for spec in (_ideal(),
                 _ideal(internal_closure=ms.DECLARED, internal_nh=1.0,
                        internal_source="measured 2026-09-16")):
        assert "BOARD COPPER ONLY" in ms.basis_note(ms.parse_spec(spec))


def test_manifest_carries_provenance():
    m = ms.manifest(ms.parse_spec(_ideal(internal_closure=ms.DECLARED,
                                         internal_nh=1.0,
                                         internal_source="bench, 2026-09-16")))
    assert m["board_copper_only"] is True
    assert m["internal_source"] == "bench, 2026-09-16"
    assert ms.manifest(None) is None
