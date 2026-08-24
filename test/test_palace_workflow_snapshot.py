#!/usr/bin/env python3
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from palace_snapshot_materializer import validate_request  # noqa: E402
from palace_workflow import (  # noqa: E402
    build_execution_snapshot_request,
    implementation_identity,
    prepare_execution_snapshot,
    validate_execution_snapshot,
    validate_execution_snapshot_privilege_boundary,
)
from provenance import bytes_sha256, canonical_sha256  # noqa: E402


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
        "implementation_sha256": canonical_sha256(
            implementation_identity(ROOT / "lib" / "palace.py")
        ),
    }
    return manifest, workload, executable, native, launcher


def prepare(values):
    manifest, workload, executable, native, launcher = values
    return prepare_execution_snapshot(
        manifest, workload, executable=executable,
        binaries=(executable, native), mpi_launcher=launcher,
    )


def build_request(values, snapshot_id="c" * 64):
    manifest, workload, executable, native, launcher = values
    return build_execution_snapshot_request(
        manifest, workload, executable=executable,
        binaries=(executable, native), mpi_launcher=launcher,
        snapshot_id=snapshot_id,
    )


def snapshot_root(values):
    manifest, workload, *_ = values
    return manifest.config_path.parent / (
        f".{manifest.config_path.name}.execution.{workload['content_sha256']}"
    )


def make_writable(path):
    path.chmod(0o700)
    (path / "bin").chmod(0o700)


def test_execution_snapshot_request_binds_exact_workload_inputs(tmp_path):
    values = snapshot_inputs(tmp_path)
    request = build_request(values)
    assert validate_request(request, client_uid=request["client_uid"]) == request
    assert request["workload_sha256"] == "a" * 64
    assert [item["role"] for item in request["inputs"]] == [
        "config", "mesh", "executable", "solver_binary", "mpi_launcher",
    ]


def test_execution_snapshot_request_rejects_colliding_output(tmp_path):
    values = snapshot_inputs(tmp_path)
    manifest, _, _, _, _ = values
    manifest.output_directory = tmp_path / "bin"
    with pytest.raises(ValueError, match="namespace collides"):
        build_request(values)


def test_materializer_request_rejects_tampering(tmp_path):
    request = build_request(snapshot_inputs(tmp_path))
    request["output_name"] = "changed"
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_request(request, client_uid=request["client_uid"])


def test_materializer_request_rejects_duplicate_runtime_target(tmp_path):
    request = build_request(snapshot_inputs(tmp_path))
    request["inputs"][-1]["target"] = request["inputs"][2]["target"]
    payload = {name: value for name, value in request.items()
               if name != "content_sha256"}
    request["content_sha256"] = canonical_sha256(payload)
    with pytest.raises(ValueError, match="role or target roster"):
        validate_request(request, client_uid=request["client_uid"])


def external_snapshot(values, request):
    manifest, workload, executable, native, launcher = values
    root = manifest.config_path.parent / f"snapshot.{request['snapshot_id']}"
    root.mkdir()
    (root / "bin").mkdir()
    (root / manifest.output_directory.name).mkdir()
    inputs = []
    for item in request["inputs"]:
        snapshot = root / item["target"]
        snapshot.write_bytes(Path(item["source"]).read_bytes())
        snapshot.chmod(item["mode"])
        inputs.append({
            "role": item["role"], "source": item["source"],
            "source_sha256": item["source_sha256"],
            "snapshot": str(snapshot.resolve()),
            "snapshot_sha256": item["source_sha256"],
        })
    payload = {
        "format": "palace-execution-snapshot-v2",
        "snapshot_id": request["snapshot_id"],
        "materializer_sha256": request["materializer_sha256"],
        "client_uid": request["client_uid"],
        "client_gid": request["client_gid"],
        "root": str(root.resolve()),
        "output": str((root / manifest.output_directory.name).resolve()),
        "workload_sha256": workload["content_sha256"],
        "request_sha256": request["content_sha256"],
        "request_file_sha256": "e" * 64,
        "inputs": inputs,
    }
    record = {**payload, "content_sha256": canonical_sha256(payload)}
    attestation = root / "snapshot.json"
    attestation.write_text(json.dumps(record, sort_keys=True) + "\n")
    attestation.chmod(0o440)
    (root / "bin").chmod(0o550)
    root.chmod(0o550)
    return record, (executable, native), launcher


def test_external_execution_snapshot_binds_materializer_and_attestation(tmp_path):
    values = snapshot_inputs(tmp_path)
    request = build_request(values)
    record, binaries, launcher = external_snapshot(values, request)
    manifest, workload, executable, _, _ = values
    assert validate_execution_snapshot(
        record, manifest, workload, executable=executable,
        binaries=binaries, mpi_launcher=launcher,
    ) == record


def test_external_execution_snapshot_rejects_materializer_substitution(tmp_path):
    values = snapshot_inputs(tmp_path)
    request = build_request(values)
    record, binaries, launcher = external_snapshot(values, request)
    manifest, workload, executable, _, _ = values
    record["materializer_sha256"] = "f" * 64
    payload = {name: value for name, value in record.items()
               if name != "content_sha256"}
    record["content_sha256"] = canonical_sha256(payload)
    (Path(record["root"]) / "snapshot.json").chmod(0o600)
    (Path(record["root"]) / "snapshot.json").write_text(
        json.dumps(record, sort_keys=True) + "\n"
    )
    (Path(record["root"]) / "snapshot.json").chmod(0o440)
    with pytest.raises(ValueError, match="materializer identity mismatch"):
        validate_execution_snapshot(
            record, manifest, workload, executable=executable,
            binaries=binaries, mpi_launcher=launcher,
        )


def privilege_boundary_fixture(monkeypatch, *, ancestor_mode=0o755,
                               snapshot_mode=0o550):
    root = Path("/authority") / f"snapshot.{('a' * 64)}"
    config = root / "config.json"
    output = root / "postpro"
    record = {
        "format": "palace-execution-snapshot-v2",
        "root": str(root),
        "output": str(output),
        "client_uid": 503,
        "client_gid": 20,
        "inputs": [{"snapshot": str(config)}],
    }
    directories = {root, root / "bin", output, root.parent, Path("/")}
    modes = {
        Path("/"): 0o755,
        root.parent: ancestor_mode,
        root: snapshot_mode,
        root / "bin": 0o550,
        config: 0o440,
        root / "snapshot.json": 0o440,
        output: 0o700,
    }

    def metadata(path):
        path = Path(path)
        mode = modes[path]
        owner = 503 if path == output else 0
        group = 20 if path in {root, root / "bin", config,
                               root / "snapshot.json", output} else 0
        return SimpleNamespace(
            st_mode=(stat.S_IFDIR if path in directories else stat.S_IFREG) | mode,
            st_uid=owner, st_gid=group,
        )

    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(Path, "iterdir", lambda path: iter(()))
    monkeypatch.setattr("palace_workflow.os.geteuid", lambda: 503)
    monkeypatch.setattr("palace_workflow.os.getegid", lambda: 20)
    monkeypatch.setattr("palace_workflow.os.getgroups", lambda: [20])
    return record


def test_privilege_boundary_allows_root_owner_writable_ancestry(monkeypatch):
    record = privilege_boundary_fixture(monkeypatch, ancestor_mode=0o755)
    assert validate_execution_snapshot_privilege_boundary(record)


def test_privilege_boundary_rejects_group_writable_ancestry(monkeypatch):
    record = privilege_boundary_fixture(monkeypatch, ancestor_mode=0o775)
    with pytest.raises(ValueError, match="authority ancestry is unsafe"):
        validate_execution_snapshot_privilege_boundary(record)


def test_privilege_boundary_rejects_owner_writable_snapshot(monkeypatch):
    record = privilege_boundary_fixture(monkeypatch, snapshot_mode=0o750)
    with pytest.raises(ValueError, match="lacks root authority"):
        validate_execution_snapshot_privilege_boundary(record)


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
