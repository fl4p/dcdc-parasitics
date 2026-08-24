#!/usr/bin/env python3
"""Content-addressed provenance helpers."""
import hashlib
import json
import os
from pathlib import Path
import secrets


def bytes_sha256(content):
    return hashlib.sha256(content).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_json_file(path, *, label="JSON document"):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is unavailable or unsafe")

    def strict_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label} contains duplicate key {key!r}")
            value[key] = item
        return value

    try:
        return json.loads(path.read_bytes(), object_pairs_hook=strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid {label}: {error}") from error


def canonical_sha256(value):
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(content).hexdigest()


def canonical_equal(left, right):
    try:
        return canonical_sha256(left) == canonical_sha256(right)
    except (TypeError, ValueError):
        return False


def exclusive_publish_bytes(path, content):
    path = Path(path)
    if type(content) is not bytes:
        raise ValueError("published content must be bytes")
    if path.name in ("", ".", "..") or path.parent.is_symlink():
        raise ValueError("publication path is unsafe")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600
        )
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while publishing artifact")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path
    directory = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
    )
    temporary = f".{path.name}.tmp.{secrets.token_hex(16)}"
    descriptor = None
    linked = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
            dir_fd=directory,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while publishing artifact")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(
            temporary,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        linked = True
        os.fsync(directory)
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not linked:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)
    return path


def exclusive_publish_json(path, value):
    content = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False)
               + "\n").encode()
    return exclusive_publish_bytes(path, content)
