#!/usr/bin/env python3
"""Render extracted parasitics as a SPICE subckt (primary), JSON, and Markdown.

Input is the reduced-parasitics dict from solve_reduce.py:
    L_loop, R_loop, L_gate_hs, R_gate_hs, L_gate_ls, R_gate_ls,
    csi_hs, csi_ls, m_gate, freq_Hz, port_L (matrix), topo, meta

The **common-source inductance** is emitted as a *shared source-lead branch*:
each FET's source-lead inductor sits in BOTH the commutation loop and that FET's
gate loop, which is the physical definition of CSI. Exposed pins let a gate-drive
sim choose Kelvin (return to the die node) or non-Kelvin (return to SW/GND):

    .SUBCKT pwrstage VIN SW GND HSG LSG HSKEL LSKEL
      VIN/SW/GND         power nodes (add your Cin across VIN-GND, Coss/devices)
      HSG/LSG            gate-driver outputs
      HSKEL/LSKEL        HS/LS die-source (Kelvin) taps
    drive HS gate between HSG and SW  -> CSI in the loop (non-Kelvin)
                     or  HSG and HSKEL -> CSI excluded (Kelvin sense)
"""
import json
import os


def _fmt(v, unit=""):
    return f"{v:.6g}{unit}"


def _fmt_freq(f):
    if not f:
        return "LF"
    if f >= 1e6:
        return f"{f/1e6:g} MHz"
    if f >= 1e3:
        return f"{f/1e3:g} kHz"
    return f"{f:g} Hz"


def _split_rest(l_loop, csi_hs, csi_ls):
    """Non-shared loop inductance, split HS/LS side (arbitrary split; total is exact)."""
    rest = l_loop - csi_hs - csi_ls
    if rest < 0:
        rest = 0.0
    return rest / 2.0, rest / 2.0


def subckt(p):
    """Return the .SUBCKT text (nanohenry / milliohm values)."""
    nH = 1e9
    # A None CSI/gate value means the gate port was unavailable (gate routing
    # dropped by an importer, extracted with --allow-missing-gate-ports). The
    # SPICE netlist needs a number, so these branches are emitted as 0 — but
    # LABELLED as placeholders + a header WARNING, so a 0 here is never mistaken
    # for a measured zero. parasitics.json carries the honest null.
    hs_gate_available = p.get("csi_hs") is not None
    ls_gate_available = p.get("csi_ls") is not None
    csi_hs = max(p["csi_hs"] or 0.0, 0.0)
    csi_ls = max(p["csi_ls"] or 0.0, 0.0)
    lg_hs = p["L_gate_hs"] or 0.0
    lg_ls = p["L_gate_ls"] or 0.0
    loop_hs, loop_ls = _split_rest(p["L_loop"], csi_hs, csi_ls)
    lghs_rest = max(lg_hs - csi_hs, 0.0)
    lgls_rest = max(lg_ls - csi_ls, 0.0)

    warn = []
    if hs_gate_available and lg_hs - csi_hs < 0:
        warn.append("CSI_hs exceeds HS gate-loop L (clamped) — check gate-return/Kelvin detection")
    if ls_gate_available and lg_ls - csi_ls < 0:
        warn.append("CSI_ls exceeds LS gate-loop L (clamped)")
    if not hs_gate_available:
        warn.append("HS CSI / gate-loop UNAVAILABLE (HS gate routing missing from the "
                    "extraction) — Lscs_hs/Lghs below are 0 PLACEHOLDERS, not measured zeros")
    if not ls_gate_available:
        warn.append("LS CSI / gate-loop UNAVAILABLE (LS gate routing missing from the "
                    "extraction) — Lscs_ls/Lgls below are 0 PLACEHOLDERS, not measured zeros")

    # per-side loop R: total is the HF ring R_loop (plateau, damping), split by the
    # real LF conduction proportion (r_hs:r_ls) when available, else 50/50.
    r_hs, r_ls = p.get("r_hs"), p.get("r_ls")
    if r_hs is not None and r_ls is not None and (r_hs + r_ls) > 0:
        frac_hs = min(1.0, max(0.0, r_hs / (r_hs + r_ls)))  # clamp: never negative Rser
    else:
        frac_hs = 0.5
    rser_hs = p["R_loop"] * frac_hs
    rser_ls = p["R_loop"] * (1.0 - frac_hs)

    t = p["topo"]
    L = lambda v: _fmt(v * nH)  # noqa: E731  henries -> 'nH-number'
    lines = [
        "* ------------------------------------------------------------------",
        "* Power-stage parasitics extracted by dcdc-tools/parasitics",
        f"* board  : {os.path.basename(t.get('pcb',''))}",
        f"* nets   : Vin={t.get('vin')}  SW={t.get('sw')}  GND={t.get('gnd')}",
        f"* HS={','.join(t['hs']['refs'])} ({'Kelvin' if t['hs']['kelvin'] else 'non-Kelvin'})"
        f"  LS={','.join(t['ls']['refs'])} ({'Kelvin' if t['ls']['kelvin'] else 'non-Kelvin'})",
        f"* freq   : {p['freq_Hz']:g} Hz plateau   mesh pitch {p['meta'].get('pitch')} mm"
        + (f"   Cu {p['meta'].get('cu_temp'):g} C" if p['meta'].get('cu_temp') not in (None, 20.0) else ""),
        "* R values are isothermal copper (no self-heating); L is temperature-independent.",
        "* CSI is the shared source-lead branch (Lscs_*). Drive HS gate between",
        "* HSG and SW for non-Kelvin (CSI in loop), or HSG-HSKEL for Kelvin.",
        "* Lloop_hs/Lloop_ls are a 50/50 split of the non-CSI loop L; only total L_loop is solved.",
        "* Rser on Lloop_hs/Lloop_ls is split by LF r_hs:r_ls for damping; only total R_loop is solved.",
    ]
    if p.get("n_cin", 1) > 1:
        kind = "physical (cap ESL/ESR)" if p.get("L_loop_physical") is not None \
            else "ideal-cap copper-only (lower bound)"
        lines.append(
            f"* L_loop = {p['L_loop']*nH:.3g} nH: effective {p['n_cin']} input caps in "
            f"parallel ({','.join(t.get('cin_used', []))}), {kind}.")
        lines.append(
            f"*   bracket: single nearest cap {p['L_loop_single']*nH:.3g} nH (upper) >= "
            f"truth >= ideal-cap {p.get('L_loop_ideal', p['L_loop'])*nH:.3g} nH (lower).")
    for w in warn + (p.get("reduce_warn") or []):
        lines.append(f"* WARNING: {w}")
    # Context for a model this run REJECTED (scalar trunk, when a valid matrix Cin resolved).
    # Not a warning on what is emitted below, but kept in the artifact: the scalar fields are
    # still in parasitics.json, so the reason they are unsafe to consume must travel with them.
    for m in p.get("reduce_info") or []:
        lines.append(f"* INFO: {m}")
    lines += [
        "* ------------------------------------------------------------------",
        ".SUBCKT pwrstage VIN SW GND HSG LSG HSKEL LSKEL",
        f"Lloop_hs VIN  nHS   {L(loop_hs)}n Rser={_fmt(rser_hs)}",
        f"Lscs_hs  nHS  SW    {L(csi_hs)}n            ; HS source lead (SHARED = CSI)"
        + ("" if hs_gate_available else "  [PLACEHOLDER 0 — HS gate unavailable]"),
        f"Lloop_ls SW   nLS   {L(loop_ls)}n Rser={_fmt(rser_ls)}",
        f"Lscs_ls  nLS  GND   {L(csi_ls)}n            ; LS source lead (SHARED = CSI)"
        + ("" if ls_gate_available else "  [PLACEHOLDER 0 — LS gate unavailable]"),
        f"Lghs     HSG  nHS   {L(lghs_rest)}n Rser={_fmt(p['R_gate_hs'] or 0.0)}  ; HS gate branch (driver->die)"
        + ("" if hs_gate_available else "  [PLACEHOLDER 0]"),
        f"Lgls     LSG  nLS   {L(lgls_rest)}n Rser={_fmt(p['R_gate_ls'] or 0.0)}  ; LS gate branch"
        + ("" if ls_gate_available else "  [PLACEHOLDER 0]"),
        "Rhskel   nHS  HSKEL 0                        ; HS die-source (Kelvin tap)",
        "Rlskel   nLS  LSKEL 0                        ; LS die-source (Kelvin tap)",
        ".ENDS pwrstage",
    ]
    return "\n".join(lines) + "\n", warn


def _altium_banner(p):
    """Provenance banner for boards converted from Altium by lib/altium_import.py.

    Returns a list of blockquote markdown lines (empty if the board was not an
    Altium import). The imported board is a RECONSTRUCTION — the KiCad importer
    inverts layers and can DROP copper pours — so absolute L values are flagged
    PROVISIONAL and pinned to a GUI-import cross-check."""
    ai = (p.get("meta") or {}).get("altium_import")
    if not ai:
        return []
    relayer = ai.get("relayer", "partial")
    b = [
        "> ⚠️ **PROVISIONAL — converted from Altium `.PcbDoc`** by "
        f"`lib/altium_import.py` (relayer=`{relayer}`).",
    ]
    fixes = []
    if ai.get("pads_fixed"):
        fixes.append(f"{ai['pads_fixed']} pad layer-sets de-inverted")
    if ai.get("tracks_relayered"):
        fixes.append(f"{ai['tracks_relayered']} power tracks relayered")
    if ai.get("zones_relayered"):
        fixes.append(f"{ai['zones_relayered']} power zones relayered")
    if fixes:
        b.append("> The KiCad importer inverted the stack onto B.Cu; corrected: "
                 + ", ".join(fixes) + ". The multilayer via-stitched return is preserved.")
    vp = ai.get("vb_pour_synthesized")
    if vp:
        bb = vp.get("bbox")
        bbs = (f" bbox {bb}" if bb else "")
        b.append(f"> **A dropped Vin pour was SYNTHESIZED** ({vp.get('area_mm2','?')} mm²{bbs}) "
                 "to bridge the FET drain to the local Cin — this is INVENTED copper, "
                 "so `L_loop` carries geometry uncertainty.")
    sens = ai.get("sensitivity")
    if sens and sens.get("L_loop_lo") is not None and sens.get("L_loop_hi") is not None:
        lo, hi = sens["L_loop_lo"] * 1e9, sens["L_loop_hi"] * 1e9
        basis = sens.get("basis", "synth-pour size sweep")
        b.append(f"> Synth-pour sensitivity: `L_loop` spans **{lo:.2f}–{hi:.2f} nH** "
                 f"({basis}).")
    b.append("> **Validate against a KiCad GUI import** before trusting absolute values.")
    for w in ai.get("warnings", []):
        b.append(f"> - {w}")
    b.append("")
    return b


def markdown(p):
    nH = 1e9
    t = p["topo"]
    L = lambda v: f"{v*nH:.2f} nH"  # noqa: E731
    # None => gate port unavailable (routing dropped, --allow-missing-gate-ports);
    # label it, never render a fabricated 0.00 nH that looks like a measurement.
    LA = lambda v: L(v) if v is not None else "n/a — gate routing unavailable"  # noqa: E731
    lines = [
        f"# Power-stage parasitics — {os.path.basename(t.get('pcb',''))}",
        "",
        *_altium_banner(p),
        f"Extracted by `dcdc-tools/parasitics` at the {p['freq_Hz']:g} Hz plateau "
        f"(mesh pitch {p['meta'].get('pitch')} mm, FET lead {p['meta'].get('lead_mm')} mm).",
        "",
        f"- **Vin** `{t.get('vin')}`  **SW** `{t.get('sw')}`  **GND** `{t.get('gnd')}`",
        f"- **HS** {', '.join(t['hs']['refs'])} — gate `{t['hs']['gate']}` — "
        f"{'Kelvin sense (CSI excluded)' if t['hs']['kelvin'] else 'non-Kelvin (CSI in gate loop)'}",
        f"- **LS** {', '.join(t['ls']['refs'])} — gate `{t['ls']['gate']}` — "
        f"{'Kelvin sense (CSI excluded)' if t['ls']['kelvin'] else 'non-Kelvin (CSI in gate loop)'}",
        f"- **Cin ported** (in order, nearest→): {', '.join(t.get('cin_used', [])) or '(single nearest)'}"
        + (f" — {p['n_cin']} caps in parallel" if p.get('n_cin', 1) > 1 else ""),
        "",
        "| Parasitic | Value |",
        "|---|---|",
        f"| Commutation loop L (Cin→HS→SW→LS→GND){' — %d caps ‖' % p['n_cin'] if p.get('n_cin', 1) > 1 else ''} | **{L(p['L_loop'])}** |",
    ]
    if p.get("n_cin", 1) > 1:
        # SW-peak L is bracketed: single-cap (upper) ≥ truth ≥ ideal-cap copper-only (lower)
        lines.append(
            f"| ⤷ single nearest cap alone — **upper bound** | {L(p['L_loop_single'])} |")
        lines.append(
            f"| ⤷ ideal-cap parallel (copper only) — **lower bound** | {L(p.get('L_loop_ideal', p['L_loop']))} |")
        if p.get("L_loop_physical") is not None:
            lines.append(
                f"| ⤷ with cap ESL {p['cin_esl']*1e9:.2g} nH / ESR {p['cin_esr']*1e3:.2g} mΩ — **physical** | {L(p['L_loop_physical'])} |")
    cu_t = p['meta'].get('cu_temp')
    rtemp = f", {cu_t:g} °C Cu" if cu_t not in (None, 20.0) else ""
    # conduction R rows (LF, bulk-anchored, per switch) — only if the ports solved
    cond_rows = []
    if p.get("r_hs") is not None and p.get("r_ls") is not None:
        fdc = p.get("r_cond_freq") or 0.0
        cref = p.get("cond_ref") or {}
        anchor = f" — anchored on {cref.get('ref')} ({cref.get('cls')})" if cref else ""
        cond_rows = [
            f"| ⤷ HS conduction R (Vin→SW via HS, @ {fdc:.2g} Hz{anchor}) | **{p['r_hs']*1e3:.2f} mΩ** |",
            f"| ⤷ LS conduction R (SW→GND via LS, @ {fdc:.2g} Hz) | **{p['r_ls']*1e3:.2f} mΩ** |",
        ]
        if p.get("r_sw") is not None:
            cond_rows.append(
                f"| ⤷ SW-node spreading R (residual) | {p['r_sw']*1e3:.2f} mΩ |")
    lines += [
        f"| Commutation loop R (HF ring @ {p['freq_Hz']:.2g} Hz{rtemp}) | {p['R_loop']*1e3:.2f} mΩ |",
        *cond_rows,
        f"| HS common-source inductance | **{LA(p['csi_hs'])}** |",
        f"| LS common-source inductance | **{LA(p['csi_ls'])}** |",
        f"| HS gate-loop L | {LA(p['L_gate_hs'])} |",
        f"| LS gate-loop L | {LA(p['L_gate_ls'])} |",
        f"| gate–gate mutual | {LA(p.get('m_gate'))} |",
        "",
        "## Where the common-source inductance sits",
        "",
        "```",
        "  Vin ─Lloop_hs─┐                         gate driver (HS)",
        "                nHS ──Lghs── HSG           │",
        "        (HS die) │                          │ drive between",
        "            Lscs_hs  ◀── SHARED = CSI_hs     │ HSG and SW  (non-Kelvin)",
        "                │                          or HSG and HSKEL (Kelvin)",
        "  SW ───────────┤",
        "                Lloop_ls",
        "                nLS ──Lgls── LSG",
        "            Lscs_ls  ◀── SHARED = CSI_ls",
        "  GND ──────────┘",
        "```",
        "",
        "The HS source lead `Lscs_hs` carries **both** the commutation current and "
        "the HS gate-return current, so power di/dt develops a voltage across it that "
        "opposes the gate drive — the common-source feedback that slows switching and "
        "aggravates shoot-through. Use `parasitics.lib` in a gate-drive/DPT sim; use "
        "`L_loop` with device Coss for switch-node peak-voltage / ringing.",
    ]

    if p.get("r_hs") is not None:
        lines += [
            "",
            "## Two resistances, two frequencies",
            "",
            "The loop carries current at two very different frequencies, so it has two "
            "resistances — don't use one for the other:",
            "",
            f"- **Ring R** ({p['R_loop']*1e3:.2f} mΩ @ {p['freq_Hz']:.2g} Hz) — the HF "
            "commutation-edge loop, anchored on the nearest **MLCC**, skin-elevated. "
            "Sets the switch-node ringing Q / damping.",
            f"- **Conduction R** (HS {p['r_hs']*1e3:.2f} / LS {p['r_ls']*1e3:.2f} mΩ @ "
            f"{p.get('r_cond_freq', 0):.2g} Hz) — the near-DC fundamental, anchored on the "
            "**bulk electrolytics** (the MLCCs are ~open at the switching fundamental and "
            "carry no conduction current). This is the copper the loss tool multiplies by "
            "I²: HS by D, LS by (1−D) — so at **low duty the LS conduction R dominates** "
            "the copper budget. The two sides are split by real geometry, not 50/50.",
        ]

    pdev = p.get("parallel_devices") or {}
    if any(pdev.get(role) for role in ("hs", "ls")):
        lines += [
            "",
            "## Per-device parallel FET parasitics",
            "",
            "`--parallel-fets per-device` was used, so each physical FET has its own "
            "gate-loop and source-lead CSI port. The side-level `L_gate_*` / `csi_*` "
            "rows above are compatibility summaries; use these per-ref rows for "
            "layout-asymmetry checks and the loss deck's per-device branches.",
            "",
            "| Side | Ref | Gate port | Switch port | Gate-loop L | CSI | CSI vs full loop | Switch-path L | Switch R |",
            "|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
        for role, side_name in (("hs", "HS"), ("ls", "LS")):
            for d in pdev.get(role) or []:
                lsw = d.get("L_switch")
                rsw = d.get("r_switch")
                lsw_txt = L(lsw) if lsw is not None else "—"
                rsw_txt = f"{rsw*1e3:.2f} mΩ" if rsw is not None else "—"
                lines.append(
                    f"| {side_name} | {d.get('ref','?')} | `{d.get('gate_port','')}` | "
                    f"`{d.get('switch_port','')}` | {L(d.get('L_gate') or 0)} | "
                    f"{L(d.get('csi') or 0)} | {L(d.get('csi_loop') or 0)} | "
                    f"{lsw_txt} | {rsw_txt} |")

    branches = p.get("cin_branches_lf") or p.get("cin_branches") or []
    if branches:
        cin_l = p.get("cin_L_shared_lf", p.get("cin_L_shared", 0))
        cin_r = p.get("cin_R_shared_lf", p.get("cin_R_shared", 0))
        cin_f = p.get("cin_branch_freq_Hz")
        lines += [
            "",
            "## Input-cap branch network (`--emit-cin-network`)",
            "",
        ]
        # This table IS the shared-trunk decomposition. When the run resolved a valid matrix Cin
        # model, that decomposition was REJECTED and is not what the run emits — say so here, on
        # the table itself, not only in a Notes block at the bottom of the document. reduce_info
        # carries the rejection evidence; without this the reader sees an authoritative-looking
        # per-cap Lb/Rb table with nothing anywhere marking it as the model that lost.
        cm = p.get("cin_model") or {}
        if p.get("reduce_info") and cm.get("matrix_valid") is True:
            lines += [
                f"> ⚠️ **This shared-trunk decomposition was REJECTED for this board.** The run "
                f"emits the **{cm.get('mode')}** Cin model (`{cm.get('basis')}` basis); the "
                f"`Lb`/`Rb` values below are the losing model's, kept for reference only. "
                f"**Do not consume them** — see the rejection evidence in `parasitics.json` "
                f"(`reduce_info`) and the `.lib` header.",
                "",
            ]
        lines += [
            f"Per-cap **copper at {_fmt_freq(cin_f)}** decomposed into a shared "
            f"Vin/GND trunk (**{cin_l*nH:.2f} nH** / {cin_r*1e3:.2f} mΩ) "
            f"plus a private branch per cap "
            f"(`Lb`/`Rb`, from L[i,i]−L_shared). These are LF copper-only values; "
            f"each cap's C/ESR/ESL is a separate dslib term. "
            f"`Rb` is **branch copper**, not dielectric ESR — bulk electrolytics carry "
            f"the 39 kHz ripple ESR loss; ceramics are ~open at fsw.",
            "",
            "| Cap | class | branch L (`Lb`) | branch R (`Rb`) |",
            "|---|---|---|---|",
        ]
        for b in branches:
            lines.append(f"| {b['ref']} | {b['cls']} | {b['Lb']*nH:.2f} nH | "
                         f"{b['Rb']*1e3:.3f} mΩ |")

    split = p.get("current_split") or {}
    if len(split) > 1:
        lines += [
            "",
            "## Input-cap current split (at the ring frequency)",
            "",
            "Fraction of the total commutation current each ported cap carries "
            "(from `y = Zc⁻¹·1`). The parallel loop L is bracketed **single-cap "
            "(upper) ≥ truth ≥ ideal-cap copper-only (lower)**; the truth sits near "
            "the lower bound when cap ESL ≪ per-cap branch L. Ideal caps assume each "
            "MLCC is a short — pass `--cin-esl/--cin-esr` for the physical split.",
            "",
            "| Cap | current share |",
            "|---|---|",
        ]
        for ref, s in split.items():
            lines.append(f"| {ref} | {s['mag']*100:.0f}% |")

    excl = t.get("cin_excluded_bulk") or []
    warns = p.get("reduce_warn") or []
    infos = p.get("reduce_info") or []
    depop = _depop_notes(p)
    if excl or warns or infos or depop:
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        if excl:
            lines.append(f"- Bulk caps excluded by package/type (electrolytic/THT, "
                         f"ineffective at the edge): {', '.join(excl)}. "
                         f"Re-run with `--include-bulk-cin` to keep them.")
        for wm in warns:
            lines.append(f"- **WARNING:** {wm}")
        # Context for the model this run REJECTED. Not a warning on what it emits, but the
        # report renders the rejected model's table, so its evidence has to be in the report.
        for im in infos:
            lines.append(f"- **INFO:** {im}")
        lines += depop

    return "\n".join(lines) + "\n"


# Fraction of the ring current below which a ceramic input cap is doing no HF
# decoupling work — a depopulation candidate. 2% ≈ 1/50th of the loop current;
# below it a cap's branch is so far from the commutation loop it barely conducts.
_RING_DEPOP_FRAC = 0.02


def _depop_notes(p):
    """Depopulation-candidate flag: ceramic input caps carrying negligible RING
    current (they contribute ~no HF decoupling, so the SW peak/ring barely moves
    if they're removed). Keyed off the `y=Zc⁻¹·1` current split.

    ONLY trustworthy from the PHYSICAL split (per-cap ESL/ESR included): the
    ideal-cap copper-only limit treats every MLCC as a short and so OVER-rates far
    caps — flagging from it would wrongly spare exactly the caps you'd cull. So if
    the split is ideal (no --cin-esl/--cin-esr), we refuse to name candidates and
    instead tell the user to re-run physical.

    Scoped to ceramics (bulk electrolytics carry the fsw RIPPLE, not the ring) and
    each candidate is printed WITH its footprint C, because a large ceramic
    (e.g. 10 µF) still does real ripple work at fsw even at ~0 ring share — the
    ring is a necessary, not sufficient, condition for depopulation.

    Classification (mlcc vs bulk) comes from `cin_branches` (an `--emit-cin-network`
    product). WITHOUT it we cannot tell a ceramic from an electrolytic, so we NEVER
    default a cap to ceramic — an unclassifiable sub-threshold cap is reported as a
    can't-classify advisory (re-run with `--emit-cin-network`), never as a bare
    `(mlcc)` candidate. That keeps the "bulk is never flagged" invariant even when
    the split is physical but the branch data is absent."""
    split = p.get("current_split") or {}
    if len(split) < 2:
        return []
    physical = (p.get("cin_esl") or 0) > 0 or (p.get("cin_esr") or 0) > 0
    cls_of = {b["ref"]: b.get("cls", "mlcc") for b in (p.get("cin_branches") or [])}
    c_of = {b["ref"]: b.get("C") for b in (p.get("cin_branches") or [])}
    # sub-threshold caps, sorted lowest-share first, skipping malformed entries.
    sub = sorted(((r, s["mag"]) for r, s in split.items()
                  if s.get("mag") is not None and s["mag"] < _RING_DEPOP_FRAC),
                 key=lambda x: x[1])
    # a cap is a candidate only if it is KNOWN ceramic; unknown class (ref absent from
    # cin_branches) is NOT defaulted to ceramic — bulk must never be flagged.
    cands = [(r, m) for r, m in sub if cls_of.get(r) == "mlcc"]
    unclassifiable = [r for r, _ in sub if r not in cls_of]  # no branch data to classify
    if not physical:
        # ideal-cap split over-rates far caps; refuse to name candidates. Fire only when a
        # sub-threshold cap could plausibly be a ceramic (known-mlcc or not-yet-classified),
        # never for a known-bulk-only set.
        if any(cls_of.get(r) != "bulk" for r, _ in sub):
            return ["- **WARNING:** ring-current split is the **ideal-cap (copper-only)** limit — "
                    "it treats each MLCC as a short and over-rates far caps, so "
                    "depopulation candidates are **not** flagged. Re-run with "
                    "`--cin-esl/--cin-esr` for the physical split."]
        return []
    notes = []
    for r, mag in cands:
        c = c_of.get(r)
        cstr = f", {c*1e9:.0f} nF" if c else ""
        notes.append(
            f"- 🪧 **{r}** (mlcc{cstr}) carries only **{mag*100:.1f}%** of the ring "
            f"current (< {_RING_DEPOP_FRAC*100:.0f}%) — **depopulation candidate** for "
            f"HF decoupling. Verify its fsw ripple role first (a large-C ceramic still "
            f"carries ripple even at ~0 ring share).")
    if unclassifiable:
        notes.append(
            f"- ℹ️ {', '.join(unclassifiable)} carry < {_RING_DEPOP_FRAC*100:.0f}% of the "
            f"ring current but cannot be classified (no `--emit-cin-network` branch data) — "
            f"re-run with `--emit-cin-network` to flag ceramics for depopulation "
            f"(bulk electrolytics are never candidates).")
    return notes


def emit_all(p, outdir, svg=False):
    os.makedirs(outdir, exist_ok=True)
    sub, warn = subckt(p)
    with open(os.path.join(outdir, "parasitics.lib"), "w") as f:
        f.write(sub)
    with open(os.path.join(outdir, "parasitics.json"), "w") as f:
        json.dump(p, f, indent=2)
    with open(os.path.join(outdir, "report.md"), "w") as f:
        f.write(markdown(p))
    if svg:
        import emit_svg
        emit_svg.emit_svg(p, os.path.join(outdir, "schematic.svg"))
        if p.get("cin_branches"):        # full-bank --emit-cin-network run: LF view too
            import emit_svg_lf
            emit_svg_lf.emit_svg_lf(p, os.path.join(outdir, "cin_network.svg"))
    return warn


if __name__ == "__main__":
    import sys
    p = json.load(open(sys.argv[1]))
    print(subckt(p)[0])
    print(markdown(p))
