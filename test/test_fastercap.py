#!/usr/bin/env python3
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from fastercap import (  # noqa: E402
    ConductorSurface,
    FasterCapRunConfig,
    FasterCapRunRejected,
    box_dielectric_surface,
    box_surface,
    classify_fastercap_output,
    extruded_triangulation_surface,
    parse_fastercap_output,
    render_fastercap_input,
    run_fastercap,
    run_fastercap_manual,
    validate_deck_manifest,
    write_fastercap_input,
)
from process_monitor import ProcessExecution  # noqa: E402


SAMPLE_OUTPUT = """Running FasterCap version 6.0.7
Starting capacitance extraction with the following parameters:
Auto calculation with max error: 0.005
Remark: Auto option overrides all other Manual settings
3D Solver Engine invoked
Solution scheme (-g): Collocation, GMRES tolerance (-t): 0.0025
Number of input panels to solver engine: 80
Iteration number #0
Mesh refinement (-m): 10
Precond Type(s) (-p): Jacobi
GMRES Iteration: 0 1 2
GMRES Iteration: 0 1 2
Capacitance matrix is:
Dimension 2 x 2
g1_dcp_c0001  3.0e-12 -2.0e-12
g2_dcp_c0002  -2.0e-12 4.0e-12
Number of panels after refinement: 100
Iteration number #1
Mesh refinement (-m): 7
Precond Type(s) (-p): Jacobi
GMRES Iteration: 0 1 2 3
GMRES Iteration: 0 1 2 3
Capacitance matrix is:
Dimension 2 x 2
g1_dcp_c0001  3.1e-12 -2.1e-12
g2_dcp_c0002  -2.101e-12 4.1e-12
Weighted Frobenius norm of the difference between capacitance (auto option): 0.004
Number of panels after refinement: 120
Total allocated memory: 1100 kilobytes
Total time: 0.1s
"""

MANUAL_OUTPUT = """Running FasterCap version 6.0.7
Starting capacitance extraction with the following parameters:
3D Solver Engine invoked
Solution scheme (-g): Collocation, GMRES tolerance (-t): 0.01
Mesh refinement (-m): 10
Precond Type(s) (-p): Jacobi
Number of input panels to solver engine: 80
GMRES Iteration: 0 1 2
GMRES Iteration: 0 1 2
Capacitance matrix is:
Dimension 2 x 2
g1_dcp_c0001  3.0e-12 -2.0e-12
g2_dcp_c0002  -2.0e-12 4.0e-12
Number of panels after refinement: 100
Total allocated memory: 1100 kilobytes
Total time: 0.1s
"""


def _two_conductor_deck(tmp_path):
    return write_fastercap_input(
        tmp_path / "parallel_plate.lst",
        (
            box_surface("plate_a", (0, 0, 0), (1, 1, 0.1)),
            box_surface("plate_b", (0, 0, 1), (1, 1, 1.1)),
        ),
    )


def _deck_manifest(path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return validate_deck_manifest(
        path.with_name(f"{path.name}.manifest.json"), deck_sha256=digest
    )


def _execution(stdout, stderr=b"", *, returncode=0, failures=(),
               max_panels=120, max_gmres=3, elapsed=0.1):
    stdout = stdout.encode() if isinstance(stdout, str) else stdout
    stderr = stderr.encode() if isinstance(stderr, str) else stderr
    return ProcessExecution(
        command=("FasterCap", "model.lst"),
        cwd="/tmp",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed_s=elapsed,
        peak_rss_bytes=1024,
        output_bytes=len(stdout) + len(stderr),
        directory_growth_bytes=0,
        max_refined_panels=max_panels,
        max_gmres_iteration=max_gmres,
        limit_failures=tuple(failures),
    )


def _classify(tmp_path, stdout=SAMPLE_OUTPUT, *, stderr=b"", config=None,
              execution=None):
    path = _two_conductor_deck(tmp_path)
    config = config or FasterCapRunConfig(
        mode="automatic", resource_class="synthetic",
        relative_error=0.005,
    )
    stdout_bytes = stdout.encode() if isinstance(stdout, str) else stdout
    execution = execution or _execution(stdout_bytes, stderr)
    return classify_fastercap_output(
        stdout_bytes,
        stderr.encode() if isinstance(stderr, str) else stderr,
        deck_manifest=_deck_manifest(path),
        config=config,
        execution=execution,
    )


def _python_fake(tmp_path, name, *, stdout="", stderr="", exit_code=0,
                 sleep_s=0.0, record_path=None):
    script = tmp_path / f"{name}.py"
    record = "" if record_path is None else (
        f"open({str(record_path)!r}, 'w').write('\\n'.join(sys.argv[1:]))\n"
    )
    script.write_text(
        "import sys,time\n"
        + record
        + f"sys.stdout.write({stdout!r}); sys.stdout.flush()\n"
        + f"sys.stderr.write({stderr!r}); sys.stderr.flush()\n"
        + f"time.sleep({sleep_s!r})\n"
        + f"raise SystemExit({exit_code})\n"
    )
    return [sys.executable, str(script)]


def test_direct_matrix_parser_cannot_bypass_classifier():
    with pytest.raises(RuntimeError, match="diagnostic-only"):
        parse_fastercap_output(SAMPLE_OUTPUT)


def test_classifier_accepts_only_complete_qualified_transcript(tmp_path):
    transcript = _classify(tmp_path)
    assert transcript.failures == ()
    assert len(transcript.iterations) == 2
    assert transcript.iterations[-1].auto_norm == pytest.approx(0.004)
    assert transcript.effective_gmres_tolerance == pytest.approx(0.0025)
    assert transcript.effective_mesh_refinement == pytest.approx(7.0)
    assert transcript.effective_preconditioners == ("Jacobi",)
    assert transcript.averaging_max_displacement_f == pytest.approx(0.5e-15)


def test_classifier_accepts_exact_fastercap_human_duration(tmp_path):
    output = SAMPLE_OUTPUT.replace(
        "Total time: 0.1s",
        "Total time: 0.1s (0 days, 0 hours, 0 mins, 0 s)",
    )
    assert _classify(tmp_path, output).failures == ()


@pytest.mark.parametrize("mutation, gate", [
    (lambda text: text + "Error: singular operator\n", "solver_diagnostics"),
    (lambda text: text + "ERROR: unhandled exception, exiting\n",
     "solver_diagnostics"),
    (lambda text: text +
     "Error: cannot retrieve the information about the free memory quantity\n",
     "solver_diagnostics"),
    (lambda text: text.replace(
        "Iteration number #1", "Error: not converging after 1000 iterations, "
        "norm of the residual is 0.012, while targeting 0.0025\n"
        "Iteration number #1"), "solver_diagnostics"),
    (lambda text: text + "Iteration number #0\nGMRES Iteration: 0 1\n",
     "normal_termination"),
    (lambda text: text.replace("g1_dcp_c0001", "g9_replacement", 1),
     "solver_label_identity"),
    (lambda text: text.replace("g2_dcp_c0002", "g1_dcp_c0001", 1),
     "complete_iterations"),
    (lambda text: text.replace("g2_dcp_c0002", "g9_replacement"),
     "solver_label_identity"),
    (lambda text: text.replace("GMRES Iteration: 0 1 2", "GMRES Iteration: nonsense"),
     "gmres_rhs_outcomes"),
    (lambda text: text.replace("Total time: 0.1s", "Total time: nonsense"),
     "normal_termination"),
    (lambda text: text.replace(
        "Total time: 0.1s", "Total time: 0.1s Error: late failure"),
     "solver_diagnostics"),
    (lambda text: text.replace("Running FasterCap version 6.0.7\n", ""),
     "solver_version"),
    (lambda text: text.replace(
        "Number of input panels to solver engine: 80\n", ""),
     "initial_panels"),
    (lambda text: text.replace("Mesh refinement (-m):", "Mesh value:"),
     "effective_mesh_refinement"),
    (lambda text: text.replace("Precond Type(s) (-p):", "Preconditioner:"),
     "effective_preconditioners"),
    (lambda text: text.replace(
        "Iteration number #1\nMesh refinement (-m): 7\n",
        "Iteration number #1\n"),
     "complete_iterations"),
    (lambda text: text.replace(
        "Iteration number #1\nMesh refinement (-m): 7\n"
        "Precond Type(s) (-p): Jacobi\n",
        "Iteration number #1\nMesh refinement (-m): 7\n"),
     "complete_iterations"),
    (lambda text: text.replace(
        "Number of panels after refinement: 120\n", ""),
     "complete_iterations"),
])
def test_classifier_rejects_adversarial_transcripts(tmp_path, mutation, gate):
    transcript = _classify(tmp_path, mutation(SAMPLE_OUTPUT))
    assert not transcript.gate_results[gate]["passed"]
    assert transcript.failures


def test_malformed_matrix_and_terminal_before_norm_are_rejected(tmp_path):
    malformed = SAMPLE_OUTPUT.replace(
        "g2_dcp_c0002  -2.101e-12 4.1e-12",
        "g2_dcp_c0002  -2.101e-12",
    )
    assert _classify(tmp_path, malformed).failures
    misplaced = SAMPLE_OUTPUT.replace(
        "Weighted Frobenius norm of the difference between capacitance "
        "(auto option): 0.004\n",
        "",
    ).replace(
        "Total time: 0.1s",
        "Total time: 0.1s\nWeighted Frobenius norm of the difference "
        "between capacitance (auto option): 0.004",
    )
    transcript = _classify(tmp_path, misplaced)
    assert not transcript.gate_results["normal_termination"]["passed"]


def test_captured_complete_matrix_failure_is_diagnostic_only(tmp_path):
    failed = (Path(ROOT) / "test" / "fixtures" /
              "fastercap_fugu_complete_matrix_failure.out").read_text()
    failed = (failed.replace("g1_SW", "g1_dcp_c0001")
              .replace("g2_VIN", "g2_dcp_c0002")
              .replace("g3_PGND", "g3_dcp_c0003"))
    path = write_fastercap_input(
        tmp_path / "fugu.lst",
        tuple(box_surface(name, (0, 0, index), (1, 1, index + 0.1))
              for index, name in enumerate(("SW", "VIN", "PGND"))),
    )
    execution = _execution(
        failed, failures=("wall time exceeded 1200s",),
        max_panels=626785, max_gmres=1000, elapsed=1800,
    )
    transcript = classify_fastercap_output(
        failed.encode(), b"",
        deck_manifest=_deck_manifest(path),
        config=FasterCapRunConfig(
            mode="automatic", resource_class="fugu_diagnostic",
            relative_error=0.01,
        ),
        execution=execution,
    )
    assert transcript.failures
    assert not transcript.gate_results["final_iteration_complete"]["passed"]
    assert transcript.resource_observations["max_refined_panels"] == 626785
    assert not hasattr(transcript, "accepted_matrix")


def test_self_contained_input_contains_stable_labels():
    conductors = (
        box_surface("plate_a", (-0.01, -0.01, 0), (0.01, 0.01, 0.0001)),
        box_surface("plate_b", (-0.01, -0.01, 0.002),
                    (0.01, 0.01, 0.0021)),
    )
    text = render_fastercap_input(conductors)
    assert "C conductor_1.pan 1 0 0 0" in text
    assert text.count("Q dcp_c0001 ") == 6
    assert text.count("Q dcp_c0002 ") == 6
    assert "* solver_output_label g1_dcp_c0001" in text


def test_self_contained_input_emits_dielectric_interface():
    conductor = box_surface("plate", (-1, -1, -0.1), (1, 1, 0.1),
                            relative_permittivity=4.5)
    dielectric = box_dielectric_surface(
        "fr4", (-2, -2, -1), (2, 2, 1), inside_permittivity=4.5
    )
    text = render_fastercap_input((conductor,), (dielectric,))
    assert "C conductor_1.pan 4.5 0 0 0" in text
    assert "D dielectric_1.pan 1 4.5 0 0 0 0 0 0 -" in text
    assert text.count("Q fr4 ") == 6


def test_triangle_mesh_extrusion_closes_top_bottom_and_boundary():
    surface = extruded_triangulation_surface(
        "copper",
        (((0, 0), (1, 0), (1, 1)), ((0, 0), (1, 1), (0, 1))),
        0.0, 0.1,
    )
    assert len(surface.panels) == 8
    text = render_fastercap_input((surface,))
    assert text.count("T dcp_c0001 ") == 4
    assert text.count("Q dcp_c0001 ") == 4


def test_collated_parts_preserve_one_solver_conductor_identity(tmp_path):
    first = box_surface("net", (0, 0, 0), (1, 1, 1))
    second = box_surface("net", (2, 0, 0), (3, 1, 1))
    conductor = ConductorSurface(
        "net",
        first.panels + second.panels,
        parts=(first.panels, second.panels),
    )
    text = render_fastercap_input((conductor,))
    assert "C conductor_1_part_0001.pan 1 0 0 0 +" in text
    assert "C conductor_1.pan 1 0 0 0" in text
    assert text.count("* conductor net") == 1
    assert text.count("dcp_c0001") == 13
    path = tmp_path / "collated.lst"
    write_fastercap_input(path, (conductor,))
    manifest = validate_deck_manifest(
        tmp_path / "collated.lst.manifest.json",
        deck_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        deck_path=path,
    )
    assert manifest.conductor_names == ("net",)

    with pytest.raises(ValueError, match="partition panels"):
        ConductorSurface(
            "bad", first.panels + second.panels,
            parts=(second.panels, first.panels),
        )


def test_triangle_mesh_extrusion_rejects_nonconforming_edges():
    duplicate = ((0, 0), (1, 0), (0, 1))
    with pytest.raises(ValueError, match="opposite edge directions"):
        extruded_triangulation_surface("duplicate", (duplicate, duplicate), 0, 1)
    with pytest.raises(ValueError, match="non-manifold"):
        extruded_triangulation_surface(
            "triplicate", (duplicate, duplicate, duplicate), 0, 1
        )


def test_runner_uses_auto_mode_and_byte_exact_manifest(tmp_path):
    input_path = _two_conductor_deck(tmp_path)
    stdout_digest = hashlib.sha256(SAMPLE_OUTPUT.encode()).hexdigest()
    corrupt_stdout = input_path.with_name(
        f"{input_path.name}.stdout.{stdout_digest}.bin"
    )
    corrupt_stdout.write_bytes(b"corrupt")
    arguments_path = tmp_path / "arguments.txt"
    executable = _python_fake(
        tmp_path, "success", stdout=SAMPLE_OUTPUT, record_path=arguments_path
    )
    result = run_fastercap(
        input_path, conductor_names=("plate_a", "plate_b"),
        executable=executable, relative_error=0.005,
    )
    assert result.matrix.names == ("plate_a", "plate_b")
    np.testing.assert_array_equal(result.matrix.values, result.matrix.values.T)
    assert arguments_path.read_text().splitlines() == [
        "parallel_plate.lst", "-a0.005", "-ap", "-b"
    ]
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["lifecycle_state"] == "numerically_converged_diagnostic"
    assert manifest["content_id_sha256"] in result.manifest_path.name
    assert Path(manifest["stdout"]).read_bytes() == SAMPLE_OUTPUT.encode()
    assert manifest["solver_binary"] == str(Path(sys.executable).resolve())
    assert manifest["solver_binary_sha256"]
    assert manifest["averaging_max_displacement_f"] == pytest.approx(0.5e-15)
    assert manifest["resource_observations"]["peak_rss_bytes"] > 0
    assert manifest["matrix_sha256"]
    assert manifest["matrix_gate_results"]["raw"]["row_sum"]["passed"]
    assert manifest["matrix_gate_results"]["downstream"]["positive_semidefinite"]["passed"]
    assert manifest["matrix_gate_results"]["exact_branch_reconstruction"]["passed"]
    assert tuple(manifest["executed_command"]) == result.command
    assert manifest["parser_sha256"] and manifest["monitor_sha256"]
    assert manifest["maxwell_sha256"]
    assert manifest["dependencies"]["numpy"] == np.__version__
    assert manifest["dependencies"]["psutil"]
    assert corrupt_stdout.read_bytes() == SAMPLE_OUTPUT.encode()


def test_path_resolved_solver_binary_is_hashed(tmp_path, monkeypatch):
    input_path = _two_conductor_deck(tmp_path)
    script = _python_fake(tmp_path, "path-success", stdout=SAMPLE_OUTPUT)[1]
    executable_name = Path(sys.executable).name
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    result = run_fastercap(
        input_path, executable=[executable_name, script], relative_error=0.005
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["solver_binary"] == str(Path(sys.executable).resolve())
    assert manifest["solver_binary_sha256"]


def test_manual_mode_is_distinct_and_never_returns_matrix(tmp_path):
    input_path = _two_conductor_deck(tmp_path)
    arguments_path = tmp_path / "manual-arguments.txt"
    executable = _python_fake(
        tmp_path, "manual", stdout=MANUAL_OUTPUT, record_path=arguments_path
    )
    result = run_fastercap_manual(
        input_path, mesh_refinement=10, gmres_tolerance=0.01,
        executable=executable,
    )
    assert not hasattr(result, "matrix")
    assert result.transcript.mode == "manual"
    assert arguments_path.read_text().splitlines() == [
        "parallel_plate.lst", "-m10", "-t0.01", "-pj", "-b"
    ]
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["lifecycle_state"] == "rejected_diagnostic"
    assert manifest["diagnostic_kind"] == "manual_solver_probe"
    assert manifest["matrix_sha256"] is None


def test_manual_preconditioner_is_requested_and_verified(tmp_path):
    input_path = _two_conductor_deck(tmp_path)
    output = MANUAL_OUTPUT.replace(
        "Precond Type(s) (-p): Jacobi",
        "Precond Type(s) (-p): Block, block preconditioner dimension (-pb): 16",
    )
    arguments_path = tmp_path / "block-arguments.txt"
    executable = _python_fake(
        tmp_path, "manual-block", stdout=output, record_path=arguments_path
    )
    result = run_fastercap_manual(
        input_path, mesh_refinement=10, gmres_tolerance=0.01,
        preconditioner="block", preconditioner_dimension=16,
        executable=executable,
    )
    assert result.transcript.gate_results[
        "manual_preconditioner_setting"
    ]["passed"]
    assert "-pb16" in arguments_path.read_text().splitlines()


def test_manual_probe_retains_failed_matrix_without_exposing_it(tmp_path):
    input_path = _two_conductor_deck(tmp_path)
    nonreciprocal = MANUAL_OUTPUT.replace(
        "g2_dcp_c0002  -2.0e-12 4.0e-12",
        "g2_dcp_c0002  -1.0e-12 4.0e-12",
    )
    executable = _python_fake(tmp_path, "manual-failed", stdout=nonreciprocal)
    result = run_fastercap_manual(
        input_path, mesh_refinement=10, gmres_tolerance=0.01,
        executable=executable,
    )
    assert result.transcript.failures
    assert not hasattr(result, "matrix")
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["matrix_sha256"] is None
    assert manifest["diagnostic_matrices"]


def _move_final_rhs_after_terminal(text):
    evidence = "GMRES Iteration: 0 1 2 3\n" * 2
    return text.replace(evidence, "").replace(
        "Total time: 0.1s", f"Total time: 0.1s\n{evidence.rstrip()}"
    )


def _move_final_settings_after_terminal(text):
    evidence = (
        "Mesh refinement (-m): 7\n"
        "Precond Type(s) (-p): Jacobi\n"
        "Number of panels after refinement: 120\n"
    )
    changed = text
    for line in evidence.splitlines(keepends=True):
        before, separator, after = changed.rpartition(line)
        assert separator
        changed = before + after
    return changed.replace(
        "Total time: 0.1s", f"Total time: 0.1s\n{evidence.rstrip()}"
    )


def _move_final_norm_before_matrix(text):
    norm = (
        "Weighted Frobenius norm of the difference between capacitance "
        "(auto option): 0.004\n"
    )
    marker = (
        "Capacitance matrix is:\nDimension 2 x 2\n"
        "g1_dcp_c0001  3.1e-12 -2.1e-12\n"
    )
    return text.replace(norm, "").replace(marker, norm + marker)


def _add_after_final_matrix(text, record):
    row = "g2_dcp_c0002  -2.101e-12 4.1e-12\n"
    return text.replace(row, row + record + "\n")


def _add_extra_matrix_row(text):
    return _add_after_final_matrix(text, "g3_extra 1.0e-12 2.0e-12")


@pytest.mark.parametrize("name,mutation", [
    ("rhs-after-terminal", _move_final_rhs_after_terminal),
    ("settings-after-terminal", _move_final_settings_after_terminal),
    ("norm-before-matrix", _move_final_norm_before_matrix),
    ("extra-matrix-row", _add_extra_matrix_row),
    ("nonfinite-extra-row", lambda text: _add_after_final_matrix(
        text, "g1_dcp_c0001 nan nan"
    )),
    ("short-extra-row", lambda text: _add_after_final_matrix(
        text, "g1_dcp_c0001 1e-12"
    )),
    ("duplicate-extra-row", lambda text: _add_after_final_matrix(
        text, "g2_dcp_c0002 -1e-12 2e-12"
    )),
    ("extra-dimension", lambda text: _add_after_final_matrix(
        text, "Dimension 2 x 2"
    )),
    ("mixed-case-error", lambda text: text.replace(
        "Total time: 0.1s", "prefix eRrOr: late failure\nTotal time: 0.1s"
    )),
    ("terminal-not-converged", lambda text: text.replace(
        "Total time: 0.1s", "Total time: 0.1s (not converged)"
    )),
    ("terminal-aborted", lambda text: text.replace(
        "Total time: 0.1s", "Total time: 0.1s (aborted)"
    )),
    ("terminal-incomplete", lambda text: text.replace(
        "Total time: 0.1s", "Total time: 0.1s (incomplete output)"
    )),
])
def test_runner_rejects_positionally_invalid_evidence(
        tmp_path, name, mutation):
    input_path = _two_conductor_deck(tmp_path)
    executable = _python_fake(
        tmp_path, name, stdout=mutation(SAMPLE_OUTPUT)
    )
    with pytest.raises(FasterCapRunRejected) as caught:
        run_fastercap(input_path, executable=executable)
    manifest = json.loads(caught.value.manifest_path.read_text())
    assert manifest["lifecycle_state"] == "rejected_diagnostic"
    assert manifest["failures"]
    assert manifest["matrix_sha256"] is None


def test_runner_rejects_failure_and_timeout_with_manifests(tmp_path):
    input_path = _two_conductor_deck(tmp_path)
    failed = _python_fake(
        tmp_path, "failed", stderr="Error: geometry failed\n", exit_code=7
    )
    with pytest.raises(FasterCapRunRejected) as caught:
        run_fastercap(input_path, executable=failed)
    manifest = json.loads(caught.value.manifest_path.read_text())
    assert manifest["lifecycle_state"] == "rejected_diagnostic"
    assert manifest["matrix_sha256"] is None
    assert Path(manifest["stderr"]).read_bytes() == b"Error: geometry failed\n"

    slow = _python_fake(tmp_path, "slow", sleep_s=2)
    with pytest.raises(FasterCapRunRejected) as caught:
        run_fastercap(input_path, executable=slow, timeout=0.05)
    manifest = json.loads(caught.value.manifest_path.read_text())
    assert manifest["gate_results"]["resource_limits"]["passed"] is False


def test_incomplete_iteration_resource_limit_is_enforced_while_running(tmp_path):
    input_path = _two_conductor_deck(tmp_path)
    output = (
        "Running FasterCap version 6.0.7\n"
        "Auto calculation with max error: 0.005\n"
        "Iteration number #0\n"
        "Number of panels after refinement: 250001\n"
    )
    executable = _python_fake(tmp_path, "panels", stdout=output, sleep_s=2)
    with pytest.raises(FasterCapRunRejected) as caught:
        run_fastercap(input_path, executable=executable)
    manifest = json.loads(caught.value.manifest_path.read_text())
    resources = manifest["resource_observations"]
    assert resources["max_refined_panels"] == 250001
    assert manifest["gate_results"]["resource_limits"]["passed"] is False


@pytest.mark.parametrize("mutation", [
    lambda raw: raw["conductors"][1].__setitem__(
        "solver_output_label", "g1_dcp_c0001"
    ),
    lambda raw: raw["conductors"][1].__setitem__(
        "name", raw["conductors"][0]["name"]
    ),
    lambda raw: raw["conductors"][1].__setitem__("index", 9),
    lambda raw: (
        raw["conductors"][0].__setitem__("name", "plate_b"),
        raw["conductors"][1].__setitem__("name", "plate_a"),
    ),
    lambda raw: raw["conductors"][1].__setitem__("name", "replacement"),
    lambda raw: raw["conductors"][1].__setitem__("extra", True),
])
def test_manifest_schema_and_identity_fail_closed_before_launch(tmp_path, mutation):
    input_path = _two_conductor_deck(tmp_path)
    manifest_path = input_path.with_name(f"{input_path.name}.manifest.json")
    raw = json.loads(manifest_path.read_text())
    mutation(raw)
    manifest_path.write_text(json.dumps(raw))
    with pytest.raises(FasterCapRunRejected, match="before launch") as caught:
        run_fastercap(input_path, executable="unused")
    manifest = json.loads(caught.value.manifest_path.read_text())
    assert manifest["launch_error"]
    assert manifest["deck_manifest_sha256"]
    assert manifest["deck_manifest_validated"] is False
    assert manifest["matrix_sha256"] is None


def test_v1_runner_exposes_no_configurable_matrix_policy():
    parameters = inspect.signature(run_fastercap).parameters
    assert "reciprocity_rtol" not in parameters
    assert "reciprocity_atol" not in parameters


def test_auto_and_manual_configuration_semantics_are_disjoint():
    with pytest.raises(ValueError, match="cannot request manual"):
        FasterCapRunConfig(
            mode="automatic", resource_class="synthetic",
            relative_error=0.01, mesh_refinement=1,
        )
    with pytest.raises(ValueError, match="cannot request automatic"):
        FasterCapRunConfig(
            mode="manual", resource_class="synthetic",
            mesh_refinement=1, gmres_tolerance=0.01,
            relative_error=0.01, auto_precondition=False,
        )


def test_launch_and_classifier_exceptions_write_rejected_manifests(
        tmp_path, monkeypatch):
    input_path = _two_conductor_deck(tmp_path)
    with pytest.raises(FasterCapRunRejected, match="before launch") as caught:
        run_fastercap(input_path, executable="unused", timeout=float("nan"))
    timeout_manifest = json.loads(caught.value.manifest_path.read_text())
    assert "timeout must be finite" in timeout_manifest["launch_error"]

    invalid_executable = tmp_path / "not-executable"
    invalid_executable.write_text("not a binary")
    with pytest.raises(FasterCapRunRejected, match="launch failed") as caught:
        run_fastercap(input_path, executable=invalid_executable)
    manifest = json.loads(caught.value.manifest_path.read_text())
    assert manifest["launch_error"]
    assert manifest["matrix_sha256"] is None

    executable = _python_fake(tmp_path, "parser-error", stdout=SAMPLE_OUTPUT)
    monkeypatch.setattr(
        "fastercap._classify_fastercap_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("parser exploded")),
    )
    with pytest.raises(FasterCapRunRejected, match="classification failed") as caught:
        run_fastercap(input_path, executable=executable, relative_error=0.005)
    manifest = json.loads(caught.value.manifest_path.read_text())
    assert "parser exploded" in manifest["launch_error"]
    assert Path(manifest["stdout"]).read_bytes() == SAMPLE_OUTPUT.encode()


def test_runtime_error_during_finalization_writes_rejected_manifest(
        tmp_path, monkeypatch):
    input_path = _two_conductor_deck(tmp_path)
    executable = _python_fake(tmp_path, "finalizer-error", stdout=SAMPLE_OUTPUT)
    monkeypatch.setattr(
        "fastercap.symmetrize_maxwell",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("finalizer exploded")),
    )
    with pytest.raises(FasterCapRunRejected, match="finalization failed") as caught:
        run_fastercap(input_path, executable=executable, relative_error=0.005)
    manifest = json.loads(caught.value.manifest_path.read_text())
    assert "finalizer exploded" in manifest["launch_error"]
    assert manifest["matrix_sha256"] is None


def test_runner_rejects_conductor_order_with_manifest(tmp_path):
    input_path = _two_conductor_deck(tmp_path)
    with pytest.raises(FasterCapRunRejected, match="before launch") as caught:
        run_fastercap(
            input_path, conductor_names=("plate_b", "plate_a"),
            executable="unused",
        )
    assert caught.value.manifest_path.is_file()


@pytest.mark.parametrize("panel, message", [
    (((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0.1)), "planar"),
    (((0, 0, 0), (1, 1, 0), (0, 1, 0), (1, 0, 0)), "ordered"),
    (((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 0, 0)), "ordered"),
])
def test_invalid_quadrilateral_panels_are_rejected(panel, message):
    with pytest.raises(ValueError, match=message):
        ConductorSurface("bad_panel", (panel,))


def test_fastercap_supports_package_import():
    completed = subprocess.run(
        [sys.executable, "-c", "import lib.fastercap"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(
    shutil.which(os.environ.get("FASTERCAP", "FasterCap")) is None,
    reason="FasterCap executable is not installed",
)
def test_real_fastercap_parallel_plate(tmp_path):
    width = 20e-3
    gap = 2e-3
    thickness = 0.1e-3
    half = width / 2
    square_mesh = (
        ((-half, -half), (half, -half), (half, half)),
        ((-half, -half), (half, half), (-half, half)),
    )
    conductors = (
        extruded_triangulation_surface("plate_low", square_mesh, 0, thickness),
        extruded_triangulation_surface(
            "plate_high", square_mesh, thickness + gap, 2 * thickness + gap
        ),
    )
    input_path = write_fastercap_input(tmp_path / "parallel_plate.lst", conductors)
    result = run_fastercap(
        input_path,
        conductor_names=tuple(conductor.name for conductor in conductors),
        timeout=30,
    )
    assert result.transcript.failures == ()
    assert result.transcript.iterations[-1].matrix is not None
    assert result.transcript.normal_termination
