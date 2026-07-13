#!/usr/bin/env python3
"""Run FastHenry on a .inp, parse the multiport Zc.mat, reduce to named parasitics.

System-python step (numpy). The port order in Zc.mat follows the `.external`
order emitted by kicad_geom.py: [P_pwr, P_ghs, P_gls].

    L_ij(f) = Im(Z_ij) / (2*pi*f)      R_ij(f) = Re(Z_ij)

Reduced parasitics (at the low-MHz plateau):
    L_loop      = L[pwr,pwr]           commutation-loop inductance (SW peak V)
    R_loop      = R[pwr,pwr]           HF ring-loop R (MLCC-anchored, skin-elevated)
    L_gate_hs   = L[ghs,ghs]           HS gate-loop inductance
    L_gate_ls   = L[gls,gls]
    csi_hs      = |L[pwr,ghs]|         HS common-source inductance (shared source lead)
    csi_ls      = |L[pwr,gls]|
    m_gate      = L[ghs,gls]

Conduction-loss R (at the LOWEST swept freq ~= DC, bulk-electrolytic-anchored, so
the MLCCs — near-open at the 39 kHz fundamental — are excluded):
    r_hs        = R[hs,hs]             HS conduction copper (Vin_bulk -> SW via HS)
    r_ls        = R[ls,ls]             LS conduction copper (SW -> GND_bulk via LS)
    r_loop_cond = R[bulk,bulk]         full LF conduction loop over the bulk cap
    r_sw        = r_loop_cond-r_hs-r_ls   SW-node spreading residual

With --emit-cin-network the full input bank is ported (topo['cin_net']) and
decomposed into a shared Vin/GND trunk + per-cap private branch (copper only):
    cin_branches = [{ref, cls, Lb, Rb}]   per-cap branch L/R (Lb=L[i,i]-L_shared)
    cin_L_shared, cin_R_shared            the shared trunk (loss.py adds cap C/ESR)
"""
import os
import re
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FASTHENRY = os.environ.get("FASTHENRY", "/Users/fab/dev/vendor/FastHenry2/bin/fasthenry")


def run_fasthenry(inp, suffix="dcdc", fasthenry=FASTHENRY, cwd=None):
    """Run fasthenry; return the path to the produced Zc<suffix>.mat."""
    cwd = cwd or os.path.dirname(os.path.abspath(inp)) or "."
    subprocess.run([fasthenry, "-p", "diag", "-S", suffix, os.path.basename(inp)],
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.join(cwd, f"Zc{suffix}.mat")


def parse_zc(path):
    """Return {freq_Hz: ZmatrixComplex(n,n)}."""
    out = {}
    freq = None
    rows = []
    n = 0
    hdr = re.compile(r"Impedance matrix for frequency\s*=\s*([-\d.eE+]+)\s+(\d+)\s*x\s*(\d+)")
    for ln in open(path):
        m = hdr.search(ln)
        if m:
            if freq is not None and rows:
                out[freq] = np.array(rows)
            freq = float(m.group(1)); n = int(m.group(2)); rows = []
            continue
        if freq is None:
            continue
        toks = ln.replace("j", "").split()
        if len(toks) >= 2 * n:
            vals = [float(t) for t in toks[:2 * n]]
            rows.append([complex(vals[2 * k], vals[2 * k + 1]) for k in range(n)])
    if freq is not None and rows:
        out[freq] = np.array(rows)
    return out


def pick_plateau(zc, target=5e6):
    """Return (freq, Z) at the frequency closest to `target` (low-MHz L plateau)."""
    f = min(zc.keys(), key=lambda x: abs(np.log10(x) - np.log10(target)))
    return f, zc[f]


def pick_frequency(zc, target):
    """Return (freq, Z) at the frequency closest to target."""
    f = min(zc.keys(), key=lambda x: abs(np.log10(x) - np.log10(target)))
    return f, zc[f]


def _eff_commutation(Z, cin_idx, zcap=None):
    """Effective 2-terminal commutation impedance of the input caps driven in
    parallel (common voltage across every Cin port, other ports open).

    For N cap ports with complex port matrix Zc, driving all at the same voltage
    V gives port currents I = V*Zc^-1*1, total I_tot = V*(1^T Zc^-1 1), so the
    effective impedance seen by the FET commutation current is

        Z_eff = V / I_tot = 1 / (1^T Zc^-1 1).

    This folds in every mutual Mij between the cap-return loops exactly; for N=1
    it degenerates to Zc[0,0] (the single-cap loop). With `zcap` (a per-cap
    complex branch impedance ESR+jw*ESL) it adds each cap's own parasitics in
    series, making the current split physical at the ring frequency instead of
    the ideal-cap (copper-only) limit.

    Solves Zc x = 1 (never forms an explicit inverse — Zc can be ill-conditioned
    when caps are closely coupled). Returns (Z_eff, y, denom, cond, weights),
    where y are the branch currents, weights = y/denom sum to 1 (the current
    split), and cond is the 2-norm condition number for a reliability check."""
    Zc = Z[np.ix_(cin_idx, cin_idx)].astype(complex)
    if zcap is not None:
        Zc = Zc + np.diag(zcap)
    ones = np.ones(len(cin_idx), dtype=complex)
    y = np.linalg.solve(Zc, ones)
    denom = complex(y.sum())
    cond = float(np.linalg.cond(Zc))
    return 1.0 / denom, y, denom, cond, y / denom


def _cin_branch_decomp(Lm, Rm, cin_idx, refs, cls_map, c_map=None) -> "dict | None":
    """Partial-inductance decomposition of the ported input-cap bank into a shared
    trunk + per-cap private branch, for the `--emit-cin-network` model.

    With a single common Vin/GND trunk feeding every cap, the port L/R matrices
    read (to first order): diagonal L[i,i] = L_shared + Lb_i, off-diagonal
    L[i,j] = L_shared (only the trunk is common). So the trunk is the mean of the
    off-diagonals and each private branch is diagonal minus trunk:

        L_shared = mean(L[i,j], i!=j)     Lb_i = L[i,i] - L_shared
        R_shared = mean(R[i,j], i!=j)     Rb_i = R[i,i] - R_shared

    Returns per-cap {ref, cls, Lb, Rb} + the shared trunk L/R + the off-diagonal
    spread (a high spread means the single-trunk model is a poor fit -> warn). The
    consumer (loss.py) assembles the SPICE `cin_network` as one series trunk
    (L_shared/R_shared) feeding each cap's branch (Lb_i/Rb_i) + its datasheet
    C/ESR/ESL. None if <2 caps (no shared/branch split is defined)."""
    n = len(cin_idx)
    if n < 2:
        return None
    offL = [Lm[a, b] for i, a in enumerate(cin_idx)
            for j, b in enumerate(cin_idx) if i != j]
    offR = [Rm[a, b] for i, a in enumerate(cin_idx)
            for j, b in enumerate(cin_idx) if i != j]
    # Shared trunk = the common floor. Use the off-diagonal mean (mutual = trunk in
    # a clean single-trunk topology), but NEVER above the smallest self-L, so every
    # private branch Lb_i = L[i,i] - L_shared stays >= 0 — a negative branch
    # inductance is non-physical for the SPICE cin_network. With heterogeneous caps
    # (bulk self-L >> mlcc) the raw off-diagonal mean can exceed the mlcc diagonals;
    # the min-clamp pins the trunk to the nearest cap (its Lb -> 0). Homogeneous
    # banks (mean off-diag < min diag) are unaffected.
    min_diagL = float(min(Lm[a, a] for a in cin_idx))
    min_diagR = float(min(Rm[a, a] for a in cin_idx))
    mean_offL = float(np.mean(offL))
    L_shared = min(mean_offL, min_diagL)
    R_shared = min(float(np.mean(offR)), min_diagR)
    L_spread = float(np.std(offL))
    clamped = L_shared < mean_offL - 1e-18   # trunk pinned to min-diag (heterogeneous)
    branches = []
    for k, a in enumerate(cin_idx):
        ref = refs[k] if k < len(refs) else f"P{a}"
        branches.append(dict(ref=ref, cls=(cls_map or {}).get(ref, "mlcc"),
                             Lb=max(0.0, float(Lm[a, a]) - L_shared),
                             Rb=max(0.0, float(Rm[a, a]) - R_shared),
                             C=(c_map or {}).get(ref)))
    return dict(branches=branches, L_shared=L_shared, R_shared=R_shared,
                L_spread=L_spread, clamped=clamped)


def _cin_decomp_with_model_limits(dec, Lm, Rm, cin_idx, refs, cls_map, c_map=None,
                                  L_limit=None, R_limit=None) -> tuple:
    """Return (model_dec, meta) after applying deck-consumption limits.

    `_cin_branch_decomp` returns the raw full-bank shared trunk that best fits the
    port matrix. The loss deck, however, also places `L_loop_switch`/R residuals;
    if the raw trunk exceeds the selected HF loop basis, consuming both would
    over-count. Keep the raw values for diagnostics, but clamp the model values
    that existing consumers read.
    """
    if not dec:
        return dec, None
    L_raw = float(dec["L_shared"])
    R_raw = float(dec["R_shared"])
    L_model = L_raw if L_limit is None else min(L_raw, max(0.0, float(L_limit)))
    R_model = R_raw if R_limit is None else min(R_raw, max(0.0, float(R_limit)))
    L_clamped = L_model < L_raw - 1e-18
    R_clamped = R_model < R_raw - 1e-15
    if not (L_clamped or R_clamped):
        return dec, dict(clamped=False, basis="raw")

    branches = []
    for k, a in enumerate(cin_idx):
        ref = refs[k] if k < len(refs) else f"P{a}"
        branches.append(dict(ref=ref, cls=(cls_map or {}).get(ref, "mlcc"),
                             Lb=max(0.0, float(Lm[a, a]) - L_model),
                             Rb=max(0.0, float(Rm[a, a]) - R_model),
                             C=(c_map or {}).get(ref)))
    model = dict(dec)
    model.update(branches=branches, L_shared=L_model, R_shared=R_model)
    meta = dict(
        clamped=True,
        basis="clamped_to_loop",
        reason="cin shared trunk exceeded the selected loop/residual basis",
        L_shared_raw=L_raw,
        L_shared_model=L_model,
        L_loop_limit=L_limit,
        L_clamped=L_clamped,
        R_shared_raw=R_raw,
        R_shared_model=R_model,
        R_residual_limit=R_limit,
        R_clamped=R_clamped,
    )
    return model, meta


def _cin_region_diagnostics(Lm, cin_idx, refs, weights=None, residual_raw=None):
    """Return scalar-model validity diagnostics for the currently available basis.

    This is not the future matrix-mode separability test. It only evaluates the
    legacy one-shared-trunk scalar model against the existing full-loop cap-port
    matrix. A later cap-branch-only extraction will add a separate switch
    separability check by comparing cap-only and full-loop off-diagonals.
    """
    if len(cin_idx) < 2:
        return dict(valid=True, diagnostics=[], regions=[], metrics={})
    Lc = Lm[np.ix_(cin_idx, cin_idx)]
    off = np.array([Lc[i, j] for i in range(len(cin_idx))
                    for j in range(len(cin_idx)) if i != j], dtype=float)
    mean = float(np.mean(off)) if off.size else 0.0
    std = float(np.std(off)) if off.size else 0.0
    spread_ratio = (std / abs(mean)) if abs(mean) > 1e-18 else 0.0
    neg_share = []
    if weights is not None:
        for k, wk in enumerate(weights):
            if complex(wk).real < -1e-3:
                name = refs[k] if k < len(refs) else f"P{k}"
                neg_share.append(dict(ref=name, share=complex(wk).real))
    diagnostics = []
    if spread_ratio > 0.5:
        diagnostics.append(dict(
            code="cin_region_heterogeneous",
            message=(f"cap-cap mutual spread is high ({std*1e9:.2f} nH std vs "
                     f"{mean*1e9:.2f} nH mean); one shared trunk is not a valid "
                     "model for this cap bank"),
            severity="error",
            spread_ratio=spread_ratio,
        ))
    if neg_share:
        diagnostics.append(dict(
            code="negative_ideal_current_share",
            message=("ideal-cap parallel solve has circulating current "
                     "mode(s) with negative share(s): "
                     + ", ".join(f"{d['ref']}={d['share']*100:.1f}%"
                                 for d in neg_share)
                     + ". This is allowed by the passive port matrix, but "
                     "invalidates the scalar_trunk Cin reduction"),
            severity="error",
            shares=neg_share,
        ))
    # Temporary sign-test band. When the cap-only matrix basis lands, this should
    # reference the calibrated same-fixture numerical floor from the null-drop
    # perturbation test, not a hard-coded physics tolerance.
    if residual_raw is not None and residual_raw < -0.05e-9:
        diagnostics.append(dict(
            code="negative_switch_residual",
            message=(f"scalar switch residual is negative "
                     f"({residual_raw*1e9:.2f} nH); cin_L_shared and L_loop "
                     "were reduced in inconsistent bases"),
            severity="error",
            L_loop_switch_raw=residual_raw,
        ))

    # Lightweight region hint from the full-loop matrix, for diagnostics only.
    # The future implementation should use the cap-only matrix for authoritative
    # region assignment.
    row_mean = np.array([
        np.mean([Lc[i, j] for j in range(len(cin_idx)) if j != i])
        for i in range(len(cin_idx))
    ])
    near = float(np.median(row_mean))
    regions = []
    for i, m in enumerate(row_mean):
        name = refs[i] if i < len(refs) else f"P{i}"
        regions.append(dict(ref=name, mean_mutual=float(m),
                            weak_region=bool(near > 0 and m < 0.5 * near)))
    return dict(
        valid=not any(d.get("severity") == "error" for d in diagnostics),
        diagnostics=diagnostics,
        regions=regions,
        metrics=dict(offdiag_mean=mean, offdiag_std=std,
                     offdiag_spread_ratio=spread_ratio),
    )


def _cin_model_valid_for_mode(model):
    """Validity of the resolved cin_model.mode.

    Today only scalar_trunk is implemented, but keeping the mode dispatch here
    prevents the top-level compat field from ossifying as "scalar_valid" when
    matrix modes land.
    """
    mode = model.get("mode")
    if mode == "scalar_trunk":
        return model.get("scalar_valid")
    if mode in ("matrix", "matrix_with_sw_coupling"):
        return model.get("matrix_valid")
    if mode == "none":
        return model.get("full_multiport_valid")
    return None


def _cin_matrix_payload(Lm, R_100k, net_idx, net_refs, basis, R_dc=None,
                        r_100k_freq_Hz=None, r_dc_freq_Hz=None):
    if not net_idx:
        return None
    Lc = Lm[np.ix_(net_idx, net_idx)]
    R_100k = np.asarray(R_100k, dtype=float)
    R_dc = np.asarray(R_dc if R_dc is not None else R_100k, dtype=float)
    Rc = R_100k[np.ix_(net_idx, net_idx)]
    Rdc = R_dc[np.ix_(net_idx, net_idx)]
    kmax = 0.0
    kmax_pair = None
    for i, ri in enumerate(net_refs):
        for j in range(i + 1, len(net_refs)):
            denom = float(abs(Lc[i, i] * Lc[j, j])) ** 0.5
            kij = abs(float(Lc[i, j]) / denom) if denom > 0 else 0.0
            if kij > kmax:
                kmax = kij
                kmax_pair = [ri, net_refs[j]]
    return dict(
        basis=basis,
        refs=list(net_refs),
        L=Lc.tolist(),
        R=Rc.tolist(),
        R_100k=Rc.tolist(),
        R_dc=Rdc.tolist(),
        R_100k_freq_Hz=r_100k_freq_Hz,
        R_dc_freq_Hz=r_dc_freq_Hz,
        L_sw_element=0.0,
        gauge_fix_status="structurally_not_required",
        gauge_fix_reason="zero_by_plane_p_equiv",
        identity_basis_reason="pad_ideal_identity_basis",
        switch_board_copper="in_matrix",
        kmax=kmax,
        kmax_pair=kmax_pair,
        spice_realizable=_k_below_spice_rail(kmax),
    )


def _k_below_spice_rail(k):
    k = abs(float(k))
    return k < 0.95 and not np.isclose(k, 0.95, rtol=0.0, atol=1e-12)


def _kmax_from_matrix(Lc, refs):
    kmax = 0.0
    kmax_pair = None
    for i, ri in enumerate(refs):
        for j in range(i + 1, len(refs)):
            denom = float(abs(Lc[i, i] * Lc[j, j])) ** 0.5
            kij = abs(float(Lc[i, j]) / denom) if denom > 0 else 0.0
            if kij > kmax:
                kmax = kij
                kmax_pair = [ri, refs[j]]
    return kmax, kmax_pair


def _fit_switch_additive_delta(L_full, L_cap, refs, L_sw_physical, floor=0.0):
    """Fit delta_ij = L_sw + m_i + m_j with L_sw fixed by the port gauge.

    The additive model has gauge freedom if L_sw is not fixed. The caller must
    pass the explicit switch-residual port measurement as L_sw_physical; this
    helper then emits both physical-gauge metadata and modeling-gauge element
    values.
    """
    refs = list(refs)
    L_full = np.asarray(L_full, dtype=float)
    L_cap = np.asarray(L_cap, dtype=float)
    if L_full.shape != L_cap.shape or L_full.ndim != 2 or L_full.shape[0] != L_full.shape[1]:
        raise ValueError("L_full and L_cap must be same-size square matrices")
    n = L_full.shape[0]
    if len(refs) != n:
        raise ValueError("refs length must match matrix size")
    L_sw_physical = float(L_sw_physical)
    if not np.isfinite(L_sw_physical):
        raise ValueError("L_sw_physical must be finite")
    if L_sw_physical <= 0:
        raise ValueError("L_sw_physical must be positive for switch-coupled matrix mode")
    floor = float(floor or 0.0)
    if floor < 0 or not np.isfinite(floor):
        raise ValueError("floor must be finite and nonnegative")

    delta = L_full - L_cap
    rows = []
    vals = []
    for i in range(n):
        for j in range(n):
            row = np.zeros(n, dtype=float)
            row[i] += 1.0
            row[j] += 1.0
            rows.append(row)
            vals.append(delta[i, j] - L_sw_physical)
    A = np.vstack(rows)
    b = np.asarray(vals, dtype=float)
    m_phys, *_ = np.linalg.lstsq(A, b, rcond=None)

    recon = L_sw_physical + m_phys[:, None] + m_phys[None, :]
    residual = delta - recon
    regauge_c = float(np.median(m_phys)) if n else 0.0
    m_model = m_phys - regauge_c
    L_sw_element = L_sw_physical + 2.0 * regauge_c
    if L_sw_element <= 0 or not np.isfinite(L_sw_element):
        raise ValueError("modeling-gauge L_sw_element must be positive")
    significant = [
        {"ref": ref, "m_i_modeling": float(m)}
        for ref, m in zip(refs, m_model)
        if abs(float(m)) > floor
    ]
    return dict(
        refs=refs,
        L_sw_physical=L_sw_physical,
        m_i_physical=[float(x) for x in m_phys],
        regauge_c=regauge_c,
        L_sw_element=float(L_sw_element),
        m_i_modeling=[float(x) for x in m_model],
        significant_couplings=significant,
        residual_fro=float(np.linalg.norm(residual, ord="fro")),
        residual_rms=float(np.linalg.norm(residual, ord="fro") / max(1, n)),
        residual_max_abs=float(np.max(np.abs(residual))) if n else 0.0,
        residual_matrix=residual.tolist(),
    )


def _cin_decomposed_matrix_payload(L_full, L_cap, R_cap_100k, refs, L_sw_physical,
                                   floor=0.0, basis="cap_only_additive",
                                   R_cap_dc=None, r_100k_freq_Hz=None,
                                   r_dc_freq_Hz=None):
    """Build the future non-identity matrix payload from full/cap/switch bases."""
    refs = list(refs)
    L_cap = np.asarray(L_cap, dtype=float)
    R_cap_100k = np.asarray(R_cap_100k, dtype=float)
    R_cap_dc = np.asarray(R_cap_dc if R_cap_dc is not None else R_cap_100k, dtype=float)
    if (L_cap.shape != R_cap_100k.shape or L_cap.shape != R_cap_dc.shape
            or L_cap.ndim != 2 or L_cap.shape[0] != L_cap.shape[1]):
        raise ValueError("L_cap, R_cap_100k, and R_cap_dc must be same-size square matrices")
    if len(refs) != L_cap.shape[0]:
        raise ValueError("refs length must match matrix size")
    fit = _fit_switch_additive_delta(L_full, L_cap, refs, L_sw_physical, floor=floor)
    separability_fit = dict(
        residual_fro=fit["residual_fro"],
        residual_rms=fit["residual_rms"],
        residual_max_abs=fit["residual_max_abs"],
        residual_matrix=fit["residual_matrix"],
        floor=floor,
    )
    if fit["residual_fro"] > floor:
        return dict(
            basis=basis,
            mode="none",
            refs=refs,
            L=L_cap.tolist(),
            R=R_cap_100k.tolist(),
            R_100k=R_cap_100k.tolist(),
            R_dc=R_cap_dc.tolist(),
            R_100k_freq_Hz=r_100k_freq_Hz,
            R_dc_freq_Hz=r_dc_freq_Hz,
            L_sw_physical=fit["L_sw_physical"],
            m_i_physical=fit["m_i_physical"],
            regauge_c=fit["regauge_c"],
            m_i_modeling=fit["m_i_modeling"],
            switch_couplings=[],
            separability_fit=separability_fit,
            switch_separability=dict(
                status="failed",
                reason="additive_fit_residual_above_floor"),
            gauge_fix_status="fixed",
            gauge_fix_reason="explicit_switch_residual_port",
            switch_board_copper="nonseparable_full_multiport_required",
            full_multiport_required=True,
            full_multiport_valid=False,
            full_multiport_reason="switch_separability_failed_additive_residual_above_floor",
            decomposition_valid=False,
            spice_realizable=False,
        )
    kmax, kmax_pair = _kmax_from_matrix(L_cap, refs)
    sw_kmax = 0.0
    sw_kmax_pair = None
    switch_couplings = []
    L_sw_element = float(fit["L_sw_element"])
    for entry in fit["significant_couplings"]:
        ref = entry["ref"]
        i = refs.index(ref)
        denom = float(abs(L_cap[i, i] * L_sw_element)) ** 0.5
        kij = float(entry["m_i_modeling"]) / denom if denom > 0 else 0.0
        switch_couplings.append(dict(
            ref=ref,
            m_i_modeling=float(entry["m_i_modeling"]),
            K=float(kij),
        ))
        if abs(kij) > sw_kmax:
            sw_kmax = abs(kij)
            sw_kmax_pair = [ref, "L_sw_element"]
    mode = "matrix_with_sw_coupling" if switch_couplings else "matrix"
    spice_realizable = _k_below_spice_rail(kmax) and _k_below_spice_rail(sw_kmax)
    payload = dict(
        basis=basis,
        mode=mode,
        refs=refs,
        L=L_cap.tolist(),
        R=R_cap_100k.tolist(),
        R_100k=R_cap_100k.tolist(),
        R_dc=R_cap_dc.tolist(),
        R_100k_freq_Hz=r_100k_freq_Hz,
        R_dc_freq_Hz=r_dc_freq_Hz,
        L_sw_element=L_sw_element,
        L_sw_physical=fit["L_sw_physical"],
        m_i_physical=fit["m_i_physical"],
        regauge_c=fit["regauge_c"],
        m_i_modeling=fit["m_i_modeling"],
        switch_couplings=switch_couplings,
        separability_fit=separability_fit,
        switch_separability=dict(status="passed", reason="additive_fit_within_floor"),
        gauge_fix_status="fixed",
        gauge_fix_reason="explicit_switch_residual_port",
        switch_board_copper="split_lsw_element",
        full_multiport_required=False,
        decomposition_valid=True,
        kmax=kmax,
        kmax_pair=kmax_pair,
        switch_kmax=sw_kmax,
        switch_kmax_pair=sw_kmax_pair,
        spice_realizable=spice_realizable,
    )
    return payload


def _cin_matrix_from_reductions(full_p, cap_p, switch_p, floor=0.0):
    """Assemble a decomposed Cin matrix payload from three reduced runs.

    This is the reducer-side handoff for the future CLI orchestration:
    full-loop run + cap-only run + switch-residual run, all on the same fixture.
    """
    expected_basis = (
        ("full", full_p, "full_loop"),
        ("cap_only", cap_p, "cap_only"),
        ("switch_residual", switch_p, "switch_residual"),
    )
    for name, p, want in expected_basis:
        got = ((p.get("topo") or {}).get("cin_extraction_basis"))
        if got != want:
            raise ValueError(f"{name}: cin_extraction_basis={got!r}, expected {want!r}")

    def cin_lr(p, name):
        ports_ = list(p.get("ports") or [])
        port_pos = {label: i for i, label in enumerate(ports_)}
        cin_net = ((p.get("topo") or {}).get("cin_net") or [])
        if not cin_net:
            raise ValueError(f"{name}: no cin_net ports available")
        rows = []
        for e in cin_net:
            ref, label = e.get("ref"), e.get("label")
            if not ref or not label:
                raise ValueError(f"{name}: cin_net entries must carry ref and label")
            rows.append((ref, label))
        refs_seen = [r for r, _l in rows]
        labels_seen = [l for _r, l in rows]
        dup_refs = sorted({r for r in refs_seen if refs_seen.count(r) > 1})
        dup_labels = sorted({l for l in labels_seen if labels_seen.count(l) > 1})
        if dup_refs or dup_labels:
            raise ValueError(
                f"{name}: duplicate cin_net refs/labels "
                f"refs={dup_refs or []} labels={dup_labels or []}")
        missing = [label for _ref, label in rows if label not in port_pos]
        if missing:
            raise ValueError(f"{name}: cin_net label(s) missing from solved ports: {missing}")
        refs_ = [r for r, _l in rows]
        ii = [port_pos[l] for _r, l in rows]
        L_ = np.asarray(p.get("port_L"), dtype=float)
        R_100k = np.asarray(p.get("port_R_100k", p.get("port_R")), dtype=float)
        R_dc = np.asarray(p.get("port_R_dc", p.get("port_R")), dtype=float)
        return (refs_, L_[np.ix_(ii, ii)], R_100k[np.ix_(ii, ii)],
                R_dc[np.ix_(ii, ii)])

    full_refs, L_full, _R_full_100k, _R_full_dc = cin_lr(full_p, "full")
    cap_refs, L_cap, R_cap_100k, R_cap_dc = cin_lr(cap_p, "cap_only")
    if full_refs != cap_refs:
        raise ValueError(
            f"full/cap cin refs differ: full={full_refs}, cap_only={cap_refs}")

    sw_ports = list(switch_p.get("ports") or [])
    sw_labels_declared = []
    plane = ((switch_p.get("topo") or {}).get("demarcation_plane") or {})
    if plane.get("switch_residual_port"):
        sw_labels_declared.append(plane["switch_residual_port"])
    sw_labels_declared.extend(plane.get("switch_residual_ports") or [])
    sw_labels_declared = list(dict.fromkeys(sw_labels_declared))
    if len(sw_labels_declared) != 1:
        raise ValueError(
            "switch_residual run must provide exactly one gauge port for this "
            f"reducer helper, got {sw_labels_declared or 'none'}")
    sw_label = sw_labels_declared[0]
    if sw_label not in sw_ports:
        raise ValueError(
            f"switch_residual gauge port {sw_label!r} missing from solved ports")
    i_sw = sw_ports.index(sw_label)
    L_sw_physical = float(np.asarray(switch_p.get("port_L"), dtype=float)[i_sw, i_sw])
    payload = _cin_decomposed_matrix_payload(
        L_full, L_cap, R_cap_100k, full_refs, L_sw_physical, floor=floor,
        R_cap_dc=R_cap_dc,
        r_100k_freq_Hz=cap_p.get("R_100k_freq_Hz"),
        r_dc_freq_Hz=cap_p.get("R_dc_freq_Hz"))
    payload["source_runs"] = dict(
        full_basis=((full_p.get("topo") or {}).get("cin_extraction_basis")),
        cap_basis=((cap_p.get("topo") or {}).get("cin_extraction_basis")),
        switch_basis=((switch_p.get("topo") or {}).get("cin_extraction_basis")),
        switch_residual_port=sw_label,
    )
    return payload


def classify_cin_warnings(p):
    """Split the scalar-trunk caveats into reduce_warn vs reduce_info from the FINAL cin_model.

    Any caller that REPLACES p["cin_model"] after reduce_parasitics() returned MUST re-run
    this. The cap_only/switch_residual split path does exactly that: each individual solve
    resolves scalar_trunk (a single leg cannot see a matrix), and the valid matrix exists only
    once _cin_matrix_from_reductions has combined the three legs. Deciding at solve time would
    therefore never demote on that path — the common case for leaded (lead_mm>0) boards. So the
    verdict is a pure function of the final model, and this is idempotent: it always recomputes
    both lists from the two immutable inputs (reduce_warn_base, reduce_scalar_warn) instead of
    mutating either in place, so re-running after a combine cannot double-append.

    Demotion needs POSITIVE evidence on both axes (mode is a matrix AND matrix_valid is True).
    matrix_valid None means "never evaluated" — unverified, not fine — and keeps the warnings.

    CAVEAT on matrix_valid strength: for the single-run identity path, reduce_parasitics()
    sets it from spice_realizable ALONE — weaker than extract_parasitics's
    _matrix_valid_for_payload (gauge_fix_status, switch_board_copper, symmetry/PSD) and the
    loss consumer's gate. That is safe only because solve_pitch() re-validates strictly and
    aborts on disagreement. A direct caller of solve()/reduce_parasitics() that skips
    solve_pitch() must re-check with the strict predicate before trusting a demotion here;
    demotion never licenses consuming the matrix payload.
    The scalar quantities (cin_L_shared/cin_R_shared, per-cap Lb/Rb, *_switch residuals) stay in
    the JSON either way, so the text is re-labelled, never dropped: a consumer that reads them
    anyway must still be able to find out why they are unsafe.
    """
    base = list(p.get("reduce_warn_base") or [])
    scalar = list(p.get("reduce_scalar_warn") or [])
    cm = p.get("cin_model") or {}
    matrix_in_use = (cm.get("mode") in ("matrix", "matrix_with_sw_coupling")
                     and cm.get("matrix_valid") is True)
    if scalar and matrix_in_use:
        p["reduce_warn"] = base
        p["reduce_info"] = [
            f"scalar shared-trunk Cin reduction was REJECTED and is not what this run emits "
            f"(cin_model.mode={cm.get('mode')}, basis={cm.get('basis')!r}, matrix_valid=true). "
            f"The {len(scalar)} message(s) below are the evidence for that rejection, not "
            f"defects in the emitted matrix. They still apply to any scalar field left in the "
            f"JSON (cin_L_shared, per-cap Lb/Rb in cin_branches and report.md, *_switch "
            f"residuals) — do not consume those."
        ] + scalar
    else:
        p["reduce_warn"] = base + scalar
        p["reduce_info"] = []
    return p


def reduce_parasitics(zc, ports, topo, meta, plateau=5e6, cin_ports=None,
                      cin_esl=0.0, cin_esr=0.0) -> dict:
    f, Z = pick_plateau(zc, plateau)
    w = 2 * np.pi * f
    L = Z.imag / w
    R = Z.real
    idx = {p: i for i, p in enumerate(ports)}
    is_per_device = isinstance(topo, dict) and topo.get("parallel_fets") == "per-device"

    def device_ports(role):
        if not is_per_device:
            return []
        side = (topo or {}).get(role, {}) if isinstance(topo, dict) else {}
        devs = side.get("device_ports") or []
        out = []
        for dev in devs:
            gl = dev.get("gate_label")
            sl = dev.get("switch_label")
            if gl in idx:
                out.append(dict(ref=dev.get("ref"), gate=gl, switch=sl))
        return out

    def gate_labels(role, legacy):
        dev = [d["gate"] for d in device_ports(role)]
        if dev:
            return dev
        if legacy in idx:
            return [legacy]
        prefix = f"P_g{role}_"
        return [p for p in ports if p.startswith(prefix)]

    hs_gate_labels = gate_labels("hs", "P_ghs")
    ls_gate_labels = gate_labels("ls", "P_gls")
    ih = idx.get(hs_gate_labels[0]) if hs_gate_labels else None
    il = idx.get(ls_gate_labels[0]) if ls_gate_labels else None

    if not cin_ports:
        cin_ports = ["P_pwr"]
    cin_idx = [idx[c] for c in cin_ports if c in idx]
    if not cin_idx:
        cin_idx = [idx["P_pwr"]] if "P_pwr" in idx else [0]

    # optional per-cap branch impedance (ESR + jw*ESL) -> physical current split
    def zcap_at(wf):
        if cin_esl <= 0 and cin_esr <= 0:
            return None
        return np.array([cin_esr + 1j * wf * cin_esl] * len(cin_idx), dtype=complex)

    # headline reduction (physical if ESL/ESR given, else ideal copper-only)
    Zeff, y, denom, cond, weights = _eff_commutation(Z, cin_idx, zcap_at(w))
    L_loop = float(Zeff.imag / w)
    R_loop = float(Zeff.real)
    # bracket: ideal copper-only (lower bound) and nearest single cap (upper bound)
    Zeff0, _, _, _, _ = _eff_commutation(Z, cin_idx, None)
    L_loop_ideal = float(Zeff0.imag / w)
    i0 = cin_idx[0]
    L_loop_single = float(Z[i0, i0].imag / w)
    per_cap_L = {ports[j]: float(Z[j, j].imag / w) for j in cin_idx}

    # per-frequency effective L/R across the whole sweep (confirm the plateau)
    sweep = []
    for ff in sorted(zc.keys()):
        Zf = zc[ff]
        wf = 2 * np.pi * ff
        Ze, _, _, cf, _ = _eff_commutation(Zf, cin_idx, zcap_at(wf))
        sweep.append(dict(f=ff, L_eff=float(Ze.imag / wf),
                          R_eff=float(Ze.real), cond=cf))

    # current split keyed by refdes (cin_used order matches cin_ports order)
    refs = (topo or {}).get("cin_used", []) if isinstance(topo, dict) else []
    split = {}
    for k, ci in enumerate(cin_idx):
        name = refs[k] if k < len(refs) else ports[ci]
        wk = complex(weights[k])
        split[name] = dict(re=wk.real, im=wk.imag, mag=abs(wk))

    warn = []
    # Warnings that caveat ONLY the scalar shared-trunk reduction. They are collected
    # apart from `warn` and re-joined below once matrix validity is known: if the run
    # resolved a VALID matrix Cin model, the scalar trunk is not what any deck consumes,
    # so these become INFO context rather than warnings on the emitted model. They are
    # only ever demoted on positive evidence (mode==matrix AND matrix_valid is True) —
    # an unevaluated/None validity keeps them at WARNING.
    scalar_warn = []
    if cond > 1e6:
        warn.append(f"Zc ill-conditioned (cond={cond:.1e}) — parallel reduction "
                    f"unreliable; check cap ports / near-coincident caps")
    neg = [n for n, s in split.items() if s["re"] < -1e-3]
    if neg:
        scalar_warn.append(
            f"negative ideal current share on {neg} — the shorted-cap parallel "
            f"solve contains a circulating current mode. This is allowed by the "
            f"passive port matrix, but it invalidates the scalar_trunk Cin "
            f"reduction. Per-cap --cin-esl/--cin-esr is diagnostic only; it may "
            f"make the split physical but does not make scalar_trunk valid. If "
            f"negative share persists with realistic ESL/ESR, also check port "
            f"polarity / geometry")
    if len(cin_idx) > 1 and L_loop_ideal > L_loop_single + 1e-12:
        warn.append("effective loop L exceeds single-cap L — unexpected for "
                    "parallel caps; check mutual signs / port polarity")
    if len(cin_idx) > 1 and L_loop_ideal < L_loop_single / len(cin_idx) - 1e-12:
        # positively-coupled parallel caps can't drop below the uncoupled floor
        warn.append(f"effective loop L ({L_loop_ideal*1e9:.2f} nH) is below the "
                    f"uncoupled parallel floor ({L_loop_single/len(cin_idx)*1e9:.2f} nH) "
                    f"— likely reversed cap-port polarity or a mutual-sign error")

    # ---- conduction-path resistances (LF, per-side) ----
    # Read R/L at the LOWEST swept frequency: there the skin depth dwarfs the
    # copper thickness, so this is the DC/fundamental conduction R, not
    # the skin-elevated ring-plateau R_loop. P_hs/P_ls are anchored on the bulk cap
    # (the fundamental source), so r_hs/r_ls are each switch's true conduction
    # copper; P_bulk is the full LF loop, and r_sw is the SW-node spreading residual.
    f_dc = min(zc.keys())
    Z_lf = zc[f_dc]
    f_100k, Z_100k = pick_frequency(zc, 1e5)
    w_lf = 2 * np.pi * f_dc
    L_lf = Z_lf.imag / w_lf
    R_dc = Z_lf.real
    R_100k = Z_100k.real
    if f_dc >= f:   # conduction read collapsed onto the ring plateau
        warn.append(
            f"conduction freq ({f_dc:g} Hz) >= plateau ({f:g} Hz) — the ring and "
            f"conduction R collapse to one value; lower --plateau or the sweep fmin")

    def rdc(label):
        i = idx.get(label)
        return float(R_dc[i, i]) if i is not None else None

    def port_group(role, legacy):
        if legacy in idx:
            return [legacy]
        devs = device_ports(role)
        labels = [d.get("switch") for d in devs if d.get("switch") in idx]
        if labels:
            return labels
        prefix = f"P_{role}_"
        return [p for p in ports if p.startswith(prefix)]

    def eff_rdc(labels):
        labels = [l for l in labels if l in idx]
        if not labels:
            return None
        if len(labels) == 1:
            return rdc(labels[0])
        ii = [idx[l] for l in labels]
        Zeff, *_ = _eff_commutation(Z_lf, ii, None)
        return float(Zeff.real)

    hs_switch_labels = port_group("hs", "P_hs")
    ls_switch_labels = port_group("ls", "P_ls")
    r_hs, r_ls = eff_rdc(hs_switch_labels), eff_rdc(ls_switch_labels)
    r_loop_cond = rdc("P_bulk")
    r_sw = None
    if r_hs is not None and r_ls is not None and r_loop_cond is not None:
        r_sw = r_loop_cond - r_hs - r_ls
        if r_sw < -0.05e-3:   # -0.05 mOhm tolerance
            warn.append(
                f"conduction R_hs+R_ls ({(r_hs + r_ls) * 1e3:.2f} mOhm) exceeds LF "
                f"loop R ({r_loop_cond * 1e3:.2f} mOhm) — check P_hs/P_ls port "
                f"polarity or SW-node reference")
    # a single negative per-switch R would emit a non-physical negative Rser
    if (r_hs is not None and r_hs < -0.05e-3) or (r_ls is not None and r_ls < -0.05e-3):
        warn.append(
            f"negative per-switch conduction R (r_hs={(r_hs or 0)*1e3:.2f}, "
            f"r_ls={(r_ls or 0)*1e3:.2f} mOhm) — numerical/geometry artifact; "
            f"would emit a negative Rser")
    cond_ref = (topo or {}).get("cond_ref") if isinstance(topo, dict) else None

    # ---- per-cap branch decomposition (ONLY under --emit-cin-network) ----
    # Uses the dedicated full-bank port set (topo['cin_net']: bulk+mlcc, one port
    # per cap) that geometry adds ONLY under --emit-cin-network — SEPARATE from the
    # MLCC-only HF cin_ports, so the L_loop reduction is never perturbed. It must
    # NOT fall back to the HF / --cin-parallel / --cin-refs set: that set is curated
    # for loop-L accuracy (often MLCC-only, missing the bulk caps), so decomposing
    # it would emit a cin_branches table mislabeled as the full bank while silently
    # dropping the electrolytics — the exact miscompute this feature exists to
    # prevent. cin_branches stays None unless a genuine cin_net was ported.
    cin_net = (topo or {}).get("cin_net") if isinstance(topo, dict) else None
    cin_dec = None
    cin_dec_lf = None
    cin_matrix = None
    if cin_net:
        net_idx = [idx[e["label"]] for e in cin_net if e.get("label") in idx]
        net_refs = [e["ref"] for e in cin_net if e.get("label") in idx]
        net_cls = {e["ref"]: e.get("cls", "mlcc") for e in cin_net}
        net_c = {e["ref"]: e.get("C") for e in cin_net}
        cin_dec = _cin_branch_decomp(L, R_100k, net_idx, net_refs, net_cls, net_c)
        cin_dec_lf = _cin_branch_decomp(L_lf, R_dc, net_idx, net_refs, net_cls, net_c)
        if (topo or {}).get("cin_network_model") == "matrix" and (
                topo or {}).get("fet_closure") == "pad_ideal":
            cin_matrix = _cin_matrix_payload(
                L, R_100k, net_idx, net_refs, basis="identity", R_dc=R_dc,
                r_100k_freq_Hz=f_100k, r_dc_freq_Hz=f_dc)
    if cin_dec:
        _Lsh = float(cin_dec["L_shared"])
        _Lsp = float(cin_dec["L_spread"])
        if _Lsh > 0 and _Lsp > 0.5 * _Lsh:
            scalar_warn.append(
                f"cin branch decomposition: off-diagonal L spread high "
                f"({_Lsp*1e9:.2f} vs shared {_Lsh*1e9:.2f} nH) — single shared-trunk "
                f"model approximate; per-cap Lb less reliable")
        if cin_dec.get("clamped"):
            scalar_warn.append(
                "cin shared-trunk clamped to the smallest cap self-L (heterogeneous "
                "bulk+MLCC bank: off-diagonal mean exceeded a diagonal) — per-cap Lb "
                "near the floor is a shared-trunk-model artifact, not a real ~0 branch")

    cin_dec_raw = cin_dec
    cin_shared_model = None
    if cin_dec:
        cin_dec, cin_shared_model = _cin_decomp_with_model_limits(
            cin_dec, L, R_100k, net_idx, net_refs, net_cls, net_c,
            L_limit=L_loop, R_limit=None)
        if cin_shared_model and cin_shared_model.get("clamped"):
            bits = []
            if cin_shared_model.get("L_clamped"):
                bits.append(
                    f"L_shared raw {cin_shared_model['L_shared_raw']*1e9:.2f} nH "
                    f"exceeds L_loop {L_loop*1e9:.2f} nH")
            if cin_shared_model.get("R_clamped"):
                bits.append(
                    f"R_shared raw {cin_shared_model['R_shared_raw']*1e3:.2f} mOhm "
                    f"exceeds r_hs+r_ls {(r_limit or 0)*1e3:.2f} mOhm")
            scalar_warn.append(
                "cin shared-trunk model clamped for deck consumption: "
                + "; ".join(bits)
                + " — raw values preserved as *_raw")

    # ---- trunk-excluded switch-side residuals (for cin_network double-count) ----
    # L_loop / r_hs / r_ls are the FULL bulk-anchored loop and OVERLAP the cin trunk
    # (cin_L_shared/cin_R_shared): the trunk IS the loop's shared Vin/GND leg. When the
    # loss deck consumes cin_network it places the trunk separately, so Lloop must carry
    # only the switch-side residual or the trunk copper is counted twice (and the ring L
    # ~doubles). Subtract the trunk ONCE: L scalar-wise; R allocated per-side in
    # proportion to r_hs:r_ls so the two subtractions sum to exactly cin_R_shared (never
    # double-subtracting the shared return). Documented convention — an exact Vin-leg vs
    # GND-leg split would need switch-node-referenced ports (a re-solve). Consumers with
    # cin_network use *_switch in Lloop and cin_L_shared/cin_R_shared in the trunk; the
    # trunk then correctly sees INPUT-RIPPLE current, not the full switch current.
    L_loop_switch = r_hs_switch = r_ls_switch = None
    L_loop_switch_raw = r_hs_switch_raw = r_ls_switch_raw = None
    if cin_dec_raw:
        csh_L_raw = float(cin_dec_raw["L_shared"])
        csh_R_raw = float(cin_dec_raw["R_shared"])
        if L_loop is not None:
            L_loop_switch_raw = L_loop - csh_L_raw
        if r_hs is not None and r_ls is not None and (r_hs + r_ls) > 0:
            f_hs = r_hs / (r_hs + r_ls)
            r_hs_switch_raw = r_hs - csh_R_raw * f_hs
            r_ls_switch_raw = r_ls - csh_R_raw * (1.0 - f_hs)
    if cin_dec:
        csh_L = float(cin_dec["L_shared"])
        csh_R = float(cin_dec["R_shared"])
        if L_loop is not None:
            L_loop_switch = L_loop - csh_L
            if L_loop_switch < -0.05e-9:
                scalar_warn.append(
                    f"L_loop_switch negative ({L_loop_switch*1e9:.2f} nH): cin trunk "
                    f"L_shared ({csh_L*1e9:.2f}) exceeds L_loop ({L_loop*1e9:.2f}) — "
                    f"basis mismatch (HF nearest-MLCC loop vs full-bank trunk); clamped 0")
            L_loop_switch = max(0.0, L_loop_switch)
        if r_hs is not None and r_ls is not None and (r_hs + r_ls) > 0:
            f_hs = r_hs / (r_hs + r_ls)
            r_hs_switch = r_hs - csh_R * f_hs
            r_ls_switch = r_ls - csh_R * (1.0 - f_hs)
            if r_hs_switch < -0.05e-3 or r_ls_switch < -0.05e-3:
                # NOT scalar_warn: unlike L_loop_switch (which the matrix deck discards --
                # loss/lib/deck.py:313 hardcodes Lsh=2e-15), r_hs_switch/r_ls_switch ARE
                # required and consumed in matrix mode (deck.py:308-314, :363-367) as the
                # loop-side Rser. Demoting this on a valid matrix run would hand the deck a
                # silently clamped-to-zero conduction R for a whole loop side. It stays a
                # WARNING in every mode.
                warn.append(
                    f"switch-side residual R negative (r_hs_switch="
                    f"{r_hs_switch*1e3:.2f}, r_ls_switch={r_ls_switch*1e3:.2f} mOhm): "
                    f"cin trunk R_shared ({csh_R*1e3:.2f}) exceeds r_hs+r_ls "
                    f"({(r_hs+r_ls)*1e3:.2f}) — trunk/loop basis mismatch; clamped 0")
            r_hs_switch = max(0.0, r_hs_switch)
            r_ls_switch = max(0.0, r_ls_switch)

    cin_diag = dict(valid=True, diagnostics=[], regions=[], metrics={})
    if cin_dec_raw:
        cin_diag = _cin_region_diagnostics(
            L, net_idx, net_refs, weights=weights,
            residual_raw=L_loop_switch_raw)
        for d in cin_diag["diagnostics"]:
            msg = d.get("message")
            if msg:
                scalar_warn.append("scalar cin model invalid: " + msg)
    requested_cin_mode = (
        (topo or {}).get("cin_network_model") if isinstance(topo, dict) else None)
    resolved_cin_mode = "matrix" if cin_matrix else "scalar_trunk"
    # actionable fix for the errors above: the single-trunk reduction broke down on this
    # heterogeneous bank (negative switch residual / circulating-share clamp). Matrix mode
    # keeps the off-diagonal cap coupling, so there is no trunk-vs-loop subtraction to clamp.
    if resolved_cin_mode == "scalar_trunk" and any(
            d.get("severity") == "error" for d in cin_diag.get("diagnostics", [])):
        if requested_cin_mode != "matrix":
            warn.append(
                "scalar cin model invalid -> RECOMMEND matrix mode: re-run with "
                "--emit-cin-network --cin-network-model matrix (keeps off-diagonal cap coupling; "
                "no negative switch residual / circulating-share clamp). The scalar_trunk deck "
                "under-counts the switch-loop L and mis-attributes per-cap Cin current — loss-band "
                "impact may be small, but the HF-ring per-cap attribution is wrong.")
        else:
            # matrix WAS requested but cin_matrix wasn't produced — don't tell them to pass the
            # flag they already passed; point at the real blocker (the emission preconditions).
            #
            # scalar_warn, NOT warn: this asserts a RUN-LEVEL outcome ("falling back to the
            # invalid scalar_trunk reduction"), but a single reduce_parasitics() call is not the
            # whole run. On the cap_only/switch_residual split path every leg legitimately has
            # cin_matrix=None — the matrix is assembled from the legs AFTERWARDS — so emitting
            # this as a hard warning would tell an operator whose combine SUCCEEDED that no
            # matrix was produced. Routing it here lets classify_cin_warnings settle it against
            # the final model: still a WARNING if the run really did fall back to scalar, demoted
            # to INFO if a valid matrix was ultimately emitted.
            scalar_warn.append(
                "scalar cin model invalid AND matrix mode was requested but not produced "
                "(cin_matrix is None) — the identity matrix basis needs the pad-ideal fet "
                "closure; check cin_extraction_basis / cin_closure in the extraction config. "
                "Falling back to the invalid scalar_trunk reduction.")
    matrix_diag = []
    matrix_valid = None
    if requested_cin_mode == "matrix":
        if cin_matrix:
            matrix_valid = bool(cin_matrix.get("spice_realizable"))
            if not matrix_valid:
                matrix_diag.append(dict(
                    severity="error",
                    code="cin_matrix_k_too_high",
                    message=(
                        f"cin identity matrix Kmax={cin_matrix.get('kmax'):.4f} "
                        "is at/above 0.95 SPICE realizability merge threshold")))
        else:
            matrix_valid = False
            matrix_diag.append(dict(
                severity="error",
                code="cin_matrix_basis_unavailable",
                message=(
                    "matrix cin model requested but only the pad_ideal identity "
                    "basis is implemented; use lead_mm=0/leads-internal fixture "
                    "or keep scalar_trunk disabled")))
    cin_model = dict(
        mode=resolved_cin_mode,
        requested_mode=requested_cin_mode,
        basis=(cin_matrix.get("basis") if cin_matrix else None),
        scalar_valid=bool(cin_diag["valid"]),
        scalar_valid_basis="homogeneity_only",
        matrix_valid=matrix_valid,
        full_multiport_required=None,
        switch_separability=dict(
            status=("structurally_not_required" if cin_matrix else "not_evaluated"),
            reason=("identity basis uses the full commutation matrix; no "
                    "cap/switch decomposition is performed" if cin_matrix
                    else "cap-branch-only matrix extraction is not implemented")),
        region_assignment=dict(
            basis=("identity_full_matrix" if cin_matrix else "full_loop_matrix_diagnostic"),
            regions=cin_diag["regions"],
            metrics=cin_diag["metrics"]),
        gauge_fix_status=(cin_matrix.get("gauge_fix_status") if cin_matrix else None),
        gauge_fix_reason=(cin_matrix.get("gauge_fix_reason") if cin_matrix else None),
        diagnostics=cin_diag["diagnostics"] + matrix_diag,
    )


    def eff_loop_csi(g):
        """Effective gate-loop mutual to the full commutation port set.

        Gate-loop voltage per unit *total*
        commutation current, using the parallel-cap current distribution y.
        Reduces to |L[pwr,gate]| when a single cap is ported."""
        if g is None:
            return 0.0
        m = Z[g, cin_idx]                      # gate<->each-cap coupling row
        Zmg = complex(np.dot(m, y) / denom)
        return abs(Zmg.imag / w)

    def side_csi(g, side_label, fallback):
        """Common-source L for the selected switch's own power path.

        P_hs/P_ls are switch-side ports and therefore measure the mutual with the
        source lead the gate loop actually shares. Older sidecars did not have
        those ports, so fall back to the full-loop mutual for compatibility.
        """
        if g is None:
            return 0.0
        side = idx.get(side_label)
        if side is None:
            return fallback
        return abs(float(L[g, side]))

    def LL(a, b):
        return float(L[a, b]) if (a is not None and b is not None) else 0.0

    def RR(a):
        return float(R[a, a]) if a is not None else 0.0

    physical = cin_esl > 0 or cin_esr > 0
    csi_hs_loop = eff_loop_csi(ih)
    csi_ls_loop = eff_loop_csi(il)

    def per_device(role, gate_labels_, switch_labels_):
        if not is_per_device:
            return []
        by_gate = {d.get("gate"): d for d in device_ports(role)}
        rows = []
        for n, gl in enumerate(gate_labels_):
            gi = idx.get(gl)
            dev = by_gate.get(gl, {})
            sl = dev.get("switch")
            if sl not in idx and n < len(switch_labels_):
                sl = switch_labels_[n]
            csi_loop = eff_loop_csi(gi)
            csi_side = side_csi(gi, sl, csi_loop) if sl else csi_loop
            prefix = f"P_g{role}_"
            ref = dev.get("ref") or (gl[len(prefix):] if gl.startswith(prefix) else gl)
            rows.append(dict(
                ref=ref,
                gate_port=gl, switch_port=sl,
                L_gate=LL(gi, gi), R_gate=RR(gi),
                csi=csi_side, csi_loop=csi_loop,
                L_switch=LL(idx.get(sl), idx.get(sl)) if sl in idx else None,
                r_switch=rdc(sl) if sl in idx else None,
            ))
        return rows

    hs_devices = per_device("hs", hs_gate_labels, hs_switch_labels)
    ls_devices = per_device("ls", ls_gate_labels, ls_switch_labels)

    def representative(devices, key, fallback):
        vals = [d[key] for d in devices if d.get(key) is not None]
        if not vals:
            return fallback
        return max(vals)

    csi_hs = representative(hs_devices, "csi", side_csi(ih, "P_hs", csi_hs_loop))
    csi_ls = representative(ls_devices, "csi", side_csi(il, "P_ls", csi_ls_loop))
    L_gate_hs = representative(hs_devices, "L_gate", LL(ih, ih))
    R_gate_hs = representative(hs_devices, "R_gate", RR(ih))
    L_gate_ls = representative(ls_devices, "L_gate", LL(il, il))
    R_gate_ls = representative(ls_devices, "R_gate", RR(il))
    m_gate = LL(ih, il)
    if len(hs_devices) > 1 or len(ls_devices) > 1:
        warn.append(
            "per-device parallel-FET ports present: side-level L_gate/csi scalars are "
            "max-per-device compatibility values; use parallel_devices for per-ref data")

    # A missing gate port (e.g. --allow-missing-gate-ports on a board whose gate
    # routing the KiCad importer dropped) leaves ih/il None, which the helpers
    # above flatten to 0.0 — indistinguishable from a physically-measured zero, so
    # a consumer/report could read "CSI = 0" as real. Encode UNAVAILABLE as None
    # (JSON null) and carry an availability flag so the CLI/report/lib label it and
    # the downstream loss tool can refuse rather than trust a fabricated zero.
    # Availability must also catch PARTIAL per-device loss: device_ports() silently
    # drops a paralleled device whose own gate port is missing, so ih/il can still
    # resolve from a SURVIVING device and the side scalar would report a numeric
    # value that omits the dropped device — masking the loss. Require EVERY expected
    # device gate port to be present for a per-device side to count as available,
    # and record which refs were dropped.
    def _expected_device_ports(role):
        side = (topo or {}).get(role, {}) if isinstance(topo, dict) else {}
        return side.get("device_ports") or []

    def _dropped_device_refs(role):
        if not is_per_device:
            return []
        present = {d["gate"] for d in device_ports(role)}
        return [dev.get("ref") for dev in _expected_device_ports(role)
                if dev.get("gate_label") not in present]

    def _side_gate_available(role, gate_index):
        if gate_index is None:
            return False
        if is_per_device and _expected_device_ports(role):
            return not _dropped_device_refs(role)   # all expected devices present
        return True

    hs_dropped = _dropped_device_refs("hs")
    ls_dropped = _dropped_device_refs("ls")
    hs_gate_available = _side_gate_available("hs", ih)
    ls_gate_available = _side_gate_available("ls", il)
    if not hs_gate_available:
        csi_hs = csi_hs_loop = L_gate_hs = R_gate_hs = None
    if not ls_gate_available:
        csi_ls = csi_ls_loop = L_gate_ls = R_gate_ls = None
    if not (hs_gate_available and ls_gate_available):
        m_gate = None

    p = dict(
        freq_Hz=f,
        L_loop=L_loop, R_loop=R_loop,
        L_loop_ideal=L_loop_ideal,            # copper-only lower bound
        L_loop_single=L_loop_single,          # nearest single cap, upper bound
        L_loop_physical=(L_loop if physical else None),
        per_cap_L=per_cap_L, current_split=split,
        cin_esl=cin_esl, cin_esr=cin_esr, cond_Zc=cond,
        # The two immutable inputs to classify_cin_warnings(); reduce_warn/reduce_info below
        # are DERIVED from them and are recomputed whenever cin_model is replaced (the
        # cap_only/switch_residual combine). Keep both so that decision stays re-runnable.
        reduce_warn_base=warn,
        reduce_scalar_warn=scalar_warn,
        reduce_warn=None, reduce_info=None,   # set by classify_cin_warnings() below
        L_eff_sweep=sweep, n_cin=len(cin_idx),
        L_gate_hs=L_gate_hs,
        R_gate_hs=R_gate_hs,
        L_gate_ls=L_gate_ls,
        R_gate_ls=R_gate_ls,
        gate_ports_available=dict(hs=hs_gate_available, ls=ls_gate_available,
                                  hs_dropped_devices=hs_dropped,
                                  ls_dropped_devices=ls_dropped),
        r_hs=r_hs, r_ls=r_ls, r_loop_cond=r_loop_cond, r_sw=r_sw,
        r_cond_freq=f_dc, R_dc_freq_Hz=f_dc, R_100k_freq_Hz=f_100k,
        cond_ref=cond_ref,
        cin_branches=(cin_dec["branches"] if cin_dec else None),
        cin_L_shared=(cin_dec["L_shared"] if cin_dec else None),
        cin_R_shared=(cin_dec["R_shared"] if cin_dec else None),
        cin_branches_raw=(cin_dec_raw["branches"] if cin_dec_raw else None),
        cin_L_shared_raw=(cin_dec_raw["L_shared"] if cin_dec_raw else None),
        cin_R_shared_raw=(cin_dec_raw["R_shared"] if cin_dec_raw else None),
        cin_shared_model=cin_shared_model,
        # LF/switching-frequency view of the same cap network, for the LF ripple
        # schematic. Keep historical fields above consumer-compatible: L from
        # the loop plateau, R from the 100 kHz damping basis. Put near-DC R only
        # under explicitly LF/DC-named fields.
        cin_branches_lf=(cin_dec_lf["branches"] if cin_dec_lf else None),
        cin_L_shared_lf=(cin_dec_lf["L_shared"] if cin_dec_lf else None),
        cin_R_shared_lf=(cin_dec_lf["R_shared"] if cin_dec_lf else None),
        cin_branch_freq_Hz=f_dc,
        cin_matrix=cin_matrix,
        cin_model=cin_model,
        cin_model_valid=_cin_model_valid_for_mode(cin_model),
        cin_model_diagnostics=cin_model["diagnostics"],
        # trunk-excluded switch-side residuals: consumers with cin_network place THESE
        # in Lloop_hs/ls (not L_loop/r_hs/r_ls) so the trunk copper isn't double-counted
        L_loop_switch=L_loop_switch,
        L_loop_switch_raw=L_loop_switch_raw,
        r_hs_switch=r_hs_switch, r_ls_switch=r_ls_switch,
        r_hs_switch_raw=r_hs_switch_raw, r_ls_switch_raw=r_ls_switch_raw,
        csi_hs=csi_hs,
        csi_ls=csi_ls,
        csi_hs_loop=csi_hs_loop,
        csi_ls_loop=csi_ls_loop,
        m_gate=m_gate,
        port_L=L.tolist(), port_R=R.tolist(), port_R_dc=R_dc.tolist(),
        port_R_100k=R_100k.tolist(), ports=ports, cin_ports=cin_ports,
        topo=topo, meta=meta,
    )
    if is_per_device:
        p["parallel_devices"] = dict(hs=hs_devices, ls=ls_devices)
    # Single-basis verdict. A caller that later replaces cin_model (the cap_only/
    # switch_residual combine) must call classify_cin_warnings(p) again — it is idempotent.
    classify_cin_warnings(p)
    return p


def solve(inp, ports, topo, meta, plateau=5e6, suffix="dcdc", cin_ports=None,
          cin_esl=0.0, cin_esr=0.0):
    zc = parse_zc(run_fasthenry(inp, suffix=suffix))
    return reduce_parasitics(zc, ports, topo, meta, plateau, cin_ports=cin_ports,
                             cin_esl=cin_esl, cin_esr=cin_esr)


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="parse a FastHenry Zc.mat -> parasitics")
    ap.add_argument("zc")
    ap.add_argument("--ports", default="P_pwr,P_ghs,P_gls")
    ap.add_argument("--plateau", type=float, default=5e6)
    args = ap.parse_args()
    zc = parse_zc(args.zc)
    p = reduce_parasitics(zc, args.ports.split(","), {}, {}, args.plateau)
    nH = 1e9
    print(f"plateau f = {p['freq_Hz']:g} Hz")
    print(f"L_loop    = {p['L_loop']*nH:7.2f} nH   R_loop = {p['R_loop']*1e3:.2f} mOhm (HF ring)")
    print(f"L_gate_hs = {p['L_gate_hs']*nH:7.2f} nH   L_gate_ls = {p['L_gate_ls']*nH:7.2f} nH")
    print(f"CSI_hs    = {p['csi_hs']*nH:7.2f} nH   CSI_ls    = {p['csi_ls']*nH:7.2f} nH")
    if p.get("r_hs") is not None:
        print(f"R_hs      = {p['r_hs']*1e3:7.2f} mOhm R_ls   = {p['r_ls']*1e3:.2f} mOhm "
              f"(LF conduction @ {p['r_cond_freq']:g} Hz, SW spread {p.get('r_sw',0)*1e3:.2f} mOhm)")
