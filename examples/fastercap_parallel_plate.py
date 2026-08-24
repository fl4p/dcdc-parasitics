#!/usr/bin/env python3
"""Emit and optionally solve a two-plate FasterCap validation fixture."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from fastercap import box_surface, run_fastercap, write_fastercap_input  # noqa: E402
from maxwell import maxwell_to_branches, spice_subcircuit  # noqa: E402

EPSILON_0 = 8.8541878128e-12


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output", default="out/fastercap-parallel-plate")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--executable")
    args = parser.parse_args(argv)

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    width = 20e-3
    gap = 2e-3
    thickness = 0.1e-3
    half = width / 2
    conductors = (
        box_surface("plate_low", (-half, -half, 0),
                    (half, half, thickness)),
        box_surface("plate_high", (-half, -half, thickness + gap),
                    (half, half, 2 * thickness + gap)),
    )
    input_path = write_fastercap_input(output / "parallel_plate.lst", conductors)
    analytic = EPSILON_0 * width * width / gap
    print(f"input: {input_path}")
    print(f"parallel-plate estimate without fringing: {analytic:.6g} F")
    if not args.run:
        return 0

    result = run_fastercap(
        input_path,
        conductor_names=tuple(conductor.name for conductor in conductors),
        executable=args.executable,
    )
    pair = next(
        branch for branch in maxwell_to_branches(result.matrix)
        if branch.node_a != "0" and branch.node_b != "0"
    )
    (output / "capacitance.lib").write_text(spice_subcircuit(
        result.matrix, subckt_name="parallel_plate"
    ))
    print(f"FasterCap pair capacitance including fringing: {pair.farads:.6g} F")
    print(f"ratio to parallel-plate estimate: {pair.farads / analytic:.6g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
