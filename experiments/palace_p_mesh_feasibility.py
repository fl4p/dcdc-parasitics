#!/usr/bin/env python3
"""Generate P1 shared p-axis meshes without running Palace."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT))

from experiments.palace_fixture_study import (  # noqa: E402
    _conductors,
    _fixture,
    _model_bounds,
    _near_mesh_size,
    _outer_bounds,
)
from experiments.palace_fixture_ladder_study import FIXTURES  # noqa: E402
from palace import MESH_LIMITS, RESOURCE_LIMITS  # noqa: E402
from palace_mesh import BoxBounds, DielectricBox, generate_palace_mesh  # noqa: E402
from provenance import canonical_sha256, file_sha256  # noqa: E402


FORMAT = "dcdc-palace-p-mesh-feasibility-v1"
NEAR_MESH_SCALE = 0.5
FAR_MESH_SCALE = 1.0
OUTER_SCALE = 6.0
RESOURCE_SAFETY_FACTOR = 1.5


def generate_feasibility(*, output_directory, baseline_aggregate):
    output_directory = Path(output_directory).resolve()
    if output_directory.exists() and any(output_directory.iterdir()):
        raise ValueError("p-mesh feasibility output directory must be empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    baseline_aggregate = Path(baseline_aggregate).resolve()
    baseline = json.loads(baseline_aggregate.read_text())
    baseline_by_fixture = {item["fixture"]: item for item in baseline["fixtures"]}
    fixtures = []
    failures = []
    for name in FIXTURES:
        fixture = _fixture(name)
        conductors = _conductors(fixture)
        outer = _outer_bounds(fixture, conductors, OUTER_SCALE)
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
        near_size = _near_mesh_size(fixture, NEAR_MESH_SCALE)
        result = generate_palace_mesh(
            output_directory / name / "fixture.msh",
            outer_bounds=outer,
            conductors=conductors,
            dielectrics=dielectrics,
            mesh_size_near_m=near_size,
            mesh_size_far_m=max(near_size, model_span / 5.0 * FAR_MESH_SCALE),
            transition_distance_m=max(4.0 * near_size, 0.5 * model_span),
        )
        old_p2 = next(
            item for item in baseline_by_fixture[name]["rungs"]
            if item["rung"]["name"] == "p2"
        )
        old_run = json.loads(Path(old_p2["run_manifest"]).read_text())
        ratio = result.tetrahedron_count / old_p2["tetrahedron_count"]
        estimated_rss = (
            old_run["execution"]["peak_rss_bytes"] * ratio * RESOURCE_SAFETY_FACTOR
        )
        estimated_wall = (
            old_run["execution"]["elapsed_s"] * ratio * RESOURCE_SAFETY_FACTOR
        )
        mesh_passed = (
            result.node_count <= MESH_LIMITS["pcb_diagnostic"]["nodes"]
            and result.tetrahedron_count
            <= MESH_LIMITS["pcb_diagnostic"]["tetrahedra"]
        )
        resource_passed = (
            estimated_rss <= RESOURCE_LIMITS["pcb_diagnostic"].peak_rss_bytes
            and estimated_wall <= RESOURCE_LIMITS["pcb_diagnostic"].wall_time_s
        )
        if not mesh_passed:
            failures.append(f"{name}: mesh exceeds pcb_diagnostic bounds")
        if not resource_passed:
            failures.append(f"{name}: conservative p2 estimate exceeds pcb_diagnostic bounds")
        fixtures.append({
            "fixture": name,
            "mesh_path": str(result.mesh_path),
            "mesh_sha256": file_sha256(result.mesh_path),
            "mesh_manifest": str(result.manifest_path),
            "mesh_manifest_sha256": file_sha256(result.manifest_path),
            "node_count": result.node_count,
            "tetrahedron_count": result.tetrahedron_count,
            "near_mesh_size_m": near_size,
            "far_mesh_size_m": max(
                near_size, model_span / 5.0 * FAR_MESH_SCALE
            ),
            "baseline_p2_run_manifest": old_p2["run_manifest"],
            "baseline_p2_run_manifest_sha256": file_sha256(old_p2["run_manifest"]),
            "tetrahedron_ratio_to_scale_1": ratio,
            "estimated_p2_peak_rss_bytes": estimated_rss,
            "estimated_p2_wall_time_s": estimated_wall,
            "mesh_bounds_passed": mesh_passed,
            "resource_estimate_passed": resource_passed,
        })
    report = {
        "format": FORMAT,
        "implementation": str(Path(__file__).resolve()),
        "implementation_sha256": file_sha256(__file__),
        "baseline_aggregate": str(baseline_aggregate),
        "baseline_aggregate_sha256": file_sha256(baseline_aggregate),
        "controls": {
            "near_mesh_scale": NEAR_MESH_SCALE,
            "far_mesh_scale": FAR_MESH_SCALE,
            "outer_scale": OUTER_SCALE,
            "resource_class": "pcb_diagnostic",
            "resource_safety_factor": RESOURCE_SAFETY_FACTOR,
        },
        "mesh_limits": MESH_LIMITS["pcb_diagnostic"],
        "resource_limits": asdict(RESOURCE_LIMITS["pcb_diagnostic"]),
        "fixtures": fixtures,
        "failures": failures,
        "passed": not failures,
        "scope": "Mesh and resource feasibility only; no Palace matrix was run.",
    }
    report["content_sha256"] = canonical_sha256(report)
    path = output_directory / f"palace-p-mesh-feasibility.{report['content_sha256']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-aggregate", type=Path, required=True)
    arguments = parser.parse_args()
    path, report = generate_feasibility(
        output_directory=arguments.output,
        baseline_aggregate=arguments.baseline_aggregate,
    )
    print(f"report: {path}")
    print(f"passed: {report['passed']}")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
