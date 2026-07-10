#!/usr/bin/env python3
"""Run extract_parasitics.py across a swept parameter, in parallel.

Each swept value is an independent extraction (its own tmp workdir + output
dir), so they fan out across cores with no shared state. FastHenry is
single-threaded, so N concurrent runs on an N-core box finish in roughly the
wall-clock of ONE run instead of N sequential runs.

Example — the ReboostV2 lead_mm sweep, 5 points at once:

    python3 sweep.py --param lead_mm --values 0 0.5 1.0 2.0 3.0 \\
        --out-base /tmp/reboost-convert/psweep -- \\
        /tmp/reboost-convert/reboost_groundtruth.kicad_pcb \\
        --sw HSS --gnd GND --vin Vb --hs-ref Q1 --ls-ref Q2 \\
        --weld-tol 1.7 --allow-missing-gate-ports --pitch 1.0 \\
        --cin-loop-refs C37 --cin-parallel 1 --no-viewer

Everything after `--` is passed through to extract_parasitics.py verbatim;
this script appends `--<param> <value>` and `-o <out-base>_<value>` per run.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRACT = os.path.join(HERE, "extract_parasitics.py")


def one_run(python, param_flag, value, out_dir, passthrough, logf):
    """Run a single extraction; return (value, seconds, result-dict-or-error)."""
    cmd = [python, EXTRACT, *passthrough, param_flag, str(value), "-o", out_dir]
    t0 = time.time()
    with open(logf, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    dt = time.time() - t0
    if rc != 0:
        return value, dt, {"error": f"exit {rc}; see {logf}"}
    try:
        with open(os.path.join(out_dir, "parasitics.json")) as fh:
            d = json.load(fh)
        return value, dt, {
            "L_nH": d.get("L_loop", 0.0) * 1e9,
            "r_hs_mOhm": d.get("r_hs", 0.0) * 1e3,
            "r_ls_mOhm": d.get("r_ls", 0.0) * 1e3,
        }
    except (OSError, ValueError) as e:
        return value, dt, {"error": f"no/invalid parasitics.json: {e}"}


def main():
    ncpu = os.cpu_count() or 4
    ap = argparse.ArgumentParser(
        description="Parallel parameter sweep over extract_parasitics.py",
        epilog="Pass the base extract_parasitics.py args after a literal `--`.")
    ap.add_argument("--param", required=True,
                    help="swept parameter name, e.g. lead_mm or pitch "
                         "(mapped to the --lead-mm / --pitch CLI flag)")
    ap.add_argument("--values", required=True, nargs="+",
                    help="values to sweep")
    ap.add_argument("--out-base", required=True,
                    help="output dir prefix; each run writes <out-base>_<value>")
    ap.add_argument("--jobs", type=int, default=0,
                    help=f"max concurrent runs (default: min(#values, {ncpu-2}))")
    ap.add_argument("--python", default=sys.executable,
                    help="python interpreter for the extract subprocess")
    ap.add_argument("extract_args", nargs=argparse.REMAINDER,
                    help="-- then the base extract_parasitics.py arguments")
    args = ap.parse_args()

    passthrough = args.extract_args
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    if not passthrough:
        ap.error("provide the base extract_parasitics.py args after `--`")

    param_flag = "--" + args.param.replace("_", "-")
    jobs = args.jobs or max(1, min(len(args.values), ncpu - 2))
    print(f"sweep {param_flag} over {args.values}  ({jobs} concurrent, {ncpu} cores)",
          flush=True)

    t0 = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {}
        for v in args.values:
            out_dir = f"{args.out_base}_{v}"
            logf = f"{args.out_base}_{v}.log"
            futs[ex.submit(one_run, args.python, param_flag, v, out_dir,
                           passthrough, logf)] = v
        for fut in as_completed(futs):
            v, dt, res = fut.result()
            results[v] = (dt, res)
            tag = res.get("error") or (
                f"L={res['L_nH']:.3f} nH  r_hs={res['r_hs_mOhm']:.3f}  "
                f"r_ls={res['r_ls_mOhm']:.3f} mOhm")
            print(f"  {param_flag}={v}: {dt:.0f} s  {tag}", flush=True)
    total = time.time() - t0

    print(f"\n=== sweep results ({param_flag}) ===")
    slowest = 0.0
    serial = 0.0
    for v in args.values:
        if v not in results:
            continue
        dt, res = results[v]
        slowest = max(slowest, dt)
        serial += dt
        tag = res.get("error") or (
            f"L_loop={res['L_nH']:7.3f} nH   r_hs={res['r_hs_mOhm']:.3f}   "
            f"r_ls={res['r_ls_mOhm']:.3f} mOhm")
        print(f"{param_flag}={v:<6} {tag}")
    print(f"\nwall-clock {total:.0f} s (slowest single run {slowest:.0f} s; "
          f"sum-of-runs {serial:.0f} s → {serial/total:.1f}x speedup vs serial)")


if __name__ == "__main__":
    main()
