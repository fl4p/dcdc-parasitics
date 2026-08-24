#!/usr/bin/env python3
"""Strict external validation of native Palace electrostatic checkpoints."""
import hashlib
import json
try:
    import fcntl
except ImportError:  # pragma: no cover - validator is POSIX-only
    fcntl = None
import math
import os
from pathlib import Path
import stat
import struct
import sys

if __package__:
    from .provenance import canonical_sha256
else:
    from provenance import canonical_sha256


COMPLETION_FORMAT = "palace-electrostatic-checkpoint-completion-v1"
SHARD_FORMAT = "palace-electrostatic-checkpoint-shard-v1"
RESPONSE_FORMAT = "palace-electrostatic-checkpoint-response-v1"
INVENTORY_FORMAT = "palace-checkpoint-inventory-v2"
MAX_METADATA_BYTES = 16 * 1024 * 1024

COMMON_KEYS = {
    "format", "campaign_identity", "campaign_identity_sha256", "rhs",
    "terminal", "ordered_terminals", "process_count", "global_true_dofs",
    "partition", "numeric_abi",
}
SHARD_KEYS = COMMON_KEYS | {
    "rank", "local_true_dofs", "ownership_start", "ownership_end",
    "data_file", "data_bytes", "data_sha256",
}
RESPONSE_KEYS = COMMON_KEYS | {
    "column_size", "data_file", "data_bytes", "data_sha256",
}
COMPLETION_KEYS = COMMON_KEYS | {
    "response_metadata_file", "response_metadata_bytes",
    "response_metadata_sha256", "response_data_file", "response_data_bytes",
    "response_data_sha256", "shards",
}
SHARD_ENTRY_KEYS = {
    "rank", "metadata_file", "metadata_bytes", "metadata_sha256",
    "data_file", "data_bytes", "data_sha256",
}


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _load_json(content, label):
    try:
        return json.loads(content, object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"native checkpoint {label} JSON is invalid: {error}") from error


def _sha256(content):
    return hashlib.sha256(content).hexdigest()


def _typed_equal(left, right):
    try:
        return json.dumps(
            left, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


def _digest(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"native checkpoint {label} digest is invalid")
    return value


def _nonnegative_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"native checkpoint {label} is invalid")
    return value


def _open_root(path):
    path = Path(path).absolute()
    if os.name != "posix" or not path.is_absolute() or len(path.parts) < 2:
        raise ValueError("native checkpoint validation requires an absolute POSIX path")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    parent = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:-1]:
            if component in ("", ".", ".."):
                raise ValueError("native checkpoint root component is invalid")
            child = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        name = path.parts[-1]
        if name in ("", ".", ".."):
            raise ValueError("native checkpoint root component is invalid")
        root = os.open(name, flags, dir_fd=parent)
    except Exception:
        os.close(parent)
        raise
    return path, parent, name, root


def _list_directory(descriptor, label):
    try:
        names = os.listdir(descriptor)
    except OSError as error:
        raise ValueError(f"native checkpoint {label} cannot be listed: {error}") from error
    records = {}
    for name in names:
        if name in records or name in ("", ".", ".."):
            raise ValueError(f"native checkpoint {label} entry is invalid")
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise ValueError(f"native checkpoint {label} entry is unsafe: {error}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"native checkpoint {label} contains a symlink")
        records[name] = metadata
    return records


def _read_file(descriptor, name, label, *, exact_bytes=None, maximum_bytes=None):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    file_descriptor = os.open(name, flags, dir_fd=descriptor)
    try:
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"native checkpoint {label} is not regular")
        if (exact_bytes is not None and before.st_size != exact_bytes):
            raise ValueError(f"native checkpoint {label} size mismatch")
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            raise ValueError(f"native checkpoint {label} exceeds its size bound")
        if before.st_size and before.st_blocks * 512 < before.st_size:
            raise ValueError(f"native checkpoint {label} is sparse")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(file_descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"native checkpoint {label} is truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(file_descriptor, 1):
            raise ValueError(f"native checkpoint {label} grew during validation")
        after = os.fstat(file_descriptor)
        if ((before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)):
            raise ValueError(f"native checkpoint {label} changed during validation")
        return b"".join(chunks)
    finally:
        os.close(file_descriptor)


def _stat_identity(metadata):
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_mode,
        metadata.st_size, metadata.st_blocks,
    )


def _same_directory(left, right):
    return (set(left) == set(right)
            and all(_stat_identity(left[name]) == _stat_identity(right[name])
                    for name in left))


def _identity(path, content):
    return {"path": str(path), "bytes": len(content), "sha256": _sha256(content)}


def _numeric_abi():
    return {
        "byte_order": sys.byteorder,
        "scalar": "ieee754-binary64",
        "scalar_bytes": 8,
        "radix": 2,
        "digits": 53,
        "is_iec559": True,
    }


def _decode_finite(content, count, label):
    if len(content) != count * 8:
        raise ValueError(f"native checkpoint {label} binary64 size mismatch")
    prefix = "<" if sys.byteorder == "little" else ">"
    values = struct.unpack(f"{prefix}{count}d", content)
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"native checkpoint {label} contains nonfinite values")
    return values


def _validate_common(value, expected, *, format_name, rhs, label):
    if not isinstance(value, dict) or value.get("format") != format_name:
        raise ValueError(f"native checkpoint {label} format mismatch")
    for name in (
            "campaign_identity", "campaign_identity_sha256", "terminal",
            "ordered_terminals", "process_count", "global_true_dofs",
            "partition", "numeric_abi"):
        if not _typed_equal(value.get(name), expected[name]):
            raise ValueError(f"native checkpoint {label} {name} mismatch")
    if type(value.get("rhs")) is not int or value.get("rhs") != rhs:
        raise ValueError(f"native checkpoint {label} RHS mismatch")


def validate_native_checkpoint(
        root, *, campaign_identity, ordered_terminal_indices,
        process_count, global_true_dofs, partition):
    if not isinstance(campaign_identity, str) or not campaign_identity:
        raise ValueError("native checkpoint campaign identity is invalid")
    terminals = list(ordered_terminal_indices)
    if (not terminals or len(terminals) > 999999
            or any(type(value) is not int or not 0 < value <= 2**31 - 1
                   for value in terminals)
            or len(set(terminals)) != len(terminals)):
        raise ValueError("native checkpoint terminal roster is invalid")
    if type(process_count) is not int or not 0 < process_count <= 2**31 - 1:
        raise ValueError("native checkpoint process count is invalid")
    if type(global_true_dofs) is not int or not 0 < global_true_dofs <= 2**63 - 1:
        raise ValueError("native checkpoint global true-DOF count is invalid")
    partition = list(partition)
    if (len(partition) != process_count
            or any(type(value) is not int or not 0 <= value <= 2**31 - 1
                   for value in partition)
            or sum(partition) != global_true_dofs):
        raise ValueError("native checkpoint partition is invalid")
    expected = {
        "campaign_identity": campaign_identity,
        "campaign_identity_sha256": _sha256(campaign_identity.encode()),
        "ordered_terminals": terminals,
        "process_count": process_count,
        "global_true_dofs": global_true_dofs,
        "partition": partition,
        "numeric_abi": _numeric_abi(),
    }
    root_path, root_parent_descriptor, root_name, root_descriptor = _open_root(root)
    root_metadata = os.fstat(root_descriptor)
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    lock_descriptor = None
    try:
        root_entries = _list_directory(root_descriptor, "root")
        if (".writer.lock" not in root_entries
                or not stat.S_ISREG(root_entries[".writer.lock"].st_mode)):
            raise ValueError("native checkpoint writer lock is absent or invalid")
        if fcntl is None:
            raise ValueError("native checkpoint validation requires POSIX flock")
        lock_descriptor = os.open(
            ".writer.lock",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=root_descriptor,
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("native checkpoint writer lock is busy") from error
        if _stat_identity(os.fstat(lock_descriptor)) != _stat_identity(
                root_entries[".writer.lock"]):
            raise ValueError("native checkpoint writer lock was replaced")
        rhs_names = []
        for name, metadata in root_entries.items():
            if name == ".writer.lock":
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("native checkpoint writer lock is not regular")
                continue
            if (not stat.S_ISDIR(metadata.st_mode) or len(name) != 10
                    or not name.startswith("rhs-") or not name[4:].isdigit()):
                raise ValueError("native checkpoint root contains an unknown entry")
            rhs_names.append(name)
        rhs_names.sort()
        if rhs_names != [f"rhs-{rhs:06d}" for rhs in range(1, len(rhs_names) + 1)]:
            raise ValueError("native checkpoint RHS inventory is not contiguous")
        if len(rhs_names) > len(terminals):
            raise ValueError("native checkpoint RHS inventory exceeds terminals")
        rhs_inventory = []
        retained_bytes = 0
        for rhs, name in enumerate(rhs_names, 1):
            rhs_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            try:
                entries = _list_directory(rhs_descriptor, f"RHS {rhs}")
                expected_names = {"complete.json", "response.json", "response.bin"}
                for rank in range(process_count):
                    expected_names |= {f"rank-{rank:06d}.json", f"rank-{rank:06d}.bin"}
                if set(entries) != expected_names or any(
                        not stat.S_ISREG(metadata.st_mode) for metadata in entries.values()):
                    raise ValueError("native checkpoint RHS file inventory mismatch")
                common = {**expected, "terminal": terminals[rhs - 1]}
                marker_bytes = _read_file(
                    rhs_descriptor, "complete.json", "completion marker",
                    maximum_bytes=MAX_METADATA_BYTES,
                )
                marker = _load_json(marker_bytes, "completion marker")
                if set(marker) != COMPLETION_KEYS:
                    raise ValueError("native checkpoint completion fields mismatch")
                _validate_common(
                    marker, common, format_name=COMPLETION_FORMAT,
                    rhs=rhs, label="completion marker",
                )
                if not isinstance(marker["shards"], list) or len(marker["shards"]) != process_count:
                    raise ValueError("native checkpoint shard inventory mismatch")
                rhs_retained = len(marker_bytes)
                shard_records = []
                ownership = 0
                for rank in range(process_count):
                    stem = f"rank-{rank:06d}"
                    metadata_bytes = _read_file(
                        rhs_descriptor, f"{stem}.json", f"rank {rank} metadata",
                        maximum_bytes=MAX_METADATA_BYTES,
                    )
                    metadata = _load_json(metadata_bytes, f"rank {rank} metadata")
                    if set(metadata) != SHARD_KEYS:
                        raise ValueError("native checkpoint shard fields mismatch")
                    _validate_common(
                        metadata, common, format_name=SHARD_FORMAT,
                        rhs=rhs, label=f"rank {rank} metadata",
                    )
                    data_bytes = partition[rank] * 8
                    data = _read_file(
                        rhs_descriptor, f"{stem}.bin", f"rank {rank} data",
                        exact_bytes=data_bytes, maximum_bytes=data_bytes,
                    )
                    _decode_finite(data, partition[rank], f"rank {rank} data")
                    data_digest = _sha256(data)
                    if not _typed_equal(metadata, {
                            **{key: common[key] for key in common},
                            "format": SHARD_FORMAT, "rhs": rhs, "rank": rank,
                            "local_true_dofs": partition[rank],
                            "ownership_start": ownership,
                            "ownership_end": ownership + partition[rank],
                            "data_file": f"{stem}.bin", "data_bytes": data_bytes,
                            "data_sha256": data_digest}):
                        raise ValueError("native checkpoint shard metadata mismatch")
                    entry = marker["shards"][rank]
                    if set(entry) != SHARD_ENTRY_KEYS or not _typed_equal(entry, {
                            "rank": rank, "metadata_file": f"{stem}.json",
                            "metadata_bytes": len(metadata_bytes),
                            "metadata_sha256": _sha256(metadata_bytes),
                            "data_file": f"{stem}.bin", "data_bytes": len(data),
                            "data_sha256": data_digest}):
                        raise ValueError("native checkpoint completion shard binding mismatch")
                    rhs_retained += len(metadata_bytes) + len(data)
                    shard_records.append({
                        "rank": rank,
                        "metadata": _identity(root_path / name / f"{stem}.json", metadata_bytes),
                        "data": _identity(root_path / name / f"{stem}.bin", data),
                    })
                    ownership += partition[rank]
                response_metadata_bytes = _read_file(
                    rhs_descriptor, "response.json", "response metadata",
                    maximum_bytes=MAX_METADATA_BYTES,
                )
                response = _load_json(response_metadata_bytes, "response metadata")
                if set(response) != RESPONSE_KEYS:
                    raise ValueError("native checkpoint response fields mismatch")
                _validate_common(
                    response, common, format_name=RESPONSE_FORMAT,
                    rhs=rhs, label="response metadata",
                )
                response_data_bytes = len(terminals) * 8
                response_data = _read_file(
                    rhs_descriptor, "response.bin", "response data",
                    exact_bytes=response_data_bytes, maximum_bytes=response_data_bytes,
                )
                _decode_finite(response_data, len(terminals), "response data")
                response_digest = _sha256(response_data)
                if not _typed_equal(response, {
                        **{key: common[key] for key in common},
                        "format": RESPONSE_FORMAT, "rhs": rhs,
                        "column_size": len(terminals), "data_file": "response.bin",
                        "data_bytes": response_data_bytes,
                        "data_sha256": response_digest}):
                    raise ValueError("native checkpoint response metadata mismatch")
                if any(not _typed_equal(marker[name], value) for name, value in {
                        "response_metadata_file": "response.json",
                        "response_metadata_bytes": len(response_metadata_bytes),
                        "response_metadata_sha256": _sha256(response_metadata_bytes),
                        "response_data_file": "response.bin",
                        "response_data_bytes": len(response_data),
                        "response_data_sha256": response_digest}.items()):
                    raise ValueError("native checkpoint completion response binding mismatch")
                rhs_retained += len(response_metadata_bytes) + len(response_data)
                final_entries = _list_directory(rhs_descriptor, f"RHS {rhs} final")
                if not _same_directory(entries, final_entries):
                    raise ValueError("native checkpoint RHS changed during validation")
                retained_bytes += rhs_retained
                rhs_inventory.append({
                    "rhs": rhs, "terminal_index": terminals[rhs - 1],
                    "directory": str(root_path / name),
                    "completion": _identity(root_path / name / "complete.json", marker_bytes),
                    "response_metadata": _identity(
                        root_path / name / "response.json", response_metadata_bytes),
                    "response_data": _identity(
                        root_path / name / "response.bin", response_data),
                    "shards": shard_records,
                })
            finally:
                os.close(rhs_descriptor)
        final_root_entries = _list_directory(root_descriptor, "root final")
        final_path_metadata = os.stat(
            root_name, dir_fd=root_parent_descriptor, follow_symlinks=False,
        )
        if (not _same_directory(root_entries, final_root_entries)
                or (final_path_metadata.st_dev, final_path_metadata.st_ino)
                != root_identity):
            raise ValueError("native checkpoint root changed during validation")
    finally:
        if lock_descriptor is not None:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
        os.close(root_descriptor)
        os.close(root_parent_descriptor)
    payload = {
        "format": INVENTORY_FORMAT,
        "campaign_identity": campaign_identity,
        "campaign_identity_sha256": expected["campaign_identity_sha256"],
        "prefix": len(rhs_inventory),
        "ordered_terminal_indices": terminals,
        "process_count": process_count,
        "global_true_dofs": global_true_dofs,
        "partition": partition,
        "numeric_abi": expected["numeric_abi"],
        "root_identity": {"device": root_identity[0], "inode": root_identity[1]},
        "rhs": rhs_inventory,
        "retained_bytes": retained_bytes,
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}
