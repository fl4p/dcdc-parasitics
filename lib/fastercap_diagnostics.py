#!/usr/bin/env python3
"""Fail-closed FasterCap deck identity and transcript classification."""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re

import numpy as np

if __package__:
    from .maxwell import (
        MaxwellMatrix,
        maxwell_gate_results,
        maxwell_to_branches,
        raw_maxwell_gate_results,
        symmetrize_maxwell,
    )
else:
    from maxwell import (
        MaxwellMatrix,
        maxwell_gate_results,
        maxwell_to_branches,
        raw_maxwell_gate_results,
        symmetrize_maxwell,
    )


GATE_POLICY = "fastercap-pcb-gates-v1"
DECK_MANIFEST_FORMAT = "dcdc-fastercap-deck-v1"
RUN_MANIFEST_FORMAT = "dcdc-fastercap-run-v1"
RESOURCE_LIMITS = {
    "synthetic": {
        "wall_time_s": 120.0,
        "peak_rss_bytes": 4 * 1024**3,
        "output_bytes": 1024**3,
        "refined_panels": 250_000,
        "gmres_iterations_per_rhs": 1_000,
    },
    "fugu_diagnostic": {
        "wall_time_s": 20 * 60.0,
        "peak_rss_bytes": 8 * 1024**3,
        "output_bytes": 5 * 1024**3,
        "refined_panels": 500_000,
        "gmres_iterations_per_rhs": 1_000,
    },
    "fugu_physical": {
        "wall_time_s": 30 * 60.0,
        "peak_rss_bytes": 12 * 1024**3,
        "output_bytes": 10 * 1024**3,
        "refined_panels": 1_000_000,
        "gmres_iterations_per_rhs": 1_000,
    },
}


@dataclass(frozen=True)
class FasterCapDeckConductor:
    index: int
    name: str
    panel_label: str
    solver_output_label: str


@dataclass(frozen=True)
class FasterCapDeckManifest:
    path: Path
    deck_sha256: str
    conductors: tuple[FasterCapDeckConductor, ...]
    raw: dict

    @property
    def conductor_names(self):
        return tuple(conductor.name for conductor in self.conductors)

    @property
    def solver_output_labels(self):
        return tuple(
            conductor.solver_output_label for conductor in self.conductors
        )


@dataclass(frozen=True)
class FasterCapRunConfig:
    mode: str
    resource_class: str
    relative_error: float | None = None
    mesh_refinement: float | None = None
    gmres_tolerance: float | None = None
    auto_precondition: bool = True
    manual_preconditioner: str | None = None
    preconditioner_dimension: int | None = None

    def __post_init__(self):
        if self.mode not in ("automatic", "manual"):
            raise ValueError("FasterCap mode must be 'automatic' or 'manual'")
        if self.resource_class not in RESOURCE_LIMITS:
            raise ValueError(
                f"unknown FasterCap resource class {self.resource_class!r}"
            )
        if self.mode == "automatic":
            if self.relative_error is None:
                raise ValueError("automatic mode requires relative_error")
            value = float(self.relative_error)
            if not np.isfinite(value) or not 0.0 < value < 1.0:
                raise ValueError(
                    "relative_error must be finite and lie between zero and one"
                )
            if (self.mesh_refinement is not None
                    or self.gmres_tolerance is not None
                    or self.manual_preconditioner is not None
                    or self.preconditioner_dimension is not None):
                raise ValueError(
                    "automatic mode cannot request manual solver settings"
                )
            object.__setattr__(self, "relative_error", value)
            return
        if self.mesh_refinement is None or self.gmres_tolerance is None:
            raise ValueError("manual mode requires both -m and -t")
        mesh = float(self.mesh_refinement)
        tolerance = float(self.gmres_tolerance)
        if (not np.isfinite(mesh) or mesh <= 0.0
                or not np.isfinite(tolerance) or not 0.0 < tolerance < 1.0):
            raise ValueError("manual -m and -t must be finite and positive")
        if self.relative_error is not None or self.auto_precondition:
            raise ValueError("manual mode cannot request automatic -a or -ap")
        preconditioner = self.manual_preconditioner or "jacobi"
        if preconditioner not in ("none", "jacobi", "block", "two_level"):
            raise ValueError("unsupported manual preconditioner")
        dimension = self.preconditioner_dimension
        if preconditioner in ("block", "two_level"):
            if (not isinstance(dimension, int) or isinstance(dimension, bool)
                    or dimension <= 0):
                raise ValueError(
                    "block and two_level preconditioners require a positive dimension"
                )
        elif dimension is not None:
            raise ValueError(
                "none and jacobi preconditioners do not take a dimension"
            )
        object.__setattr__(self, "mesh_refinement", mesh)
        object.__setattr__(self, "gmres_tolerance", tolerance)
        object.__setattr__(self, "manual_preconditioner", preconditioner)


@dataclass(frozen=True)
class FasterCapRhsOutcome:
    conductor_label: str
    maximum_iteration: int
    converged: bool
    residual: float | None
    target: float | None
    evidence: str


@dataclass(frozen=True)
class FasterCapIteration:
    number: int
    solver_labels: tuple[str, ...]
    matrix: MaxwellMatrix | None
    auto_norm: float | None
    refined_panels: int | None
    mesh_refinement: float | None
    preconditioners: tuple[str, ...]
    rhs_outcomes: tuple[FasterCapRhsOutcome, ...]
    parse_failures: tuple[str, ...]
    matrix_end_line: int | None
    norm_line: int | None


@dataclass(frozen=True)
class FasterCapTranscript:
    solver_version: str | None
    mode: str
    effective_gmres_tolerance: float | None
    effective_mesh_refinement: float | None
    effective_preconditioners: tuple[str, ...]
    initial_panels: int | None
    iterations: tuple[FasterCapIteration, ...]
    normal_termination: bool
    solver_runtime_s: float | None
    averaging_max_displacement_f: float | None
    matrix_gate_results: dict
    failures: tuple[str, ...]
    gate_results: dict
    resource_observations: dict


@dataclass(frozen=True)
class FasterCapRun:
    command: tuple[str, ...]
    stdout: str
    stderr: str
    matrix: MaxwellMatrix
    transcript: FasterCapTranscript
    manifest_path: Path


@dataclass(frozen=True)
class FasterCapManualDiagnostic:
    command: tuple[str, ...]
    stdout: str
    stderr: str
    transcript: FasterCapTranscript
    manifest_path: Path


class FasterCapRunRejected(RuntimeError):
    def __init__(self, message, *, transcript, manifest_path):
        super().__init__(message)
        self.transcript = transcript
        self.manifest_path = Path(manifest_path)


def validate_deck_manifest(path, *, deck_sha256, deck_path=None):
    path = Path(path)
    if deck_path is None:
        suffix = ".manifest.json"
        if not path.name.endswith(suffix):
            raise ValueError("deck path is required for this manifest filename")
        deck_path = path.with_name(path.name[:-len(suffix)])
    deck_path = Path(deck_path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid FasterCap deck manifest: {error}") from error
    if not isinstance(raw, dict) or raw.get("format") != DECK_MANIFEST_FORMAT:
        raise ValueError("unsupported FasterCap deck manifest format")
    if raw.get("deck_sha256") != deck_sha256:
        raise ValueError("FasterCap deck does not match its manifest hash")
    provenance = raw.get("provenance")
    provenance_hash = raw.get("provenance_sha256")
    if (provenance is None) != (provenance_hash is None):
        raise ValueError("deck provenance and hash must appear together")
    if provenance is not None:
        if not isinstance(provenance, dict):
            raise ValueError("deck provenance must be an object")
        canonical = json.dumps(
            provenance, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if hashlib.sha256(canonical).hexdigest() != provenance_hash:
            raise ValueError("deck provenance does not match its hash")
    entries = raw.get("conductors")
    if not isinstance(entries, list) or not entries:
        raise ValueError("deck manifest conductors must be a non-empty list")
    conductors = []
    for expected_index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError("deck manifest conductor entry must be an object")
        required = {"index", "name", "panel_label", "solver_output_label"}
        if set(entry) != required:
            raise ValueError("deck manifest conductor fields do not match schema")
        conductor = FasterCapDeckConductor(
            entry["index"], entry["name"], entry["panel_label"],
            entry["solver_output_label"],
        )
        if conductor.index != expected_index:
            raise ValueError("deck manifest conductor indices must be contiguous")
        expected_panel = f"dcp_c{expected_index:04d}"
        expected_solver = f"g{expected_index}_{expected_panel}"
        if (conductor.panel_label != expected_panel
                or conductor.solver_output_label != expected_solver):
            raise ValueError("deck manifest solver labels are not canonical")
        if not conductor.name or re.search(r"\s", conductor.name):
            raise ValueError("deck manifest conductor name is invalid")
        conductors.append(conductor)
    for attribute in ("name", "panel_label", "solver_output_label"):
        values = [getattr(conductor, attribute) for conductor in conductors]
        if len(set(values)) != len(values):
            raise ValueError(f"deck manifest {attribute} values must be unique")

    try:
        deck_text = deck_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read FasterCap deck text: {error}") from error
    embedded_names = tuple(re.findall(
        r"^\* conductor (\S+)$", deck_text, re.MULTILINE
    ))
    embedded_solver_labels = tuple(re.findall(
        r"^\* solver_output_label (\S+)$", deck_text, re.MULTILINE
    ))
    panel_labels = tuple(dict.fromkeys(re.findall(
        r"^[TQ]\s+(dcp_c\d+)\s+", deck_text, re.MULTILINE
    )))
    conductor_files = tuple(int(value) for value in re.findall(
        r"^C conductor_(\d+)\.pan\s+", deck_text, re.MULTILINE
    ))
    expected_indices = tuple(range(1, len(conductors) + 1))
    if conductor_files != expected_indices:
        raise ValueError("deck conductor declarations do not match manifest order")
    if embedded_names != tuple(item.name for item in conductors):
        raise ValueError("deck conductor names do not match manifest mapping")
    if embedded_solver_labels != tuple(
            item.solver_output_label for item in conductors):
        raise ValueError("deck solver labels do not match manifest mapping")
    if panel_labels != tuple(item.panel_label for item in conductors):
        raise ValueError("deck panel labels do not match manifest mapping")
    return FasterCapDeckManifest(path, deck_sha256, tuple(conductors), raw)


def _parse_real(token):
    token = token.strip().replace("D", "e").replace("d", "e")
    if "j" in token.lower():
        raise ValueError("complex FasterCap matrices are not supported")
    value = float(token)
    if not np.isfinite(value):
        raise ValueError("non-finite FasterCap matrix value")
    return value


def _last_float(text, pattern):
    matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
    return float(matches[-1]) if matches else None


def _last_int(text, pattern):
    matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
    return int(matches[-1]) if matches else None


def _parse_matrix(lines, start):
    failures = []
    cursor = start + 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines):
        return cursor, (), None, ("matrix dimension is missing",)
    match = re.fullmatch(
        r"\s*Dimension\s+(\d+)\s*x\s*(\d+)\s*",
        lines[cursor],
        re.IGNORECASE,
    )
    if not match:
        return cursor, (), None, ("matrix dimension is malformed",)
    rows, columns = (int(value) for value in match.groups())
    if rows != columns or rows <= 0:
        return cursor, (), None, ("matrix must be non-empty and square",)
    cursor += 1
    names = []
    values = []
    while cursor < len(lines) and len(values) < rows:
        tokens = lines[cursor].split()
        cursor += 1
        if not tokens:
            continue
        if len(tokens) != columns + 1:
            failures.append("matrix row has the wrong number of columns")
            break
        try:
            row = tuple(_parse_real(token) for token in tokens[1:])
        except (TypeError, ValueError) as error:
            failures.append(str(error))
            break
        names.append(tokens[0])
        values.append(row)
    matrix = None
    if len(values) != rows:
        failures.append(f"matrix has {len(values)} rows; expected {rows}")
    elif len(set(names)) != len(names):
        failures.append("matrix solver labels must be unique")
    else:
        try:
            matrix = MaxwellMatrix(tuple(names), values)
        except ValueError as error:
            failures.append(str(error))
    return cursor, tuple(names), matrix, tuple(failures)


def _new_block(number):
    return {
        "number": number,
        "labels": (),
        "matrix": None,
        "auto_norm": None,
        "refined_panels": None,
        "mesh_refinement": None,
        "preconditioners": [],
        "rhs": [],
        "failures": [],
        "matrix_end_line": None,
        "norm_line": None,
        "phase": "settings",
    }


def _finish_block(block, expected_labels):
    labels = block["labels"]
    rhs = []
    for index, outcome in enumerate(block["rhs"]):
        label = expected_labels[index] if index < len(expected_labels) else "<extra>"
        rhs.append(FasterCapRhsOutcome(label, *outcome))
    return FasterCapIteration(
        number=block["number"],
        solver_labels=labels,
        matrix=block["matrix"],
        auto_norm=block["auto_norm"],
        refined_panels=block["refined_panels"],
        mesh_refinement=block["mesh_refinement"],
        preconditioners=tuple(block["preconditioners"]),
        rhs_outcomes=tuple(rhs),
        parse_failures=tuple(block["failures"]),
        matrix_end_line=block["matrix_end_line"],
        norm_line=block["norm_line"],
    )


def _is_extra_matrix_record(line):
    stripped = line.strip()
    if re.match(r"^Dimension\b", stripped, re.IGNORECASE):
        return True
    tokens = stripped.split()
    return bool(tokens and re.fullmatch(r"g\d+_\S+", tokens[0], re.IGNORECASE))


def _parse_iterations(stdout_text, expected_labels, mode, gmres_tolerance):
    lines = stdout_text.splitlines()
    blocks = []
    current = None
    global_failures = []
    header_positions = []
    matrix_positions = []
    terminal_positions = []
    terminal_seen = False
    index = 0

    def iteration_failure(message):
        assert current is not None
        current["failures"].append(f"line {index + 1}: {message}")

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if terminal_seen:
            if stripped:
                global_failures.append(
                    f"meaningful stdout after terminal at line {index + 1}: "
                    f"{stripped}"
                )
            index += 1
            continue

        terminal = re.fullmatch(
            r"Total time:\s*[0-9.eE+-]+s"
            r"(?:\s*\(\d+ days, \d+ hours, \d+ mins, \d+ s\))?\s*",
            line,
            re.IGNORECASE,
        )
        if terminal:
            terminal_positions.append(index)
            terminal_seen = True
            if current is None or current["matrix"] is None:
                global_failures.append("terminal precedes a complete matrix")
            index += 1
            continue

        header = re.search(r"Iteration number\s+#(\d+)", line)
        if header:
            if current is not None:
                blocks.append(_finish_block(current, expected_labels))
            current = _new_block(int(header.group(1)))
            header_positions.append(index)
            index += 1
            continue

        solver_evidence = bool(
            re.search(
                r"Mesh refinement \(-m\):|Precond Type\(s\) \(-p\):|"
                r"Number of panels after refinement:|GMRES Iteration:|"
                r"Capacitance matrix is:|Weighted Frobenius norm",
                line,
                re.IGNORECASE,
            )
        )
        if current is None and mode == "manual" and solver_evidence:
            current = _new_block(0)
        if current is None:
            index += 1
            continue

        mesh = re.search(
            r"Mesh refinement \(-m\):\s*([0-9.eE+-]+)", line,
            re.IGNORECASE,
        )
        if mesh:
            if current["phase"] != "settings" or current["mesh_refinement"] is not None:
                iteration_failure("mesh refinement is out of phase or duplicated")
            else:
                current["mesh_refinement"] = float(mesh.group(1))
            index += 1
            continue

        preconditioner = re.search(
            r"Precond Type\(s\) \(-p\):\s*(.+)", line, re.IGNORECASE
        )
        if preconditioner:
            if current["phase"] != "settings" or current["preconditioners"]:
                iteration_failure("preconditioner evidence is out of phase or duplicated")
            else:
                current["preconditioners"].append(
                    preconditioner.group(1).strip()
                )
            index += 1
            continue

        panels = re.search(
            r"Number of panels after refinement:\s*(\d+)", line,
            re.IGNORECASE,
        )
        if panels:
            value = int(panels.group(1))
            if current["phase"] not in ("settings", "matrix", "post"):
                iteration_failure("refined-panel evidence is out of phase")
            elif (current["refined_panels"] is not None
                  and current["refined_panels"] != value):
                iteration_failure("refined-panel evidence changes within iteration")
            else:
                current["refined_panels"] = value
            index += 1
            continue

        gmres = re.search(r"GMRES Iteration:\s*(.*)", line, re.IGNORECASE)
        if gmres:
            if current["phase"] not in ("settings", "rhs"):
                iteration_failure("GMRES evidence is out of phase")
                index += 1
                continue
            current["phase"] = "rhs"
            tokens = gmres.group(1).split()
            valid_tokens = bool(tokens) and all(
                re.fullmatch(r"\d+", token) for token in tokens
            )
            numbers = [int(token) for token in tokens] if valid_tokens else []
            valid_trace = valid_tokens and numbers == list(range(len(numbers)))
            if not valid_trace:
                iteration_failure(f"malformed GMRES iteration trace: {stripped}")
            current["rhs"].append([
                max(numbers) if numbers else 0,
                valid_trace,
                None,
                gmres_tolerance,
                ("strict contiguous GMRES iteration trace followed by no "
                 "failure diagnostic" if valid_trace else stripped),
            ])
            index += 1
            continue

        failure = re.search(
            r"not converging after\s+(\d+)\s+iterations,\s*"
            r"norm of the residual is\s*([0-9.eE+-]+),\s*"
            r"while targeting\s*([0-9.eE+-]+)",
            line,
            re.IGNORECASE,
        )
        if failure:
            if current["phase"] != "rhs" or not current["rhs"]:
                iteration_failure("GMRES failure diagnostic is out of phase")
            else:
                current["rhs"][-1] = [
                    int(failure.group(1)),
                    False,
                    float(failure.group(2)),
                    float(failure.group(3)),
                    stripped,
                ]
            iteration_failure(stripped)
            index += 1
            continue

        if stripped.lower() == "capacitance matrix is:":
            if current["phase"] != "rhs":
                iteration_failure("capacitance matrix is out of phase")
            if current["matrix"] is not None or current["labels"]:
                iteration_failure("iteration has multiple matrices")
            matrix_positions.append(index)
            cursor, labels, matrix, failures = _parse_matrix(lines, index)
            current["labels"] = labels
            current["matrix"] = matrix
            current["failures"].extend(failures)
            current["matrix_end_line"] = cursor - 1
            current["phase"] = "matrix"
            index = cursor
            continue

        norm = re.search(
            r"Weighted Frobenius norm[^:]*:\s*([0-9.eE+-]+)",
            line,
            re.IGNORECASE,
        )
        if norm:
            if (mode != "automatic" or current["phase"] != "matrix"
                    or current["auto_norm"] is not None):
                iteration_failure("automatic norm is out of phase or duplicated")
            else:
                current["auto_norm"] = float(norm.group(1))
                current["norm_line"] = index
                current["phase"] = "post"
            index += 1
            continue

        if current["matrix"] is not None and _is_extra_matrix_record(line):
            iteration_failure("unexpected matrix record after declared rows")
        index += 1

    if current is not None:
        blocks.append(_finish_block(current, expected_labels))
    if not blocks:
        global_failures.append("no iteration blocks found")
    numbers = [block.number for block in blocks]
    if numbers and numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        global_failures.append(f"iteration numbers are not contiguous: {numbers!r}")
    return (
        tuple(blocks), tuple(global_failures), tuple(header_positions),
        tuple(matrix_positions), tuple(terminal_positions), lines,
    )


def classify_fastercap_output(stdout, stderr, *, deck_manifest, config,
                              execution):
    """Classify every transcript state without exposing a qualified matrix."""
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    combined = stdout_text + "\n" + stderr_text
    expected_labels = deck_manifest.solver_output_labels
    version_match = re.search(r"Running FasterCap version\s+([^\s]+)", combined)
    effective_gmres = _last_float(
        combined, r"GMRES tolerance \(-t\):\s*([0-9.eE+-]+)"
    )
    (iterations, parse_failures, header_positions, _, terminal_positions,
     _) = _parse_iterations(
        stdout_text, expected_labels, config.mode, effective_gmres
    )
    initial_panels = _last_int(
        combined, r"Number of input panels to solver engine:\s*(\d+)"
    )
    solver_runtime = _last_float(
        stdout_text, r"^Total time:\s*([0-9.eE+-]+)s",
    )
    auto_declared = _last_float(
        stdout_text, r"Auto calculation with max error:\s*([0-9.eE+-]+)"
    )
    final = iterations[-1] if iterations else None
    reported_mesh = None if final is None else final.mesh_refinement
    effective_preconditioners = (
        () if final is None else final.preconditioners
    )
    failures = list(parse_failures)
    gates = {}

    def gate(name, passed, detail):
        passed = bool(passed)
        gates[name] = {"passed": passed, "detail": detail}
        if not passed:
            failures.append(f"{name}: {detail}")

    gate("process_exit", execution.returncode == 0 and not execution.limit_failures,
         f"exit code {execution.returncode}; limits {execution.limit_failures!r}")
    gate("resource_limits", not execution.limit_failures,
         "none" if not execution.limit_failures else execution.limit_failures)
    gate("solver_version", version_match is not None,
         None if version_match is None else version_match.group(1))
    gate("initial_panels", initial_panels is not None and initial_panels > 0,
         initial_panels)
    gate("effective_mesh_refinement",
         reported_mesh is not None and np.isfinite(reported_mesh)
         and reported_mesh > 0.0,
         reported_mesh)
    gate("effective_preconditioners", bool(effective_preconditioners),
         effective_preconditioners)
    gate("final_refined_panels",
         final is not None and final.refined_panels is not None
         and final.refined_panels > 0,
         None if final is None else final.refined_panels)
    gate("solver_runtime",
         solver_runtime is not None and np.isfinite(solver_runtime)
         and solver_runtime >= 0.0,
         solver_runtime)

    error_lines = []
    for line in combined.splitlines():
        stripped = line.strip()
        error_candidate = re.sub(
            r"Auto calculation with max error:\s*[0-9.eE+-]+",
            "",
            stripped,
            flags=re.IGNORECASE,
        )
        if re.search(r"\berror\s*:", error_candidate, re.IGNORECASE):
            error_lines.append(stripped)
        elif re.search(
                r"\b(singular|segmentation fault|fatal error|"
                r"degenerate quadrilateral|thin triangles|malformed)\b",
                stripped,
                re.IGNORECASE):
            error_lines.append(stripped)
    gate("solver_diagnostics", not error_lines,
         "none" if not error_lines else tuple(error_lines))

    all_complete = bool(iterations) and all(
        iteration.matrix is not None
        and not iteration.parse_failures
        and iteration.refined_panels is not None
        and iteration.mesh_refinement is not None
        and bool(iteration.preconditioners)
        for iteration in iterations
    )
    gate("complete_iterations", all_complete,
         tuple({
             "number": item.number,
             "parse_failures": item.parse_failures,
             "matrix": item.matrix is not None,
             "refined_panels": item.refined_panels,
             "mesh_refinement": item.mesh_refinement,
             "preconditioners": item.preconditioners,
         } for item in iterations))
    last_position_complete = bool(
        final and final.matrix_end_line is not None
        and (not header_positions or final.number == iterations[-1].number)
    )
    gate("final_iteration_complete", last_position_complete,
         "last iteration by position has a complete matrix")
    labels_ok = bool(iterations) and all(
        iteration.solver_labels == expected_labels for iteration in iterations
    )
    gate("solver_label_identity", labels_ok,
         tuple(iteration.solver_labels for iteration in iterations))

    rhs_ok = bool(iterations) and all(
        len(iteration.rhs_outcomes) == len(expected_labels)
        and all(outcome.converged for outcome in iteration.rhs_outcomes)
        for iteration in iterations
    )
    gate("gmres_rhs_outcomes", rhs_ok,
         tuple((item.number, tuple(asdict(outcome)
                                  for outcome in item.rhs_outcomes))
               for item in iterations))
    gate("effective_gmres_tolerance",
         effective_gmres is not None and effective_gmres > 0.0,
         effective_gmres)

    if config.mode == "automatic":
        mode_ok = auto_declared is not None and np.isclose(
            auto_declared, config.relative_error, rtol=0.0, atol=1e-15
        )
        gate("automatic_mode_declaration", mode_ok,
             f"reported {auto_declared!r}; requested {config.relative_error!r}")
        norm_ok = bool(final and final.auto_norm is not None
                       and final.auto_norm <= config.relative_error)
        gate("automatic_refinement", norm_ok,
             f"final norm {None if final is None else final.auto_norm!r}; "
             f"target {config.relative_error!r}")
        terminal_after_norm = bool(
            final and final.norm_line is not None and terminal_positions
            and terminal_positions[-1] > final.norm_line
        )
    else:
        gate("manual_mode_declaration", auto_declared is None,
             f"automatic declaration {auto_declared!r}")
        mesh_ok = bool(reported_mesh is not None and np.isclose(
            reported_mesh, config.mesh_refinement, rtol=1e-12, atol=0.0
        ))
        gate("manual_mesh_setting", mesh_ok,
             f"reported {reported_mesh!r}; "
             f"requested {config.mesh_refinement!r}")
        tolerance_ok = bool(effective_gmres is not None and np.isclose(
            effective_gmres, config.gmres_tolerance, rtol=1e-12, atol=0.0
        ))
        gate("manual_gmres_setting", tolerance_ok,
             f"reported {effective_gmres!r}; requested {config.gmres_tolerance!r}")
        expected_preconditioner = {
            "none": "None",
            "jacobi": "Jacobi",
            "block": (
                "Block, block preconditioner dimension (-pb): "
                f"{config.preconditioner_dimension}"
            ),
            "two_level": (
                "Two-levels, two-levels preconditioner dimension (-ps): "
                f"{config.preconditioner_dimension}"
            ),
        }[config.manual_preconditioner]
        preconditioner_ok = bool(effective_preconditioners) and all(
            value == expected_preconditioner
            for value in effective_preconditioners
        )
        gate("manual_preconditioner_setting", preconditioner_ok,
             f"reported {effective_preconditioners!r}; "
             f"requested {expected_preconditioner!r}")
        terminal_after_norm = bool(
            final and final.matrix_end_line is not None and terminal_positions
            and terminal_positions[-1] > final.matrix_end_line
        )
    normal_termination = terminal_after_norm and not parse_failures
    gate("normal_termination", normal_termination,
         "terminal follows final convergence evidence, is exact, and is the "
         f"last meaningful stdout record; lines {terminal_positions!r}")

    matrix_ok = False
    displacement = None
    matrix_detail = "no final matrix"
    matrix_gates = {
        "raw": {},
        "downstream": {},
        "exact_branch_reconstruction": {"passed": False},
    }
    if final is not None and final.matrix is not None and labels_ok:
        try:
            raw = MaxwellMatrix(
                deck_manifest.conductor_names, final.matrix.values
            )
            matrix_gates["raw"] = raw_maxwell_gate_results(raw)
            downstream = symmetrize_maxwell(raw)
            displacement = float(np.max(np.abs(downstream.values - raw.values)))
            matrix_gates["downstream"] = maxwell_gate_results(
                downstream, rtol=0.0, atol=0.0
            )
            maxwell_to_branches(downstream)
            matrix_gates["exact_branch_reconstruction"] = {"passed": True}
            matrix_ok = True
            matrix_detail = "raw gates, strict downstream gates, exact branches pass"
        except ValueError as error:
            matrix_detail = str(error)
    gate("matrix_validation", matrix_ok, matrix_detail)

    resources = {
        "elapsed_s": execution.elapsed_s,
        "peak_rss_bytes": execution.peak_rss_bytes,
        "output_bytes": execution.output_bytes,
        "directory_growth_bytes": execution.directory_growth_bytes,
        "max_refined_panels": execution.max_refined_panels,
        "max_gmres_iteration": execution.max_gmres_iteration,
        "limits": (
            RESOURCE_LIMITS[config.resource_class]
            if execution.limits is None else asdict(execution.limits)
        ),
    }
    return FasterCapTranscript(
        solver_version=version_match.group(1) if version_match else None,
        mode=config.mode,
        effective_gmres_tolerance=effective_gmres,
        effective_mesh_refinement=reported_mesh,
        effective_preconditioners=effective_preconditioners,
        initial_panels=initial_panels,
        iterations=iterations,
        normal_termination=normal_termination,
        solver_runtime_s=solver_runtime,
        averaging_max_displacement_f=displacement,
        matrix_gate_results=matrix_gates,
        failures=tuple(dict.fromkeys(failures)),
        gate_results=gates,
        resource_observations=resources,
    )
