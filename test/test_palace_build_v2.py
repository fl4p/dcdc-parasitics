#!/usr/bin/env python3
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import palace_build  # noqa: E402
import palace_build_v2  # noqa: E402
from provenance import canonical_sha256  # noqa: E402

SOURCE_IDENTITY = ROOT / "docs/artifacts/palace-e4-source-v5" / (
    "palace-source-identity."
    "9a50e1ad35a4b6b644afdff791f312c75b8d58ff7bc01cc5669fe7faa16be94b.json"
)
BUILD = Path("/Users/fab/dev/vendor/palace-build-qualification-make")
EXECUTABLE = BUILD / "bin/palace"


def build_manifest(tmp_path):
    return palace_build_v2.write_palace_build_manifest_v2(
        tmp_path, source_identity_path=SOURCE_IDENTITY,
        build_directory=BUILD, executable=EXECUTABLE,
    )


def test_build_v2_binds_authorized_source_and_current_build(tmp_path):
    path = build_manifest(tmp_path)
    raw = palace_build_v2.validate_palace_build_manifest_v2(
        path, executable=EXECUTABLE,
    )
    assert raw["identity"]["source_identity"]["content_sha256"] == (
        "9a50e1ad35a4b6b644afdff791f312c75b8d58ff7bc01cc5669fe7faa16be94b"
    )
    assert raw["identity"]["format"] == "dcdc-palace-build-v2"
    assert palace_build.validate_palace_build_manifest(
        path, executable=EXECUTABLE,
    )["content_sha256"] == raw["content_sha256"]


def test_build_v2_rejects_source_identity_substitution(tmp_path):
    path = build_manifest(tmp_path)
    raw = json.loads(path.read_text())
    raw["identity"]["source_identity"]["content_sha256"] = "f" * 64
    raw["content_sha256"] = canonical_sha256(raw["identity"])
    rebound = tmp_path / f"palace-build-v2.{raw['content_sha256']}.json"
    rebound.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="source identity was substituted"):
        palace_build_v2.validate_palace_build_manifest_v2(
            rebound, executable=EXECUTABLE,
        )


def test_build_v2_rejects_filename_rebinding_and_duplicate_json(tmp_path):
    path = build_manifest(tmp_path)
    renamed = tmp_path / ("palace-build-v2." + "f" * 64 + ".json")
    renamed.write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="identity mismatch"):
        palace_build_v2.validate_palace_build_manifest_v2(
            renamed, executable=EXECUTABLE,
        )
    path.write_text('{"identity":{},"identity":{},"content_sha256":"x"}')
    with pytest.raises(ValueError, match="duplicate key"):
        palace_build_v2.validate_palace_build_manifest_v2(
            path, executable=EXECUTABLE,
        )


@pytest.mark.parametrize("mutation,match", [
    ("omit_binary", "binary roster"),
    ("duplicate_cache", "CMake cache roster"),
    ("build_root", "build root is not authorized"),
    ("launcher", "MPI launcher is not authorized"),
])
def test_build_v2_rejects_fully_rehashed_roster_rebinding(tmp_path, mutation, match):
    path = build_manifest(tmp_path)
    raw = json.loads(path.read_text())
    if mutation == "omit_binary":
        raw["identity"]["binaries"] = raw["identity"]["binaries"][:1]
    elif mutation == "duplicate_cache":
        raw["identity"]["cmake_caches"].append(
            raw["identity"]["cmake_caches"][0]
        )
    elif mutation == "build_root":
        raw["identity"]["build_directory"] = str(tmp_path)
    else:
        raw["identity"]["mpi_launcher"]["path"] = str(EXECUTABLE)
        raw["identity"]["mpi_launcher"]["sha256"] = raw["identity"][
            "binaries"
        ][0]["sha256"]
        raw["identity"]["mpi_launcher"]["bytes"] = raw["identity"][
            "binaries"
        ][0]["bytes"]
    raw["content_sha256"] = canonical_sha256(raw["identity"])
    rebound = tmp_path / f"palace-build-v2.{raw['content_sha256']}.json"
    rebound.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=match):
        palace_build_v2.validate_palace_build_manifest_v2(
            rebound, executable=EXECUTABLE,
        )


def test_build_v2_rejects_unreviewed_source_before_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(
        palace_build_v2, "validate_authorized_palace_source_identity",
        lambda path: (_ for _ in ()).throw(ValueError("not authorized")),
    )
    with pytest.raises(ValueError, match="not authorized"):
        build_manifest(tmp_path)
    assert not list(tmp_path.iterdir())
