#!/usr/bin/env python3
"""Preflight and evaluate source-bound Palace PCB convergence ladders."""
import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from maxwell import entrywise_convergence_gate  # noqa: E402
from palace import (  # noqa: E402
    MESH_LIMITS,
    load_palace_config_manifest,
    validate_palace_run_manifest,
)
from palace_plc_mesh import validate_palace_plc_mesh_manifest  # noqa: E402
from provenance import canonical_sha256, file_sha256  # noqa: E402


PLAN_FORMAT = "dcdc-palace-pcb-ladder-plan-v1"
REPORT_FORMAT = "dcdc-palace-pcb-ladder-report-v1"


@dataclass(frozen=True)
class Rung:
    name: str
    mesh_path: Path
    mesh_manifest_path: Path
    run_manifest_path: Path | None
    outer_scale: float
    mask_er: float
    max_planar_area_m2: float | None
    max_vertical_step_m: float | None
    order: int


def _number(value, name, *, allow_none=False):
    if value is None and allow_none:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0.0):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _resolve(base, value, name, *, allow_none=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty path")
    path = Path(value)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_plan(path):
    path = Path(path).resolve()
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or set(raw) != {
        "format", "rungs", "comparisons", "p_shared_mesh",
        "mask_shared_mesh", "outer_sequence",
    } or raw["format"] != PLAN_FORMAT:
        raise ValueError("Palace PCB ladder plan schema mismatch")
    if not isinstance(raw["rungs"], list) or not raw["rungs"]:
        raise ValueError("Palace PCB ladder plan requires rungs")
    rungs = {}
    required = {
        "name", "mesh_path", "mesh_manifest_path", "run_manifest_path",
        "outer_scale", "mask_er", "max_planar_area_m2",
        "max_vertical_step_m", "order",
    }
    for item in raw["rungs"]:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("Palace PCB ladder rung schema mismatch")
        name = item["name"]
        if not isinstance(name, str) or not name or name in rungs:
            raise ValueError("Palace PCB ladder rung names must be unique")
        order = item["order"]
        if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
            raise ValueError("Palace PCB ladder order must be positive")
        mesh_path = _resolve(path.parent, item["mesh_path"], "mesh_path")
        mesh_manifest_path = _resolve(
            path.parent, item["mesh_manifest_path"], "mesh_manifest_path"
        )
        outer_scale = _number(item["outer_scale"], "outer_scale")
        mask_er = _number(item["mask_er"], "mask_er")
        assert mesh_path is not None and mesh_manifest_path is not None
        assert outer_scale is not None and mask_er is not None
        rungs[name] = Rung(
            name=name,
            mesh_path=mesh_path,
            mesh_manifest_path=mesh_manifest_path,
            run_manifest_path=_resolve(
                path.parent, item["run_manifest_path"], "run_manifest_path",
                allow_none=True,
            ),
            outer_scale=outer_scale,
            mask_er=mask_er,
            max_planar_area_m2=_number(
                item["max_planar_area_m2"], "max_planar_area_m2",
                allow_none=True,
            ),
            max_vertical_step_m=_number(
                item["max_vertical_step_m"], "max_vertical_step_m",
                allow_none=True,
            ),
            order=order,
        )
    for key in ("p_shared_mesh", "mask_shared_mesh", "outer_sequence"):
        values = raw[key]
        if (not isinstance(values, list) or len(values) < 2
                or any(not isinstance(name, str) or name not in rungs for name in values)
                or len(set(values)) != len(values)):
            raise ValueError(f"{key} must name distinct known rungs")
    if len(raw["outer_sequence"]) < 3:
        raise ValueError("outer_sequence requires at least three rungs")
    comparisons = []
    for item in raw["comparisons"]:
        if (not isinstance(item, dict) or set(item) != {"name", "axis", "left", "right"}
                or item["axis"] not in {"h", "p", "material"}
                or item["left"] not in rungs or item["right"] not in rungs):
            raise ValueError("Palace PCB ladder comparison schema mismatch")
        comparisons.append(dict(item))
    if len({item["name"] for item in comparisons}) != len(comparisons):
        raise ValueError("Palace PCB ladder comparison names must be unique")
    return path, raw, rungs, comparisons


def _mesh_evidence(rung):
    manifest = validate_palace_plc_mesh_manifest(
        rung.mesh_manifest_path, mesh_path=rung.mesh_path
    )
    provenance = manifest["provenance"]
    parameters = provenance["mesh_parameters"]
    source = provenance["source_identity"]
    expected_masks = {
        "Top Solder Mask": rung.mask_er,
        "Bottom Solder Mask": rung.mask_er,
    }
    if source["outer_scale"] != rung.outer_scale:
        raise ValueError(f"{rung.name}: outer scale mismatch")
    if source["material_permittivity_overrides"] != expected_masks:
        raise ValueError(f"{rung.name}: solder-mask policy mismatch")
    if parameters["max_planar_area_m2"] != rung.max_planar_area_m2:
        raise ValueError(f"{rung.name}: planar h control mismatch")
    if parameters["max_vertical_step_m"] != rung.max_vertical_step_m:
        raise ValueError(f"{rung.name}: vertical h control mismatch")
    if (provenance["node_count"] > MESH_LIMITS["pcb_diagnostic"]["nodes"]
            or provenance["tetrahedron_count"]
            > MESH_LIMITS["pcb_diagnostic"]["tetrahedra"]):
        raise ValueError(f"{rung.name}: mesh exceeds pcb_diagnostic limits")
    return {
        "mesh_sha256": file_sha256(rung.mesh_path),
        "manifest_sha256": file_sha256(rung.mesh_manifest_path),
        "provenance_sha256": manifest["provenance_sha256"],
        "node_count": provenance["node_count"],
        "tetrahedron_count": provenance["tetrahedron_count"],
    }


def preflight(plan_path):
    path, raw, rungs, comparisons = load_plan(plan_path)
    evidence = {name: _mesh_evidence(rung) for name, rung in rungs.items()}
    p_hashes = {evidence[name]["mesh_sha256"] for name in raw["p_shared_mesh"]}
    if len(p_hashes) != 1:
        raise ValueError("p ladder does not use a byte-identical shared mesh")
    mask_hashes = {evidence[name]["mesh_sha256"] for name in raw["mask_shared_mesh"]}
    if len(mask_hashes) != 1:
        raise ValueError("mask ladder topology is not byte-identical")
    outer_scales = [rungs[name].outer_scale for name in raw["outer_sequence"]]
    if any(right <= left for left, right in zip(outer_scales, outer_scales[1:])):
        raise ValueError("outer ladder scales must increase strictly")
    h_pairs = [item for item in comparisons if item["axis"] == "h"]
    if not h_pairs:
        raise ValueError("plan lacks an h comparison")
    for pair in h_pairs:
        left, right = rungs[pair["left"]], rungs[pair["right"]]
        if (left.max_planar_area_m2 == right.max_planar_area_m2
                or left.max_vertical_step_m == right.max_vertical_step_m):
            raise ValueError("h comparison must refine planar and vertical controls")
    identity = {
        "format": REPORT_FORMAT,
        "stage": "preflight",
        "plan_path": str(path),
        "plan_sha256": file_sha256(path),
        "mesh_evidence": evidence,
        "p_shared_mesh_sha256": next(iter(p_hashes)),
        "mask_shared_mesh_sha256": next(iter(mask_hashes)),
        "outer_scales": outer_scales,
    }
    return {**identity, "content_sha256": canonical_sha256(identity)}


def _outer_gate(matrices):
    row_sums = np.asarray([matrix.values.sum(axis=1) for matrix in matrices])
    rows = []
    for index in range(row_sums.shape[1]):
        values = row_sums[:, index]
        increments = np.diff(values)
        monotone = bool(np.all(increments >= 0.0) or np.all(increments <= 0.0))
        shrinking = bool(np.all(np.abs(increments[1:]) <= np.abs(increments[:-1])))
        rows.append({
            "index": index,
            "values_f": values.tolist(),
            "increments_f": increments.tolist(),
            "passed": monotone or shrinking,
        })
    return {"passed": all(row["passed"] for row in rows), "rows": rows}


def evaluate(plan_path):
    preflight_report = preflight(plan_path)
    path, raw, rungs, comparisons = load_plan(plan_path)
    runs = {}
    for name, rung in rungs.items():
        if rung.run_manifest_path is None:
            raise ValueError(f"{name}: run manifest is not assigned")
        run = validate_palace_run_manifest(rung.run_manifest_path)
        config = load_palace_config_manifest(run["raw"]["config_manifest"])
        if (config.order != rung.order or config.mesh_path != rung.mesh_path
                or config.mesh_manifest_path != rung.mesh_manifest_path):
            raise ValueError(f"{name}: run controls do not match the frozen plan")
        runs[name] = run
    gates = {}
    for item in comparisons:
        gates[item["name"]] = {
            "axis": item["axis"],
            **entrywise_convergence_gate(
                runs[item["left"]]["raw_matrix"],
                runs[item["right"]]["raw_matrix"],
            ),
        }
    outer = _outer_gate([
        runs[name]["raw_matrix"] for name in raw["outer_sequence"]
    ])
    numerical = [gate for gate in gates.values() if gate["axis"] in {"h", "p"}]
    passed = all(gate["passed"] for gate in numerical) and outer["passed"]
    identity = {
        "format": REPORT_FORMAT,
        "stage": "evaluated",
        "lifecycle": (
            "numerically_converged_diagnostic" if passed else "rejected_diagnostic"
        ),
        "plan_path": str(path),
        "plan_sha256": file_sha256(path),
        "preflight_content_sha256": preflight_report["content_sha256"],
        "run_content_sha256": {
            name: run["raw"]["content_sha256"] for name, run in runs.items()
        },
        "comparison_gates": gates,
        "outer_row_sum_gate": outer,
        "passed": passed,
    }
    return {**identity, "content_sha256": canonical_sha256(identity)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("preflight", "evaluate"))
    parser.add_argument("plan")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)
    report = preflight(args.plan) if args.stage == "preflight" else evaluate(args.plan)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
