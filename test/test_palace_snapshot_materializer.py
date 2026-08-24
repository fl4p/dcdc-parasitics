#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

import palace_snapshot_materializer as materializer  # noqa: E402
from provenance import bytes_sha256, canonical_sha256  # noqa: E402


def request_fixture(tmp_path, *, bad_digest=False):
    inputs = []
    for role, name, content, mode in (
            ("config", "config.json", b"config", 0o440),
            ("mesh", "fixture.msh", b"mesh", 0o440),
            ("executable", "palace", b"wrapper", 0o550),
            ("mpi_launcher", "mpirun", b"launcher", 0o550)):
        source = tmp_path / f"source-{name}"
        source.write_bytes(content)
        digest = bytes_sha256(content)
        if bad_digest and role == "mesh":
            digest = "f" * 64
        inputs.append({
            "role": role,
            "source": str(source.resolve()),
            "source_sha256": digest,
            "target": name if role in {"config", "mesh"} else f"bin/{name}",
            "mode": mode,
        })
    payload = {
        "format": materializer.REQUEST_FORMAT,
        "snapshot_id": "d" * 64,
        "materializer_sha256": bytes_sha256(
            Path(materializer.__file__).read_bytes()
        ),
        "client_uid": os.getuid(),
        "client_gid": os.getgid(),
        "workload_sha256": "a" * 64,
        "output_name": "postpro",
        "inputs": inputs,
    }
    request = {**payload, "content_sha256": canonical_sha256(payload)}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request, sort_keys=True) + "\n")
    return path


def patch_root_service(monkeypatch):
    real_open = os.open
    monkeypatch.setattr(materializer.os, "geteuid", lambda: 0)
    real_fchmod = os.fchmod
    monkeypatch.setattr(materializer.os, "fchown", lambda *args, **kwargs: None)
    monkeypatch.setattr(materializer.os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        materializer.os, "fchmod",
        lambda descriptor, mode: (
            None if mode == 0o550 else real_fchmod(descriptor, mode)
        ),
    )

    def authority(root, client_uid, service_uid):
        return Path(root), real_open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )

    monkeypatch.setattr(materializer, "_validate_authority_root", authority)


def test_materializer_atomically_publishes_complete_snapshot(tmp_path, monkeypatch):
    request = request_fixture(tmp_path)
    authority = tmp_path / "authority"
    authority.mkdir()
    patch_root_service(monkeypatch)
    attestation = materializer.materialize(
        request, authority, client_uid=os.getuid(), client_gid=os.getgid(),
    )
    assert attestation == authority / f"snapshot.{('d' * 64)}" / "snapshot.json"
    record = json.loads(attestation.read_bytes())
    assert record["format"] == materializer.SNAPSHOT_FORMAT
    assert record["client_uid"] == os.getuid()
    assert not list(authority.glob(".pending.*"))
    assert (attestation.parent / "postpro").is_dir()
    assert (attestation.parent / "postpro").stat().st_mode & 0o777 == 0o700
    assert not any((attestation.parent / "postpro").iterdir())


def test_materializer_removes_failed_staging_tree(tmp_path, monkeypatch):
    request = request_fixture(tmp_path, bad_digest=True)
    authority = tmp_path / "authority"
    authority.mkdir()
    patch_root_service(monkeypatch)
    with pytest.raises(ValueError, match="differs from its request"):
        materializer.materialize(
            request, authority, client_uid=os.getuid(), client_gid=os.getgid(),
        )
    assert list(authority.iterdir()) == []
