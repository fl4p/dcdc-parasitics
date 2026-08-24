#!/usr/bin/env python3
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT))

from experiments.palace_pcb_ladder_study import (  # noqa: E402
    PLAN_FORMAT,
    _outer_gate,
    load_plan,
)
from maxwell import MaxwellMatrix  # noqa: E402


def _plan():
    rungs = []
    for name, scale, er, area, vertical, order in (
        ("base", 2.0, 3.3, None, None, 2),
        ("fine", 2.0, 3.3, 1e-7, 0.02, 2),
        ("p3", 2.0, 3.3, 1e-7, 0.02, 3),
        ("mask1", 2.0, 1.0, 1e-7, 0.02, 2),
        ("mask5", 2.0, 5.0, 1e-7, 0.02, 2),
        ("outer3", 3.0, 3.3, 1e-7, 0.02, 2),
        ("outer4", 4.0, 3.3, 1e-7, 0.02, 2),
    ):
        rungs.append({
            "name": name,
            "mesh_path": f"{name}/pcb.msh",
            "mesh_manifest_path": f"{name}/pcb.msh.manifest.json",
            "run_manifest_path": None,
            "outer_scale": scale,
            "mask_er": er,
            "max_planar_area_m2": area,
            "max_vertical_step_m": vertical,
            "order": order,
        })
    return {
        "format": PLAN_FORMAT,
        "rungs": rungs,
        "comparisons": [
            {"name": "h", "axis": "h", "left": "base", "right": "fine"},
            {"name": "p", "axis": "p", "left": "fine", "right": "p3"},
        ],
        "p_shared_mesh": ["fine", "p3"],
        "mask_shared_mesh": ["mask1", "fine", "mask5"],
        "outer_sequence": ["fine", "outer3", "outer4"],
    }


def test_plan_requires_three_outer_rungs_and_resolves_paths(tmp_path):
    raw = _plan()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw))
    _, loaded, rungs, comparisons = load_plan(path)
    assert loaded == raw
    assert rungs["fine"].mesh_path == tmp_path / "fine/pcb.msh"
    assert comparisons[0]["axis"] == "h"

    raw["outer_sequence"] = ["fine", "outer3"]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="at least three"):
        load_plan(path)


def test_plan_rejects_boolean_numeric_controls(tmp_path):
    raw = _plan()
    raw["rungs"][0]["outer_scale"] = True
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="positive finite"):
        load_plan(path)


def test_outer_gate_accepts_monotone_or_shrinking_reference_rows():
    names = ("A", "B")
    matrices = [
        MaxwellMatrix(names, np.array([[4.0, -2.0], [-2.0, 5.0]]) * scale)
        for scale in (1.0, 1.1, 1.15)
    ]
    report = _outer_gate(matrices)
    assert report["passed"]
    assert len(report["rows"]) == 2


def test_outer_gate_rejects_nonmonotone_growing_increment():
    names = ("A",)
    matrices = [
        MaxwellMatrix(names, [[value]]) for value in (1.0, 1.2, 0.8)
    ]
    assert not _outer_gate(matrices)["passed"]
