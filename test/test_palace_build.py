#!/usr/bin/env python3
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import palace_build  # noqa: E402
from palace_build import (  # noqa: E402
    validate_palace_build_manifest,
    write_palace_build_manifest,
)


def _git(path, *args):
    subprocess.run(("git", "-C", str(path), *args), check=True, capture_output=True)


def test_build_manifest_binds_source_patch_caches_binaries_and_launcher(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Test")
    tracked = source / "solver.cpp"
    tracked.write_text("original\n")
    _git(source, "add", "solver.cpp")
    _git(source, "commit", "-qm", "base")
    tracked.write_text("qualification patch\n")

    build = tmp_path / "build"
    (build / "palace-build").mkdir(parents=True)
    (build / "CMakeCache.txt").write_text("ROOT=1\n")
    (build / "palace-build" / "CMakeCache.txt").write_text("PALACE=1\n")
    binary_dir = build / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "palace"
    executable.write_text("#!/bin/sh\n")
    (binary_dir / "palace-test.bin").write_bytes(b"binary")
    launcher = tmp_path / "mpirun"
    launcher.write_text("launcher\n")

    monkeypatch.setattr(palace_build.shutil, "which", lambda name: str(launcher))
    monkeypatch.setattr(
        palace_build,
        "_linked_library_identity",
        lambda binary, build_directory: {"reported": [], "resolved": {}},
    )
    manifest = write_palace_build_manifest(
        tmp_path / "evidence",
        source_directory=source,
        build_directory=build,
        executable=executable,
    )
    validated = validate_palace_build_manifest(manifest, executable=executable)
    assert validated["provenance"]["source_files"] == {
        "solver.cpp": palace_build.file_sha256(tracked)
    }
    tracked.write_text("changed after attestation\n")
    with pytest.raises(ValueError, match="source patch changed"):
        validate_palace_build_manifest(manifest, executable=executable)
