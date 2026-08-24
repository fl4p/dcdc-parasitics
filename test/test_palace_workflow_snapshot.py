#!/usr/bin/env python3
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from palace_workflow import prepare_execution_snapshot  # noqa: E402
from provenance import bytes_sha256  # noqa: E402


def snapshot_inputs(tmp_path, identity="a" * 64):
    config = tmp_path / "config.json"
    mesh = tmp_path / "fixture.msh"
    binary_dir = tmp_path / "source-bin"
    binary_dir.mkdir()
    executable = binary_dir / "palace"
    native = binary_dir / "palace-arm64.bin"
    launcher = tmp_path / "mpirun"
    config.write_bytes(b"config")
    mesh.write_bytes(b"mesh")
    executable.write_bytes(b"wrapper")
    native.write_bytes(b"native")
    launcher.write_bytes(b"launcher")
    manifest = SimpleNamespace(
        config_path=config, mesh_path=mesh,
        output_directory=tmp_path / "postpro",
    )
    workload = {
        "content_sha256": identity,
        "config_sha256": bytes_sha256(config.read_bytes()),
        "mesh_sha256": bytes_sha256(mesh.read_bytes()),
        "solver_binary_sha256": bytes_sha256(executable.read_bytes()),
        "mpi_launcher_sha256": bytes_sha256(launcher.read_bytes()),
        "runtime_binaries": [
            {"name": executable.name, "sha256": bytes_sha256(executable.read_bytes())},
            {"name": native.name, "sha256": bytes_sha256(native.read_bytes())},
        ],
    }
    return manifest, workload, executable, native, launcher


def prepare(values):
    manifest, workload, executable, native, launcher = values
    return prepare_execution_snapshot(
        manifest, workload, executable=executable,
        binaries=(executable, native), mpi_launcher=launcher,
    )


def snapshot_root(values):
    manifest, workload, *_ = values
    return manifest.config_path.parent / (
        f".{manifest.config_path.name}.execution.{workload['content_sha256']}"
    )


def make_writable(path):
    path.chmod(0o700)
    (path / "bin").chmod(0o700)


def test_execution_snapshot_retry_is_exactly_idempotent(tmp_path):
    values = snapshot_inputs(tmp_path)
    first = prepare(values)
    second = prepare(values)
    assert second == first


def test_execution_snapshot_retry_completes_exact_partial_tree(tmp_path):
    values = snapshot_inputs(tmp_path, "b" * 64)
    manifest, _, _, _, _ = values
    root = snapshot_root(values)
    root.mkdir(mode=0o700)
    (root / "bin").mkdir(mode=0o700)
    (root / "postpro").mkdir(mode=0o700)
    (root / manifest.config_path.name).write_bytes(manifest.config_path.read_bytes())
    result = prepare(values)
    assert len(result["inputs"]) == 5
    assert (root / manifest.mesh_path.name).is_file()


@pytest.mark.parametrize(
    "defect", ["changed", "symlink", "unknown", "bin_unknown", "output"],
)
def test_execution_snapshot_retry_rejects_retained_defects(tmp_path, defect):
    values = snapshot_inputs(tmp_path)
    prepare(values)
    manifest, _, _, _, _ = values
    root = snapshot_root(values)
    make_writable(root)
    retained = root / manifest.config_path.name
    if defect == "changed":
        retained.chmod(0o600)
        retained.write_bytes(b"changed")
        match = "retained snapshot input differs"
    elif defect == "symlink":
        retained.unlink()
        retained.symlink_to(manifest.config_path)
        match = "retained snapshot input differs"
    elif defect == "unknown":
        (root / "unknown").write_bytes(b"unknown")
        match = "unknown entries"
    elif defect == "bin_unknown":
        (root / "bin" / "unknown").write_bytes(b"unknown")
        match = "unknown entries"
    else:
        (root / "postpro" / "partial.csv").write_bytes(b"partial")
        match = "output is not empty"
    with pytest.raises(ValueError, match=match):
        prepare(values)


@pytest.mark.parametrize("name", ["bin", "config.json", "fixture.msh"])
def test_execution_snapshot_rejects_output_namespace_collisions(tmp_path, name):
    values = snapshot_inputs(tmp_path)
    manifest, _, _, _, _ = values
    manifest.output_directory = tmp_path / name
    with pytest.raises(ValueError, match="input and output names collide"):
        prepare(values)


def test_execution_snapshot_rejects_binary_namespace_collisions(tmp_path):
    values = snapshot_inputs(tmp_path)
    manifest, workload, executable, native, _ = values
    with pytest.raises(ValueError, match="input and output names collide"):
        prepare_execution_snapshot(
            manifest, workload, executable=executable,
            binaries=(executable, native), mpi_launcher=executable,
        )


def test_execution_snapshot_revalidates_after_input_publication(tmp_path, monkeypatch):
    import palace_workflow

    values = snapshot_inputs(tmp_path)
    root = snapshot_root(values)
    original = palace_workflow._publish_or_validate_snapshot_file
    calls = 0

    def publish_then_inject_output(path, content, mode):
        nonlocal calls
        original(path, content, mode)
        calls += 1
        if calls == 10:
            (root / "postpro" / "concurrent.csv").write_bytes(b"partial")

    monkeypatch.setattr(
        palace_workflow, "_publish_or_validate_snapshot_file",
        publish_then_inject_output,
    )
    with pytest.raises(ValueError, match="output is not empty"):
        prepare(values)


def test_execution_snapshot_retry_rejects_symlinked_root(tmp_path):
    values = snapshot_inputs(tmp_path)
    root = snapshot_root(values)
    target = tmp_path / "elsewhere"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="directory is unsafe"):
        prepare(values)
