"""scalar-trunk Cin warnings: demoted to INFO ONLY on positive evidence of a valid matrix.

The bug this guards: a run configured for matrix Cin that resolved a valid matrix model was
still emitting the scalar shared-trunk caveats as WARNINGs, because the demotion was done by
substring-matching warning prose against a hand-maintained marker tuple that had drifted from
the producer's wording. Classification now happens at the source and is a pure function of the
FINAL cin_model, so it must be re-derived after the cap_only/switch_residual combine replaces
that model.

Calibration matters more than the happy path here: a warning-suppressor that cannot see its
input must keep warning. matrix_valid=None ("never evaluated") must NEVER demote.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
sys.path.insert(0, os.path.dirname(HERE))

import solve_reduce  # noqa: E402
import extract_parasitics  # noqa: E402

SCALAR = ["scalar cin model invalid: circulating share", "cin shared-trunk clamped"]
BASE = ["Zc ill-conditioned (cond=1e+07)"]


def _routed_to(message_fragment):
    """Which list does the producer append `message_fragment` to — 'warn' or 'scalar_warn'?

    Careful: "scalar_warn.append(" ENDS with "warn.append(", so a naive substring search finds
    the inner match and reports 'warn' for both. Anchor on "warn.append(" and then inspect the
    characters immediately before it. (An earlier version of this helper got exactly that wrong
    and asserted `"scalar_warn.append(" not in slice`, which was vacuously true.)
    """
    src = open(os.path.join(os.path.dirname(HERE), "lib", "solve_reduce.py")).read()
    head = src[:src.index(message_fragment)]
    j = head.rindex("warn.append(")
    return "scalar_warn" if head[:j].endswith("scalar_") else "warn"


def _p(mode, matrix_valid, basis="identity"):
    return dict(
        reduce_warn_base=list(BASE),
        reduce_scalar_warn=list(SCALAR),
        cin_model=dict(mode=mode, basis=basis, matrix_valid=matrix_valid),
    )


def test_valid_matrix_demotes_scalar_warnings_to_info():
    p = solve_reduce.classify_cin_warnings(_p("matrix", True))
    assert p["reduce_warn"] == BASE, "scalar caveats must not stay WARNINGs on a valid matrix"
    # text is re-labelled, never dropped: the scalar fields are still in the JSON
    assert all(s in p["reduce_info"] for s in SCALAR)
    assert "REJECTED" in p["reduce_info"][0]


def test_unevaluated_matrix_does_not_demote():
    """matrix_valid=None is 'unverified', not 'fine' — the warnings must survive."""
    p = solve_reduce.classify_cin_warnings(_p("scalar_trunk", None, basis=None))
    assert p["reduce_warn"] == BASE + SCALAR
    assert p["reduce_info"] == []


def test_invalid_matrix_does_not_demote():
    p = solve_reduce.classify_cin_warnings(_p("matrix", False))
    assert p["reduce_warn"] == BASE + SCALAR
    assert p["reduce_info"] == []


def test_matrix_mode_without_validity_flag_does_not_demote():
    """A truthy-but-not-True matrix_valid (e.g. a stray string) must not pass the gate."""
    p = solve_reduce.classify_cin_warnings(_p("matrix", "yes"))
    assert p["reduce_warn"] == BASE + SCALAR


def test_classification_is_idempotent():
    """Re-run after the combine must not double-append or re-demote."""
    p = solve_reduce.classify_cin_warnings(_p("matrix", True))
    once_warn, once_info = list(p["reduce_warn"]), list(p["reduce_info"])
    solve_reduce.classify_cin_warnings(p)
    solve_reduce.classify_cin_warnings(p)
    assert p["reduce_warn"] == once_warn
    assert p["reduce_info"] == once_info


def test_switch_side_residual_R_warning_is_never_demoted():
    """r_hs_switch/r_ls_switch ARE consumed by the matrix deck (loss/lib/deck.py:308-314),
    so their negative-and-clamped-to-0 warning must survive even on a valid matrix run —
    otherwise the deck silently gets Rser~0 on a whole loop side. Contrast L_loop_switch,
    which the matrix deck discards (deck.py:313 Lsh=2e-15) and which may be demoted.

    Pinned by reading the producer's source: the r_*_switch message must go to `warn`, never
    to `scalar_warn`.
    """
    assert _routed_to(  "switch-side residual R negative") == "warn", (
        "switch-side residual R warning was routed to scalar_warn — it would be demoted to "
        "INFO on a valid matrix run, but the matrix deck consumes r_hs_switch/r_ls_switch")


def _valid_cap_only_payload():
    """A cap_only_additive payload that really passes _matrix_valid_for_payload."""
    L = np.array([[3.0e-9, 1.0e-9], [1.0e-9, 3.0e-9]])
    R = np.array([[2.0e-3, 5.0e-4], [5.0e-4, 2.0e-3]])
    return dict(
        basis="cap_only_additive", mode="matrix", refs=["C1", "C2"],
        L=L.tolist(), R=R.tolist(), R_100k=R.tolist(), R_dc=R.tolist(),
        full_multiport_required=False, decomposition_valid=True,
        gauge_fix_status="fixed", switch_board_copper="split_lsw_element",
        L_sw_element=0.3e-9, spice_realizable=True, kmax=0.33,
    )


def test_split_basis_combine_redemotes_from_the_final_model():
    """The regression: on the cap_only/switch_residual path every individual leg resolves
    scalar_trunk, so the demotion decision made inside each solve() is stale. Only the
    COMBINED model is valid — the caveats must follow that verdict, not the leg's."""
    payload = _valid_cap_only_payload()
    ok, reason = extract_parasitics._matrix_valid_for_payload(payload)
    assert ok, f"test fixture must be a genuinely valid payload, got: {reason}"

    # what a full_loop leg looks like on a leaded board: scalar_trunk, warnings NOT demoted
    full_p = solve_reduce.classify_cin_warnings(_p("scalar_trunk", None, basis=None))
    assert full_p["reduce_warn"] == BASE + SCALAR, "leg itself must warn"

    combined = extract_parasitics._apply_cin_matrix_payload(full_p, payload, "matrix")
    assert combined["cin_model"]["matrix_valid"] is True
    assert combined["reduce_warn"] == BASE, "combined matrix is valid — scalar caveats demote"
    assert all(s in combined["reduce_info"] for s in SCALAR)


def test_combine_surfaces_warnings_from_the_legs_that_build_the_matrix():
    """cap_only/switch_residual are the solves the emitted cin_matrix is DERIVED from, but the
    combined payload starts as dict(full_p) — so their warnings used to vanish. A defect in the
    solve that builds the shipped matrix must not be invisible."""
    payload = _valid_cap_only_payload()
    full_p = solve_reduce.classify_cin_warnings(_p("scalar_trunk", None, basis=None))
    cap_p = dict(reduce_warn_base=["Zc ill-conditioned (cond=9e+08)"], reduce_scalar_warn=[])
    sw_p = dict(reduce_warn_base=["negative per-switch conduction R (r_hs=-0.31 mOhm)"],
                reduce_scalar_warn=[])

    combined = extract_parasitics._apply_cin_matrix_payload(
        full_p, payload, "matrix",
        legs=(("cap_only", cap_p), ("switch_residual", sw_p)))

    joined = "\n".join(combined["reduce_warn"])
    assert "[cap_only basis] Zc ill-conditioned" in joined
    assert "[switch_residual basis] negative per-switch conduction R" in joined
    # ...and a valid matrix still demotes the scalar caveats (the legs' warnings are not scalar)
    assert all(s not in combined["reduce_warn"] for s in SCALAR)


def test_combine_dedupes_identical_per_basis_geometry_warnings():
    """Each basis re-runs the geometry, so board-level warnings repeat verbatim. Saying the
    same thing three times is not more information."""
    payload = _valid_cap_only_payload()
    same = "dropped 1 port(s) disconnected from the loop"
    full_p = solve_reduce.classify_cin_warnings(
        dict(reduce_warn_base=[same], reduce_scalar_warn=[],
             cin_model=dict(mode="scalar_trunk", basis=None, matrix_valid=None)))
    leg = dict(reduce_warn_base=[same], reduce_scalar_warn=[])

    combined = extract_parasitics._apply_cin_matrix_payload(
        full_p, payload, "matrix",
        legs=(("cap_only", dict(leg)), ("switch_residual", dict(leg))))

    assert combined["reduce_warn"].count(same) == 1
    assert not any("basis]" in w for w in combined["reduce_warn"])


MATRIX_NOT_PRODUCED = (
    "scalar cin model invalid AND matrix mode was requested but not produced "
    "(cin_matrix is None) — the identity matrix basis needs the pad-ideal fet "
    "closure; check cin_extraction_basis / cin_closure in the extraction config. "
    "Falling back to the invalid scalar_trunk reduction.")


def test_matrix_not_produced_claim_is_routed_so_it_CAN_be_demoted():
    """Pin the routing, not just the downstream handling: the producer must put this run-level
    claim in scalar_warn. If it goes to `warn` it can never be settled against the final model,
    and the split path will tell an operator whose combine succeeded that no matrix was made.
    (The test below would pass vacuously without this — it injects the message by hand.)"""
    assert _routed_to("matrix mode was requested but not produced") == "scalar_warn", (
        "'matrix requested but not produced' was routed to warn — it asserts a run-level "
        "outcome and must be classifiable against the FINAL cin_model")


def test_matrix_not_produced_claim_does_not_survive_a_successful_combine():
    """That message asserts a RUN-LEVEL outcome ('falling back to scalar'). On the split path
    every leg legitimately has cin_matrix=None — the matrix is assembled from the legs
    afterwards — so it must not reach an operator whose combine SUCCEEDED."""
    payload = _valid_cap_only_payload()
    full_p = solve_reduce.classify_cin_warnings(dict(
        reduce_warn_base=list(BASE), reduce_scalar_warn=[MATRIX_NOT_PRODUCED],
        cin_model=dict(mode="scalar_trunk", basis=None, matrix_valid=False)))
    assert MATRIX_NOT_PRODUCED in full_p["reduce_warn"], "leg alone must still warn"

    combined = extract_parasitics._apply_cin_matrix_payload(full_p, payload, "matrix")
    assert combined["cin_model"]["matrix_valid"] is True
    assert not any("not produced" in w for w in combined["reduce_warn"]), (
        "a run that DID produce a valid matrix must not claim it did not")
    assert any("not produced" in m for m in combined["reduce_info"])


def test_matrix_not_produced_claim_survives_a_genuine_scalar_fallback():
    """The other direction: if the run really did fall back to scalar, it must still shout."""
    p = solve_reduce.classify_cin_warnings(dict(
        reduce_warn_base=list(BASE), reduce_scalar_warn=[MATRIX_NOT_PRODUCED],
        cin_model=dict(mode="scalar_trunk", basis=None, matrix_valid=False)))
    assert MATRIX_NOT_PRODUCED in p["reduce_warn"]
    assert p["reduce_info"] == []


def test_report_marks_the_cin_branch_table_as_the_rejected_model():
    """report.md renders the shared-trunk per-cap Lb/Rb table. On a valid-matrix run that table
    IS the rejected model — the reader must not meet it unannotated."""
    import emit
    p = dict(
        L_loop=3.2e-9, R_loop=3.1e-3, L_loop_ideal=3.2e-9, L_loop_single=9.2e-9,
        L_gate_hs=1e-8, R_gate_hs=2e-2, L_gate_ls=1e-9, R_gate_ls=6e-3,
        csi_hs=1.4e-9, csi_ls=3.2e-10, m_gate=0.0, freq_Hz=3.9e6, n_cin=2,
        meta=dict(pitch=1.0, lead_mm=0.0), topo=dict(
            hs=dict(refs=["Q1"], kelvin=False, gate="HSG"),
            ls=dict(refs=["Q2"], kelvin=False, gate="LSG"),
            pcb="b.kicad_pcb", vin="V", sw="SW", gnd="G", cin_used=["C1", "C2"]),
        cin_branches=[dict(ref="C1", cls="mlcc", Lb=1e-9, Rb=1e-3),
                      dict(ref="C2", cls="mlcc", Lb=2e-9, Rb=2e-3)],
        cin_L_shared=3.9e-9, cin_R_shared=1e-3,
        cin_model=dict(mode="matrix", basis="identity", matrix_valid=True),
        reduce_warn=[], reduce_info=["scalar ... was REJECTED ..."],
    )
    md = emit.markdown(p)
    table_at = md.index("| Cap | class | branch L")
    assert "REJECTED" in md[:table_at], "the rejection must appear ABOVE the table it describes"
    assert "Do not consume them" in md


def test_split_basis_combine_keeps_warnings_when_the_combined_matrix_is_invalid():
    """The other direction: a combine that FAILS must not inherit a demotion."""
    bad = _valid_cap_only_payload()
    bad["decomposition_valid"] = False          # combine broke down
    full_p = solve_reduce.classify_cin_warnings(_p("matrix", True))   # leg had demoted
    assert full_p["reduce_info"], "precondition: leg demoted"

    combined = extract_parasitics._apply_cin_matrix_payload(full_p, bad, "matrix")
    assert combined["cin_model"]["matrix_valid"] is False
    assert combined["reduce_warn"] == BASE + SCALAR, "invalid matrix must re-raise the caveats"
    assert combined["reduce_info"] == []
