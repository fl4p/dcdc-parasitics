#!/usr/bin/env python3
"""Runtime, quarantine, and native-milestone checks for Palace runs."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile

import numpy as np

if __package__:
    from .palace_build import palace_build_source_commit
    from .provenance import bytes_sha256
else:
    from palace_build import palace_build_source_commit
    from provenance import bytes_sha256


def _write_all(descriptor, content):
    offset = 0
    while offset < len(content):
        offset += os.write(descriptor, content[offset:])


def _append_capture(captured, evidence_fd, *, append_separator):
    metadata = os.lstat(captured)
    if append_separator:
        _write_all(evidence_fd, b"\n--PALACE-RECREATED-MATRIX--\n")
    if stat.S_ISLNK(metadata.st_mode):
        _write_all(evidence_fd, os.readlink(captured).encode())
        captured.unlink()
        return
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(captured, os.O_RDONLY | nofollow)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("Palace matrix quarantine source is not regular")
        captured.unlink()
        while chunk := os.read(descriptor, 1024 * 1024):
            _write_all(evidence_fd, chunk)
    finally:
        os.close(descriptor)


def _publish_evidence(quarantine, source_fd, path_name):
    os.lseek(source_fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(source_fd, 1024 * 1024):
        digest.update(chunk)
    content_sha256 = digest.hexdigest()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for _ in range(8):
        target = quarantine / (
            f"{path_name}.{content_sha256}.{os.urandom(16).hex()}.quarantined"
        )
        try:
            target_fd = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o400,
            )
        except FileExistsError:
            continue
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while chunk := os.read(source_fd, 1024 * 1024):
                _write_all(target_fd, chunk)
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
        return target
    raise ValueError("Palace matrix quarantine destination remained occupied")


class QuarantineCleanupError(RuntimeError):
    def __init__(self, quarantine):
        super().__init__("Palace matrix quarantine cleanup failed")
        self.quarantine = Path(quarantine)


def quarantine_matrix_files(output_directory, paths):
    output_directory = Path(output_directory)
    quarantine = Path(tempfile.mkdtemp(
        prefix=".quarantine.", dir=output_directory,
    ))
    quarantine.chmod(0o700)
    try:
        return _quarantine_into(output_directory, paths, quarantine)
    except Exception:
        try:
            shutil.rmtree(quarantine)
            for path in map(Path, paths):
                try:
                    path.rmdir()
                except (FileNotFoundError, NotADirectoryError, OSError):
                    pass
        except OSError as cleanup_error:
            raise QuarantineCleanupError(quarantine) from cleanup_error
        raise


def _quarantine_into(output_directory, paths, quarantine):
    result = []
    for ordinal, path in enumerate(paths):
        path = Path(path)
        evidence_fd, evidence_name = tempfile.mkstemp(
            prefix=f".evidence.{ordinal}.", dir=quarantine,
        )
        evidence = Path(evidence_name)
        captures = 0
        try:
            while True:
                captured = quarantine / (
                    f".captured.{ordinal}.{os.urandom(16).hex()}"
                )
                try:
                    os.rename(path, captured)
                except FileNotFoundError:
                    try:
                        path.mkdir(mode=0o500)
                    except FileExistsError:
                        continue
                    path.chmod(0o500)
                    break
                captures += 1
                if captures > 8:
                    raise ValueError("Palace matrix quarantine source kept reappearing")
                try:
                    _append_capture(
                        captured, evidence_fd, append_separator=captures > 1,
                    )
                finally:
                    try:
                        captured.unlink()
                    except FileNotFoundError:
                        pass
            if captures == 0:
                result.append(path)
            else:
                os.fsync(evidence_fd)
                result.append(_publish_evidence(
                    quarantine, evidence_fd, path.name,
                ))
        finally:
            os.close(evidence_fd)
            try:
                evidence.unlink()
            except FileNotFoundError:
                pass
    for directory in (quarantine, output_directory):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return tuple(result)


def _freeze_surviving_evidence(quarantine):
    quarantine = Path(quarantine)
    if quarantine.is_symlink() or not quarantine.is_dir():
        raise RuntimeError("Palace surviving quarantine root is unsafe")
    evidence = []
    for path in quarantine.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("Palace surviving quarantine evidence is unsafe")
        if stat.S_ISDIR(metadata.st_mode):
            path.chmod(0o500)
        elif stat.S_ISREG(metadata.st_mode):
            path.chmod(0o400)
            evidence.append(path)
        else:
            raise RuntimeError("Palace surviving quarantine evidence is unsafe")
    quarantine.chmod(0o500)
    return tuple(evidence)


def quarantine_or_purge_matrix_files(output_directory, paths):
    survivors = ()
    try:
        return quarantine_matrix_files(output_directory, paths), None
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, QuarantineCleanupError):
            survivors = _freeze_surviving_evidence(error.quarantine)
        try:
            purge_matrix_files(paths)
        except (OSError, ValueError) as purge_error:
            raise RuntimeError(
                "Palace matrix quarantine and purge both failed"
            ) from purge_error
        return (*map(Path, paths), *survivors), str(error)


def purge_matrix_files(paths):
    for path in map(Path, paths):
        removals = 0
        while True:
            try:
                path.unlink()
            except FileNotFoundError:
                try:
                    path.mkdir(mode=0o500)
                except FileExistsError:
                    continue
                path.chmod(0o500)
                break
            except (IsADirectoryError, PermissionError):
                metadata = path.stat(follow_symlinks=False)
                if (stat.S_ISDIR(metadata.st_mode)
                        and stat.S_IMODE(metadata.st_mode) == 0o500
                        and not any(path.iterdir())):
                    break
                raise ValueError("Palace matrix purge name is occupied")
            removals += 1
            if removals > 16:
                raise ValueError("Palace matrix purge source kept reappearing")


def validate_runtime_metadata_schema(metadata):
    expected_top_level = {
        "ElapsedTime", "GitTag", "LinearSolver", "PeakMemoryGrowthMegabytes",
        "PeakMemoryMegabytes", "PeakNodeMemoryGrowthMegabytes",
        "PeakNodeMemoryMegabytes", "Problem",
    }
    if set(metadata) != expected_top_level:
        raise ValueError("Palace runtime metadata schema mismatch")
    elapsed = metadata.get("ElapsedTime")
    if not isinstance(elapsed, dict) or set(elapsed) != {"Counts", "Durations"}:
        raise ValueError("Palace runtime elapsed-time metadata is invalid")
    counts = elapsed["Counts"]
    durations = elapsed["Durations"]
    if (not isinstance(counts, dict) or not counts
            or set(counts) != set(durations)
            or any(type(value) is not int or value < 0 for value in counts.values())
            or any(type(value) is not float
                   or not np.isfinite(value) or value < 0.0
                   for value in durations.values())):
        raise ValueError("Palace runtime timing metadata is invalid")
    linear = metadata.get("LinearSolver")
    if not isinstance(linear, dict) or set(linear) != {"TotalIts", "TotalSolves"}:
        raise ValueError("Palace runtime linear-solver metadata is invalid")
    problem = metadata.get("Problem")
    if (not isinstance(problem, dict) or set(problem) != {
            "DegreesOfFreedom", "MPISize", "MeshElements",
            "MultigridDegreesOfFreedom"}):
        raise ValueError("Palace runtime problem metadata is invalid")
    multigrid = problem["MultigridDegreesOfFreedom"]
    if (not isinstance(multigrid, list) or not multigrid
            or any(type(value) is not int or value <= 0 for value in multigrid)):
        raise ValueError("Palace runtime multigrid metadata is invalid")
    for name in ("PeakMemoryGrowthMegabytes", "PeakNodeMemoryGrowthMegabytes"):
        value = metadata.get(name)
        if (not isinstance(value, dict) or set(value) != {"Max", "Min", "Sum"}
                or any(not isinstance(group, dict) or set(group) != set(counts)
                       for group in value.values())
                or any(type(number) is not float
                       or not np.isfinite(number) or number < 0.0
                       for group in value.values() for number in group.values())):
            raise ValueError(f"Palace runtime {name} is invalid")
    for name in ("PeakMemoryMegabytes", "PeakNodeMemoryMegabytes"):
        value = metadata.get(name)
        if (not isinstance(value, dict)
                or set(value) != {"Average", "Max", "Min", "Total"}
                or any(type(number) is not float
                       or not np.isfinite(number) or number < 0.0
                       for number in value.values())):
            raise ValueError(f"Palace runtime {name} is invalid")
    for event, count in counts.items():
        duration = durations[event]
        if (count == 0 and duration != 0.0) or (count > 0 and duration <= 0.0):
            raise ValueError("Palace runtime counts and durations are inconsistent")
        for name in ("PeakMemoryGrowthMegabytes",
                     "PeakNodeMemoryGrowthMegabytes"):
            growth = metadata[name]
            if not (growth["Min"][event] <= growth["Max"][event]
                    <= growth["Sum"][event]):
                raise ValueError(f"Palace runtime {name} ordering is inconsistent")
    total_duration = durations.get("Total")
    if (type(total_duration) is not float or total_duration <= 0.0
            or any(value > total_duration for value in durations.values())):
        raise ValueError("Palace runtime durations are inconsistent")
    for name in ("PeakMemoryMegabytes", "PeakNodeMemoryMegabytes"):
        value = metadata[name]
        if not (value["Min"] <= value["Average"] <= value["Max"] <= value["Total"]):
            raise ValueError(f"Palace runtime {name} ordering is inconsistent")


def validate_execution_runtime_binding(execution, metadata):
    runtime_s = metadata["ElapsedTime"]["Durations"]["Total"]
    peak_memory_bytes = metadata["PeakMemoryMegabytes"]["Max"] * 1024**2
    if execution["elapsed_s"] < runtime_s:
        raise ValueError("Palace execution elapsed time contradicts runtime metadata")
    if execution["peak_rss_bytes"] < peak_memory_bytes:
        raise ValueError("Palace execution RSS contradicts runtime metadata")


def native_campaign_digest(manifest):
    if manifest.checkpoint is None:
        return "disabled"
    return bytes_sha256(manifest.checkpoint["native_campaign_identity"].encode())


def validate_runtime_build_identity(metadata, build):
    if palace_build_source_commit(build)[:8] not in metadata["GitTag"]:
        raise ValueError("Palace runtime Git/build identity mismatch")


def validate_runtime_progress_accounting(metadata, progress):
    linear = metadata["LinearSolver"]
    counts = metadata["ElapsedTime"]["Counts"]
    if (linear["TotalSolves"] != progress["new_solve_count"]
            or counts["LinearSolve"] != progress["new_solve_count"]
            or linear["TotalIts"] != progress["total_iterations"]):
        raise ValueError("Palace runtime metadata differs from native milestones")
