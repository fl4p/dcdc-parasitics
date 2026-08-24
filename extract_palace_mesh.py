#!/usr/bin/env python3
"""Build a source-bound conformal Palace mesh from a complete KiCad PCB."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
sys.path.insert(0, str(LIB))

from kicad_fastercap import parse_stackup  # noqa: E402
from kicad_palace import (  # noqa: E402
    load_pcb_volumes,
    pcb_volume_source_identity,
)
from palace_plc_mesh import generate_palace_plc_mesh  # noqa: E402


KICAD_PY = os.environ.get(
    "KICAD_PY",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
)


def _material(value):
    try:
        name, raw = value.rsplit("=", 1)
        relative_permittivity = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("material must be NAME=ER") from error
    if not name or relative_permittivity <= 0.0:
        raise argparse.ArgumentTypeError("material name and ER must be positive")
    return name, relative_permittivity


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="complete KiCad PCB -> conformal Palace PLC mesh"
    )
    parser.add_argument("pcb")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--material", action="append", type=_material, default=[],
                        help="stackup material override NAME_OR_KIND=ER")
    parser.add_argument("--plating-um", type=float, default=25.0)
    parser.add_argument("--geometry-tolerance-mm", type=float, default=1e-4)
    parser.add_argument("--simplification-area-rtol", type=float, default=1e-4)
    parser.add_argument("--outer-scale", type=float, default=2.0)
    parser.add_argument("--drill-circle-points", type=int, default=64)
    parser.add_argument("--max-planar-area-mm2", type=float)
    parser.add_argument("--max-vertical-step-mm", type=float)
    args = parser.parse_args(argv)

    pcb_path = Path(args.pcb).resolve()
    output = Path(args.output).resolve()
    if not pcb_path.is_file():
        parser.error(f"PCB does not exist: {pcb_path}")
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be empty")
    if len(dict(args.material)) != len(args.material):
        parser.error("material override names must be unique")
    output.mkdir(parents=True, exist_ok=True)
    dump_path = output / "pcb-volumes.json"
    completed = subprocess.run(
        [
            KICAD_PY,
            str(LIB / "kicad_palace_dump.py"),
            str(pcb_path),
            "--all-copper",
            "--output", str(dump_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"KiCad volume dump failed: {detail}")

    stackup = parse_stackup(pcb_path)
    geometry = load_pcb_volumes(
        dump_path,
        stackup,
        plating_thickness_m=args.plating_um * 1e-6,
        material_permittivity=dict(args.material),
        geometry_tolerance_mm=args.geometry_tolerance_mm,
        simplification_area_rtol=args.simplification_area_rtol,
        outer_scale=args.outer_scale,
        drill_circle_points=args.drill_circle_points,
    )
    source_identity = pcb_volume_source_identity(
        geometry, dump_path, pcb_path, stackup
    )
    result = generate_palace_plc_mesh(
        output / "pcb.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
        max_planar_area_m2=(
            None if args.max_planar_area_mm2 is None
            else args.max_planar_area_mm2 * 1e-6
        ),
        max_vertical_step_m=(
            None if args.max_vertical_step_mm is None
            else args.max_vertical_step_mm * 1e-3
        ),
        source_identity=source_identity,
    )
    print(
        f"wrote {result.node_count} nodes, {result.tetrahedron_count} tetrahedra, "
        f"{len(result.terminal_attributes)} terminals to {result.mesh_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
