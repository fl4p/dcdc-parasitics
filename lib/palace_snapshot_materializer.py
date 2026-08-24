#!/usr/bin/env python3
"""Privileged materializer for immutable Palace execution snapshots."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import sys

import fcntl


REQUEST_FORMAT = "palace-execution-snapshot-request-v1"
SNAPSHOT_FORMAT = "palace-execution-snapshot-v2"
ATTESTATION_NAME = "snapshot.json"
HEX = frozenset("0123456789abcdef")


def _canonical_bytes(value):
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) + "\n").encode()


def _canonical_sha256(value):
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _strict_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("snapshot request contains a duplicate key")
        value[name] = item
    return value


def _digest(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in HEX for character in value)):
        raise ValueError(f"snapshot {label} is not a lowercase SHA-256")
    return value


def _simple_name(value, label):
    if (not isinstance(value, str) or not value or len(value) > 255
            or PurePosixPath(value).name != value or value in {".", ".."}):
        raise ValueError(f"snapshot {label} is invalid")
    return value


def _read_descriptor(descriptor):
    content = bytearray()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return bytes(content)
        content.extend(chunk)


def _open_owned_regular(path, owner_uid, label):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"snapshot {label} path is not absolute")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError(f"snapshot {label} is unavailable") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner_uid:
        os.close(descriptor)
        raise ValueError(f"snapshot {label} owner or type is invalid")
    return descriptor


def _read_request(path, client_uid):
    descriptor = _open_owned_regular(path, client_uid, "request")
    try:
        content = _read_descriptor(descriptor)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(content, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("snapshot request is not strict JSON") from error
    return value, hashlib.sha256(content).hexdigest()


def _validate_authority_root(root, client_uid, service_uid):
    root = Path(root).absolute()
    cursor = root
    while True:
        metadata = cursor.lstat()
        if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise ValueError("snapshot authority ancestry is mutable by its client")
        if cursor == root and metadata.st_uid != service_uid:
            raise ValueError("snapshot authority root is not service-owned")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    metadata = os.fstat(descriptor)
    current = os.lstat(root)
    if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
        os.close(descriptor)
        raise ValueError("snapshot authority root changed during validation")
    return root, descriptor


def validate_request(value, *, client_uid, client_gid=None):
    if client_gid is None:
        client_gid = os.getegid()
    if not isinstance(value, dict) or set(value) != {
            "format", "snapshot_id", "materializer_sha256", "client_uid",
            "client_gid", "workload_sha256", "output_name", "inputs",
            "content_sha256"}:
        raise ValueError("snapshot request schema mismatch")
    payload = {name: item for name, item in value.items()
               if name != "content_sha256"}
    if (value["format"] != REQUEST_FORMAT
            or value["content_sha256"] != _canonical_sha256(payload)
            or type(value["client_uid"]) is not int
            or value["client_uid"] != client_uid
            or type(value["client_gid"]) is not int
            or value["client_gid"] != client_gid):
        raise ValueError("snapshot request identity mismatch")
    _digest(value["snapshot_id"], "ID")
    _digest(value["materializer_sha256"], "materializer digest")
    _digest(value["workload_sha256"], "workload digest")
    if value["materializer_sha256"] != hashlib.sha256(
            Path(__file__).read_bytes()).hexdigest():
        raise ValueError("snapshot materializer differs from the requested implementation")
    output_name = _simple_name(value["output_name"], "output name")
    inputs = value["inputs"]
    if not isinstance(inputs, list) or len(inputs) < 4:
        raise ValueError("snapshot request input roster is invalid")
    roles = []
    targets = []
    normalized = []
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {
                "role", "source", "source_sha256", "target", "mode"}:
            raise ValueError("snapshot request input schema mismatch")
        role = item["role"]
        if role not in {"config", "mesh", "executable", "solver_binary", "mpi_launcher"}:
            raise ValueError("snapshot request input role is invalid")
        source = Path(item["source"])
        if not source.is_absolute():
            raise ValueError("snapshot request source is not absolute")
        _digest(item["source_sha256"], "input digest")
        target = PurePosixPath(item["target"])
        parts = target.parts
        runtime = role in {"executable", "solver_binary", "mpi_launcher"}
        if ((runtime and (len(parts) != 2 or parts[0] != "bin"))
                or (not runtime and len(parts) != 1)
                or any(_simple_name(part, "target component") != part for part in parts)
                or item["mode"] != (0o550 if runtime else 0o440)):
            raise ValueError("snapshot request target or mode is invalid")
        roles.append(role)
        targets.append(str(target))
        normalized.append(dict(item))
    if (roles.count("config") != 1 or roles.count("mesh") != 1
            or roles.count("executable") != 1
            or roles.count("mpi_launcher") != 1
            or len(targets) != len(set(targets))):
        raise ValueError("snapshot request role or target roster is invalid")
    root_names = {"bin", output_name, ATTESTATION_NAME}
    root_names.update(target for target in targets if "/" not in target)
    expected_root_count = 3 + sum("/" not in target for target in targets)
    if len(root_names) != expected_root_count:
        raise ValueError("snapshot request namespace collides")
    return {**value, "inputs": normalized}


def _copy_input(item, root_fd, bin_fd, client_uid, client_gid, root):
    source_fd = _open_owned_regular(item["source"], client_uid, "input")
    target = PurePosixPath(item["target"])
    parent_fd = bin_fd if len(target.parts) == 2 else root_fd
    name = target.name
    target_fd = None
    digest = hashlib.sha256()
    try:
        target_fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            item["mode"], dir_fd=parent_fd,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        if digest.hexdigest() != item["source_sha256"]:
            raise ValueError("snapshot input changed or differs from its request")
        os.fchown(target_fd, 0, client_gid)
        os.fchmod(target_fd, item["mode"])
        os.fsync(target_fd)
    finally:
        os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)
    snapshot = root / target
    return {
        "role": item["role"],
        "source": item["source"],
        "source_sha256": item["source_sha256"],
        "snapshot": str(snapshot),
        "snapshot_sha256": item["source_sha256"],
    }


def _publish_bytes(directory_fd, name, content, mode, group_id):
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode, dir_fd=directory_fd,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchown(descriptor, 0, group_id)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def materialize(request_path, authority_root, *, client_uid, client_gid):
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise ValueError("snapshot materializer requires POSIX ownership semantics")
    service_uid = os.geteuid()
    if service_uid != 0:
        raise ValueError("snapshot materializer requires root authority")
    if (type(client_uid) is not int or client_uid <= 0 or client_uid == service_uid
            or type(client_gid) is not int or client_gid <= 0):
        raise ValueError("snapshot materializer client identity is invalid")
    request, request_file_sha256 = _read_request(request_path, client_uid)
    request = validate_request(
        request, client_uid=client_uid, client_gid=client_gid,
    )
    authority_root, authority_fd = _validate_authority_root(
        authority_root, client_uid, service_uid,
    )
    snapshot_name = f"snapshot.{request['snapshot_id']}"
    staging_name = f".pending.{request['snapshot_id']}.{secrets.token_hex(16)}"
    staging_path = authority_root / staging_name
    root_fd = bin_fd = output_fd = None
    published = False
    try:
        fcntl.flock(authority_fd, fcntl.LOCK_EX)
        try:
            os.stat(snapshot_name, dir_fd=authority_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("snapshot identity already exists")
        os.mkdir(staging_name, 0o700, dir_fd=authority_fd)
        root_fd = os.open(
            staging_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=authority_fd,
        )
        os.fchown(root_fd, 0, client_gid)
        os.mkdir("bin", 0o700, dir_fd=root_fd)
        bin_fd = os.open(
            "bin", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        os.fchown(bin_fd, 0, client_gid)
        root = authority_root / snapshot_name
        inputs = [
            _copy_input(item, root_fd, bin_fd, client_uid, client_gid, root)
            for item in request["inputs"]
        ]
        os.mkdir(request["output_name"], 0o700, dir_fd=root_fd)
        output_fd = os.open(
            request["output_name"],
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        os.fchown(output_fd, client_uid, client_gid)
        os.fchmod(output_fd, 0o700)
        os.fsync(output_fd)
        payload = {
            "format": SNAPSHOT_FORMAT,
            "snapshot_id": request["snapshot_id"],
            "materializer_sha256": request["materializer_sha256"],
            "client_uid": client_uid,
            "client_gid": client_gid,
            "root": str(root),
            "output": str(root / request["output_name"]),
            "workload_sha256": request["workload_sha256"],
            "request_sha256": request["content_sha256"],
            "request_file_sha256": request_file_sha256,
            "inputs": inputs,
        }
        record = {**payload, "content_sha256": _canonical_sha256(payload)}
        _publish_bytes(
            root_fd, ATTESTATION_NAME, _canonical_bytes(record), 0o440,
            client_gid,
        )
        os.fchmod(bin_fd, 0o550)
        os.fchmod(root_fd, 0o550)
        os.fsync(bin_fd)
        os.fsync(root_fd)
        os.rename(
            staging_name, snapshot_name,
            src_dir_fd=authority_fd, dst_dir_fd=authority_fd,
        )
        os.fsync(authority_fd)
        published = True
        return root / ATTESTATION_NAME
    finally:
        if output_fd is not None:
            os.close(output_fd)
        if bin_fd is not None:
            os.close(bin_fd)
        if root_fd is not None:
            os.close(root_fd)
        if not published and staging_path.exists():
            shutil.rmtree(staging_path)
        fcntl.flock(authority_fd, fcntl.LOCK_UN)
        os.close(authority_fd)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("authority_root")
    parser.add_argument("--client-uid", type=int, required=True)
    parser.add_argument("--client-gid", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        path = materialize(
            args.request, args.authority_root,
            client_uid=args.client_uid, client_gid=args.client_gid,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
