#!/usr/bin/env python3
"""FasterCap input, execution, and result parsing helpers."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

import numpy as np
import psutil

if __package__:
    from .fastercap_diagnostics import (
        DECK_MANIFEST_FORMAT,
        GATE_POLICY,
        RESOURCE_LIMITS,
        RUN_MANIFEST_FORMAT,
        FasterCapManualDiagnostic,
        FasterCapRun,
        FasterCapRunConfig,
        FasterCapRunRejected,
        classify_fastercap_output as _classify_fastercap_output,
        validate_deck_manifest,
    )
    from .maxwell import MaxwellMatrix, symmetrize_maxwell
    from .process_monitor import ProcessLimits, run_monitored_process
else:
    from fastercap_diagnostics import (
        DECK_MANIFEST_FORMAT,
        GATE_POLICY,
        RESOURCE_LIMITS,
        RUN_MANIFEST_FORMAT,
        FasterCapManualDiagnostic,
        FasterCapRun,
        FasterCapRunConfig,
        FasterCapRunRejected,
        classify_fastercap_output as _classify_fastercap_output,
        validate_deck_manifest,
    )
    from maxwell import MaxwellMatrix, symmetrize_maxwell
    from process_monitor import ProcessLimits, run_monitored_process


@dataclass(frozen=True)
class ConductorSurface:
    name: str
    panels: tuple[tuple[tuple[float, float, float], ...], ...]
    relative_permittivity: float = 1.0
    parts: tuple[tuple[tuple[tuple[float, float, float], ...], ...], ...] | None = None

    def __post_init__(self):
        if not self.name or re.search(r"\s", self.name):
            raise ValueError("conductor name must be non-empty and contain no whitespace")
        permittivity = float(self.relative_permittivity)
        if not np.isfinite(permittivity) or permittivity <= 0.0:
            raise ValueError("relative permittivity must be finite and positive")
        panels = []
        for panel in self.panels:
            points = tuple(tuple(float(value) for value in point) for point in panel)
            if len(points) not in (3, 4) or any(len(point) != 3 for point in points):
                raise ValueError("each panel must contain three or four 3D points")
            if not np.isfinite(np.asarray(points)).all():
                raise ValueError("panel coordinates must be finite")
            vectors = tuple(np.asarray(point) for point in points)
            p0, p1, p2 = vectors[:3]
            normal = np.cross(p1 - p0, p2 - p0)
            normal_length = np.linalg.norm(normal)
            if normal_length == 0.0:
                raise ValueError("panel area must be non-zero")
            if len(vectors) == 4:
                p3 = vectors[3]
                scale = max(np.linalg.norm(point - p0) for point in vectors[1:])
                planarity_error = abs(float(np.dot(p3 - p0, normal))) / normal_length
                if planarity_error > 1e-9 * scale:
                    raise ValueError("quadrilateral panel must be planar")
                turns = [
                    float(np.dot(
                        np.cross(vectors[(index + 1) % 4] - vectors[index],
                                 vectors[(index + 2) % 4] - vectors[(index + 1) % 4]),
                        normal,
                    ))
                    for index in range(4)
                ]
                if any(turn <= 0.0 for turn in turns):
                    raise ValueError(
                        "quadrilateral panel corners must be distinct and ordered"
                    )
            panels.append(points)
        if not panels:
            raise ValueError("conductor surface must contain at least one panel")
        panels = tuple(panels)
        if self.parts is None:
            parts = (panels,)
        else:
            parts = tuple(tuple(part) for part in self.parts)
            if not parts or any(not part for part in parts):
                raise ValueError("conductor parts must be non-empty")
            if tuple(panel for part in parts for panel in part) != panels:
                raise ValueError(
                    "conductor parts must partition panels in their original order"
                )
        object.__setattr__(self, "panels", panels)
        object.__setattr__(self, "relative_permittivity", permittivity)
        object.__setattr__(self, "parts", parts)


@dataclass(frozen=True)
class DielectricSurface:
    name: str
    panels: tuple[tuple[tuple[float, float, float], ...], ...]
    outside_permittivity: float
    inside_permittivity: float
    reference_point: tuple[float, float, float]
    reference_is_inside: bool = True

    def __post_init__(self):
        validated = ConductorSurface(self.name, self.panels)
        outside = float(self.outside_permittivity)
        inside = float(self.inside_permittivity)
        reference = tuple(float(value) for value in self.reference_point)
        if (not np.isfinite((outside, inside)).all()
                or outside <= 0.0 or inside <= 0.0):
            raise ValueError("dielectric permittivities must be finite and positive")
        if len(reference) != 3 or not np.isfinite(reference).all():
            raise ValueError("dielectric reference point must be a finite 3D point")
        object.__setattr__(self, "panels", validated.panels)
        object.__setattr__(self, "outside_permittivity", outside)
        object.__setattr__(self, "inside_permittivity", inside)
        object.__setattr__(self, "reference_point", reference)


def box_surface(name, minimum, maximum, *, relative_permittivity=1.0):
    """Return a closed six-panel box conductor."""
    x0, y0, z0 = (float(value) for value in minimum)
    x1, y1, z1 = (float(value) for value in maximum)
    if not (x1 > x0 and y1 > y0 and z1 > z0):
        raise ValueError("box maximum must exceed minimum on every axis")
    p000 = (x0, y0, z0)
    p001 = (x0, y0, z1)
    p010 = (x0, y1, z0)
    p011 = (x0, y1, z1)
    p100 = (x1, y0, z0)
    p101 = (x1, y0, z1)
    p110 = (x1, y1, z0)
    p111 = (x1, y1, z1)
    panels = (
        (p000, p100, p110, p010),
        (p001, p011, p111, p101),
        (p000, p001, p101, p100),
        (p010, p110, p111, p011),
        (p000, p010, p011, p001),
        (p100, p101, p111, p110),
    )
    return ConductorSurface(name, panels, relative_permittivity)


def extruded_triangulation_surface(name, triangles, z_min, z_max, *,
                                   relative_permittivity=1.0):
    """Close a conforming 2-D triangle mesh into a finite-thickness conductor."""
    z_min = float(z_min)
    z_max = float(z_max)
    if not np.isfinite((z_min, z_max)).all() or z_max <= z_min:
        raise ValueError("extrusion z_max must exceed finite z_min")

    oriented = []
    edge_uses = {}
    for triangle in triangles:
        points = tuple(tuple(float(value) for value in point) for point in triangle)
        if len(points) != 3 or any(len(point) != 2 for point in points):
            raise ValueError("each mesh triangle must contain three 2D points")
        if not np.isfinite(np.asarray(points)).all():
            raise ValueError("mesh coordinates must be finite")
        p0, p1, p2 = points
        signed_area_twice = (
            (p1[0] - p0[0]) * (p2[1] - p0[1])
            - (p1[1] - p0[1]) * (p2[0] - p0[0])
        )
        if signed_area_twice == 0.0:
            raise ValueError("mesh triangle area must be non-zero")
        if signed_area_twice < 0.0:
            points = (p0, p2, p1)
        oriented.append(points)
        for point_a, point_b in zip(points, points[1:] + points[:1]):
            edge_key = tuple(sorted((point_a, point_b)))
            edge_uses.setdefault(edge_key, []).append((point_a, point_b))

    if not oriented:
        raise ValueError("triangle mesh must not be empty")
    for uses in edge_uses.values():
        if len(uses) > 2:
            raise ValueError("triangle mesh has a non-manifold edge")
        if len(uses) == 2 and uses[0] != tuple(reversed(uses[1])):
            raise ValueError("adjacent mesh triangles must share opposite edge directions")

    panels = []
    for triangle in oriented:
        panels.append(tuple((x, y, z_max) for x, y in triangle))
        panels.append(tuple((x, y, z_min) for x, y in reversed(triangle)))
    for uses in edge_uses.values():
        if len(uses) != 1:
            continue
        point_a, point_b = uses[0]
        panels.append((
            (point_a[0], point_a[1], z_min),
            (point_b[0], point_b[1], z_min),
            (point_b[0], point_b[1], z_max),
            (point_a[0], point_a[1], z_max),
        ))
    return ConductorSurface(name, tuple(panels), relative_permittivity)


def box_dielectric_surface(name, minimum, maximum, *, inside_permittivity,
                           outside_permittivity=1.0):
    """Return a closed dielectric box with its reference point inside."""
    minimum = tuple(float(value) for value in minimum)
    maximum = tuple(float(value) for value in maximum)
    if len(minimum) != 3 or len(maximum) != 3:
        raise ValueError("dielectric box bounds must be 3D points")
    conductor = box_surface(name, minimum, maximum)
    reference_point = (
        0.5 * (minimum[0] + maximum[0]),
        0.5 * (minimum[1] + maximum[1]),
        0.5 * (minimum[2] + maximum[2]),
    )
    return DielectricSurface(
        name,
        conductor.panels,
        outside_permittivity,
        inside_permittivity,
        reference_point,
        True,
    )


def _format_point(point):
    return " ".join(f"{coordinate:.17g}" for coordinate in point)


def _solver_panel_label(number):
    return f"dcp_c{number:04d}"


def _solver_output_label(number):
    return f"g{number}_{_solver_panel_label(number)}"


def _conductor_part_filenames(number, conductor):
    count = len(conductor.parts)
    if count == 1:
        return (f"conductor_{number}.pan",)
    return tuple(
        f"conductor_{number}_part_{index:04d}.pan"
        for index in range(1, count)
    ) + (f"conductor_{number}.pan",)


def _sha256_bytes(content):
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path):
    with open(path, "rb") as stream:
        return _sha256_bytes(stream.read())


def _atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _deck_manifest_path(path):
    path = Path(path)
    return path.with_name(f"{path.name}.manifest.json")


def render_fastercap_input(conductors, dielectrics=()):
    """Render a self-contained FasterCap 3D list file."""
    conductors = tuple(conductors)
    dielectrics = tuple(dielectrics)
    if not conductors:
        raise ValueError("at least one conductor is required")
    if len({conductor.name for conductor in conductors}) != len(conductors):
        raise ValueError("conductor names must be unique")

    lines = ["* generated by dcdc-parasitics"]
    for number, conductor in enumerate(conductors, 1):
        filenames = _conductor_part_filenames(number, conductor)
        for index, filename in enumerate(filenames):
            collate = " +" if index < len(filenames) - 1 else ""
            lines.append(
                f"C {filename} {conductor.relative_permittivity:.12g} "
                f"0 0 0{collate}"
            )
    for number, dielectric in enumerate(dielectrics, 1):
        reference = _format_point(dielectric.reference_point)
        inside_marker = " -" if dielectric.reference_is_inside else ""
        lines.append(
            f"D dielectric_{number}.pan "
            f"{dielectric.outside_permittivity:.12g} "
            f"{dielectric.inside_permittivity:.12g} 0 0 0 "
            f"{reference}{inside_marker}"
        )
    lines.append("End")
    for number, conductor in enumerate(conductors, 1):
        panel_label = _solver_panel_label(number)
        filenames = _conductor_part_filenames(number, conductor)
        for filename, part in zip(filenames, conductor.parts):
            lines.append(f"File {filename}")
            if filename == f"conductor_{number}.pan":
                lines.extend((
                    f"* conductor {conductor.name}",
                    f"* solver_output_label {_solver_output_label(number)}",
                ))
            for panel in part:
                kind = "T" if len(panel) == 3 else "Q"
                coordinates = " ".join(_format_point(point) for point in panel)
                lines.append(f"{kind} {panel_label} {coordinates}")
            lines.append("End")
    for number, dielectric in enumerate(dielectrics, 1):
        lines.extend((
            f"File dielectric_{number}.pan",
            f"* dielectric {dielectric.name}",
        ))
        for panel in dielectric.panels:
            kind = "T" if len(panel) == 3 else "Q"
            coordinates = " ".join(_format_point(point) for point in panel)
            lines.append(f"{kind} {dielectric.name} {coordinates}")
        lines.append("End")
    return "\n".join(lines) + "\n"


def write_fastercap_input(path, conductors, dielectrics=(), *, provenance=None):
    path = Path(path)
    conductors = tuple(conductors)
    dielectrics = tuple(dielectrics)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_fastercap_input(conductors, dielectrics)
    path.write_text(content)
    manifest = {
        "format": DECK_MANIFEST_FORMAT,
        "deck_sha256": _sha256_bytes(content.encode()),
        "conductors": [
            {
                "index": number,
                "name": conductor.name,
                "panel_label": _solver_panel_label(number),
                "solver_output_label": _solver_output_label(number),
            }
            for number, conductor in enumerate(conductors, 1)
        ],
    }
    if provenance is not None:
        if not isinstance(provenance, dict):
            raise TypeError("deck provenance must be a dictionary")
        canonical = json.dumps(
            provenance, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        manifest["provenance"] = provenance
        manifest["provenance_sha256"] = _sha256_bytes(canonical)
    _atomic_write_json(_deck_manifest_path(path), manifest)
    return path


def parse_fastercap_output(*args, **kwargs):
    raise RuntimeError(
        "direct matrix parsing is diagnostic-only; use classify_fastercap_output"
    )


def classify_fastercap_output(stdout, stderr, *, deck_manifest, config, execution):
    return _classify_fastercap_output(
        stdout,
        stderr,
        deck_manifest=deck_manifest,
        config=config,
        execution=execution,
    )


def _write_content_addressed_blob(input_path, kind, content, *, suffix="bin"):
    encoded = content.encode() if isinstance(content, str) else bytes(content)
    digest = _sha256_bytes(encoded)
    path = input_path.with_name(
        f"{input_path.name}.{kind}.{digest}.{suffix}"
    )
    if path.exists() and _sha256_file(path) == digest:
        return path, digest
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{id(encoded)}.tmp"
    )
    try:
        with open(temporary, "xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if _sha256_file(path) != digest:
                    os.replace(temporary, path)
        if _sha256_file(path) != digest:
            raise RuntimeError(f"content-addressed blob verification failed: {path}")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path, digest


def _manifest_matrix(iteration):
    values = None if iteration.matrix is None else iteration.matrix.values.tolist()
    matrix_hash = None
    if iteration.matrix is not None:
        matrix_hash = _sha256_bytes(iteration.matrix.values.tobytes())
    return {
        "iteration": iteration.number,
        "solver_labels": iteration.solver_labels,
        "values": values,
        "sha256": matrix_hash,
        "auto_norm": iteration.auto_norm,
        "refined_panels": iteration.refined_panels,
        "mesh_refinement": iteration.mesh_refinement,
        "preconditioners": iteration.preconditioners,
        "rhs_outcomes": [asdict(outcome) for outcome in iteration.rhs_outcomes],
        "parse_failures": iteration.parse_failures,
    }


def _write_run_manifest(input_path, *, state, solver_command,
                        resolved_executable, execution, config,
                        deck_manifest=None, transcript=None, matrix=None,
                        launch_error=None):
    stdout = b"" if execution is None else execution.stdout
    stderr = b"" if execution is None else execution.stderr
    stdout_path, stdout_hash = _write_content_addressed_blob(
        input_path, "stdout", stdout
    )
    stderr_path, stderr_hash = _write_content_addressed_blob(
        input_path, "stderr", stderr
    )
    matrix_path = None
    matrix_hash = None
    if matrix is not None:
        matrix_content = json.dumps({
            "names": matrix.names,
            "values": matrix.values.tolist(),
        }, sort_keys=True, separators=(",", ":")).encode()
        matrix_path, matrix_hash = _write_content_addressed_blob(
            input_path, "matrix", matrix_content, suffix="json"
        )
    parser_path = Path(_classify_fastercap_output.__code__.co_filename).resolve()
    monitor_path = Path(run_monitored_process.__code__.co_filename).resolve()
    windows_job_path = monitor_path.with_name("windows_job_runner.py")
    maxwell_path = Path(symmetrize_maxwell.__code__.co_filename).resolve()
    manifest = {
        "format": RUN_MANIFEST_FORMAT,
        "gate_policy": GATE_POLICY,
        "lifecycle_state": state,
        "diagnostic_kind": "manual_solver_probe" if config.mode == "manual" else None,
        "solver_command": list(solver_command),
        "executed_command": None if execution is None else list(execution.command),
        "cwd": str(input_path.parent if execution is None else execution.cwd),
        "platform": platform.platform(),
        "environment": {
            "python_executable": sys.executable,
            "python_version": sys.version,
            "LANG": os.environ.get("LANG"),
            "LC_ALL": os.environ.get("LC_ALL"),
        },
        "dependencies": {
            "numpy": np.__version__,
            "psutil": psutil.__version__,
        },
        "requested_settings": asdict(config),
        "returncode": None if execution is None else execution.returncode,
        "launch_error": launch_error,
        "deck_sha256": _sha256_file(input_path) if input_path.is_file() else None,
        "deck_manifest_sha256": (
            _sha256_file(_deck_manifest_path(input_path))
            if _deck_manifest_path(input_path).is_file() else None
        ),
        "deck_manifest_validated": deck_manifest is not None,
        "deck_manifest": None if deck_manifest is None else deck_manifest.raw,
        "stdout": str(stdout_path),
        "stdout_sha256": stdout_hash,
        "stderr": str(stderr_path),
        "stderr_sha256": stderr_hash,
        "library_sha256": _sha256_file(__file__),
        "parser_sha256": _sha256_file(parser_path),
        "monitor_sha256": _sha256_file(monitor_path),
        "windows_job_runner_sha256": _sha256_file(windows_job_path),
        "maxwell_sha256": _sha256_file(maxwell_path),
        "solver_version": None if transcript is None else transcript.solver_version,
        "solver_binary": resolved_executable,
        "solver_binary_sha256": (
            _sha256_file(resolved_executable)
            if resolved_executable and Path(resolved_executable).is_file() else None
        ),
        "effective_settings": None if transcript is None else {
            "gmres_tolerance": transcript.effective_gmres_tolerance,
            "mesh_refinement": transcript.effective_mesh_refinement,
            "preconditioners": transcript.effective_preconditioners,
        },
        "solver_runtime_s": None if transcript is None else transcript.solver_runtime_s,
        "resource_observations": (
            None if transcript is None else transcript.resource_observations
        ),
        "gate_results": None if transcript is None else transcript.gate_results,
        "failures": (
            (launch_error,) if transcript is None else transcript.failures
        ),
        "averaging_max_displacement_f": (
            None if transcript is None else transcript.averaging_max_displacement_f
        ),
        "matrix_gate_results": (
            None if transcript is None else transcript.matrix_gate_results
        ),
        "uncertainty_f": None,
        "diagnostic_matrices": [] if transcript is None else [
            _manifest_matrix(iteration) for iteration in transcript.iterations
        ],
        "matrix": None if matrix_path is None else str(matrix_path),
        "matrix_sha256": matrix_hash,
    }
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    content_id = _sha256_bytes(canonical)
    manifest["content_id_sha256"] = content_id
    path = input_path.with_name(
        f"{input_path.name}.run.{state}.{content_id}.json"
    )
    _atomic_write_json(path, manifest)
    return path


def _resolve_executable(executable):
    executable = str(executable)
    if os.path.dirname(executable):
        path = Path(executable).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"FasterCap executable does not exist: {path}")
        return str(path)
    resolved = shutil.which(executable)
    if resolved is None:
        raise ValueError(f"FasterCap executable is not on PATH: {executable}")
    return str(Path(resolved).resolve())


def _run_fastercap_config(input_path, *, config, conductor_names=None,
                          executable=None, timeout=None):
    input_path = Path(input_path).resolve()
    executable_request = executable or os.environ.get("FASTERCAP", "FasterCap")
    executable_prefix = (
        [str(value) for value in executable_request]
        if isinstance(executable_request, (tuple, list))
        else [str(executable_request)]
    )
    if not executable_prefix:
        raise ValueError("FasterCap executable command must not be empty")
    solver_command = [*executable_prefix, input_path.name]
    if config.mode == "automatic":
        solver_command.append(f"-a{config.relative_error:.12g}")
        if config.auto_precondition:
            solver_command.append("-ap")
    else:
        solver_command.extend((
            f"-m{config.mesh_refinement:.12g}",
            f"-t{config.gmres_tolerance:.12g}",
        ))
        preconditioner_flags = {
            "none": "-pn",
            "jacobi": "-pj",
            "block": f"-pb{config.preconditioner_dimension}",
            "two_level": f"-ps{config.preconditioner_dimension}",
        }
        solver_command.append(
            preconditioner_flags[config.manual_preconditioner]
        )
    solver_command.append("-b")

    deck_manifest = None
    resolved_executable = None
    try:
        limit_values = dict(RESOURCE_LIMITS[config.resource_class])
        if timeout is not None:
            requested_timeout = float(timeout)
            if not np.isfinite(requested_timeout) or requested_timeout <= 0.0:
                raise ValueError("timeout must be finite and positive")
            limit_values["wall_time_s"] = min(
                requested_timeout, limit_values["wall_time_s"]
            )
        limits = ProcessLimits(**limit_values)
        if not input_path.is_file():
            raise ValueError(f"FasterCap deck does not exist: {input_path}")
        deck_manifest = validate_deck_manifest(
            _deck_manifest_path(input_path),
            deck_sha256=_sha256_file(input_path),
        )
        names = (
            deck_manifest.conductor_names
            if conductor_names is None else tuple(conductor_names)
        )
        if names != deck_manifest.conductor_names:
            raise ValueError(
                "conductor names do not match deck manifest: "
                f"expected {deck_manifest.conductor_names!r}, got {names!r}"
            )
        resolved_executable = _resolve_executable(executable_prefix[0])
        solver_command[0] = resolved_executable
    except (OSError, OverflowError, TypeError, ValueError) as error:
        manifest_path = _write_run_manifest(
            input_path,
            state="rejected_diagnostic",
            solver_command=solver_command,
            resolved_executable=resolved_executable,
            execution=None,
            config=config,
            deck_manifest=deck_manifest,
            launch_error=str(error),
        )
        raise FasterCapRunRejected(
            f"FasterCap run rejected before launch: {error}",
            transcript=None,
            manifest_path=manifest_path,
        ) from error

    try:
        execution = run_monitored_process(
            solver_command,
            cwd=input_path.parent,
            limits=limits,
            environment=os.environ.copy(),
        )
    except (OSError, psutil.Error, subprocess.SubprocessError) as error:
        manifest_path = _write_run_manifest(
            input_path,
            state="rejected_diagnostic",
            solver_command=solver_command,
            resolved_executable=resolved_executable,
            execution=None,
            config=config,
            deck_manifest=deck_manifest,
            launch_error=str(error),
        )
        raise FasterCapRunRejected(
            f"FasterCap launch failed: {error}",
            transcript=None,
            manifest_path=manifest_path,
        ) from error

    try:
        transcript = classify_fastercap_output(
            execution.stdout,
            execution.stderr,
            deck_manifest=deck_manifest,
            config=config,
            execution=execution,
        )
    except Exception as error:
        manifest_path = _write_run_manifest(
            input_path,
            state="rejected_diagnostic",
            solver_command=solver_command,
            resolved_executable=resolved_executable,
            execution=execution,
            config=config,
            deck_manifest=deck_manifest,
            launch_error=f"transcript classification failed: {error}",
        )
        raise FasterCapRunRejected(
            f"FasterCap transcript classification failed: {error}",
            transcript=None,
            manifest_path=manifest_path,
        ) from error

    stdout = execution.stdout.decode("utf-8", errors="replace")
    stderr = execution.stderr.decode("utf-8", errors="replace")
    if config.mode == "manual":
        manifest_path = _write_run_manifest(
            input_path,
            state="rejected_diagnostic",
            solver_command=solver_command,
            resolved_executable=resolved_executable,
            execution=execution,
            config=config,
            deck_manifest=deck_manifest,
            transcript=transcript,
        )
        return FasterCapManualDiagnostic(
            tuple(solver_command), stdout, stderr, transcript, manifest_path
        )

    if transcript.failures:
        manifest_path = _write_run_manifest(
            input_path,
            state="rejected_diagnostic",
            solver_command=solver_command,
            resolved_executable=resolved_executable,
            execution=execution,
            config=config,
            deck_manifest=deck_manifest,
            transcript=transcript,
        )
        raise FasterCapRunRejected(
            "FasterCap run rejected: " + "; ".join(transcript.failures),
            transcript=transcript,
            manifest_path=manifest_path,
        )

    try:
        raw_matrix = MaxwellMatrix(
            deck_manifest.conductor_names,
            transcript.iterations[-1].matrix.values,
        )
        matrix = symmetrize_maxwell(raw_matrix)
    except Exception as error:
        manifest_path = _write_run_manifest(
            input_path,
            state="rejected_diagnostic",
            solver_command=solver_command,
            resolved_executable=resolved_executable,
            execution=execution,
            config=config,
            deck_manifest=deck_manifest,
            transcript=transcript,
            launch_error=f"fixed-policy matrix finalization failed: {error}",
        )
        raise FasterCapRunRejected(
            f"FasterCap fixed-policy matrix finalization failed: {error}",
            transcript=transcript,
            manifest_path=manifest_path,
        ) from error
    manifest_path = _write_run_manifest(
        input_path,
        state="numerically_converged_diagnostic",
        solver_command=solver_command,
        resolved_executable=resolved_executable,
        execution=execution,
        config=config,
        deck_manifest=deck_manifest,
        transcript=transcript,
        matrix=matrix,
    )
    return FasterCapRun(
        tuple(solver_command), stdout, stderr, matrix, transcript, manifest_path
    )


def run_fastercap(input_path, *, conductor_names=None, executable=None,
                  relative_error=0.01, auto_precondition=True,
                  timeout=None, resource_class="synthetic"):
    config = FasterCapRunConfig(
        mode="automatic",
        resource_class=resource_class,
        relative_error=relative_error,
        auto_precondition=auto_precondition,
    )
    return _run_fastercap_config(
        input_path,
        config=config,
        conductor_names=conductor_names,
        executable=executable,
        timeout=timeout,
    )


def run_fastercap_manual(input_path, *, mesh_refinement, gmres_tolerance,
                         preconditioner="jacobi", preconditioner_dimension=None,
                         conductor_names=None, executable=None, timeout=None,
                         resource_class="synthetic"):
    config = FasterCapRunConfig(
        mode="manual",
        resource_class=resource_class,
        mesh_refinement=mesh_refinement,
        gmres_tolerance=gmres_tolerance,
        auto_precondition=False,
        manual_preconditioner=preconditioner,
        preconditioner_dimension=preconditioner_dimension,
    )
    return _run_fastercap_config(
        input_path,
        config=config,
        conductor_names=conductor_names,
        executable=executable,
        timeout=timeout,
    )
