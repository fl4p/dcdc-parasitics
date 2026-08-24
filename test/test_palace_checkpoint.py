#!/usr/bin/env python3
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

import palace_checkpoint  # noqa: E402
from palace_checkpoint import (  # noqa: E402
    COMPLETION_FORMAT,
    RESPONSE_FORMAT,
    SHARD_FORMAT,
    validate_native_checkpoint,
)


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _abi():
    return {
        "byte_order": sys.byteorder,
        "scalar": "ieee754-binary64",
        "scalar_bytes": 8,
        "radix": 2,
        "digits": 53,
        "is_iec559": True,
    }


def _binary(values):
    prefix = "<" if sys.byteorder == "little" else ">"
    return struct.pack(f"{prefix}{len(values)}d", *values)


def _write(path, content):
    path.write_bytes(content)
    return content


def _checkpoint(tmp_path, *, prefix=2):
    root = tmp_path / "checkpoint"
    root.mkdir()
    (root / ".writer.lock").write_bytes(b"")
    campaign = "campaign-identity-v2"
    terminals = [7, 8]
    partition = [2, 1]
    global_dofs = sum(partition)
    common = {
        "campaign_identity": campaign,
        "campaign_identity_sha256": _sha(campaign.encode()),
        "ordered_terminals": terminals,
        "process_count": len(partition),
        "global_true_dofs": global_dofs,
        "partition": partition,
        "numeric_abi": _abi(),
    }
    for rhs in range(1, prefix + 1):
        directory = root / f"rhs-{rhs:06d}"
        directory.mkdir()
        rhs_common = {**common, "rhs": rhs, "terminal": terminals[rhs - 1]}
        shard_entries = []
        ownership = 0
        for rank, local_size in enumerate(partition):
            stem = f"rank-{rank:06d}"
            data = _write(
                directory / f"{stem}.bin",
                _binary([rhs + rank + index / 10 for index in range(local_size)]),
            )
            metadata = {
                **rhs_common,
                "format": SHARD_FORMAT,
                "rank": rank,
                "local_true_dofs": local_size,
                "ownership_start": ownership,
                "ownership_end": ownership + local_size,
                "data_file": f"{stem}.bin",
                "data_bytes": len(data),
                "data_sha256": _sha(data),
            }
            metadata_bytes = _write(
                directory / f"{stem}.json", _json_bytes(metadata)
            )
            shard_entries.append({
                "rank": rank,
                "metadata_file": f"{stem}.json",
                "metadata_bytes": len(metadata_bytes),
                "metadata_sha256": _sha(metadata_bytes),
                "data_file": f"{stem}.bin",
                "data_bytes": len(data),
                "data_sha256": _sha(data),
            })
            ownership += local_size
        response_data = _write(
            directory / "response.bin", _binary([rhs * 1e-12, -rhs * 2e-12])
        )
        response = {
            **rhs_common,
            "format": RESPONSE_FORMAT,
            "column_size": len(terminals),
            "data_file": "response.bin",
            "data_bytes": len(response_data),
            "data_sha256": _sha(response_data),
        }
        response_bytes = _write(
            directory / "response.json", _json_bytes(response)
        )
        completion = {
            **rhs_common,
            "format": COMPLETION_FORMAT,
            "response_metadata_file": "response.json",
            "response_metadata_bytes": len(response_bytes),
            "response_metadata_sha256": _sha(response_bytes),
            "response_data_file": "response.bin",
            "response_data_bytes": len(response_data),
            "response_data_sha256": _sha(response_data),
            "shards": shard_entries,
        }
        _write(directory / "complete.json", _json_bytes(completion))
    arguments = {
        "campaign_identity": campaign,
        "ordered_terminal_indices": terminals,
        "process_count": len(partition),
        "global_true_dofs": global_dofs,
        "partition": partition,
    }
    return root, arguments


def test_native_checkpoint_inventory_is_complete_and_content_addressed(tmp_path):
    root, arguments = _checkpoint(tmp_path)
    inventory = validate_native_checkpoint(root, **arguments)
    assert inventory["format"] == "palace-checkpoint-inventory-v2"
    assert inventory["prefix"] == 2
    assert [record["rhs"] for record in inventory["rhs"]] == [1, 2]
    assert inventory["retained_bytes"] == sum(
        path.stat().st_size
        for path in root.glob("rhs-*/*")
    )
    assert len(inventory["content_sha256"]) == 64


@pytest.mark.parametrize("mutation", [
    "unknown", "missing", "lock", "hole", "symlink", "terminal", "completion_hash",
    "nonfinite", "partition_bool", "duplicate_key",
])
def test_native_checkpoint_rejects_adversarial_inventory(tmp_path, mutation):
    root, arguments = _checkpoint(tmp_path)
    first = root / "rhs-000001"
    if mutation == "unknown":
        (first / "unknown.bin").write_bytes(b"x")
    elif mutation == "missing":
        (first / "rank-000001.bin").unlink()
    elif mutation == "lock":
        (root / ".writer.lock").unlink()
    elif mutation == "hole":
        for path in first.iterdir():
            path.unlink()
        first.rmdir()
    elif mutation == "symlink":
        target = first / "response.bin"
        target.unlink()
        target.symlink_to(root / "rhs-000002" / "response.bin")
    elif mutation == "terminal":
        path = first / "response.json"
        value = json.loads(path.read_text())
        value["terminal"] = 8
        path.write_bytes(_json_bytes(value))
    elif mutation == "completion_hash":
        path = first / "complete.json"
        value = json.loads(path.read_text())
        value["response_data_sha256"] = "0" * 64
        path.write_bytes(_json_bytes(value))
    elif mutation == "nonfinite":
        (first / "response.bin").write_bytes(_binary([math.nan, 1.0]))
    elif mutation == "partition_bool":
        path = first / "rank-000000.json"
        value = json.loads(path.read_text())
        value["local_true_dofs"] = True
        path.write_bytes(_json_bytes(value))
    else:
        path = first / "complete.json"
        content = path.read_text()
        path.write_text(content[:-1] + ',"format":"duplicate"}')
    with pytest.raises(ValueError, match="native checkpoint"):
        validate_native_checkpoint(root, **arguments)


def test_native_checkpoint_rejects_mutation_during_validation(
        tmp_path, monkeypatch):
    root, arguments = _checkpoint(tmp_path)
    original = palace_checkpoint._read_file
    mutated = False

    def mutate_after_read(*args, **kwargs):
        nonlocal mutated
        content = original(*args, **kwargs)
        if not mutated:
            mutated = True
            (root / "rhs-000001" / "late-unknown").write_bytes(b"x")
        return content

    monkeypatch.setattr(palace_checkpoint, "_read_file", mutate_after_read)
    with pytest.raises(ValueError, match="changed|inventory"):
        validate_native_checkpoint(root, **arguments)


def test_native_checkpoint_rejects_busy_writer_lock(tmp_path):
    root, arguments = _checkpoint(tmp_path)
    descriptor = os.open(root / ".writer.lock", os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="lock is busy"):
            validate_native_checkpoint(root, **arguments)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_native_checkpoint_rejects_root_path_replacement(
        tmp_path, monkeypatch):
    root, arguments = _checkpoint(tmp_path)
    original_read = palace_checkpoint._read_file
    replaced = False

    def replace_root(*args, **kwargs):
        nonlocal replaced
        content = original_read(*args, **kwargs)
        if not replaced:
            replaced = True
            moved = root.with_name("checkpoint-validated")
            root.rename(moved)
            root.mkdir()
            (root / ".writer.lock").write_bytes(b"")
        return content

    monkeypatch.setattr(palace_checkpoint, "_read_file", replace_root)
    with pytest.raises(ValueError, match="root changed"):
        validate_native_checkpoint(root, **arguments)


def test_native_checkpoint_rejects_sparse_binary(tmp_path):
    root, arguments = _checkpoint(tmp_path)
    path = root / "rhs-000001" / "rank-000000.bin"
    path.unlink()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.ftruncate(descriptor, 16)
    finally:
        os.close(descriptor)
    if path.stat().st_blocks * 512 >= path.stat().st_size:
        pytest.skip("filesystem eagerly allocated the tiny sparse fixture")
    with pytest.raises(ValueError, match="sparse"):
        validate_native_checkpoint(root, **arguments)
