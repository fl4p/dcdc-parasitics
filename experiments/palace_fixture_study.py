#!/usr/bin/env python3
"""Frozen solver-qualification fixtures for Palace electrostatics."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from electrostatic_fixtures import electrostatic_fixtures  # noqa: E402
from palace import (  # noqa: E402
    PalaceMaterial,
    PalaceRunRejected,
    PalaceTerminal,
    run_palace,
    write_palace_config,
)
from palace_mesh import (  # noqa: E402
    BoxBounds,
    ConductorPrism,
    DielectricBox,
    generate_palace_mesh,
)
from provenance import canonical_sha256, file_sha256  # noqa: E402


REPORT_FORMAT = "dcdc-palace-fixture-study-v1"


def _fixture(name):
    matches = [item for item in electrostatic_fixtures() if item.name == name]
    if len(matches) != 1:
        raise ValueError(f"unknown fixture {name!r}")
    return matches[0]


def _polygon_rings(polygon):
    rings = [tuple(tuple(point) for point in polygon.exterior.coords[:-1])]
    rings.extend(
        tuple(tuple(point) for point in interior.coords[:-1])
        for interior in polygon.interiors
    )
    return tuple(rings)


def _conductors(fixture):
    conductors = []
    for index, (polygon, midplane) in enumerate(
            zip(fixture.polygons, fixture.midplanes_m), 1):
        half = 0.5 * fixture.source_thickness_m
        conductors.append(ConductorPrism(
            f"terminal_{index}",
            _polygon_rings(polygon),
            midplane - half,
            midplane + half,
        ))
    return tuple(conductors)


def _model_bounds(fixture, conductors):
    minima = [
        min(point[axis] for conductor in conductors for ring in conductor.rings
            for point in ring)
        for axis in (0, 1)
    ]
    maxima = [
        max(point[axis] for conductor in conductors for ring in conductor.rings
            for point in ring)
        for axis in (0, 1)
    ]
    minima.append(min(item.z_min for item in conductors))
    maxima.append(max(item.z_max for item in conductors))
    if fixture.dielectric_bounds_m is not None:
        for axis in range(3):
            minima[axis] = min(minima[axis], fixture.dielectric_bounds_m[0][axis])
            maxima[axis] = max(maxima[axis], fixture.dielectric_bounds_m[1][axis])
    return tuple(minima), tuple(maxima)


def _outer_bounds(fixture, conductors, scale):
    minimum, maximum = _model_bounds(fixture, conductors)
    span = max(high - low for low, high in zip(minimum, maximum))
    center = tuple(0.5 * (low + high) for low, high in zip(minimum, maximum))
    half_extent = 0.5 * span * scale
    return BoxBounds(
        tuple(value - half_extent for value in center),
        tuple(value + half_extent for value in center),
    )


def _finite_reference(outer, outer_scale):
    return {
        "kind": "finite_outer_dirichlet_approximation",
        "convergence_ladder_required": True,
        "not_a_circuit_node": True,
        "outer_bounds_m": {
            "minimum": list(outer.minimum),
            "maximum": list(outer.maximum),
        },
        "outer_scale": outer_scale,
    }


def _near_mesh_size(fixture, mesh_scale):
    lateral_scale = min(
        min(polygon.bounds[2] - polygon.bounds[0],
            polygon.bounds[3] - polygon.bounds[1])
        for polygon in fixture.polygons
    ) / 20.0
    gaps = []
    for left_index, left in enumerate(fixture.polygons):
        for right_index in range(left_index + 1, len(fixture.polygons)):
            right = fixture.polygons[right_index]
            lateral_gap = left.distance(right)
            if lateral_gap > 0.0:
                gaps.append(lateral_gap)
            if left.intersects(right):
                vertical_gap = abs(
                    fixture.midplanes_m[left_index] - fixture.midplanes_m[right_index]
                ) - fixture.source_thickness_m
                if vertical_gap > 0.0:
                    gaps.append(vertical_gap)
    if not gaps:
        raise ValueError("fixture conductors have no positive separation")
    return min(lateral_scale, min(gaps) / 3.0) * mesh_scale


def run_study(*, fixture_name, output_directory, executable, build_manifest,
              outer_scale, mesh_scale, order, processes, far_mesh_scale=None,
              resource_class="synthetic"):
    fixture = _fixture(fixture_name)
    output_directory = Path(output_directory).resolve()
    if output_directory.exists() and any(output_directory.iterdir()):
        raise ValueError("Palace fixture output directory must be empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    conductors = _conductors(fixture)
    outer = _outer_bounds(fixture, conductors, outer_scale)
    dielectrics = ()
    if fixture.dielectric_bounds_m is not None:
        dielectrics = (DielectricBox(
            "fixture_dielectric",
            BoxBounds(*fixture.dielectric_bounds_m),
            fixture.relative_permittivity,
        ),)
    model_minimum, model_maximum = _model_bounds(fixture, conductors)
    model_span = max(
        high - low for low, high in zip(model_minimum, model_maximum)
    )
    if far_mesh_scale is None:
        far_mesh_scale = mesh_scale
    near_size = _near_mesh_size(fixture, mesh_scale)
    mesh_result = generate_palace_mesh(
        output_directory / "fixture.msh",
        outer_bounds=outer,
        conductors=conductors,
        dielectrics=dielectrics,
        mesh_size_near_m=near_size,
        mesh_size_far_m=max(near_size, model_span / 5.0 * far_mesh_scale),
        transition_distance_m=max(4.0 * near_size, 0.5 * model_span),
    )
    terminals = tuple(
        PalaceTerminal(index, name, attribute)
        for index, (name, attribute) in enumerate(
            mesh_result.terminal_attributes, 1
        )
    )
    materials = tuple(
        PalaceMaterial(name, (attribute,), permittivity)
        for name, attribute, permittivity in mesh_result.material_attributes
    )
    finite_reference = _finite_reference(outer, outer_scale)
    far_size = max(near_size, model_span / 5.0 * far_mesh_scale)
    transition_distance = max(4.0 * near_size, 0.5 * model_span)
    config_manifest = write_palace_config(
        output_directory / "config.json",
        mesh_path=mesh_result.mesh_path,
        mesh_manifest_path=mesh_result.manifest_path,
        output_directory=output_directory / "postpro",
        terminals=terminals,
        materials=materials,
        ground_attribute=mesh_result.ground_attribute,
        finite_reference=finite_reference,
        order=order,
    )
    mesh_manifest = json.loads(mesh_result.manifest_path.read_text())
    mesh_identity = {
        "mesh_path": str(mesh_result.mesh_path),
        "mesh_sha256": file_sha256(mesh_result.mesh_path),
        "manifest_path": str(mesh_result.manifest_path),
        "manifest_sha256": file_sha256(mesh_result.manifest_path),
        "provenance_sha256": mesh_manifest["provenance_sha256"],
        "provenance": mesh_manifest["provenance"],
    }
    report = {
        "format": REPORT_FORMAT,
        "fixture": fixture.name,
        "scope": fixture.scope,
        "study_implementation": str(Path(__file__).resolve()),
        "study_implementation_sha256": file_sha256(__file__),
        "solver_executable": str(Path(executable).resolve()),
        "build_manifest": str(Path(build_manifest).resolve()),
        "outer_scale": outer_scale,
        "mesh_scale": mesh_scale,
        "far_mesh_scale": far_mesh_scale,
        "order": order,
        "resource_class": resource_class,
        "near_mesh_size_m": near_size,
        "far_mesh_size_m": far_size,
        "transition_distance_m": transition_distance,
        "node_count": mesh_result.node_count,
        "tetrahedron_count": mesh_result.tetrahedron_count,
        "mesh_manifest": str(mesh_result.manifest_path),
        "mesh_identity": mesh_identity,
        "config_manifest": str(config_manifest),
        "reference_semantics": finite_reference,
        "lifecycle_limit": "fixture_qualification_only",
        "qualification_limit": (
            "No fixture result authorizes a Fugu matrix or a physical PCB model."
        ),
    }
    config_manifest_value = json.loads(Path(config_manifest).read_text())
    config_path = output_directory / "config.json"
    report["config_identity"] = {
        "config_path": str(config_path),
        "config_sha256": file_sha256(config_path),
        "manifest_path": str(Path(config_manifest)),
        "manifest_sha256": file_sha256(config_manifest),
        "provenance_sha256": config_manifest_value["provenance_sha256"],
        "provenance": config_manifest_value["provenance"],
    }
    try:
        run = run_palace(
            config_manifest,
            executable=executable,
            build_manifest_path=build_manifest,
            processes=processes,
            resource_class=resource_class,
        )
    except PalaceRunRejected as error:
        report["lifecycle"] = "rejected_diagnostic"
        report["failures"] = list(error.failures)
        report["run_manifest"] = str(error.manifest_path)
    else:
        report["lifecycle"] = "numerically_converged_diagnostic"
        report["run_manifest"] = str(run.manifest_path)
        report["explicit_residuals"] = [
            {
                "terminal": item.terminal.name,
                "relative_residual": item.relative_residual,
                "target": item.target,
            }
            for item in run.residuals
        ]
        report["raw_matrix_f"] = run.raw_matrix.values.tolist()
        report["downstream_matrix_f"] = run.downstream_matrix.values.tolist()
    run_manifest_path = Path(report["run_manifest"])
    run_manifest = json.loads(run_manifest_path.read_text())
    report["run_identity"] = {
        "path": str(run_manifest_path),
        "sha256": file_sha256(run_manifest_path),
        "content_sha256": run_manifest["content_sha256"],
        "resource_class": run_manifest["resource_class"],
        "resource_limits": run_manifest["resource_limits"],
        "config_manifest": run_manifest["config_manifest"],
        "config_manifest_sha256": run_manifest["config_manifest_sha256"],
        "artifacts": run_manifest["artifacts"],
    }
    report["content_sha256"] = canonical_sha256(report)
    report_path = output_directory / (
        f"palace-fixture-study.{report['content_sha256']}.json"
    )
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report_path, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="parallel_plate_air")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--executable",
        default=os.environ.get(
            "PALACE",
            "/Users/fab/dev/vendor/palace-build-qualification-make/bin/palace",
        ),
    )
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--outer-scale", type=float, default=6.0)
    parser.add_argument("--mesh-scale", type=float, default=1.0)
    parser.add_argument("--far-mesh-scale", type=float)
    parser.add_argument("--order", type=int, default=1)
    parser.add_argument(
        "--resource-class", choices=("synthetic", "pcb_diagnostic"),
        default="synthetic",
    )
    parser.add_argument("--processes", type=int, default=1)
    arguments = parser.parse_args()
    path, report = run_study(
        fixture_name=arguments.fixture,
        output_directory=arguments.output,
        executable=arguments.executable,
        build_manifest=arguments.build_manifest,
        outer_scale=arguments.outer_scale,
        mesh_scale=arguments.mesh_scale,
        order=arguments.order,
        processes=arguments.processes,
        far_mesh_scale=arguments.far_mesh_scale,
        resource_class=arguments.resource_class,
    )
    print(f"report: {path}")
    print(f"lifecycle: {report['lifecycle']}")
    if report["lifecycle"] == "rejected_diagnostic":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
