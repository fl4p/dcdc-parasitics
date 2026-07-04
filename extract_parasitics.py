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


def run_geom(args, pitch, outdir):
    """Invoke kicad_geom.py under KiCad python; return (inp_path, sidecar)."""
    inp = os.path.join(outdir, f"model_{pitch:g}.inp")
    cmd = [KICAD_PY, os.path.join(LIB, "kicad_geom.py"), args.pcb,
           "--sw", args.sw, "--gnd", args.gnd, "--pitch", str(pitch),
           "--cin-parallel", str(args.cin_parallel),
           "--lead-mm", str(args.lead_mm), "--nwinc", str(args.nwinc),
           "--nhinc", str(args.nhinc), "-o", inp]
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
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"kicad_geom failed (pitch {pitch})")
    side = json.load(open(inp + ".ports.json"))
    return inp, side


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcb")
    ap.add_argument("--sw", required=True, help="switch-node net name")
    ap.add_argument("--gnd", required=True, help="ground net name")
    ap.add_argument("--vin", help="input rail net (auto if omitted)")
    ap.add_argument("--hs-ref", nargs="*", help="force HS FET refdes")
    ap.add_argument("--ls-ref", nargs="*", help="force LS FET refdes")
    ap.add_argument("--hs-gate", help="force HS gate net")
    ap.add_argument("--ls-gate", help="force LS gate net")
    ap.add_argument("--hs-kelvin", action="store_true", help="HS gate uses Kelvin source")
    ap.add_argument("--ls-kelvin", action="store_true", help="LS gate uses Kelvin source")
    ap.add_argument("--pitch", type=float, nargs="+", default=[1.0],
                    help="pour mesh pitch(es) mm; multiple -> convergence sweep")
    ap.add_argument("--cin-parallel", type=int, default=1,
                    help="port the N nearest input caps in parallel for the "
                         "effective (accurate) commutation-loop L; 1 = nearest-cap only")
    ap.add_argument("--cin-refs", nargs="*",
                    help="explicit input-cap refdes to port (overrides nearest-N)")
    ap.add_argument("--include-bulk-cin", action="store_true",
                    help="also port bulk electrolytics (>=10uF); default excludes them")
    ap.add_argument("--cin-esl", type=float, default=0.0,
                    help="per-cap ESL (nH) added to each branch -> physical current "
                         "split at f_ring; 0 = ideal-cap copper-only lower bound")
    ap.add_argument("--cin-esr", type=float, default=0.0, help="per-cap ESR (mOhm)")
    ap.add_argument("--lead-mm", type=float, default=3.0, help="FET exposed-lead length mm")
    ap.add_argument("--nwinc", type=int, default=1, help="skin sub-mesh width (>1: slower, more HF-accurate)")
    ap.add_argument("--nhinc", type=int, default=1, help="skin sub-mesh height")
    ap.add_argument("--plateau", type=float, default=5e6, help="L-plateau frequency Hz")
    ap.add_argument("--svg", action="store_true",
                    help="also write schematic.svg (half-bridge + parasitics)")
    ap.add_argument("-o", "--out", required=True, help="output directory")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="dcdc_par_")

    pitches = sorted(set(args.pitch), reverse=True)  # coarse -> fine
    results = []
    for i, pitch in enumerate(pitches):
        inp, side = run_geom(args, pitch, workdir)
        meta = dict(pitch=pitch, lead_mm=side.get("lead_mm"))
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
