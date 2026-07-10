#!/usr/bin/env python3
"""Extract half-bridge power-stage parasitics from a KiCad PCB.

    extract_parasitics.py PCB --sw SW_NET --gnd GND_NET [opts] -o OUTDIR

Pipeline (two python interpreters, like the rest of the repo's KiCad work):
  1. KiCad's bundled python runs `kicad_geom.py` -> multiport FastHenry `.inp`
     (+ `.ports.json` sidecar).  [needs pcbnew]
  2. this (system) python runs FastHenry, parses Zc.mat, reduces to named
     parasitics, and writes parasitics.lib / parasitics.json / report.md.

Outputs (in OUTDIR):
  parasitics.lib   SPICE .SUBCKT pwrstage with the common-source inductance as a
                   shared source-lead branch (drop into a gate-drive / DPT sim)
  parasitics.json  named parasitics + full port L/R matrix + provenance
  report.md        human-readable table + topology sketch
  mesh/mesh.html   self-contained layer viewer of the FastHenry mesh over the real
                   PCB copper (toggle F.Cu/B.Cu/vias, pan/zoom); --no-viewer to skip
                   (the mesh_*.png layer rasters land beside it in mesh/)

With several --pitch values it runs a mesh-convergence sweep and reports the
loop-L drift; the finest pitch is used for the emitted artifacts.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "lib")
sys.path.insert(0, LIB)  # library modules live in lib/; root holds only this CLI

import numpy as np  # noqa: E402  (for the LinAlgError type on a degenerate port matrix)
import emit  # noqa: E402
import pcb_source  # noqa: E402
import solve_reduce  # noqa: E402

KICAD_PY = os.environ.get(
    "KICAD_PY",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")

_Y = "\033[33m"  # yellow
_R = "\033[0m"   # reset


def _warn(msg, file=sys.stdout):
    print(f"  {_Y}WARNING{_R}: {msg}", file=file)


def _info(msg, file=sys.stdout):
    print(f"  INFO: {msg}", file=file)


# scalar_trunk-reduction invalidity markers: noise when matrix mode is the resolved model
# (the scalar decomposition is computed as a byproduct but not the deliverable). Demoted to
# a single INFO line when cin_model resolves to a valid identity matrix. NOTE: this is a
# NECESSARY subset of loss.py's consumption gate (_matrix_identity_invalid_reason also checks
# gauge_fix_status / switch_board_copper / spice_realizable) — the extractor may demote here
# while the loss consumer still refuses on one of those stricter fields, so the two are not
# guaranteed identical.
_SCALAR_CIN_REDUCE_MARKERS = (
    "negative ideal current share",
    "cin shared-trunk",  # matches both "…model clamped…" and "…clamped to the smallest cap…"
    "scalar cin model invalid",
)
_CIN_SEPARABILITY_FLOOR_PLACEHOLDER = 0.05e-9


def _cin_separability_floor_metadata(null_scatter=None, gmres_floor=0.0):
    """Return the same-fixture floor used by the additive separability gate.

    The intended floor is a null-drop calibration: remove a size/mesh-matched
    off-path filament group and measure the additive-fit scatter it introduces.
    That geometry basis does not exist yet, so keep the temporary absolute band
    explicit in the payload instead of hiding it behind a user knob.
    """
    gmres_floor = float(gmres_floor or 0.0)
    if gmres_floor < 0 or not np.isfinite(gmres_floor):
        raise ValueError("gmres_floor must be finite and nonnegative")
    if null_scatter is not None:
        null_scatter = float(null_scatter)
        if null_scatter < 0 or not np.isfinite(null_scatter):
            raise ValueError("null_scatter must be finite and nonnegative")
        value = max(null_scatter, gmres_floor)
        source = "same_fixture_null_drop"
        status = "calibrated"
        reason = (
            "floor from matched off-path null-drop perturbation; no user knob")
    else:
        value = max(_CIN_SEPARABILITY_FLOOR_PLACEHOLDER, gmres_floor)
        source = "placeholder_abs"
        status = "placeholder_pending_null_drop"
        reason = (
            "same-fixture null-drop calibration is not implemented yet; using "
            "temporary 0.05 nH absolute floor, not a user-tunable physics knob")
    return dict(
        value=value,
        units="H",
        source=source,
        status=status,
        null_scatter=null_scatter,
        gmres_floor=gmres_floor,
        reason=reason,
    )


def _attach_cin_separability_floor(payload, floor_meta):
    payload["separability_floor"] = dict(floor_meta)
    fit = payload.get("separability_fit")
    if isinstance(fit, dict):
        fit.setdefault("floor", floor_meta["value"])
        fit["floor_source"] = floor_meta["source"]
        fit["floor_status"] = floor_meta["status"]
    sep = payload.get("switch_separability")
    if isinstance(sep, dict):
        sep["floor"] = floor_meta["value"]
        sep["floor_source"] = floor_meta["source"]
        sep["floor_status"] = floor_meta["status"]
    return payload


def _emit_reduce_warnings(p, prefix=""):
    """Print a reduce result's warnings, demoting scalar_trunk-invalidity context to one INFO
    line when matrix mode resolved valid (so requesting cin_network_model=matrix doesn't spew
    scalar warnings for a model that isn't being used)."""
    cm = p.get("cin_model") or {}
    matrix_ok = (cm.get("mode") in ("matrix", "matrix_with_sw_coupling")
                 and bool(cm.get("matrix_valid")))
    skipped = False
    for w in p.get("reduce_warn") or []:
        if matrix_ok and any(m in w for m in _SCALAR_CIN_REDUCE_MARKERS):
            skipped = True
            continue
        _warn(prefix + w)
    if skipped:
        _info(prefix + "scalar_trunk Cin reduction is invalid here (circulating share / "
              "clamped negative switch residual) — expected on this heterogeneous bank; "
              f"cin_network_model=matrix resolved valid "
              f"(mode={cm.get('mode')}, basis={cm.get('basis')}), so it is used instead.")


def _replay_warnings(text):
    """Re-emit captured child-process warnings in the parent CLI style."""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("WARNING:"):
            _warn(s[len("WARNING:"):].strip())
        elif "WARNING:" in s:
            _warn(s)

DEFAULTS = {
    "vin": None,
    "hs_ref": None,
    "ls_ref": None,
    "hs_gate": None,
    "ls_gate": None,
    "hs_package": None,
    "ls_package": None,
    "hs_kelvin": False,
    "ls_kelvin": False,
    "pitch": [1.0],
    "cin_parallel": 1,
    "cin_refs": None,
    "cin_loop_refs": None,
    "cin_network_refs": None,
    "include_bulk_cin": False,
    "emit_cin_network": False,
    "cin_network_model": "scalar_trunk",
    "cin_extraction_basis": "full_loop",
    "cin_closure": "cell_bridge",
    "allow_scalar_cin": False,
    "allow_invalid_scalar_cin": False,
    "allow_missing_gate_ports": False,
    "parallel_fets": "lumped",
    "cin_esl": 0.0,
    "cin_esr": 0.0,
    "lead_mm": 3.0,
    "weld_tol": 0.6,
    "zone_mesh": "grid",
    "terminal_mode": "padland",
    "merge_vias": False,
    "merge_via_radius": 1.0,
    "margin": 8.0,
    "nwinc": 1,
    "nhinc": 1,
    "cu_temp": 20.0,
    "cu_thickness": 0.035,
    "lf_freq": 1e3,
    "plateau": 5e6,
    "svg": False,
    "viewer": True,
    "config": None,
}

REQUIRED_ARGS = ("pcb", "sw", "gnd", "out")
LIST_TYPES = {
    "hs_ref": str,
    "ls_ref": str,
    "pitch": float,
    "cin_refs": str,
    "cin_loop_refs": str,
    "cin_network_refs": str,
}
SCALAR_TYPES = {
    "pcb": str,
    "sw": str,
    "gnd": str,
    "vin": str,
    "hs_gate": str,
    "ls_gate": str,
    "hs_package": str,
    "ls_package": str,
    "cin_parallel": int,
    "cin_esl": float,
    "cin_esr": float,
    "lead_mm": float,
    "weld_tol": float,
    "cin_network_model": str,
    "cin_extraction_basis": str,
    "cin_closure": str,
    "zone_mesh": str,
    "terminal_mode": str,
    "merge_via_radius": float,
    "margin": float,
    "nwinc": int,
    "nhinc": int,
    "cu_temp": float,
    "cu_thickness": float,
    "lf_freq": float,
    "plateau": float,
    "out": str,
    "config": str,
    "parallel_fets": str,
}
BOOL_ARGS = {
    "hs_kelvin",
    "ls_kelvin",
    "merge_vias",
    "include_bulk_cin",
    "emit_cin_network",
    "allow_scalar_cin",
    "allow_invalid_scalar_cin",
    "allow_missing_gate_ports",
    "svg",
    "viewer",
}


def run_geom(args, pitch, outdir, tag=None):
    """Invoke kicad_geom.py under KiCad python; return (inp_path, sidecar)."""
    stem = f"model_{pitch:g}" + (f"_{tag}" if tag else "")
    inp = os.path.join(outdir, f"{stem}.inp")
    cmd = [KICAD_PY, os.path.join(LIB, "kicad_geom.py"), args.pcb,
           "--sw", args.sw, "--gnd", args.gnd, "--pitch", str(pitch),
           "--cin-parallel", str(args.cin_parallel),
           "--lead-mm", str(args.lead_mm), "--nwinc", str(args.nwinc),
           "--nhinc", str(args.nhinc), "--cu-temp", str(args.cu_temp),
           "--cu-thickness", str(args.cu_thickness),
           "--lf-freq", str(args.lf_freq),
           "--weld-tol", str(args.weld_tol), "--zone-mesh", args.zone_mesh,
           "--terminal-mode", args.terminal_mode,
           "--margin", str(args.margin),
           "-o", inp]
    if args.merge_vias:
        cmd += ["--merge-vias", "--merge-via-radius", str(args.merge_via_radius)]
    if args.allow_missing_gate_ports:
        cmd.append("--allow-missing-gate-ports")
    for flag, val in (("--vin", args.vin), ("--hs-gate", args.hs_gate),
                      ("--ls-gate", args.ls_gate)):
        if val:
            cmd += [flag, val]
    for flag, vals in (("--hs-ref", args.hs_ref), ("--ls-ref", args.ls_ref),
                       ("--cin-refs", args.cin_refs),
                       ("--cin-loop-refs", args.cin_loop_refs),
                       ("--cin-network-refs", args.cin_network_refs)):
        if vals:
            cmd += [flag] + vals
    if args.hs_kelvin:
        cmd.append("--hs-kelvin")
    if args.ls_kelvin:
        cmd.append("--ls-kelvin")
    if args.include_bulk_cin:
        cmd.append("--include-bulk-cin")
    if args.emit_cin_network:
        cmd.append("--emit-cin-network")
    cmd += ["--cin-network-model", args.cin_network_model]
    cmd += ["--cin-extraction-basis", args.cin_extraction_basis]
    cmd += ["--cin-closure", args.cin_closure]
    cmd += ["--parallel-fets", args.parallel_fets]
    env = dict(os.environ, PYTHONHASHSEED="0")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"kicad_geom failed (pitch {pitch})")
    _replay_warnings(r.stdout)
    _replay_warnings(r.stderr)
    side = json.load(open(inp + ".ports.json"))
    require_gate_ports(
        side, pitch, allow_missing_gate_ports=args.allow_missing_gate_ports)
    return inp, side


def fmt_work_units(n):
    n = float(n or 0)
    for suffix, scale in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= scale:
            return f"{n / scale:.3g}{suffix}"
    return f"{n:.0f}"


def mesh_complexity_line(pitch, side):
    mesh = side.get("mesh") or {}
    if not mesh:
        return None
    return (
        f"pitch {pitch:>4} mm mesh: "
        f"{mesh.get('nodes', 0)} nodes, {mesh.get('segs', 0)} segs, "
        f"~{mesh.get('filaments_est', 0)} filaments "
        f"(nwinc={mesh.get('nwinc', 1)}, nhinc={mesh.get('nhinc', 1)}), "
        f"{mesh.get('ports', 0)} ports, {mesh.get('freq_points', 0)} freqs, "
        f"work~{fmt_work_units(mesh.get('work_units', 0))}"
    )


def _clone_args(args, **updates):
    vals = vars(args).copy()
    vals.update(updates)
    return argparse.Namespace(**vals)


def _meta_for_side(args, pitch, side, pcb_input, pcb_sha256, config_sha256, altium_meta):
    return dict(pitch=pitch, lead_mm=side.get("lead_mm"),
                cu_temp=side.get("cu_temp"), cu_thickness=side.get("cu_thickness"),
                lf_freq=side.get("lf_freq"),
                zone_mesh=side.get("zone_mesh"),
                terminal_mode=side.get("terminal_mode"),
                via_merge=side.get("via_merge"),
                zone_mesh_notes=side.get("zone_mesh_notes", []),
                terminal_regions=side.get("terminal_regions", []),
                terminal_fallbacks=side.get("terminal_fallbacks", []),
                pcb_source=pcb_input, pcb_resolved=args.pcb,
                pcb_sha256=pcb_sha256,
                extract_config=args.config, extract_config_sha256=config_sha256,
                altium_import=altium_meta)


def _inject_packages(args, side):
    """Stamp CLI-supplied FET packages onto topo.{hs,ls}.package so they flow
    through to parasitics.json (emit copies topo verbatim). The loss deck reads
    topo.*.package to complete die-only source/gate leads on a copper-only run."""
    topo = (side or {}).get("topo") or {}
    for side_key, pkg in (("hs", getattr(args, "hs_package", None)),
                          ("ls", getattr(args, "ls_package", None))):
        if pkg and isinstance(topo.get(side_key), dict):
            topo[side_key]["package"] = pkg


def _run_reduce_basis(args, pitch, workdir, pcb_input, pcb_sha256, config_sha256,
                      altium_meta, basis, suffix, run_geom_fn=run_geom,
                      solve_fn=solve_reduce.solve):
    basis_args = _clone_args(args, cin_extraction_basis=basis)
    inp, side = run_geom_fn(basis_args, pitch, workdir, tag=basis)
    _inject_packages(args, side)
    meta = _meta_for_side(
        basis_args, pitch, side, pcb_input, pcb_sha256, config_sha256, altium_meta)
    p = solve_fn(inp, side["ports"], side["topo"], meta,
                 plateau=args.plateau, suffix=suffix,
                 cin_ports=side.get("cin_ports"),
                 cin_esl=args.cin_esl * 1e-9, cin_esr=args.cin_esr * 1e-3)
    return inp, side, p


def _matrix_valid_for_payload(payload):
    if not payload:
        return False, "missing cin_matrix payload"
    if payload.get("basis") == "identity":
        if payload.get("gauge_fix_status") != "structurally_not_required":
            return False, (
                f"identity payload gauge_fix_status={payload.get('gauge_fix_status')!r} "
                "is not structurally_not_required")
        if payload.get("switch_board_copper") != "in_matrix":
            return False, "identity payload does not affirm switch_board_copper='in_matrix'"
        if payload.get("L_sw_element") != 0.0:
            return False, "identity payload L_sw_element must be exactly 0"
        try:
            L = np.asarray(payload.get("L"), dtype=float)
            R = np.asarray(payload.get("R"), dtype=float)
            refs = list(payload.get("refs") or [])
            if L.ndim != 2 or L.shape[0] != L.shape[1] or L.shape[0] != len(refs):
                return False, "identity payload L matrix is not square or does not match refs"
            if R.shape != L.shape:
                return False, "identity payload R matrix shape does not match L"
            if not np.all(np.isfinite(L)) or not np.all(np.isfinite(R)):
                return False, "identity payload matrices contain non-finite values"
            if np.max(np.abs(R - R.T)) > max(1e-6, 1e-3 * float(np.max(np.abs(R)) or 0.0)):
                return False, "identity payload R matrix is not symmetric"
            for r_name in ("R_100k", "R_dc"):
                if payload.get(r_name) is None:
                    continue
                Rx = np.asarray(payload.get(r_name), dtype=float)
                if Rx.shape != L.shape:
                    return False, f"identity payload {r_name} matrix shape does not match L"
                if not np.all(np.isfinite(Rx)):
                    return False, f"identity payload {r_name} matrix contains non-finite values"
                if np.max(np.abs(Rx - Rx.T)) > max(1e-6, 1e-3 * float(np.max(np.abs(Rx)) or 0.0)):
                    return False, f"identity payload {r_name} matrix is not symmetric"
                for i, ref in enumerate(refs):
                    if Rx[i, i] < 0:
                        return False, f"identity payload ref {ref!r} has negative self {r_name}"
            if np.max(np.abs(L - L.T)) > max(5e-12, 1e-3 * float(np.max(np.abs(L)) or 0.0)):
                return False, "identity payload L matrix is not symmetric"
            l_scale = max(float(np.max(np.abs(L))), 1e-30)
            l_min = float(np.linalg.eigvalsh(L)[0])
            if l_min < -max(1e-18, 1e-9 * l_scale):
                return False, f"identity payload L matrix is not positive semidefinite (min eigenvalue {l_min:.6g})"
            for i, ref in enumerate(refs):
                if L[i, i] <= 0:
                    return False, f"identity payload ref {ref!r} has non-positive self L"
                if R[i, i] < 0:
                    return False, f"identity payload ref {ref!r} has negative self R"
        except (TypeError, ValueError, np.linalg.LinAlgError) as e:
            return False, f"identity payload numeric validation failed: {e}"
        if payload.get("spice_realizable") is not True:
            return False, (
                f"identity payload is not SPICE-realizable "
                f"(Kmax={payload.get('kmax')!r})")
        return True, None
    if payload.get("basis") not in ("cap_only_additive", "cap_only"):
        return False, f"payload basis={payload.get('basis')!r} is not supported"
    if payload.get("mode") == "none" or payload.get("full_multiport_required") is True:
        if payload.get("full_multiport_required") is not True:
            return False, "payload mode='none' must set full_multiport_required=true"
        return False, (
            "payload requires full multiport fallback; matrix subcircuit emission "
            "is refused because the cap/switch additive decomposition failed")
    if payload.get("mode") not in ("matrix", "matrix_with_sw_coupling"):
        return False, f"payload mode={payload.get('mode')!r} is not a matrix mode"
    if payload.get("full_multiport_required") is not False:
        return False, "payload does not explicitly clear full_multiport_required"
    if payload.get("decomposition_valid") is not True:
        return False, "payload decomposition_valid is not true"
    if payload.get("gauge_fix_status") != "fixed":
        return False, f"payload gauge_fix_status={payload.get('gauge_fix_status')!r} is not fixed"
    if payload.get("switch_board_copper") != "split_lsw_element":
        return False, "payload does not affirm switch_board_copper='split_lsw_element'"
    if payload.get("L_sw_element") is None:
        return False, "payload missing L_sw_element"
    try:
        L = np.asarray(payload.get("L"), dtype=float)
        R = np.asarray(payload.get("R"), dtype=float)
        refs = list(payload.get("refs") or [])
        if L.ndim != 2 or L.shape[0] != L.shape[1] or L.shape[0] != len(refs):
            return False, "payload L matrix is not square or does not match refs"
        if R.shape != L.shape:
            return False, "payload R matrix shape does not match L"
        if not np.all(np.isfinite(L)) or not np.all(np.isfinite(R)):
            return False, "payload matrices contain non-finite values"
        for r_name in ("R_100k", "R_dc"):
            if payload.get(r_name) is None:
                continue
            Rx = np.asarray(payload.get(r_name), dtype=float)
            if Rx.shape != L.shape:
                return False, f"payload {r_name} matrix shape does not match L"
            if not np.all(np.isfinite(Rx)):
                return False, f"payload {r_name} matrix contains non-finite values"
            if np.max(np.abs(Rx - Rx.T)) > max(1e-6, 1e-3 * float(np.max(np.abs(Rx)) or 0.0)):
                return False, f"payload {r_name} matrix is not symmetric"
        if np.max(np.abs(L - L.T)) > max(5e-12, 1e-3 * float(np.max(np.abs(L)) or 0.0)):
            return False, "payload L matrix is not symmetric"
        if np.max(np.abs(R - R.T)) > max(1e-6, 1e-3 * float(np.max(np.abs(R)) or 0.0)):
            return False, "payload R matrix is not symmetric"
        l_scale = max(float(np.max(np.abs(L))), 1e-30)
        l_min = float(np.linalg.eigvalsh(L)[0])
        if l_min < -max(1e-18, 1e-9 * l_scale):
            return False, f"payload L matrix is not positive semidefinite (min eigenvalue {l_min:.6g})"
        for i, ref in enumerate(refs):
            if L[i, i] <= 0:
                return False, f"payload ref {ref!r} has non-positive self L"
            if R[i, i] < 0:
                return False, f"payload ref {ref!r} has negative self R"
        for i, ri in enumerate(refs):
            for j in range(i + 1, len(refs)):
                denom = float(abs(L[i, i] * L[j, j])) ** 0.5
                kij = abs(float(L[i, j]) / denom) if denom > 0 else 0.0
                if kij >= 0.95:
                    return False, f"payload K({ri},{refs[j]})={kij:.4f} is at/above 0.95"
        lsw = float(payload.get("L_sw_element"))
        if not np.isfinite(lsw) or lsw <= 0:
            return False, "payload L_sw_element must be finite and positive"
        for item in payload.get("switch_couplings") or []:
            if not isinstance(item, dict):
                return False, "payload switch_couplings entries must be objects"
            ref = item.get("ref")
            if ref not in refs:
                return False, f"payload switch_coupling ref {ref!r} is not in refs"
            kij = float(item.get("K"))
            if not np.isfinite(kij):
                return False, f"payload switch_coupling {ref!r} has non-finite K"
            if abs(kij) >= 0.95:
                return False, f"payload switch_coupling {ref!r} K is at/above 0.95"
    except (TypeError, ValueError, np.linalg.LinAlgError) as e:
        return False, f"payload numeric validation failed: {e}"
    if payload.get("spice_realizable") is not True:
        return False, (
            f"payload is not SPICE-realizable (Kmax={payload.get('kmax')!r}, "
            f"switch_kmax={payload.get('switch_kmax')!r})")
    return True, None


def _cap_only_region_assignment(payload, floor_meta=None):
    refs = list(payload.get("refs") or [])
    floor = 0.0
    if floor_meta:
        floor = float(floor_meta.get("value") or 0.0)
    try:
        L = np.asarray(payload.get("L"), dtype=float)
    except (TypeError, ValueError):
        return dict(
            basis="cap_only_offdiag",
            status="unavailable",
            reason="cap-only L matrix is not numeric",
            regions=[],
            metrics={},
            homogeneous=None,
            n_regions=None,
        )
    if L.ndim != 2 or L.shape[0] != L.shape[1] or L.shape[0] != len(refs):
        return dict(
            basis="cap_only_offdiag",
            status="unavailable",
            reason="cap-only L matrix shape does not match refs",
            regions=[],
            metrics={},
            homogeneous=None,
            n_regions=None,
        )
    n = len(refs)
    if n <= 1:
        return dict(
            basis="cap_only_offdiag",
            status="passed",
            reason="single_cap",
            regions=[dict(ref=refs[0], mean_mutual=0.0, region=0,
                          weak_region=False)] if refs else [],
            metrics=dict(offdiag_mean=0.0, offdiag_std=0.0,
                         offdiag_spread_ratio=0.0, weak_threshold=floor),
            homogeneous=True,
            n_regions=1 if refs else 0,
        )
    off = [float(L[i, j]) for i in range(n) for j in range(n) if i != j]
    mean = float(np.mean(off)) if off else 0.0
    std = float(np.std(off)) if off else 0.0
    spread_ratio = std / abs(mean) if abs(mean) > 1e-30 else 0.0
    row_mean = np.array([
        np.mean([L[i, j] for j in range(n) if j != i])
        for i in range(n)
    ], dtype=float)
    near = float(np.median(row_mean))
    weak_threshold = max(floor, 0.5 * abs(near))
    weak = [bool(abs(near) > 0.0 and m < near - weak_threshold) for m in row_mean]
    n_regions = 1 + (1 if any(weak) else 0)
    regions = []
    for i, m in enumerate(row_mean):
        regions.append(dict(
            ref=refs[i],
            mean_mutual=float(m),
            region=(1 if weak[i] else 0),
            weak_region=weak[i],
        ))
    return dict(
        basis="cap_only_offdiag",
        status=("heterogeneous" if n_regions > 1 else "passed"),
        reason=("weak_cap_region_detected" if n_regions > 1 else "single_region"),
        regions=regions,
        metrics=dict(
            offdiag_mean=mean,
            offdiag_std=std,
            offdiag_spread_ratio=spread_ratio,
            row_median=near,
            weak_threshold=weak_threshold,
        ),
        homogeneous=(n_regions == 1),
        n_regions=n_regions,
    )


def _apply_cin_matrix_payload(full_p, payload, requested_mode):
    p = dict(full_p)
    p["cin_matrix"] = payload
    old_model = full_p.get("cin_model") or {}
    old_diag = [
        dict(d, scalar_context=True, severity="info")
        for d in (old_model.get("diagnostics") or full_p.get("cin_model_diagnostics") or [])
        if isinstance(d, dict)
    ]
    matrix_diag = []
    matrix_valid, invalid_reason = _matrix_valid_for_payload(payload)
    if not matrix_valid:
        full_multiport = (
            payload.get("mode") == "none"
            or payload.get("full_multiport_required") is True)
        matrix_diag.append(dict(
            severity="error",
            code=("cin_full_multiport_required" if full_multiport
                  else "cin_matrix_decomposition_invalid"),
            message=(
                "matrix cin model requested but decomposed cap/switch payload "
                f"cannot be emitted: {invalid_reason}")))
    region_assignment = _cap_only_region_assignment(
        payload, payload.get("separability_floor"))
    cin_model = dict(
        mode=payload.get("mode"),
        requested_mode=requested_mode,
        basis=payload.get("basis"),
        scalar_valid=old_model.get("scalar_valid"),
        scalar_valid_basis=old_model.get("scalar_valid_basis", "homogeneity_only"),
        matrix_valid=matrix_valid,
        full_multiport_required=payload.get("full_multiport_required"),
        full_multiport_valid=payload.get("full_multiport_valid"),
        full_multiport_reason=payload.get("full_multiport_reason"),
        switch_separability=payload.get("switch_separability"),
        separability_floor=payload.get("separability_floor"),
        region_assignment=region_assignment,
        gauge_fix_status=payload.get("gauge_fix_status"),
        gauge_fix_reason=payload.get("gauge_fix_reason"),
        diagnostics=old_diag + matrix_diag,
    )
    p["cin_model"] = cin_model
    p["cin_model_valid"] = matrix_valid
    p["cin_model_diagnostics"] = cin_model["diagnostics"]
    return p


def solve_pitch(args, pitch, idx, workdir, pcb_input, pcb_sha256, config_sha256,
                altium_meta, run_geom_fn=run_geom, solve_fn=solve_reduce.solve,
                combine_fn=solve_reduce._cin_matrix_from_reductions):
    requested = args.cin_network_model
    matrix_requested = args.emit_cin_network and requested in (
        "matrix", "matrix_with_sw_coupling")
    if not matrix_requested:
        return _run_reduce_basis(
            args, pitch, workdir, pcb_input, pcb_sha256, config_sha256,
            altium_meta, args.cin_extraction_basis, f"p{idx}",
            run_geom_fn=run_geom_fn, solve_fn=solve_fn)

    _info(
        f"pitch {pitch:g}: matrix Cin requested — running full_loop basis; "
        "cap_only/switch_residual will run only if a split basis is required")
    full_inp, full_side, full_p = _run_reduce_basis(
        args, pitch, workdir, pcb_input, pcb_sha256, config_sha256, altium_meta,
        "full_loop", f"p{idx}_full", run_geom_fn=run_geom_fn, solve_fn=solve_fn)
    identity_payload = full_p.get("cin_matrix") or {}
    identity_model = full_p.get("cin_model") or {}
    identity_valid, identity_reason = _matrix_valid_for_payload(identity_payload)
    if (identity_model.get("mode") == "matrix"
            and identity_payload.get("basis") == "identity"):
        if identity_valid:
            _info(
                f"pitch {pitch:g}: pad-ideal matrix Cin resolved to identity "
                "basis; cap_only/switch_residual split not required")
            return full_inp, full_side, full_p
        raise RuntimeError(
            f"pitch {pitch:g}: identity matrix Cin payload is invalid "
            f"({identity_reason}); refusing cap_only/switch_residual fallback for "
            "pad-ideal identity basis")
    _cap_inp, _cap_side, cap_p = _run_reduce_basis(
        args, pitch, workdir, pcb_input, pcb_sha256, config_sha256, altium_meta,
        "cap_only", f"p{idx}_cap", run_geom_fn=run_geom_fn, solve_fn=solve_fn)
    _sw_inp, _sw_side, switch_p = _run_reduce_basis(
        args, pitch, workdir, pcb_input, pcb_sha256, config_sha256, altium_meta,
        "switch_residual", f"p{idx}_switch", run_geom_fn=run_geom_fn, solve_fn=solve_fn)
    floor_meta = _cin_separability_floor_metadata()
    payload = combine_fn(
        full_p, cap_p, switch_p, floor=floor_meta["value"])
    _attach_cin_separability_floor(payload, floor_meta)
    p = _apply_cin_matrix_payload(full_p, payload, requested)
    return full_inp, full_side, p


def require_gate_ports(side, pitch, allow_missing_gate_ports=False):
    """Fail hard when missing gate-loop ports would force reported CSI to zero.

    kicad_geom.validate_required_ports already rejects this before FastHenry, so
    a ports sidecar that reaches here without P_ghs/P_gls means the child bypassed
    that gate. Refuse to emit a bogus 0.00 nH CSI rather than warn and continue."""
    ports = set(side.get("ports") or [])
    topo = side.get("topo") or {}
    if topo.get("cin_extraction_basis") in ("cap_only", "switch_residual"):
        return
    dropped = set((side.get("topo") or {}).get("cin_dropped_ports") or [])

    def required(role, legacy):
        if topo.get("parallel_fets") == "per-device":
            labels = [d.get("gate_label") for d in
                      ((topo.get(role) or {}).get("device_ports") or [])
                      if d.get("gate_label")]
            if labels:
                return labels
        return [legacy]

    missing = [p for p in required("hs", "P_ghs") + required("ls", "P_gls")
               if p not in ports]
    if not missing:
        return
    dropped_txt = " dropped by geometry connectivity pruning" if dropped.intersection(missing) else ""
    if allow_missing_gate_ports:
        _warn(
            f"missing gate-loop port(s) at pitch {pitch:g} mm: "
            f"{', '.join(missing)}{dropped_txt}; CSI for those side(s) will be "
            "unavailable. This was allowed by --allow-missing-gate-ports.")
        return
    raise SystemExit(
        f"missing gate-loop port(s) at pitch {pitch:g} mm: "
        f"{', '.join(missing)}{dropped_txt}; reported CSI for those side(s) "
        "would be 0.00 nH. Check gate-net connectivity / gate-driver endpoint "
        "selection; this is not evidence of zero common-source inductance.")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcb", nargs="?", default=argparse.SUPPRESS)
    ap.add_argument("--config", default=argparse.SUPPRESS,
                    help="YAML file containing CLI args as argparse dest names")
    ap.add_argument("--sw", default=argparse.SUPPRESS, help="switch-node net name")
    ap.add_argument("--gnd", default=argparse.SUPPRESS, help="ground net name")
    ap.add_argument("--vin", default=argparse.SUPPRESS, help="input rail net (auto if omitted)")
    ap.add_argument("--hs-ref", nargs="*", default=argparse.SUPPRESS,
                    help="force HS FET refdes")
    ap.add_argument("--ls-ref", nargs="*", default=argparse.SUPPRESS,
                    help="force LS FET refdes")
    ap.add_argument("--hs-gate", default=argparse.SUPPRESS, help="force HS gate net")
    ap.add_argument("--ls-gate", default=argparse.SUPPRESS, help="force LS gate net")
    ap.add_argument("--hs-package", default=argparse.SUPPRESS,
                    help="HS FET package, emitted as topo.hs.package so the loss deck can "
                         "complete die-only source/gate leads on a copper-only extraction. "
                         "The deck models TO-220, TO-247, D2PAK, DPAK, TDSON-8; an "
                         "unrecognized name is passed through but the deck will decline to "
                         "complete leads and warn (it does not guess a generic default).")
    ap.add_argument("--ls-package", default=argparse.SUPPRESS,
                    help="LS FET package (see --hs-package).")
    ap.add_argument("--hs-kelvin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="HS gate uses Kelvin source")
    ap.add_argument("--ls-kelvin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="LS gate uses Kelvin source")
    ap.add_argument("--pitch", type=float, nargs="+", default=argparse.SUPPRESS,
                    help="pour mesh pitch(es) mm; multiple -> convergence sweep")
    ap.add_argument("--cin-parallel", type=int, default=argparse.SUPPRESS,
                    help="port the N nearest input caps in parallel for the "
                         "effective (accurate) commutation-loop L; 1 = nearest-cap only")
    ap.add_argument("--cin-refs", nargs="*", default=argparse.SUPPRESS,
                    help="deprecated alias for --cin-loop-refs")
    ap.add_argument("--cin-loop-refs", nargs="*", default=argparse.SUPPRESS,
                    help="explicit input-cap refdes for the HF commutation-loop "
                         "reduction (overrides nearest-N)")
    ap.add_argument("--cin-network-refs", nargs="*", default=argparse.SUPPRESS,
                    help="explicit input-cap refdes to include in --emit-cin-network; "
                         "default is every discovered input cap")
    ap.add_argument("--include-bulk-cin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="also port bulk electrolytics (>=10uF); default excludes them")
    ap.add_argument("--emit-cin-network", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="port the full input-cap bank for the per-cap branch "
                         "decomposition (cin_branches in JSON) the loss tool consumes")
    ap.add_argument("--cin-network-model",
                    choices=("scalar_trunk", "matrix"),
                    default=argparse.SUPPRESS,
                    help="Cin copper contract for --emit-cin-network: scalar_trunk "
                         "is the legacy one-trunk model; matrix requests run full, "
                         "cap-only, and switch-residual bases and may resolve to "
                         "matrix_with_sw_coupling")
    ap.add_argument("--cin-extraction-basis",
                    choices=("full_loop", "cap_only", "switch_residual"),
                    default=argparse.SUPPRESS,
                    help="FET closure basis used by the KiCad FastHenry deck; "
                         "full_loop is the legacy die-short model")
    ap.add_argument("--cin-closure",
                    choices=("cell_bridge", "per_fet"),
                    default=argparse.SUPPRESS,
                    help="Plane-P closure gauge for cap_only/switch_residual comparison runs")
    ap.add_argument("--allow-scalar-cin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="emit scalar_trunk cin_network artifacts even when the "
                         "single-trunk validity gate fails")
    ap.add_argument("--allow-invalid-scalar-cin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help=argparse.SUPPRESS)
    ap.add_argument("--allow-missing-gate-ports", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="downgrade missing gate-loop ports to warnings. This is "
                         "for degraded Altium/GaN recovery runs where gate routing "
                         "was dropped; default is a hard failure to avoid reporting "
                         "bogus 0 nH CSI")
    ap.add_argument("--parallel-fets", choices=("lumped", "per-device"),
                    default=argparse.SUPPRESS,
                    help="parallel switch model: lumped (legacy) or per-device gates/leads")
    ap.add_argument("--cin-esl", type=float, default=argparse.SUPPRESS,
                    help="per-cap ESL (nH) added to each branch -> physical current "
                         "split at f_ring; 0 = ideal-cap copper-only lower bound")
    ap.add_argument("--cin-esr", type=float, default=argparse.SUPPRESS,
                    help="per-cap ESR (mOhm)")
    ap.add_argument("--lead-mm", type=float, default=argparse.SUPPRESS,
                    help="FET exposed-lead length mm")
    ap.add_argument("--weld-tol", type=float, default=argparse.SUPPRESS,
                    help="fuse same-net nodes within this many mm in the geometry step")
    ap.add_argument("--zone-mesh", choices=("grid", "polygon"), default=argparse.SUPPRESS,
                    help="power-pour mesher: grid is validated/default; polygon is "
                         "experimental cell-edge clipping for KiPEX-style cross-checks")
    ap.add_argument("--terminal-mode", choices=("padland", "single", "finite", "point"),
                    default=argparse.SUPPRESS,
                    help="pad-to-pour terminal model: padland is validated/default; "
                         "single is KiPEX-like nearest mesh node; finite uses finite "
                         "pad-copper spokes to mesh nodes; point is legacy/debug "
                         "pad-center stitch")
    ap.add_argument("--merge-vias", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="collapse dense same-net via fields into equivalent barrels "
                         "to shrink the FastHenry mesh (parallel R exact; spread-out "
                         "loop-path vias stay individual). Provenance in parasitics.json")
    ap.add_argument("--merge-via-radius", type=float, default=argparse.SUPPRESS,
                    help="cluster radius (mm) for --merge-vias (default 1.0)")
    ap.add_argument("--margin", type=float, default=argparse.SUPPRESS,
                    help="ROI margin (mm) around FETs/Cin for pour meshing")
    ap.add_argument("--nwinc", type=int, default=argparse.SUPPRESS,
                    help="skin sub-mesh width (>1: slower, more HF-accurate)")
    ap.add_argument("--nhinc", type=int, default=argparse.SUPPRESS,
                    help="skin sub-mesh height")
    ap.add_argument("--cu-temp", type=float, default=argparse.SUPPRESS,
                    help="copper temperature (C) for the reported R (scales sigma, "
                         "R ~ +0.39%%/K); isothermal, no self-heating, L unaffected")
    ap.add_argument("--cu-thickness", type=float, default=argparse.SUPPRESS,
                    help="copper thickness in mm for FastHenry segment height")
    ap.add_argument("--lf-freq", type=float, default=argparse.SUPPRESS,
                    help="lowest FastHenry sweep frequency Hz for near-DC conduction "
                         "R; default 1 kHz")
    ap.add_argument("--plateau", type=float, default=argparse.SUPPRESS,
                    help="L-plateau frequency Hz")
    ap.add_argument("--svg", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="also write schematic.svg (half-bridge + parasitics)")
    ap.add_argument("--viewer", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="write mesh/mesh.html (self-contained layer viewer of the "
                         "FastHenry mesh + real PCB copper overlay, with mesh_*.png "
                         "rasters beside it); on by default")
    ap.add_argument("-o", "--out", default=argparse.SUPPRESS, help="output directory")
    return ap


def _load_config(path):
    try:
        import yaml
    except ImportError:
        raise SystemExit("extract_parasitics: PyYAML required for --config; "
                         "install with `pip install pyyaml`")
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as e:
        raise SystemExit(f"{path}: failed to load YAML config: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a YAML mapping at top level")
    return data


def _coerce_scalar(name, value, typ):
    if value is None:
        return None
    if typ is str:
        if not isinstance(value, str):
            raise TypeError("expected string")
        return value
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("expected integer")
        return value
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("expected number")
        return float(value)
    raise AssertionError(f"unhandled scalar type for {name}")


def _validate_config(config, path):
    allowed = set(REQUIRED_ARGS) | set(DEFAULTS)
    unknown = sorted(set(config) - allowed)
    if unknown:
        keys = ", ".join(unknown)
        raise SystemExit(f"{path}: unknown config key(s): {keys}")

    out = {}
    for key, value in config.items():
        try:
            if key in BOOL_ARGS:
                if not isinstance(value, bool):
                    raise TypeError("expected boolean")
                out[key] = value
            elif key in LIST_TYPES:
                if value is None:
                    out[key] = None
                elif isinstance(value, list):
                    if key == "pitch" and not value:
                        raise TypeError("expected non-empty list")
                    out[key] = [_coerce_scalar(key, item, LIST_TYPES[key]) for item in value]
                else:
                    raise TypeError("expected list")
            elif key in SCALAR_TYPES:
                out[key] = _coerce_scalar(key, value, SCALAR_TYPES[key])
            else:
                raise AssertionError(f"unhandled config key {key}")
        except TypeError as e:
            raise SystemExit(f"{path}: {key}: {e}")
    return out


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    pre_args, _ = pre.parse_known_args(argv)

    yaml_args = {}
    if pre_args.config:
        yaml_args = _validate_config(_load_config(pre_args.config), pre_args.config)

    ap = build_parser()
    cli_args = vars(ap.parse_args(argv))
    merged = {}
    merged.update(DEFAULTS)
    merged.update(yaml_args)
    merged.update(cli_args)

    missing = [name for name in REQUIRED_ARGS if not merged.get(name)]
    if missing:
        ap.error("missing required argument(s): " + ", ".join(missing))
    if merged["cu_thickness"] <= 0:
        ap.error("--cu-thickness must be > 0 mm")
    if merged["lf_freq"] <= 0:
        ap.error("--lf-freq must be > 0 Hz")
    if merged.get("merge_vias") and merged["merge_via_radius"] <= 0:
        ap.error("--merge-via-radius must be > 0 mm")
    if merged["parallel_fets"] not in ("lumped", "per-device"):
        ap.error("--parallel-fets must be one of: lumped, per-device")
    if merged["cin_network_model"] not in ("scalar_trunk", "matrix"):
        ap.error("--cin-network-model must be one of: scalar_trunk, matrix")
    if merged["cin_extraction_basis"] not in ("full_loop", "cap_only", "switch_residual"):
        ap.error("--cin-extraction-basis must be one of: full_loop, cap_only, "
                 "switch_residual")
    if merged["cin_closure"] not in ("cell_bridge", "per_fet"):
        ap.error("--cin-closure must be one of: cell_bridge, per_fet")
    if merged["zone_mesh"] not in ("grid", "polygon"):
        ap.error("--zone-mesh must be one of: grid, polygon")
    if merged["terminal_mode"] not in ("padland", "single", "finite", "point"):
        ap.error("--terminal-mode must be one of: padland, single, finite, point")
    if merged.get("cin_refs") and merged.get("cin_loop_refs"):
        ap.error("--cin-refs is an alias for --cin-loop-refs; pass only one")
    if merged.get("cin_refs"):
        merged["cin_loop_refs"] = merged["cin_refs"]
        merged["cin_refs"] = None
    if merged.get("allow_invalid_scalar_cin"):
        merged["allow_scalar_cin"] = True

    return argparse.Namespace(**merged)


def main():
    args = parse_args()

    os.makedirs(args.out, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="dcdc_par_")
    pcb_input = args.pcb
    args.pcb = pcb_source.resolve_pcb_path(args.pcb, workdir, config_path=args.config)

    # --- Altium auto-conversion (subprocess under KiCad Python) ---
    altium_meta = None
    if pcb_input.lower().endswith(".pcbdoc"):
        kicad_path = os.path.join(workdir, "altium_converted.kicad_pcb")
        print(f"  Altium .PcbDoc detected — converting to KiCad ...")
        cmd = [
            KICAD_PY,
            os.path.join(HERE, "lib", "altium_import.py"),
            args.pcb,
            "-o", kicad_path,
            "--vin", args.vin or "Vb",
            "--gnd", args.gnd or "GND",
        ]
        if args.sw:
            cmd += ["--sw", args.sw]
        if args.hs_ref:
            cmd += ["--hs-ref"] + args.hs_ref
        if args.ls_ref:
            cmd += ["--ls-ref"] + args.ls_ref
        if args.cin_loop_refs:
            cmd += ["--cin-refs"] + args.cin_loop_refs
        cmd += ["--relayer", "partial"]
        cmd += ["--meta-out", kicad_path + ".altium.json"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise SystemExit(
                f"altium_import.py failed (exit {r.returncode}):\n{r.stderr}")
        import json as _json
        meta_path = kicad_path + ".altium.json"
        try:
            with open(meta_path) as mf:
                altium_meta = _json.load(mf)
        except (OSError, ValueError):
            altium_meta = {"warnings": ["altium conversion metadata parse failed"]}
        for w in altium_meta.get("warnings", []):
            _warn(w)
        vp = altium_meta.get("vb_pour_synthesized")
        print(f"  Converted: {altium_meta.get('pads_fixed', 0)} pads fixed, "
              f"{altium_meta.get('tracks_relayered', 0)} tracks relayered, "
              f"{altium_meta.get('zones_relayered', 0)} zones relayered"
              + (f", Vb pour synthesized ({vp['area_mm2']} mm^2)" if vp else ""))
        args.pcb = kicad_path

    pcb_sha256 = pcb_source.file_sha256(args.pcb)
    config_sha256 = pcb_source.file_sha256(args.config) if args.config else None

    pitches = sorted(set(args.pitch), reverse=True)  # coarse -> fine
    results = []
    for i, pitch in enumerate(pitches):
        try:
            inp, side, p = solve_pitch(
                args, pitch, i, workdir, pcb_input, pcb_sha256, config_sha256,
                altium_meta)
        except np.linalg.LinAlgError as e:
            # near-degenerate port matrix (e.g. SVD in np.linalg.cond fails to converge) —
            # almost always a mesh too coarse for this geometry. Let it crash, but point at
            # the knob: a finer --pitch. Re-raise the original so the traceback is preserved.
            _warn(f"pitch {pitch:g} mm: linear-algebra failure in the parasitic reduction "
                  f"({e}). The port matrix is near-degenerate — the mesh is likely too coarse "
                  f"at this pitch. Retry with a finer --pitch"
                  + (f" (finer than {pitch:g}; the shipped default is 1)."
                     if pitch > 1 else "."))
            raise
        line = mesh_complexity_line(pitch, side)
        if line:
            print(line)
        results.append((pitch, p))
        if (args.emit_cin_network and args.cin_network_model in (
                "matrix", "matrix_with_sw_coupling")
                and p.get("cin_model_valid") is not True):
            diag = p.get("cin_model_diagnostics") or []
            detail = "; ".join(d.get("message", str(d)) for d in diag) or "unknown reason"
            raise SystemExit(
                "matrix cin_network model is invalid for this extraction: "
                + detail)
        if (args.emit_cin_network and args.cin_network_model == "scalar_trunk"
                and not args.allow_scalar_cin
                and p.get("cin_model_valid") is False):
            diag = p.get("cin_model_diagnostics") or []
            reason = "; ".join(d.get("message", str(d)) for d in diag) or "unknown"
            raise SystemExit(
                "scalar_trunk cin_network model is invalid for this cap matrix: "
                f"{reason}. Use --allow-scalar-cin to emit the legacy "
                "clamped scalar anyway, or use --cin-network-model matrix to run "
                "the cap-only/switch-residual matrix extraction.")
        extra = ""
        if p.get("n_cin", 1) > 1:
            extra = (f"  (single-cap {p['L_loop_single']*1e9:.2f} nH -> "
                     f"{p['n_cin']} caps || {p['L_loop']*1e9:.2f} nH)")
        def _nh(v):  # None (missing gate port) -> labelled, never a fake 0.00
            return f"{v*1e9:.2f} nH" if v is not None else "n/a (gate port unavailable)"
        print(f"pitch {pitch:>4} mm : L_loop={p['L_loop']*1e9:6.2f} nH  "
              f"CSI_hs={_nh(p['csi_hs'])}  CSI_ls={_nh(p['csi_ls'])}  "
              f"L_gate_hs={_nh(p['L_gate_hs'])}{extra}")
        _emit_reduce_warnings(p, prefix=f"pitch {pitch:g}: " if len(pitches) > 1 else "")
        if p.get("r_hs") is not None:
            cr = (p.get("cond_ref") or {}).get("ref", "?")
            print(f"            R_conduction (LF, bulk={cr}): "
                  f"HS={p['r_hs']*1e3:.2f} mOhm  LS={p['r_ls']*1e3:.2f} mOhm  "
                  f"SW-spread={p.get('r_sw', 0)*1e3:.2f} mOhm   "
                  f"(ring R_loop={p['R_loop']*1e3:.2f} mOhm)")

    if len(results) > 1:
        ll = [p["L_loop"] for _, p in results]
        drift = (max(ll) - min(ll)) / (sum(ll) / len(ll)) * 100
        print(f"\nmesh convergence: L_loop drift {drift:.1f}% across pitches "
              f"{[f'{pt}' for pt, _ in results]}")

    pitch, p = results[-1]  # finest
    warn = emit.emit_all(p, args.out, svg=args.svg)
    arts = ["parasitics.lib", "parasitics.json", "report.md"]
    if args.svg:
        arts.append("schematic.svg")
    # persist the finest-pitch mesh (`inp` is finest here; loop ends coarse->fine) so
    # downstream tools (loss-density map) can consume it; independent of the viewer flag.
    if persist_mesh(args, inp):
        arts.append("mesh/model.inp")
    viewer_msg = None
    if args.viewer:
        # `inp`/`side` are the finest pitch here (loop ends coarse->fine).
        viewer_msg = write_viewer(args, inp, workdir)
        if viewer_msg:
            arts.append("mesh/mesh.html")
    print(f"\nwrote {args.out}/{{{', '.join(arts)}}} "
          f"(pitch {pitch} mm, plateau {p['freq_Hz']:g} Hz)")
    if args.viewer and viewer_msg:
        print(f"  {viewer_msg}")
    for w in warn:
        _warn(w)


def dump_copper(pcb, dst):
    """Dump real-PCB copper to `dst` (copper.json, mm frame) via copper_dump.py under
    KiCad python. Returns dst on success, else None. Shared by persist_mesh + write_viewer."""
    try:
        r = subprocess.run([KICAD_PY, os.path.join(HERE, "copper_dump.py"), pcb, "-o", dst],
                           capture_output=True, text=True)
        return dst if (r.returncode == 0 and os.path.exists(dst)) else None
    except OSError:
        return None


def persist_mesh(args, inp):
    """Copy the finest-pitch FastHenry mesh (`.inp` + `.ports.json`) into `out/mesh/`
    as `model.inp` / `model.inp.ports.json`, plus a real-PCB `copper.json` overlay. The
    extractor otherwise leaves the mesh in a temp workdir; persisting it lets downstream
    tools (e.g. the loss-density map) consume the exact mesh + board overlay via the file
    contract, like `parasitics.json`. Best-effort, never fatal. Returns the mesh path or None."""
    try:
        mesh_dir = os.path.join(args.out, "mesh")
        os.makedirs(mesh_dir, exist_ok=True)
        dst = os.path.join(mesh_dir, "model.inp")
        shutil.copyfile(inp, dst)
        ports = inp + ".ports.json"
        if os.path.exists(ports):
            shutil.copyfile(ports, dst + ".ports.json")
        dump_copper(args.pcb, os.path.join(mesh_dir, "copper.json"))   # board overlay
        return dst
    except OSError as e:
        sys.stderr.write(f"  (mesh persist skipped: {e})\n")
        return None


def write_viewer(args, inp, workdir):
    """Render the finest-pitch mesh as a self-contained mesh.html layer viewer, with
    a best-effort real-PCB copper underlay (needs KiCad python for copper_dump.py).
    All mesh artifacts (mesh.html + mesh_*.png layer rasters) go into a `mesh/`
    subfolder of the output dir so they don't clutter the top-level artifact set.
    Returns a status string, or None if rendering failed (never fatal to the run)."""
    mesh_dir = os.path.join(args.out, "mesh")
    persisted = os.path.join(mesh_dir, "copper.json")   # reuse persist_mesh's dump if present
    copper = persisted if os.path.exists(persisted) else dump_copper(
        args.pcb, os.path.join(workdir, "copper.json"))
    os.makedirs(mesh_dir, exist_ok=True)
    out_html = os.path.join(mesh_dir, "mesh.html")
    try:
        import mesh_viewer  # noqa: E402

        return "mesh/mesh.html: " + mesh_viewer.build_viewer(
            inp, out_html,
            ports_json=inp + ".ports.json", copper=copper)
    except Exception as e:                      # viewer is a convenience artifact, not core
        sys.stderr.write(f"  (mesh.html skipped: {e})\n")
        return None


if __name__ == "__main__":
    main()
