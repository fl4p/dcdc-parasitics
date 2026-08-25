#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_qualification_plan_uses_vertical_resolution_not_p_ladder():
    text = (ROOT / "docs/palace-electrostatic-qualification-plan.md").read_text()
    assert "max_vertical_step_m" in text
    assert "p` cross-check on analytic fixtures only" in text
    assert "completed z20 p1 direct matrix is rejected" in text


def test_efficiency_plan_marks_frozen_p_ladder_superseded():
    text = (ROOT / "docs/palace-workflow-efficiency-plan.md").read_text()
    assert "superseded as a physical convergence gate" in text
    assert "max_vertical_step_m` ladder at fixed low order" in text


def test_conditioning_brief_records_current_direct_solver_state():
    text = (ROOT / "docs/palace-conditioning-brief.md").read_text()
    assert "direct SuperLU" in text
    assert "completed the 82-terminal z20 Fugu solve" in text
    assert "conductor-extraction defect" in text
    assert "frozen-mesh p-ladder failed and is superseded" in text
