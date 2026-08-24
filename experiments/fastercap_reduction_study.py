#!/usr/bin/env python3
"""Fixture-scoped FasterCap representation study for extraction-plan Slice 2."""
from dataclasses import asdict, dataclass
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from shapely import set_precision

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from electrostatic_fixtures import (  # noqa: E402
    SOURCE_THICKNESS_M,
    electrostatic_fixtures as fixtures,
)
from fastercap import (  # noqa: E402
    FasterCapRunRejected,
    box_dielectric_surface,
    extruded_triangulation_surface,
    run_fastercap,
    write_fastercap_input,
)
from fastercap_reduction import (  # noqa: E402
    ThicknessReductionRequest,
    thickness_bounds,
)
from kicad_fastercap import triangulate_polygonal  # noqa: E402

POLICY = "fastercap-pcb-gates-v1"
REPORT_FORMAT = "dcdc-fastercap-reduction-study-v1"
REDUCTION_ABS_F = 2e-15
REDUCTION_REL = 0.01
LADDER_ABS_F = 1e-15
LADDER_REL = 0.02
AREA_REL_LIMIT = 1e-4
GEOMETRY_GRID_M = 1e-9
GAP_DELTA_CAP_M = 1e-6
DEFAULT_LADDER = (0.00125, 0.0009, 0.000625)


@dataclass(frozen=True)
class Candidate:
    name: str
    representation: str
    effective_thickness_m: float | None
    anchor: str
    simplify_tolerance_m: float
    documented_semantics: str

    def __post_init__(self):
        if self.representation not in (
                "physical_thickness", "effective_thickness",
                "boundary_simplification"):
            raise ValueError("unknown representation")
        if self.anchor not in ("midplane", "z_min_face", "z_max_face"):
            raise ValueError("unknown anchor")
        if (not np.isfinite(self.simplify_tolerance_m)
                or self.simplify_tolerance_m < 0.0):
            raise ValueError("simplification tolerance must be finite and non-negative")
        if (self.effective_thickness_m is None
                or not np.isfinite(self.effective_thickness_m)
                or self.effective_thickness_m <= 0.0):
            raise ValueError("finite positive effective thickness is required")


@dataclass(frozen=True)
class GeometryResult:
    valid: bool
    reasons: tuple[str, ...]
    source_clearance_m: float
    candidate_clearance_m: float
    clearance_change_m: float
    allowed_clearance_change_m: float
    maximum_anchor_displacement_m: float
    maximum_planar_boundary_displacement_m: float
    maximum_surface_displacement_m: float
    maximum_area_relative_error: float
    minimum_boundary_segment_m: float
    minimum_triangle_angle_deg: float
    source_input_panels: int
    candidate_input_panels: int


def candidates():
    values = [
        Candidate(
            "physical_35um", "physical_thickness", SOURCE_THICKNESS_M,
            "midplane", 0.0, "closed finite-thickness reference",
        ),
        Candidate(
            "effective_34um", "effective_thickness", 34e-6,
            "midplane", 0.0, "controlled closed effective thickness",
        ),
        Candidate(
            "effective_1um", "effective_thickness", 1e-6,
            "midplane", 0.0, "controlled closed effective thickness",
        ),
        Candidate(
            "simplify_1um", "boundary_simplification", SOURCE_THICKNESS_M,
            "midplane", 1e-6, "topology-preserving boundary simplification",
        ),
    ]
    return tuple(values)


def _z_bounds(midplane, source_thickness, candidate):
    representation = (
        "effective_thickness"
        if candidate.representation == "effective_thickness" else "physical"
    )
    anchor = {
        "midplane": "midplane",
        "z_min_face": "bottom",
        "z_max_face": "top",
    }[candidate.anchor]
    request = ThicknessReductionRequest(
        representation,
        candidate.effective_thickness_m if representation != "physical" else None,
        anchor,
    )
    return thickness_bounds(midplane, source_thickness, request)


def _simplified_polygons(fixture, candidate):
    sources = []
    simplified = []
    errors = []
    for polygon in fixture.polygons:
        source = set_precision(polygon, GEOMETRY_GRID_M)
        geometry = source
        if candidate.simplify_tolerance_m:
            geometry = source.simplify(
                candidate.simplify_tolerance_m, preserve_topology=True
            )
            geometry = set_precision(geometry, GEOMETRY_GRID_M)
        if geometry.geom_type != "Polygon" or geometry.is_empty:
            raise ValueError("fixture simplification must preserve one polygon")
        error = abs(geometry.area - source.area) / source.area
        sources.append(source)
        simplified.append(geometry)
        errors.append(error)
    return tuple(sources), tuple(simplified), tuple(errors)


def _minimum_boundary_segment(polygons):
    lengths = []
    for polygon in polygons:
        points = tuple(polygon.exterior.coords)
        lengths.extend(
            float(np.hypot(second[0] - first[0], second[1] - first[1]))
            for first, second in zip(points, points[1:])
        )
    return min(lengths)


def _prism_clearance(polygons, bounds):
    minimum = float("inf")
    for first in range(len(polygons)):
        for second in range(first + 1, len(polygons)):
            planar = polygons[first].distance(polygons[second])
            z_first = bounds[first]
            z_second = bounds[second]
            vertical = max(
                0.0,
                max(z_first[0], z_second[0]) - min(z_first[1], z_second[1]),
            )
            minimum = min(minimum, float(np.hypot(planar, vertical)))
    return minimum


def _surface_from_polygon(name, polygon, bounds, relative_permittivity):
    triangles = triangulate_polygonal(polygon)
    return extruded_triangulation_surface(
        name, triangles, bounds[0], bounds[1],
        relative_permittivity=relative_permittivity,
    )


def _minimum_triangle_angle_deg(surfaces):
    angles = []
    for surface in surfaces:
        for panel in surface.panels:
            if len(panel) != 3:
                continue
            points = tuple(np.asarray(point, dtype=float) for point in panel)
            for index in range(3):
                first = points[(index - 1) % 3] - points[index]
                second = points[(index + 1) % 3] - points[index]
                cosine = np.dot(first, second) / (
                    np.linalg.norm(first) * np.linalg.norm(second)
                )
                angles.append(float(np.degrees(np.arccos(np.clip(cosine, -1, 1)))))
    return min(angles) if angles else 90.0


def build_geometry(fixture, candidate):
    source_polygons, polygons, area_errors = _simplified_polygons(
        fixture, candidate
    )
    source_bounds = tuple(
        (midplane - fixture.source_thickness_m / 2.0,
         midplane + fixture.source_thickness_m / 2.0)
        for midplane in fixture.midplanes_m
    )
    candidate_bounds = tuple(
        _z_bounds(midplane, fixture.source_thickness_m, candidate)
        for midplane in fixture.midplanes_m
    )
    source_clearance = _prism_clearance(source_polygons, source_bounds)
    candidate_clearance = _prism_clearance(polygons, candidate_bounds)
    clearance_change = abs(candidate_clearance - source_clearance)
    allowed_change = min(GAP_DELTA_CAP_M, 0.01 * source_clearance)
    anchor_displacements = []
    surface_displacements = []
    planar_displacement = max(
        source.boundary.hausdorff_distance(result.boundary)
        for source, result in zip(source_polygons, polygons)
    )
    for source, result in zip(source_bounds, candidate_bounds):
        if candidate.anchor == "midplane":
            source_anchor = sum(source) / 2.0
            result_anchor = sum(result) / 2.0
        elif candidate.anchor == "z_min_face":
            source_anchor, result_anchor = source[0], result[0]
        else:
            source_anchor, result_anchor = source[1], result[1]
        anchor_displacements.append(abs(result_anchor - source_anchor))
        surface_displacements.append(max(
            abs(result[0] - source[0]), abs(result[1] - source[1])
        ))

    surfaces = tuple(
        _surface_from_polygon(
            f"conductor_{index}", polygon, bounds,
            fixture.relative_permittivity,
        )
        for index, (polygon, bounds) in enumerate(
            zip(polygons, candidate_bounds), 1
        )
    )
    source_surfaces = tuple(
        _surface_from_polygon(
            f"conductor_{index}", polygon, bounds,
            fixture.relative_permittivity,
        )
        for index, (polygon, bounds) in enumerate(
            zip(source_polygons, source_bounds), 1
        )
    )
    minimum_segment = _minimum_boundary_segment(polygons)
    minimum_triangle_angle = _minimum_triangle_angle_deg(surfaces)
    reasons = []
    if max(area_errors, default=0.0) > AREA_REL_LIMIT:
        reasons.append("simplification area error exceeds 1e-4")
    if minimum_triangle_angle < 5.0:
        reasons.append("input triangle angle is below FasterCap's 5 degree gate")
    if (candidate.simplify_tolerance_m
            and minimum_segment < candidate.simplify_tolerance_m / 2.0):
        reasons.append("retained boundary segment is shorter than half tolerance")
    if candidate_clearance <= 0.0:
        reasons.append("candidate removes conductor clearance")
    if clearance_change > allowed_change + 1e-15:
        reasons.append("candidate clearance change exceeds min(1 um, 1% source gap)")
    if fixture.dielectric_bounds_m is not None:
        lower = fixture.dielectric_bounds_m[0][2]
        upper = fixture.dielectric_bounds_m[1][2]
        if any(bounds[0] <= lower or bounds[1] >= upper
               for bounds in candidate_bounds):
            reasons.append("candidate crosses dielectric boundary")
    metrics = GeometryResult(
        not reasons,
        tuple(reasons),
        source_clearance,
        candidate_clearance,
        clearance_change,
        allowed_change,
        max(anchor_displacements, default=0.0),
        planar_displacement,
        max(max(surface_displacements, default=0.0), planar_displacement),
        max(area_errors, default=0.0),
        minimum_segment,
        minimum_triangle_angle,
        sum(len(surface.panels) for surface in source_surfaces),
        sum(len(surface.panels) for surface in surfaces),
    )
    dielectrics = ()
    if fixture.dielectric_bounds_m is not None:
        dielectrics = (box_dielectric_surface(
            "material_region",
            fixture.dielectric_bounds_m[0], fixture.dielectric_bounds_m[1],
            inside_permittivity=fixture.relative_permittivity,
            outside_permittivity=1.0,
        ),)
    return surfaces, dielectrics, metrics


def entrywise_gate(reference, candidate, absolute, relative):
    reference = np.asarray(reference, dtype=float)
    candidate = np.asarray(candidate, dtype=float)
    allowance = absolute + relative * np.maximum(
        np.abs(reference), np.abs(candidate)
    )
    difference = np.abs(candidate - reference)
    return bool(np.all(difference <= allowance)), difference, allowance


def _evaluate_ladder(matrices):
    differences = []
    allowances = []
    adjacent_pass = []
    for coarse, fine in zip(matrices, matrices[1:]):
        passed, difference, allowance = entrywise_gate(
            coarse, fine, LADDER_ABS_F, LADDER_REL
        )
        adjacent_pass.append(passed)
        differences.append(difference.tolist())
        allowances.append(allowance.tolist())
    return (
        len(matrices) >= 3 and all(adjacent_pass),
        differences,
        allowances,
    )


def _run_ladder(output, fixture, candidate, executable, ladder):
    surfaces, dielectrics, geometry = build_geometry(fixture, candidate)
    record = {
        "geometry": asdict(geometry),
        "runs": [],
        "ladder_passed": False,
        "ladder_differences_f": [],
        "ladder_allowances_f": [],
    }
    if not geometry.valid:
        record["reasons"] = list(geometry.reasons)
        return record
    matrices = []
    for tolerance in ladder:
        rung = output / fixture.name / candidate.name / f"a_{tolerance:.8g}"
        deck = write_fastercap_input(rung / "model.lst", surfaces, dielectrics)
        try:
            result = run_fastercap(
                deck,
                conductor_names=tuple(surface.name for surface in surfaces),
                executable=executable,
                relative_error=tolerance,
                timeout=120,
                resource_class="synthetic",
            )
        except FasterCapRunRejected as error:
            record["runs"].append({
                "relative_error": tolerance,
                "state": "rejected_diagnostic",
                "manifest": str(error.manifest_path),
                "failures": (
                    [] if error.transcript is None
                    else list(error.transcript.failures)
                ),
            })
            record["reasons"] = ["one or more numerical ladder rungs rejected"]
            return record
        raw_matrix = result.transcript.iterations[-1].matrix.values.copy()
        matrices.append(raw_matrix)
        observation = result.transcript.resource_observations
        record["runs"].append({
            "relative_error": tolerance,
            "state": "numerically_converged_diagnostic",
            "manifest": str(result.manifest_path),
            "raw_matrix_f": raw_matrix.tolist(),
            "matrix_f": result.matrix.values.tolist(),
            "initial_panels": result.transcript.initial_panels,
            "refined_panels": result.transcript.iterations[-1].refined_panels,
            "panel_growth": (
                result.transcript.iterations[-1].refined_panels
                / result.transcript.initial_panels
            ),
            "solver_runtime_s": result.transcript.solver_runtime_s,
            "wall_time_s": observation["elapsed_s"],
            "peak_rss_bytes": observation["peak_rss_bytes"],
            "matrix_gate_results": result.transcript.matrix_gate_results,
        })
    (record["ladder_passed"], record["ladder_differences_f"],
     record["ladder_allowances_f"]) = _evaluate_ladder(matrices)
    if not record["ladder_passed"]:
        record["reasons"] = ["three-rung numerical ladder did not converge"]
    return record


def run_study(output, *, executable, ladder=DEFAULT_LADDER):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    ladder = tuple(float(value) for value in ladder)
    if (len(ladder) < 3 or any(not np.isfinite(value) or value <= 0.0
                               for value in ladder)):
        raise ValueError("ladder requires at least three finite positive tolerances")
    fixture_values = fixtures()
    candidate_values = candidates()
    results = {}
    for fixture in fixture_values:
        fixture_results = {}
        for candidate in candidate_values:
            fixture_results[candidate.name] = _run_ladder(
                output, fixture, candidate, executable, ladder
            )
        reference = fixture_results["physical_35um"]
        if reference["ladder_passed"]:
            reference_matrix = np.asarray(
                reference["runs"][-1]["raw_matrix_f"]
            )
            reference_input_panels = reference["geometry"]["candidate_input_panels"]
            reference_refined_panels = reference["runs"][-1]["refined_panels"]
            for candidate in candidate_values[1:]:
                item = fixture_results[candidate.name]
                if not item["ladder_passed"]:
                    item["verdict"] = "rejected"
                    continue
                candidate_matrix = np.asarray(
                    item["runs"][-1]["raw_matrix_f"]
                )
                passed, difference, allowance = entrywise_gate(
                    reference_matrix, candidate_matrix,
                    REDUCTION_ABS_F, REDUCTION_REL,
                )
                item["reduction_difference_f"] = difference.tolist()
                item["reduction_allowance_f"] = allowance.tolist()
                item["reduction_budget_passed"] = passed
                input_panel_reduction = (
                    item["geometry"]["candidate_input_panels"]
                    < reference_input_panels
                )
                refined_panel_reduction = (
                    item["runs"][-1]["refined_panels"]
                    < reference_refined_panels
                )
                has_panel_reduction = refined_panel_reduction
                item["selection_evidence"] = {
                    "reference_input_panels": reference_input_panels,
                    "candidate_input_panels": item["geometry"][
                        "candidate_input_panels"
                    ],
                    "reference_refined_panels": reference_refined_panels,
                    "candidate_refined_panels": item["runs"][-1][
                        "refined_panels"
                    ],
                    "input_panel_reduction": input_panel_reduction,
                    "refined_panel_reduction": refined_panel_reduction,
                }
                if not passed:
                    item["verdict"] = "rejected"
                    item.setdefault("reasons", []).append(
                        "matrix error exceeds 2 fF + 1%"
                    )
                elif not has_panel_reduction:
                    item["verdict"] = "rejected"
                    item.setdefault("reasons", []).append(
                        "no tight-rung refined-panel reduction; input panels or runtime alone cannot select candidate"
                    )
                else:
                    item["verdict"] = "fixture_qualified"
            reference["verdict"] = "physical_reference"
        else:
            reference["verdict"] = "rejected"
            for candidate in candidate_values[1:]:
                fixture_results[candidate.name]["verdict"] = "rejected"
                fixture_results[candidate.name].setdefault("reasons", []).append(
                    "physical reference did not converge"
                )
        results[fixture.name] = fixture_results

    report = {
        "format": REPORT_FORMAT,
        "gate_policy": POLICY,
        "lifecycle_scope": "fixture_qualification_only",
        "solver_executable": str(executable),
        "ladder_relative_errors": ladder,
        "fixed_gates": {
            "reduction_absolute_f": REDUCTION_ABS_F,
            "reduction_relative": REDUCTION_REL,
            "ladder_absolute_f": LADDER_ABS_F,
            "ladder_relative": LADDER_REL,
            "area_relative": AREA_REL_LIMIT,
            "geometry_grid_m": GEOMETRY_GRID_M,
            "minimum_segment_fraction_of_tolerance": 0.5,
            "clearance_change_cap_m": GAP_DELTA_CAP_M,
        },
        "fixtures": [
            {
                "name": item.name,
                "source_thickness_m": item.source_thickness_m,
                "relative_permittivity": item.relative_permittivity,
                "scope": item.scope,
            }
            for item in fixture_values
        ],
        "candidates": [asdict(item) for item in candidate_values],
        "thin_sheet_status": (
            "excluded: homogeneous open-sheet semantics are undocumented and "
            "zero-thickness conductors at unlike-dielectric interfaces are "
            "explicitly unsupported upstream"
        ),
        "qualification_limit": (
            "No result authorizes a PCB physical model or Fugu2 use; Slice 5 "
            "must requalify over its complete geometry/material/topology envelope."
        ),
        "results": results,
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    report["content_sha256"] = digest
    path = output / f"slice2-reduction-study.{digest}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path, report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output", default="out/fastercap-reduction-study")
    parser.add_argument("--executable", default="FasterCap")
    args = parser.parse_args(argv)
    path, report = run_study(args.output, executable=args.executable)
    qualified = [
        f"{fixture}/{candidate}"
        for fixture, values in report["results"].items()
        for candidate, result in values.items()
        if result.get("verdict") == "fixture_qualified"
    ]
    print(f"report: {path}")
    print("fixture-qualified: " + (", ".join(qualified) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
