#!/usr/bin/env python3
"""Content-addressed provenance for a local Palace qualification build."""
import json
from pathlib import Path
import platform
import shutil
import subprocess

try:
    from .provenance import bytes_sha256, canonical_sha256, file_sha256
except ImportError:
    from provenance import bytes_sha256, canonical_sha256, file_sha256


BUILD_MANIFEST_FORMAT = "dcdc-palace-build-v1"


def _command_bytes(command):
    result = subprocess.run(command, check=True, capture_output=True)
    return result.stdout


def _git_bytes(source_directory, *args):
    return _command_bytes(("git", "-C", str(source_directory), *args))


def _binary_paths(executable):
    executable = Path(executable).resolve()
    paths = [executable]
    paths.extend(sorted(path.resolve() for path in executable.parent.glob("palace-*.bin")))
    return tuple(dict.fromkeys(paths))


def _linked_library_identity(binary, build_directory):
    if platform.system() != "Darwin":
        return {"reported": [], "resolved": {}}
    lines = _command_bytes(("otool", "-L", str(binary))).decode().splitlines()[1:]
    reported = [line.strip().split(" ", 1)[0] for line in lines if line.strip()]
    load_commands = _command_bytes(("otool", "-l", str(binary))).decode().splitlines()
    rpaths = []
    for index, line in enumerate(load_commands):
        if line.strip() == "cmd LC_RPATH" and index + 2 < len(load_commands):
            value = load_commands[index + 2].strip()
            if value.startswith("path "):
                rpaths.append(value[5:].split(" (offset ", 1)[0])
    resolved = {}
    for name in reported:
        path = Path(name)
        candidate = None
        if path.is_absolute() and path.is_file():
            candidate = path.resolve()
        elif name.startswith("@rpath/"):
            suffix = name.removeprefix("@rpath/")
            for rpath in rpaths:
                expanded = rpath.replace("@loader_path", str(Path(binary).parent))
                attempt = Path(expanded) / suffix
                if attempt.is_file():
                    candidate = attempt.resolve()
                    break
        if candidate is not None:
            resolved[name] = {
                "path": str(candidate),
                "sha256": file_sha256(candidate),
            }
    unresolved = [
        name for name in reported
        if name not in resolved
        and not name.startswith(("/usr/lib/", "/System/Library/"))
    ]
    if unresolved:
        raise ValueError(f"unresolved Palace runtime libraries: {unresolved}")
    return {"reported": reported, "rpaths": rpaths, "resolved": resolved}


def write_palace_build_manifest(output_directory, *, source_directory, build_directory,
                                executable):
    output_directory = Path(output_directory).resolve()
    source_directory = Path(source_directory).resolve()
    build_directory = Path(build_directory).resolve()
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise ValueError("Palace executable does not exist")
    untracked = _git_bytes(
        source_directory, "ls-files", "--others", "--exclude-standard"
    ).decode().splitlines()
    if untracked:
        raise ValueError("Palace source contains untracked files")
    commit = _git_bytes(source_directory, "rev-parse", "HEAD").decode().strip()
    patch = _git_bytes(source_directory, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    patch_sha256 = bytes_sha256(patch)
    output_directory.mkdir(parents=True, exist_ok=True)
    patch_path = output_directory / f"palace-qualification.{patch_sha256}.patch"
    patch_path.write_bytes(patch)
    changed_files = _git_bytes(
        source_directory, "diff", "--name-only", "HEAD", "--"
    ).decode().splitlines()
    source_files = {
        name: file_sha256(source_directory / name)
        for name in changed_files
        if (source_directory / name).is_file()
    }
    cache_paths = sorted(build_directory.rglob("CMakeCache.txt"))
    if not cache_paths:
        raise ValueError("Palace build has no CMake cache")
    caches = {str(path): file_sha256(path) for path in cache_paths}
    binaries = {str(path): file_sha256(path) for path in _binary_paths(executable)}
    launcher = shutil.which("mpirun")
    if not launcher:
        raise ValueError("Palace MPI launcher is unavailable")
    binary = next(
        path for path in _binary_paths(executable) if path.name.startswith("palace-")
    )
    provenance = {
        "source_directory": str(source_directory),
        "source_commit": commit,
        "source_patch": str(patch_path),
        "source_patch_sha256": patch_sha256,
        "source_files": source_files,
        "submodules": _git_bytes(
            source_directory, "submodule", "status", "--recursive"
        ).decode().splitlines(),
        "build_directory": str(build_directory),
        "cmake_caches": caches,
        "binaries": binaries,
        "linked_libraries": _linked_library_identity(binary, build_directory),
        "mpi_launcher": str(Path(launcher).resolve()),
        "mpi_launcher_sha256": file_sha256(launcher),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    manifest = {
        "format": BUILD_MANIFEST_FORMAT,
        "provenance": provenance,
        "provenance_sha256": canonical_sha256(provenance),
    }
    path = output_directory / f"palace-build.{manifest['provenance_sha256']}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def palace_build_identity(manifest):
    if (manifest.get("format") == BUILD_MANIFEST_FORMAT
            or ("provenance_sha256" in manifest and "provenance" in manifest)):
        return manifest["provenance_sha256"]
    if manifest.get("identity", {}).get("format") == "dcdc-palace-build-v2":
        return manifest["content_sha256"]
    raise ValueError("unsupported Palace build manifest format")


def palace_build_source_commit(manifest):
    if (manifest.get("format") == BUILD_MANIFEST_FORMAT
            or ("provenance_sha256" in manifest and "provenance" in manifest)):
        return manifest["provenance"]["source_commit"]
    if manifest.get("identity", {}).get("format") == "dcdc-palace-build-v2":
        try:
            from .palace_source_policy import validate_authorized_palace_source_identity
        except ImportError:
            from palace_source_policy import validate_authorized_palace_source_identity
        source = validate_authorized_palace_source_identity(
            manifest["identity"]["source_identity"]["path"]
        )
        return source["identity"]["source_commit"]
    raise ValueError("unsupported Palace build manifest format")


def validate_palace_build_manifest(path, *, executable):
    path = Path(path).resolve()
    if path.name.startswith("palace-build-v2."):
        try:
            from .palace_build_v2 import validate_palace_build_manifest_v2
        except ImportError:
            from palace_build_v2 import validate_palace_build_manifest_v2
        return validate_palace_build_manifest_v2(path, executable=executable)
    executable = Path(executable).resolve()
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Palace build manifest: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("Palace build manifest must be an object")
    provenance = raw.get("provenance")
    if (raw.get("format") != BUILD_MANIFEST_FORMAT or not isinstance(provenance, dict)
            or raw.get("provenance_sha256") != canonical_sha256(provenance)):
        raise ValueError("Palace build manifest identity mismatch")
    source_directory = Path(provenance["source_directory"])
    if _git_bytes(source_directory, "rev-parse", "HEAD").decode().strip() != provenance[
            "source_commit"]:
        raise ValueError("Palace source commit changed after build attestation")
    patch = _git_bytes(source_directory, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    if bytes_sha256(patch) != provenance["source_patch_sha256"]:
        raise ValueError("Palace source patch changed after build attestation")
    checks = {
        **provenance["source_files"],
        **provenance["cmake_caches"],
        **provenance["binaries"],
    }
    checks[provenance["source_patch"]] = provenance["source_patch_sha256"]
    checks[provenance["mpi_launcher"]] = provenance["mpi_launcher_sha256"]
    for data in provenance["linked_libraries"]["resolved"].values():
        checks[data["path"]] = data["sha256"]
    for name, expected in checks.items():
        candidate = source_directory / name if name in provenance["source_files"] else Path(name)
        if not candidate.is_file() or file_sha256(candidate) != expected:
            raise ValueError(f"Palace build artifact changed: {name}")
    if str(executable) not in provenance["binaries"]:
        raise ValueError("Palace executable is not bound by its build manifest")
    return raw
