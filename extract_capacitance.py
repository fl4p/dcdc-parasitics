#!/usr/bin/env python3
"""Emit a FasterCap deck from grouped KiCad filled copper zones."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
sys.path.insert(0, str(LIB))

import pcb_source  # noqa: E402
from fastercap import write_fastercap_input  # noqa: E402
from fastercap_reduction import (  # noqa: E402
    ReductionContext,
    ThicknessReductionRequest,
    apply_thickness_reduction,
    diagnostic_artifact_stem,
    expand_conductor_components,
    load_geometry_manifest,
    write_content_addressed_manifest,
)
from kicad_fastercap import (  # noqa: E402
    KICAD_GRID_MM,
    QUALITY_MESH_ENGINE,
    QUALITY_MESH_MIN_ANGLE_DEG,
    load_filled_zone_dump,
    load_filled_zone_surfaces,
    parse_stackup,
)
from kicad_fastercap_schema import parse_group  # noqa: E402

KICAD_PY = os.environ.get(
    "KICAD_PY",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="KiCad filled zones -> constrained FasterCap conductor deck"
    )
    parser.add_argument("pcb")
    parser.add_argument("--group", action="append", required=True,
                        help="output conductor and KiCad nets: NAME=NET[,NET...]")
    parser.add_argument("-o", "--out", required=True)
    parser.add_argument("--geometry-tolerance", type=float, default=1e-3,
                        help="boundary simplification tolerance in mm (default: 0.001)")
    parser.add_argument(
        "--representation", choices=("physical", "effective-thickness"),
        default="physical",
        help="closed conductor thickness representation (default: physical)",
    )
    parser.add_argument(
        "--effective-thickness-um", type=float,
        help="effective closed-conductor thickness in micrometres",
    )
    parser.add_argument(
        "--anchor", choices=("midplane", "top", "bottom"),
        default="midplane", help="surface held fixed during thickness reduction",
    )
    parser.add_argument(
        "--separate-components", action="store_true",
        help="solve disconnected solids separately and record ideal-short groups",
    )
    args = parser.parse_args(argv)

    if args.representation == "physical":
        if args.effective_thickness_um is not None:
            parser.error(
                "--effective-thickness-um requires --representation "
                "effective-thickness"
            )
        effective_thickness_m = None
    else:
        if args.effective_thickness_um is None:
            parser.error(
                "--representation effective-thickness requires "
                "--effective-thickness-um"
            )
        effective_thickness_m = args.effective_thickness_um * 1e-6
    try:
        reduction_request = ThicknessReductionRequest(
            args.representation.replace("-", "_"),
            effective_thickness_m,
            args.anchor,
        )
    except ValueError as error:
        parser.error(str(error))

    try:
        groups = dict(parse_group(value) for value in args.group)
    except ValueError as error:
        parser.error(str(error))
    if len(groups) != len(args.group):
        parser.error("conductor group names must be unique")

    pcb_path = Path(args.pcb).resolve()
    if not pcb_path.is_file():
        parser.error(f"PCB does not exist: {pcb_path}")
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    dump_path = output / "filled_zones.json"
    command = [
        KICAD_PY,
        str(LIB / "kicad_fastercap_dump.py"),
        str(pcb_path),
    ]
    for value in args.group:
        command.extend(("--group", value))
    command.extend(("--output", str(dump_path)))
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"KiCad filled-zone dump failed: {detail}")

    stackup = parse_stackup(pcb_path)
    zone_dump = load_filled_zone_dump(dump_path)
    extraction = load_filled_zone_surfaces(
        dump_path,
        stackup,
        geometry_tolerance_mm=args.geometry_tolerance,
    )
    reduction_context = ReductionContext(
        extraction.source_mode, "air_only"
    )
    try:
        reduction = apply_thickness_reduction(
            extraction.surfaces, reduction_request, reduction_context
        )
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    conductor_policy = (
        "base_components" if args.separate_components else "grouped"
    )
    artifact_stem = diagnostic_artifact_stem(
        reduction_context, reduction_request, conductor_policy
    )
    deck_path = output / f"{artifact_stem}.lst"
    reduction_provenance = dict(reduction.provenance)
    if args.separate_components:
        expansion = expand_conductor_components(reduction.conductors)
        deck_conductors = expansion.conductors
        assembly = {
            "solver_policy": "base_components",
            "assignments": expansion.assignments,
            "group_order": expansion.group_order,
            "postsolve_transform": "C_group = A.T @ C_base @ A",
        }
    else:
        deck_conductors = reduction.conductors
        assembly = {
            "solver_policy": "grouped",
            "assignments": {
                surface.name: surface.name for surface in reduction.conductors
            },
            "group_order": tuple(
                surface.name for surface in reduction.conductors
            ),
            "postsolve_transform": None,
        }
    reduction_provenance["electrical_assembly"] = assembly
    reduction_provenance["source_geometry"] = {
        "geometry_tolerance_mm": extraction.geometry_tolerance_mm,
        "simplification_area_error_mm2": (
            extraction.simplification_area_error_mm2
        ),
        "minimum_boundary_segment_mm": (
            extraction.minimum_boundary_segment_mm
        ),
    }
    reduction_provenance["artifact_class"] = {
        "source": extraction.source_mode,
        "material_scope": "air_only",
        "lifecycle": "diagnostic",
        "physical_model_validated": False,
        "conductor_policy": conductor_policy,
    }
    write_fastercap_input(
        deck_path, deck_conductors, provenance=reduction_provenance
    )
    deck_manifest_path = deck_path.with_name(f"{deck_path.name}.manifest.json")
    metadata = {
        "format": "dcdc-fastercap-geometry-v1",
        "source_mode": extraction.source_mode,
        "coordinate_grid_mm": KICAD_GRID_MM,
        "triangulation": {
            "engine": QUALITY_MESH_ENGINE,
            "minimum_angle_deg": QUALITY_MESH_MIN_ANGLE_DEG,
            "boundary": "exact constrained polygon segments",
        },
        "limitations": [
            "tracks, pads, vias, and copper graphics are excluded",
            "dielectric interfaces are not emitted",
            "the generated deck is not a physical PCB capacitance model",
        ],
        "pcb": str(pcb_path),
        "pcb_sha256": pcb_source.file_sha256(pcb_path),
        "filled_zone_dump_sha256": pcb_source.file_sha256(dump_path),
        "filled_zone_record_count": len(zone_dump["records"]),
        "kicad_version": zone_dump.get("kicad_version"),
        "kicad_python": zone_dump.get("python_executable"),
        "groups": groups,
        "area_mm2": extraction.area_mm2,
        "polygon_count": extraction.polygon_count,
        "triangle_count": extraction.triangle_count,
        "component_count": extraction.component_count,
        "repaired_polygon_count": extraction.repaired_polygon_count,
        "geometry_tolerance_mm": extraction.geometry_tolerance_mm,
        "simplification_area_error_mm2": (
            extraction.simplification_area_error_mm2
        ),
        "minimum_boundary_segment_mm": (
            extraction.minimum_boundary_segment_mm
        ),
        "panel_count": {
            surface.name: len(surface.panels) for surface in reduction.conductors
        },
        "solver_panel_count": {
            surface.name: len(surface.panels) for surface in deck_conductors
        },
        "solver_conductor_names": [
            surface.name for surface in deck_conductors
        ],
        "reduction": reduction_provenance,
        "stackup": [asdict(layer) for layer in stackup],
        "deck": str(deck_path),
        "deck_sha256": pcb_source.file_sha256(deck_path),
        "deck_manifest": str(deck_manifest_path),
        "deck_manifest_sha256": pcb_source.file_sha256(deck_manifest_path),
        "reduction_provenance_sha256": json.loads(
            deck_manifest_path.read_text()
        )["provenance_sha256"],
    }
    metadata_path = write_content_addressed_manifest(
        output, f"{artifact_stem}.geometry", metadata
    )
    load_geometry_manifest(metadata_path)
    print(completed.stdout.strip())
    print(f"wrote {deck_path}")
    print(f"wrote {metadata_path}")
    print(
        "WARNING: filled-zones-only air deck; see "
        f"{metadata_path.name} limitations"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
