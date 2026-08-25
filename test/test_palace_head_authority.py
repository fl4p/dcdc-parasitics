#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

import palace_head_authority  # noqa: E402
from palace_head_authority import CanonicalHeadAuthority  # noqa: E402


CAMPAIGN = "a" * 64
HEAD0 = "b" * 64
HEAD1 = "c" * 64
HEAD2 = "d" * 64
ENTRY1 = "e" * 64
ENTRY2 = "f" * 64
KEY = b"external canonical authority test key" * 2
CLIENT_UID = os.getuid() + 100_000


def authority(tmp_path):
    root = tmp_path / "authority"
    root.mkdir()
    return CanonicalHeadAuthority(
        root, authority_id="test-authority-v1", authority_key=KEY,
        client_uid=CLIENT_UID,
    )


def test_canonical_head_compare_and_swap_serializes_cloned_roots(tmp_path):
    first = authority(tmp_path)
    first.register(CAMPAIGN, HEAD0)
    second = CanonicalHeadAuthority(
        first.root, authority_id="test-authority-v1", authority_key=KEY,
        client_uid=CLIENT_UID,
    )
    assert first.read(CAMPAIGN)["head_sha256"] == HEAD0
    successor = first.compare_and_swap(
        CAMPAIGN,
        expected_head_sha256=HEAD0,
        successor_head_sha256=HEAD1,
        successor_sequence=1,
        successor_entry_sha256=ENTRY1,
    )
    assert successor["sequence"] == 1
    with pytest.raises(ValueError, match="predecessor mismatch"):
        second.compare_and_swap(
            CAMPAIGN,
            expected_head_sha256=HEAD0,
            successor_head_sha256=HEAD2,
            successor_sequence=1,
            successor_entry_sha256=ENTRY2,
        )
    assert second.read(CAMPAIGN)["head_sha256"] == HEAD1


def test_canonical_head_registration_recovery_is_exactly_idempotent(tmp_path):
    value = authority(tmp_path)
    first = value.ensure_registered(CAMPAIGN, HEAD0)
    assert value.ensure_registered(CAMPAIGN, HEAD0) == first
    with pytest.raises(ValueError, match="differs from initial head"):
        value.ensure_registered(CAMPAIGN, HEAD1)


def test_canonical_head_rejects_duplicate_registration(tmp_path):
    value = authority(tmp_path)
    value.register(CAMPAIGN, HEAD0)
    with pytest.raises(ValueError, match="already registered"):
        value.register(CAMPAIGN, HEAD0)
    assert value.read(CAMPAIGN)["head_sha256"] == HEAD0


@pytest.mark.parametrize("mutation", ["head", "mac", "duplicate", "symlink"])
def test_canonical_head_rejects_tampering(tmp_path, mutation):
    value = authority(tmp_path)
    value.register(CAMPAIGN, HEAD0)
    record = value.root / f"{CAMPAIGN}.head.json"
    os.chmod(record, 0o600)
    if mutation == "symlink":
        target = value.root / "target.json"
        record.rename(target)
        record.symlink_to(target.name)
    elif mutation == "duplicate":
        content = record.read_text().rstrip()
        record.write_text(content[:-1] + ',"sequence":0}')
    else:
        content = json.loads(record.read_text())
        content["head_sha256" if mutation == "head" else "authority_mac"] = "0" * 64
        record.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="canonical head"):
        value.read(CAMPAIGN)


def test_canonical_head_binds_authority_identity(tmp_path):
    first = authority(tmp_path)
    first.register(CAMPAIGN, HEAD0)
    with pytest.raises(ValueError, match="instance mismatch"):
        CanonicalHeadAuthority(
            first.root, authority_id="other-authority", authority_key=KEY,
            client_uid=CLIENT_UID,
        )


def test_canonical_head_rejects_symlink_root(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    link = tmp_path / "authority"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        CanonicalHeadAuthority(
            link, authority_id="test-authority-v1", authority_key=KEY,
            client_uid=CLIENT_UID,
        )


@pytest.mark.parametrize("mutation", ["client_owner", "world_writable"])
def test_canonical_head_rejects_client_mutable_authority_root(tmp_path, mutation):
    root = tmp_path / "authority"
    root.mkdir()
    client_uid = os.getuid() if mutation == "client_owner" else CLIENT_UID
    if mutation == "world_writable":
        root.chmod(0o707)
    with pytest.raises(ValueError, match="mutable by its client UID"):
        CanonicalHeadAuthority(
            root, authority_id="test-authority-v1", authority_key=KEY,
            client_uid=client_uid,
        )


def test_canonical_head_rejects_world_writable_ancestor(tmp_path):
    parent = tmp_path / "writable-parent"
    parent.mkdir()
    parent.chmod(0o707)
    root = parent / "authority"
    root.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="mutable by its client UID"):
        CanonicalHeadAuthority(
            root, authority_id="test-authority-v1", authority_key=KEY,
            client_uid=CLIENT_UID,
        )


def test_canonical_head_rejects_post_open_root_replacement(tmp_path):
    value = authority(tmp_path)
    value.register(CAMPAIGN, HEAD0)
    original = value.root.with_name("authority-original")
    value.root.rename(original)
    value.root.mkdir()
    with pytest.raises(ValueError, match="root was replaced"):
        value.read(CAMPAIGN)


def test_canonical_head_rejects_lock_replacement_during_acquisition(
        tmp_path, monkeypatch):
    value = authority(tmp_path)
    original_flock = palace_head_authority.fcntl.flock
    replaced = False

    def replace_lock(descriptor, operation):
        nonlocal replaced
        original_flock(descriptor, operation)
        if operation == palace_head_authority.fcntl.LOCK_EX and not replaced:
            replaced = True
            lock = value.root / f"{CAMPAIGN}.lock"
            lock.rename(value.root / "old.lock")
            lock.write_bytes(b"")

    monkeypatch.setattr(palace_head_authority.fcntl, "flock", replace_lock)
    with pytest.raises(ValueError, match="lock changed"):
        value.register(CAMPAIGN, HEAD0)


def test_canonical_head_exact_instance_rejects_helper_override(tmp_path):
    value = authority(tmp_path)
    with pytest.raises(AttributeError, match="immutable"):
        value._read = lambda *_args: {"head_sha256": "f" * 64}
    with pytest.raises(AttributeError, match="immutable"):
        value._publish = lambda *_args, **_kwargs: None


def test_canonical_head_registration_recovers_after_interrupted_publication(
        tmp_path, monkeypatch):
    value = authority(tmp_path)
    original_publish = CanonicalHeadAuthority._publish
    interrupted = False

    def interrupt(self, record_name, record, *, exclusive):
        nonlocal interrupted
        if record_name.endswith(".head.json") and not interrupted:
            interrupted = True
            raise OSError("injected interruption")
        return original_publish(self, record_name, record, exclusive=exclusive)

    monkeypatch.setattr(CanonicalHeadAuthority, "_publish", interrupt)
    with pytest.raises(OSError, match="injected interruption"):
        value.register(CAMPAIGN, HEAD0)
    monkeypatch.setattr(CanonicalHeadAuthority, "_publish", original_publish)
    assert value.register(CAMPAIGN, HEAD0)["sequence"] == 0


def test_canonical_head_instance_rejects_key_substitution(tmp_path):
    value = authority(tmp_path)
    with pytest.raises(ValueError, match="instance mismatch"):
        CanonicalHeadAuthority(
            value.root,
            authority_id="test-authority-v1",
            authority_key=b"different canonical authority key" * 2,
            client_uid=CLIENT_UID,
        )
