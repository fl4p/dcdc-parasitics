#!/usr/bin/env python3
"""Adversarial tests for Palace runtime evidence guards."""
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import palace_runtime  # noqa: E402


def test_quarantine_retries_disappeared_and_recreated_source(tmp_path, monkeypatch):
    output = tmp_path / "postpro"
    output.mkdir()
    raw = output / "terminal-Craw.csv"
    standard = output / "terminal-C.csv"
    raw.write_bytes(b"raw")
    real_rename = palace_runtime.os.rename
    injected = False

    def disappear_recreate(source, target):
        nonlocal injected
        if Path(source) == raw and not injected:
            injected = True
            standard.write_bytes(b"recreated while raw was captured")
        return real_rename(source, target)

    monkeypatch.setattr(palace_runtime.os, "rename", disappear_recreate)
    quarantined = palace_runtime.quarantine_matrix_files(
        output, (raw, standard),
    )
    assert injected
    assert raw.is_dir() and standard.is_dir()
    assert len(quarantined) == 2
    assert all(path.is_file() and path.name.endswith(".quarantined")
               for path in quarantined)


def test_quarantine_drains_name_created_after_initial_miss(tmp_path, monkeypatch):
    output = tmp_path / "postpro"
    output.mkdir()
    standard = output / "terminal-C.csv"
    real_rename = palace_runtime.os.rename
    injected = False

    def create_after_miss(source, target):
        nonlocal injected
        if Path(source) == standard and not injected:
            injected = True
            standard.write_bytes(b"late matrix")
            raise FileNotFoundError(standard)
        return real_rename(source, target)

    monkeypatch.setattr(palace_runtime.os, "rename", create_after_miss)
    quarantined = palace_runtime.quarantine_matrix_files(output, (standard,))
    assert standard.is_dir()
    assert quarantined[0].is_file()
    assert quarantined[0].read_bytes() == b"late matrix"


def test_quarantine_reservation_captures_final_miss_recreation(
        tmp_path, monkeypatch):
    output = tmp_path / "postpro"
    output.mkdir()
    standard = output / "terminal-C.csv"
    real_mkdir = Path.mkdir
    injected = False

    def recreate_before_reservation(path, *args, **kwargs):
        nonlocal injected
        if path == standard and not injected:
            injected = True
            standard.write_bytes(b"final miss matrix")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", recreate_before_reservation)
    quarantined = palace_runtime.quarantine_matrix_files(output, (standard,))
    assert injected and standard.is_dir()
    assert quarantined[0].read_bytes() == b"final miss matrix"


def test_purge_reservation_removes_final_miss_recreation(tmp_path, monkeypatch):
    standard = tmp_path / "terminal-C.csv"
    real_mkdir = Path.mkdir
    injected = False

    def recreate_before_reservation(path, *args, **kwargs):
        nonlocal injected
        if path == standard and not injected:
            injected = True
            standard.write_bytes(b"final miss matrix")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", recreate_before_reservation)
    palace_runtime.purge_matrix_files((standard,))
    assert injected and standard.is_dir()
    assert not standard.is_file() and not standard.is_symlink()


def test_quarantine_rejects_regular_to_symlink_capture_race(tmp_path, monkeypatch):
    output = tmp_path / "postpro"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    raw = output / "terminal-Craw.csv"
    raw.write_bytes(b"matrix")
    real_lstat = palace_runtime.os.lstat
    injected = False

    def replace_after_lstat(path, *args, **kwargs):
        nonlocal injected
        metadata = real_lstat(path, *args, **kwargs)
        candidate = Path(path)
        if candidate.name.startswith(".captured.") and not injected:
            injected = True
            candidate.unlink()
            candidate.symlink_to(outside)
        return metadata

    monkeypatch.setattr(palace_runtime.os, "lstat", replace_after_lstat)
    with pytest.raises(OSError):
        palace_runtime.quarantine_matrix_files(output, (raw,))
    assert not list(output.rglob("*.quarantined"))


def test_quarantine_cleanup_failure_still_purges_and_reserves_names(
        tmp_path, monkeypatch):
    output = tmp_path / "postpro"
    output.mkdir()
    raw = output / "terminal-Craw.csv"
    standard = output / "terminal-C.csv"
    raw.write_bytes(b"raw")
    standard.write_bytes(b"standard")

    def fail_capture(*_args, **_kwargs):
        raise OSError("injected capture failure")

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(palace_runtime, "_quarantine_into", fail_capture)
    monkeypatch.setattr(palace_runtime.shutil, "rmtree", fail_cleanup)
    paths, error = palace_runtime.quarantine_or_purge_matrix_files(
        output, (raw, standard),
    )
    assert "cleanup failed" in error
    assert paths == (raw, standard)
    assert raw.is_dir() and standard.is_dir()
    assert not raw.is_file() and not standard.is_file()


def test_quarantine_retries_destination_collision(tmp_path, monkeypatch):
    output = tmp_path / "postpro"
    output.mkdir()
    raw = output / "terminal-Craw.csv"
    raw.write_bytes(b"raw")
    real_open = palace_runtime.os.open
    collision = None

    def occupy_then_open(path, flags, *args, **kwargs):
        nonlocal collision
        candidate = Path(path)
        if (collision is None and candidate.name.endswith(".quarantined")
                and flags & os.O_EXCL):
            collision = candidate
            collision.write_bytes(b"existing evidence")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(palace_runtime.os, "open", occupy_then_open)
    quarantined = palace_runtime.quarantine_matrix_files(output, (raw,))
    assert raw.is_dir()
    assert collision is not None and collision.read_bytes() == b"existing evidence"
    assert quarantined[0].is_file() and quarantined[0] != collision


def test_quarantine_converts_symlink_to_regular_evidence(tmp_path):
    output = tmp_path / "postpro"
    output.mkdir()
    target = tmp_path / "outside.csv"
    target.write_bytes(b"outside")
    raw = output / "terminal-Craw.csv"
    raw.symlink_to(target)
    quarantined = palace_runtime.quarantine_matrix_files(output, (raw,))
    assert raw.is_dir()
    assert quarantined[0].is_file() and not quarantined[0].is_symlink()
    assert quarantined[0].read_bytes() == str(target).encode()
