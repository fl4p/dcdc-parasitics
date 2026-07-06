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

With several --pitch values it runs a mesh-convergence sweep and reports the
loop-L drift; the finest pitch is used for the emitted artifacts.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "lib")
sys.path.insert(0, LIB)  # library modules live in lib/; root holds only this CLI

import emit  # noqa: E402
import solve_reduce  # noqa: E402

KICAD_PY = os.environ.get(
    "KICAD_PY",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")

DEFAULTS = {
    "vin": None,
    "hs_ref": None,
    "ls_ref": None,
    "hs_gate": None,
    "ls_gate": None,
    "hs_kelvin": False,
    "ls_kelvin": False,
    "pitch": [1.0],
    "cin_parallel": 1,
    "cin_refs": None,
    "include_bulk_cin": False,
    "emit_cin_network": False,
    "cin_esl": 0.0,
    "cin_esr": 0.0,
    "lead_mm": 3.0,
    "weld_tol": 0.6,
    "margin": 8.0,
    "nwinc": 1,
    "nhinc": 1,
    "cu_temp": 20.0,
    "plateau": 5e6,
    "svg": False,
    "config": None,
}

REQUIRED_ARGS = ("pcb", "sw", "gnd", "out")
LIST_TYPES = {
    "hs_ref": str,
    "ls_ref": str,
    "pitch": float,
    "cin_refs": str,
}
SCALAR_TYPES = {
    "pcb": str,
    "sw": str,
    "gnd": str,
    "vin": str,
    "hs_gate": str,
    "ls_gate": str,
    "cin_parallel": int,
    "cin_esl": float,
    "cin_esr": float,
    "lead_mm": float,
    "weld_tol": float,
    "margin": float,
    "nwinc": int,
    "nhinc": int,
    "cu_temp": float,
    "plateau": float,
    "out": str,
    "config": str,
}
BOOL_ARGS = {
    "hs_kelvin",
    "ls_kelvin",
    "include_bulk_cin",
    "emit_cin_network",
    "svg",
}


def run_geom(args, pitch, outdir):
    """Invoke kicad_geom.py under KiCad python; return (inp_path, sidecar)."""
    inp = os.path.join(outdir, f"model_{pitch:g}.inp")
    cmd = [KICAD_PY, os.path.join(LIB, "kicad_geom.py"), args.pcb,
           "--sw", args.sw, "--gnd", args.gnd, "--pitch", str(pitch),
           "--cin-parallel", str(args.cin_parallel),
           "--lead-mm", str(args.lead_mm), "--nwinc", str(args.nwinc),
           "--nhinc", str(args.nhinc), "--cu-temp", str(args.cu_temp),
           "--weld-tol", str(args.weld_tol), "--margin", str(args.margin),
           "-o", inp]
    for flag, val in (("--vin", args.vin), ("--hs-gate", args.hs_gate),
                      ("--ls-gate", args.ls_gate)):
        if val:
            cmd += [flag, val]
    for flag, vals in (("--hs-ref", args.hs_ref), ("--ls-ref", args.ls_ref),
                       ("--cin-refs", args.cin_refs)):
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
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"kicad_geom failed (pitch {pitch})")
    side = json.load(open(inp + ".ports.json"))
    warn_missing_gate_ports(side, pitch)
    return inp, side


def warn_missing_gate_ports(side, pitch):
    """Warn when missing gate-loop ports will force reported CSI to zero."""
    ports = set(side.get("ports") or [])
    dropped = set((side.get("topo") or {}).get("cin_dropped_ports") or [])
    missing = [p for p in ("P_ghs", "P_gls") if p not in ports]
    if not missing:
        return
    dropped_txt = " dropped by geometry connectivity pruning" if dropped.intersection(missing) else ""
    sys.stderr.write(
        f"WARNING: missing gate-loop port(s) at pitch {pitch:g} mm: "
        f"{', '.join(missing)}{dropped_txt}; reported CSI for those side(s) "
        "will be 0.00 nH. Check gate-net connectivity / gate-driver endpoint "
        "selection; this is not evidence of zero common-source inductance.\n")


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
                    help="explicit input-cap refdes to port (overrides nearest-N)")
    ap.add_argument("--include-bulk-cin", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="also port bulk electrolytics (>=10uF); default excludes them")
    ap.add_argument("--emit-cin-network", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="port the full input-cap bank for the per-cap branch "
                         "decomposition (cin_branches in JSON) the loss tool consumes")
    ap.add_argument("--cin-esl", type=float, default=argparse.SUPPRESS,
                    help="per-cap ESL (nH) added to each branch -> physical current "
                         "split at f_ring; 0 = ideal-cap copper-only lower bound")
    ap.add_argument("--cin-esr", type=float, default=argparse.SUPPRESS,
                    help="per-cap ESR (mOhm)")
    ap.add_argument("--lead-mm", type=float, default=argparse.SUPPRESS,
                    help="FET exposed-lead length mm")
    ap.add_argument("--weld-tol", type=float, default=argparse.SUPPRESS,
                    help="fuse same-net nodes within this many mm in the geometry step")
    ap.add_argument("--margin", type=float, default=argparse.SUPPRESS,
                    help="ROI margin (mm) around FETs/Cin for pour meshing")
    ap.add_argument("--nwinc", type=int, default=argparse.SUPPRESS,
                    help="skin sub-mesh width (>1: slower, more HF-accurate)")
    ap.add_argument("--nhinc", type=int, default=argparse.SUPPRESS,
                    help="skin sub-mesh height")
    ap.add_argument("--cu-temp", type=float, default=argparse.SUPPRESS,
                    help="copper temperature (C) for the reported R (scales sigma, "
                         "R ~ +0.39%%/K); isothermal, no self-heating, L unaffected")
    ap.add_argument("--plateau", type=float, default=argparse.SUPPRESS,
                    help="L-plateau frequency Hz")
    ap.add_argument("--svg", action=argparse.BooleanOptionalAction,
                    default=argparse.SUPPRESS,
                    help="also write schematic.svg (half-bridge + parasitics)")
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

    return argparse.Namespace(**merged)


def main():
    args = parse_args()

    os.makedirs(args.out, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="dcdc_par_")

    pitches = sorted(set(args.pitch), reverse=True)  # coarse -> fine
    results = []
    for i, pitch in enumerate(pitches):
        inp, side = run_geom(args, pitch, workdir)
        meta = dict(pitch=pitch, lead_mm=side.get("lead_mm"),
                    cu_temp=side.get("cu_temp"))
        p = solve_reduce.solve(inp, side["ports"], side["topo"], meta,
                               plateau=args.plateau, suffix=f"p{i}",
                               cin_ports=side.get("cin_ports"),
                               cin_esl=args.cin_esl * 1e-9, cin_esr=args.cin_esr * 1e-3)
        results.append((pitch, p))
        extra = ""
        if p.get("n_cin", 1) > 1:
            extra = (f"  (single-cap {p['L_loop_single']*1e9:.2f} nH -> "
                     f"{p['n_cin']} caps || {p['L_loop']*1e9:.2f} nH)")
        print(f"pitch {pitch:>4} mm : L_loop={p['L_loop']*1e9:6.2f} nH  "
              f"CSI_hs={p['csi_hs']*1e9:5.2f} nH  CSI_ls={p['csi_ls']*1e9:5.2f} nH  "
              f"L_gate_hs={p['L_gate_hs']*1e9:5.2f} nH{extra}")
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
    arts = "parasitics.lib, parasitics.json, report.md" + (", schematic.svg" if args.svg else "")
    print(f"\nwrote {args.out}/{{{arts}}} "
          f"(pitch {pitch} mm, plateau {p['freq_Hz']:g} Hz)")
    for w in warn:
        print(f"  WARNING: {w}")


if __name__ == "__main__":
    main()
