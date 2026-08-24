#!/usr/bin/env python3
"""Build attestation bound to an authorized Palace source identity."""
from pathlib import Path
import platform
import shutil

try:
    from .palace_build import _binary_paths, _linked_library_identity
    from .palace_source_identity import (
        _regular_bytes, _strict_manifest, _validate_directory,
    )
    from .palace_source_policy import (
        authorized_palace_build, validate_authorized_palace_source_identity,
    )
    from .provenance import (
        bytes_sha256, canonical_sha256, exclusive_publish_json,
    )
except ImportError:
    from palace_build import _binary_paths, _linked_library_identity
    from palace_source_identity import (
        _regular_bytes, _strict_manifest, _validate_directory,
    )
    from palace_source_policy import (
        authorized_palace_build, validate_authorized_palace_source_identity,
    )
    from provenance import (
        bytes_sha256, canonical_sha256, exclusive_publish_json,
    )


BUILD_MANIFEST_FORMAT = "dcdc-palace-build-v2"


def _file_record(path, *, label):
    path = Path(path).absolute()
    content = _regular_bytes(path, label=label)
    return {"path": str(path), "sha256": bytes_sha256(content), "bytes": len(content)}


def _validate_file_record(record, *, label):
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
        raise ValueError(f"Palace build-v2 {label} record is malformed")
    if (not isinstance(record["path"], str)
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
            or not isinstance(record["bytes"], int)
            or isinstance(record["bytes"], bool) or record["bytes"] < 0):
        raise ValueError(f"Palace build-v2 {label} record is malformed")
    content = _regular_bytes(record["path"], label=label)
    if len(content) != record["bytes"] or bytes_sha256(content) != record["sha256"]:
        raise ValueError(f"Palace build-v2 {label} changed")


def write_palace_build_manifest_v2(
        output_directory, *, source_identity_path, build_directory, executable):
    source_identity_path = Path(source_identity_path).absolute()
    source_identity = validate_authorized_palace_source_identity(source_identity_path)
    policy = authorized_palace_build(source_identity["content_sha256"])
    build_directory = Path(build_directory).absolute()
    executable = Path(executable).absolute()
    if (str(build_directory) != policy["build_directory"]
            or str(executable) != policy["executable"]):
        raise ValueError("Palace build-v2 build root is not authorized")
    binaries = [_file_record(path, label="binary") for path in _binary_paths(executable)]
    if {record["path"]: record["sha256"] for record in binaries} != policy[
            "binaries"]:
        raise ValueError("Palace build-v2 binary roster is not authorized")
    native = next(
        Path(record["path"]) for record in binaries
        if Path(record["path"]).name.startswith("palace-")
    )
    caches = [
        _file_record(path, label="CMake cache")
        for path in sorted(build_directory.rglob("CMakeCache.txt"))
    ]
    if not caches:
        raise ValueError("Palace build-v2 has no CMake cache")
    launcher = shutil.which("mpirun")
    if launcher is None:
        raise ValueError("Palace MPI launcher is unavailable")
    launcher = Path(launcher).resolve()
    if (str(launcher) != policy["mpi_launcher"]
            or bytes_sha256(_regular_bytes(launcher, label="MPI launcher"))
            != policy["mpi_launcher_sha256"]):
        raise ValueError("Palace build-v2 MPI launcher is not authorized")
    identity = {
        "format": BUILD_MANIFEST_FORMAT,
        "source_identity": {
            "path": str(source_identity_path),
            "content_sha256": source_identity["content_sha256"],
        },
        "build_directory": str(build_directory),
        "cmake_caches": caches,
        "binaries": binaries,
        "linked_libraries": _linked_library_identity(native, build_directory),
        "mpi_launcher": _file_record(
            launcher, label="MPI launcher",
        ),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    manifest = {"identity": identity, "content_sha256": canonical_sha256(identity)}
    output_directory = Path(output_directory).absolute()
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / f"palace-build-v2.{manifest['content_sha256']}.json"
    exclusive_publish_json(path, manifest)
    return path


def validate_palace_build_manifest_v2(path, *, executable):
    path = Path(path).absolute()
    raw = _strict_manifest(path)
    if not isinstance(raw, dict) or set(raw) != {"identity", "content_sha256"}:
        raise ValueError("Palace build-v2 manifest schema mismatch")
    identity = raw["identity"]
    expected = {
        "format", "source_identity", "build_directory", "cmake_caches",
        "binaries", "linked_libraries", "mpi_launcher", "platform", "machine",
    }
    if (not isinstance(identity, dict) or set(identity) != expected
            or identity.get("format") != BUILD_MANIFEST_FORMAT
            or raw["content_sha256"] != canonical_sha256(identity)
            or path.name != f"palace-build-v2.{raw['content_sha256']}.json"):
        raise ValueError("Palace build-v2 identity mismatch")
    source = identity["source_identity"]
    if not isinstance(source, dict) or set(source) != {"path", "content_sha256"}:
        raise ValueError("Palace build-v2 source identity binding is malformed")
    validated_source = validate_authorized_palace_source_identity(source["path"])
    if source["content_sha256"] != validated_source["content_sha256"]:
        raise ValueError("Palace build-v2 source identity was substituted")
    policy = authorized_palace_build(validated_source["content_sha256"])
    build_directory = Path(identity["build_directory"])
    if not build_directory.is_absolute():
        raise ValueError("Palace build-v2 build directory is malformed")
    _validate_directory(build_directory, label="Palace build-v2 build directory")
    executable = str(Path(executable).absolute())
    if (str(build_directory) != policy["build_directory"]
            or executable != policy["executable"]):
        raise ValueError("Palace build-v2 build root is not authorized")
    if not isinstance(identity["cmake_caches"], list) or not identity["cmake_caches"]:
        raise ValueError("Palace build-v2 CMake cache roster is malformed")
    if not isinstance(identity["binaries"], list) or not identity["binaries"]:
        raise ValueError("Palace build-v2 binary roster is malformed")
    expected_caches = [
        _file_record(path, label="CMake cache")
        for path in sorted(build_directory.rglob("CMakeCache.txt"))
    ]
    if identity["cmake_caches"] != expected_caches:
        raise ValueError("Palace build-v2 CMake cache roster changed")
    expected_binaries = [
        _file_record(path, label="binary")
        for path in _binary_paths(executable)
    ]
    if (identity["binaries"] != expected_binaries
            or {record["path"]: record["sha256"] for record in expected_binaries}
            != policy["binaries"]):
        raise ValueError("Palace build-v2 binary roster is not authorized")
    launcher = shutil.which("mpirun")
    if launcher is None:
        raise ValueError("Palace MPI launcher is unavailable")
    expected_launcher = _file_record(Path(launcher).resolve(), label="MPI launcher")
    if (identity["mpi_launcher"] != expected_launcher
            or expected_launcher["path"] != policy["mpi_launcher"]
            or expected_launcher["sha256"] != policy["mpi_launcher_sha256"]):
        raise ValueError("Palace build-v2 MPI launcher is not authorized")
    linked = identity["linked_libraries"]
    if not isinstance(linked, dict) or set(linked) != {"reported", "rpaths", "resolved"}:
        raise ValueError("Palace build-v2 linked-library identity is malformed")
    native = next(
        (Path(record["path"]) for record in identity["binaries"]
         if Path(record["path"]).name.startswith("palace-")), None,
    )
    if native is None or linked != _linked_library_identity(native, build_directory):
        raise ValueError("Palace build-v2 linked-library identity changed")
    if identity["platform"] != platform.platform() or identity["machine"] != platform.machine():
        raise ValueError("Palace build-v2 host identity changed")
    if executable not in {record.get("path") for record in identity["binaries"]}:
        raise ValueError("Palace executable is not bound by build-v2")
    return raw
