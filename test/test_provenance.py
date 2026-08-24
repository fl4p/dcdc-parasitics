#!/usr/bin/env python3
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from provenance import exclusive_publish_bytes  # noqa: E402


def test_exclusive_publication_never_replaces_an_existing_artifact(tmp_path):
    path = tmp_path / "artifact.bin"
    exclusive_publish_bytes(path, b"first")
    with pytest.raises(FileExistsError):
        exclusive_publish_bytes(path, b"second")
    assert path.read_bytes() == b"first"


def test_exclusive_publication_does_not_follow_destination_symlinks(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"target")
    destination = tmp_path / "artifact.bin"
    destination.symlink_to(target)
    with pytest.raises(FileExistsError):
        exclusive_publish_bytes(destination, b"replacement")
    assert target.read_bytes() == b"target"


def test_exclusive_publication_rejects_a_symlink_parent(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    os.symlink(real, linked)
    with pytest.raises(ValueError, match="unsafe"):
        exclusive_publish_bytes(linked / "artifact.bin", b"content")
    assert not (real / "artifact.bin").exists()
