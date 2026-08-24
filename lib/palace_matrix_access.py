#!/usr/bin/env python3
"""Unforgeable matrix capability for completed canonical Palace campaigns."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat

if __package__:
    from .palace_ledger_v2 import CanonicalLedgerPublicationV2
    from .provenance import bytes_sha256, canonical_equal, canonical_sha256, file_sha256
else:
    from palace_ledger_v2 import CanonicalLedgerPublicationV2
    from provenance import bytes_sha256, canonical_equal, canonical_sha256, file_sha256


_MATRIX_ACCESS_NONCE = object()
_MATRIX_LIMIT = 64 * 1024 * 1024


@dataclass(frozen=True)
class PalaceMatrixAccess:
    campaign_sha256: str
    head_sha256: str
    config_sha256: str
    terminal_names: tuple[str, ...]
    matrices: tuple[tuple[str, str, str], ...]
    _ledger: CanonicalLedgerPublicationV2
    _nonce: object

    def __post_init__(self):
        if (self._nonce is not _MATRIX_ACCESS_NONCE
                or not isinstance(self._ledger, CanonicalLedgerPublicationV2)):
            raise ValueError("Palace matrix access capability is invalid")


def _validate_record(record):
    unsigned = dict(record) if isinstance(record, dict) else {}
    digest = unsigned.pop("content_sha256", None)
    if (not isinstance(record, dict) or set(record) != {
            "format", "campaign_sha256", "head_sha256", "workload_sha256",
            "config_sha256", "config_manifest_sha256", "ordered_terminals",
            "final_attempt_sha256", "matrices", "content_sha256"}
            or record["format"] != "palace-checkpoint-matrix-access-record-v2"
            or digest != canonical_sha256(unsigned)):
        raise ValueError("Palace matrix access record is invalid")
    return record


def _manifest_identity(record, manifest):
    terminal_names = tuple(
        terminal["name"] for terminal in record["ordered_terminals"]
    )
    manifest_terminals = [
        {
            "index": terminal.index,
            "name": terminal.name,
            "attribute": terminal.attribute,
        }
        for terminal in manifest.terminals
    ]
    manifest_bytes = (
        json.dumps(manifest.raw, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    config_path = Path(manifest.config_path)
    if (config_path.is_symlink() or not config_path.is_file()
            or terminal_names != manifest.terminal_names
            or not canonical_equal(
                manifest_terminals, record["ordered_terminals"])
            or record["config_sha256"] != file_sha256(config_path)
            or record["config_manifest_sha256"]
            != bytes_sha256(manifest_bytes)):
        raise ValueError("Palace matrix capability differs from its config identity")
    return terminal_names


def checkpoint_matrix_access(ledger, manifest, *, raw_path, standard_path):
    if not isinstance(ledger, CanonicalLedgerPublicationV2):
        raise ValueError("Palace matrix access requires a canonical v2 ledger")
    record = _validate_record(ledger.matrix_access_record())
    terminal_names = _manifest_identity(record, manifest)
    requested = {
        "raw": (Path(raw_path), record["matrices"]["raw_matrix"]),
        "standard": (
            Path(standard_path), record["matrices"]["standard_matrix"],
        ),
    }
    matrices = []
    for role, (supplied_path, identity) in requested.items():
        identity_path = (
            Path(identity["path"])
            if isinstance(identity, dict) and "path" in identity else None
        )
        if (not isinstance(identity, dict)
                or set(identity) != {"path", "sha256"}
                or supplied_path.is_symlink()
                or identity_path is None or identity_path.is_symlink()
                or str(supplied_path.resolve()) != str(identity_path.resolve())):
            raise ValueError("Palace matrix capability artifact identity mismatch")
        path = supplied_path.resolve()
        if (not path.is_file()
                or file_sha256(path) != identity["sha256"]):
            raise ValueError("Palace matrix capability artifact identity mismatch")
        matrices.append((role, str(path), identity["sha256"]))
    return PalaceMatrixAccess(
        campaign_sha256=record["campaign_sha256"],
        head_sha256=record["head_sha256"],
        config_sha256=canonical_sha256(manifest.raw),
        terminal_names=terminal_names,
        matrices=tuple(matrices),
        _ledger=ledger,
        _nonce=_MATRIX_ACCESS_NONCE,
    )


def validate_matrix_access(access, manifest):
    if not isinstance(access, PalaceMatrixAccess):
        raise ValueError("Palace matrix access capability is invalid")
    record = _validate_record(access._ledger.matrix_access_record())
    terminal_names = _manifest_identity(record, manifest)
    expected_matrices = {
        role: {"path": path, "sha256": digest}
        for role, path, digest in access.matrices
    }
    if (record["campaign_sha256"] != access.campaign_sha256
            or record["head_sha256"] != access.head_sha256
            or canonical_sha256(manifest.raw) != access.config_sha256
            or terminal_names != access.terminal_names
            or record["matrices"] != {
                "raw_matrix": expected_matrices.get("raw"),
                "standard_matrix": expected_matrices.get("standard"),
            }):
        raise ValueError("Palace matrix access capability is stale")
    return access


def read_attested_matrix(path, manifest, *, matrix_name, matrix_access):
    validate_matrix_access(matrix_access, manifest)
    matches = [
        record for record in matrix_access.matrices if record[0] == matrix_name
    ]
    supplied_path = Path(path)
    if supplied_path.is_symlink():
        raise ValueError("Palace matrix is unavailable or unsafe")
    path = supplied_path.resolve()
    if len(matches) != 1 or str(path) != matches[0][1]:
        raise ValueError("Palace matrix differs from the attested campaign artifact")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MATRIX_LIMIT:
            raise ValueError("Palace matrix is not a bounded regular file")
        content = b""
        while len(content) < before.st_size:
            chunk = os.read(descriptor, before.st_size - len(content))
            if not chunk:
                raise ValueError("Palace matrix is truncated")
            content += chunk
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or bytes_sha256(content) != matches[0][2]):
            raise ValueError("Palace matrix differs from the attested campaign artifact")
        return content
    except OSError as error:
        raise ValueError(f"Palace matrix is unavailable or unsafe: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
