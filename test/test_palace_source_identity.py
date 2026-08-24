#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from palace_source_identity import (  # noqa: E402
    validate_palace_source_identity,
    write_palace_source_identity,
)
from provenance import canonical_sha256  # noqa: E402


def git(path, *args):
    return subprocess.run(
        ("git", "-C", str(path), *args), check=True, capture_output=True,
    ).stdout


def source_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    (source / "tracked.cpp").write_text("base\n")
    git(source, "add", "tracked.cpp")
    git(source, "commit", "-qm", "base")
    (source / "tracked.cpp").write_text("modified\n")
    (source / "native").mkdir()
    (source / "native" / "checkpoint.cpp").write_bytes(b"native\x00source")
    return source


def identity(tmp_path):
    source = source_tree(tmp_path)
    path = write_palace_source_identity(
        tmp_path / "identity", source_directory=source,
        declared_untracked=["native/checkpoint.cpp"],
    )
    return source, path


def test_source_identity_binds_patch_submodules_and_declared_untracked(tmp_path):
    source, path = identity(tmp_path)
    raw = validate_palace_source_identity(path)
    data = raw["identity"]
    assert data["authority"] is None
    assert data["source_directory"] == str(source.resolve())
    assert data["declared_untracked"] == ["native/checkpoint.cpp"]
    assert data["tracked_files"]["tracked.cpp"]["state"] == "present"
    assert data["untracked_files"]["native/checkpoint.cpp"]["bytes"] == 13


@pytest.mark.parametrize("mutation", ["tracked", "untracked", "undeclared"])
def test_source_identity_rejects_source_mutation(tmp_path, mutation):
    source, path = identity(tmp_path)
    if mutation == "tracked":
        (source / "tracked.cpp").write_text("mutated\n")
        match = "tracked patch changed"
    elif mutation == "untracked":
        (source / "native" / "checkpoint.cpp").write_text("mutated\n")
        match = "untracked source.*changed"
    else:
        (source / "other.cpp").write_text("undeclared\n")
        match = "untracked files changed"
    with pytest.raises(ValueError, match=match):
        validate_palace_source_identity(path)


def test_source_identity_rejects_undeclared_untracked_at_creation(tmp_path):
    source = source_tree(tmp_path)
    with pytest.raises(ValueError, match="declaration mismatch"):
        write_palace_source_identity(
            tmp_path / "identity", source_directory=source,
            declared_untracked=[],
        )


@pytest.mark.parametrize("declared", [
    ["../escape"], ["/absolute"], ["native/checkpoint.cpp", "native/checkpoint.cpp"],
])
def test_source_identity_rejects_unsafe_or_duplicate_declarations(tmp_path, declared):
    source = source_tree(tmp_path)
    with pytest.raises(ValueError, match="path is invalid|unique and sorted"):
        write_palace_source_identity(
            tmp_path / "identity", source_directory=source,
            declared_untracked=declared,
        )


def test_source_identity_rejects_symlinked_untracked_source(tmp_path):
    source = source_tree(tmp_path)
    target = source / "target"
    target.write_text("target\n")
    (source / "native" / "checkpoint.cpp").unlink()
    (source / "native" / "checkpoint.cpp").symlink_to(target)
    with pytest.raises(ValueError, match="unavailable or unsafe"):
        write_palace_source_identity(
            tmp_path / "identity", source_directory=source,
            declared_untracked=["native/checkpoint.cpp", "target"],
        )


def test_source_identity_rejects_blob_replacement_and_symlink(tmp_path):
    _, path = identity(tmp_path)
    raw = json.loads(path.read_text())
    blob = Path(raw["identity"]["untracked_files"]["native/checkpoint.cpp"]["blob"])
    original = blob.read_bytes()
    blob.write_bytes(b"replacement")
    with pytest.raises(ValueError, match="artifact changed"):
        validate_palace_source_identity(path)
    blob.unlink()
    target = blob.parent / "target"
    target.write_bytes(original)
    blob.symlink_to(target)
    with pytest.raises(ValueError, match="unavailable or unsafe"):
        validate_palace_source_identity(path)


def test_source_identity_rejects_mutated_git_exclude_policy(tmp_path):
    source, path = identity(tmp_path)
    with (source / ".git" / "info" / "exclude").open("a") as exclude:
        exclude.write("*.generated\n")
    with pytest.raises(ValueError, match="repository exclude.*changed"):
        validate_palace_source_identity(path)


def test_source_identity_binds_absent_default_global_exclude(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    source, path = identity(tmp_path)
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "ignore").write_text("*.hidden\n")
    with pytest.raises(ValueError, match="global exclude.*changed"):
        validate_palace_source_identity(path)


def test_source_identity_rejects_symlinked_policy_ancestor(tmp_path):
    source = source_tree(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "ignore").write_text("*.hidden\n")
    alias = tmp_path / "policy-alias"
    alias.symlink_to(policy, target_is_directory=True)
    git(source, "config", "core.excludesFile", str(alias / "ignore"))
    with pytest.raises(ValueError, match="unavailable or unsafe"):
        write_palace_source_identity(
            tmp_path / "identity", source_directory=source,
            declared_untracked=["native/checkpoint.cpp"],
        )


def test_source_identity_rejects_rehashed_source_rebinding(tmp_path):
    _, path = identity(tmp_path)
    raw = json.loads(path.read_text())
    raw["identity"]["source_directory"] = str(tmp_path / "other")
    raw["content_sha256"] = canonical_sha256(raw["identity"])
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="filename does not bind content"):
        validate_palace_source_identity(path)


def test_source_identity_rejects_duplicate_json_and_output_inside_source(tmp_path):
    source, path = identity(tmp_path)
    path.write_text('{"identity":{},"identity":{},"content_sha256":"x"}')
    with pytest.raises(ValueError, match="duplicate key"):
        validate_palace_source_identity(path)
    with pytest.raises(ValueError, match="output cannot be inside"):
        write_palace_source_identity(
            source / "identity", source_directory=source,
            declared_untracked=["native/checkpoint.cpp"],
        )


def test_source_identity_rejects_symlinked_manifest_ancestor(tmp_path):
    _, path = identity(tmp_path)
    alias = tmp_path / "identity-alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="unavailable or unsafe"):
        validate_palace_source_identity(alias / path.name)


def test_source_identity_rejects_symlinked_source_root(tmp_path):
    source = source_tree(tmp_path)
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="source directory is unavailable or unsafe"):
        write_palace_source_identity(
            tmp_path / "identity", source_directory=alias,
            declared_untracked=["native/checkpoint.cpp"],
        )
