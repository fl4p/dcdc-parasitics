#!/usr/bin/env python3
"""Immutable, review-neutral identity for modified Palace source trees."""
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

try:
    from .provenance import (
        bytes_sha256, canonical_sha256, exclusive_publish_bytes,
        exclusive_publish_json,
    )
except ImportError:
    from provenance import (
        bytes_sha256, canonical_sha256, exclusive_publish_bytes,
        exclusive_publish_json,
    )


SOURCE_IDENTITY_FORMAT = "palace-source-identity-v1"
_SHA256_LENGTH = 64


def _git_bytes(source_directory, *args):
    try:
        return subprocess.run(
            ("git", "-C", str(source_directory), *args),
            check=True, capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise ValueError("Palace source identity git query failed") from error


def _git_optional_path(source_directory, *args):
    result = subprocess.run(
        ("git", "-C", str(source_directory), *args), capture_output=True,
    )
    if result.returncode == 1 and not result.stdout:
        return None
    if result.returncode != 0:
        raise ValueError("Palace source identity git query failed")
    try:
        value = result.stdout.decode().strip()
    except UnicodeError as error:
        raise ValueError("Palace source identity git path is not UTF-8") from error
    if not value:
        return None
    path = Path(value).expanduser()
    return (source_directory / path).absolute() if not path.is_absolute() else path.absolute()


def _effective_global_exclude(source_directory):
    configured = _git_optional_path(
        source_directory, "config", "--path", "--get", "core.excludesFile",
    )
    if configured is not None:
        return configured
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    if not base.is_absolute():
        raise ValueError("Palace source identity XDG config path is not absolute")
    return (base / "git" / "ignore").absolute()


def _source_path(name):
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ValueError("Palace source identity path is invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(
            part in ("", ".", "..") for part in path.parts):
        raise ValueError("Palace source identity path is invalid")
    return path


def _path_list(content):
    if not content:
        return []
    if not content.endswith(b"\0"):
        raise ValueError("Palace source identity git path list is malformed")
    try:
        names = content[:-1].decode().split("\0")
    except UnicodeError as error:
        raise ValueError("Palace source identity path is not UTF-8") from error
    for name in names:
        _source_path(name)
    if names != sorted(set(names)):
        raise ValueError("Palace source identity paths are not unique and sorted")
    return names


def _absolute_parts(path, *, label):
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if any(part in (".", "..") for part in path.parts):
        raise ValueError(f"{label} path is unsafe")
    return path, path.parts[1:]


def _open_anchored(path, *, label, directory=False):
    path, parts = _absolute_parts(path, label=label)
    if not parts:
        raise ValueError(f"{label} path is unsafe")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
    current = os.open(path.anchor, directory_flags)
    try:
        for component in parts[:-1]:
            following = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = following
        flags = directory_flags if directory else os.O_RDONLY | nofollow
        return os.open(parts[-1], flags, dir_fd=current)
    except OSError as error:
        raise ValueError(f"{label} is unavailable or unsafe") from error
    finally:
        os.close(current)


def _validate_directory(path, *, label):
    descriptor = _open_anchored(path, label=label, directory=True)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"{label} is not a directory")
    finally:
        os.close(descriptor)


def _regular_bytes(path, *, label):
    descriptor = _open_anchored(path, label=label)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} is not a regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError(f"{label} changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _strict_manifest(path):
    def strict_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Palace source identity contains duplicate key {key!r}")
            value[key] = item
        return value

    try:
        return json.loads(
            _regular_bytes(path, label="Palace source identity"),
            object_pairs_hook=strict_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid Palace source identity: {error}") from error


def _artifact(output_directory, stem, content):
    digest = bytes_sha256(content)
    path = output_directory / f"{stem}.{digest}.bin"
    if path.exists():
        if path.is_symlink() or _regular_bytes(path, label=stem) != content:
            raise ValueError(f"Palace source identity {stem} artifact conflicts")
    else:
        exclusive_publish_bytes(path, content)
    return {"path": str(path), "sha256": digest, "bytes": len(content)}


def _path_is_absent(path, *, label):
    path, parts = _absolute_parts(path, label=label)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
    current = os.open(path.anchor, directory_flags)
    try:
        for index, component in enumerate(parts):
            try:
                metadata = os.stat(
                    component, dir_fd=current, follow_symlinks=False,
                )
            except FileNotFoundError:
                return True
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"{label} is unavailable or unsafe")
            if index == len(parts) - 1:
                return False
            following = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = following
    finally:
        os.close(current)


def _policy_file(output_directory, source_path, *, stem):
    source_path = Path(source_path).absolute()
    try:
        content = _regular_bytes(source_path, label=stem)
    except ValueError:
        if not _path_is_absent(source_path, label=stem):
            raise
        return {"source_path": str(source_path), "state": "absent"}
    return {
        "source_path": str(source_path), "state": "present",
        "artifact": _artifact(output_directory, stem, content),
    }


def _validate_policy_file(record, *, label):
    if not isinstance(record, dict) or set(record) not in (
            {"source_path", "state"}, {"source_path", "state", "artifact"}):
        raise ValueError(f"Palace source identity {label} policy is malformed")
    if record["state"] == "absent" and set(record) == {"source_path", "state"}:
        if not _path_is_absent(record["source_path"], label=label):
            raise ValueError(f"Palace source identity {label} policy changed")
        return
    if record["state"] != "present" or "artifact" not in record:
        raise ValueError(f"Palace source identity {label} policy is malformed")
    source = _regular_bytes(record["source_path"], label=label)
    artifact = _validate_artifact(record["artifact"], label=f"{label} artifact")
    if source != artifact:
        raise ValueError(f"Palace source identity {label} policy changed")


def _validate_artifact(record, *, label):
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
        raise ValueError(f"Palace source identity {label} record is malformed")
    if (not isinstance(record["path"], str)
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != _SHA256_LENGTH
            or not isinstance(record["bytes"], int)
            or isinstance(record["bytes"], bool) or record["bytes"] < 0):
        raise ValueError(f"Palace source identity {label} record is malformed")
    content = _regular_bytes(record["path"], label=label)
    if len(content) != record["bytes"] or bytes_sha256(content) != record["sha256"]:
        raise ValueError(f"Palace source identity {label} artifact changed")
    return content


def _file_record(path, *, label):
    content = _regular_bytes(path, label=label)
    return {"sha256": bytes_sha256(content), "bytes": len(content)}


def _validate_file_record(record, path, *, label):
    if not isinstance(record, dict) or set(record) != {"sha256", "bytes"}:
        raise ValueError(f"Palace source identity {label} record is malformed")
    actual = _file_record(path, label=label)
    if record != actual:
        raise ValueError(f"Palace source identity {label} changed")


def write_palace_source_identity(output_directory, *, source_directory,
                                  declared_untracked):
    source_input = Path(source_directory).absolute()
    _validate_directory(source_input, label="Palace source directory")
    source_directory = source_input
    output_directory = Path(output_directory).absolute()
    try:
        output_directory.relative_to(source_directory)
    except ValueError:
        pass
    else:
        raise ValueError("Palace source identity output cannot be inside the source tree")
    if not isinstance(declared_untracked, (list, tuple)):
        raise ValueError("declared untracked Palace sources must be a list")
    declared = [str(_source_path(name)) for name in declared_untracked]
    if declared != sorted(set(declared)):
        raise ValueError("declared untracked Palace sources must be unique and sorted")
    observed_untracked = _path_list(_git_bytes(
        source_directory, "ls-files", "-z", "--others", "--exclude-standard",
    ))
    if observed_untracked != declared:
        raise ValueError("Palace source untracked-file declaration mismatch")
    tracked_names = _path_list(_git_bytes(
        source_directory, "diff", "--name-only", "-z", "HEAD", "--",
    ))
    output_directory.mkdir(parents=True, exist_ok=True)
    _validate_directory(output_directory, label="Palace source identity output")
    patch = _git_bytes(
        source_directory, "diff", "--binary", "--no-ext-diff", "HEAD", "--",
    )
    submodules = _git_bytes(source_directory, "submodule", "status", "--recursive")
    repository_exclude = _git_optional_path(
        source_directory, "rev-parse", "--git-path", "info/exclude",
    )
    global_exclude = _effective_global_exclude(source_directory)
    exclude_policy = {
        "repository": _policy_file(
            output_directory, repository_exclude, stem="palace-source-repository-exclude",
        ),
        "global": _policy_file(
            output_directory, global_exclude, stem="palace-source-global-exclude",
        ),
    }
    tracked = {}
    for name in tracked_names:
        candidate = source_directory / _source_path(name)
        if candidate.exists() or candidate.is_symlink():
            tracked[name] = {
                "state": "present",
                **_file_record(candidate, label=f"tracked source {name}"),
            }
        else:
            tracked[name] = {"state": "deleted"}
    untracked = {}
    for name in observed_untracked:
        candidate = source_directory / _source_path(name)
        content = _regular_bytes(candidate, label=f"untracked source {name}")
        blob = _artifact(output_directory, "palace-source-blob", content)
        untracked[name] = {
            "sha256": blob["sha256"], "bytes": blob["bytes"],
            "blob": blob["path"],
        }
    identity = {
        "format": SOURCE_IDENTITY_FORMAT,
        "source_directory": str(source_directory),
        "source_commit": _git_bytes(
            source_directory, "rev-parse", "HEAD",
        ).decode().strip(),
        "tracked_patch": _artifact(output_directory, "palace-source-patch", patch),
        "submodule_status": _artifact(
            output_directory, "palace-source-submodules", submodules,
        ),
        "exclude_policy": exclude_policy,
        "tracked_files": tracked,
        "declared_untracked": declared,
        "untracked_files": untracked,
        "authority": None,
    }
    manifest = {
        "identity": identity,
        "content_sha256": canonical_sha256(identity),
    }
    path = output_directory / f"palace-source-identity.{manifest['content_sha256']}.json"
    exclusive_publish_json(path, manifest)
    return path


def validate_palace_source_identity(path):
    path = Path(path).absolute()
    raw = _strict_manifest(path)
    if not isinstance(raw, dict) or set(raw) != {"identity", "content_sha256"}:
        raise ValueError("Palace source identity manifest is malformed")
    identity = raw["identity"]
    expected_keys = {
        "format", "source_directory", "source_commit", "tracked_patch",
        "submodule_status", "exclude_policy", "tracked_files", "declared_untracked",
        "untracked_files", "authority",
    }
    if (not isinstance(identity, dict) or set(identity) != expected_keys
            or identity.get("format") != SOURCE_IDENTITY_FORMAT
            or identity.get("authority") is not None
            or raw["content_sha256"] != canonical_sha256(identity)):
        raise ValueError("Palace source identity content mismatch")
    expected_name = f"palace-source-identity.{raw['content_sha256']}.json"
    if path.name != expected_name:
        raise ValueError("Palace source identity filename does not bind content")
    source_directory = Path(identity["source_directory"])
    if not source_directory.is_absolute():
        raise ValueError("Palace source identity source root is unsafe")
    _validate_directory(source_directory, label="Palace source identity source root")
    commit = _git_bytes(source_directory, "rev-parse", "HEAD").decode().strip()
    if identity["source_commit"] != commit:
        raise ValueError("Palace source identity commit changed")
    patch = _validate_artifact(identity["tracked_patch"], label="tracked patch")
    if patch != _git_bytes(
            source_directory, "diff", "--binary", "--no-ext-diff", "HEAD", "--"):
        raise ValueError("Palace source identity tracked patch changed")
    submodules = _validate_artifact(
        identity["submodule_status"], label="submodule status",
    )
    if submodules != _git_bytes(source_directory, "submodule", "status", "--recursive"):
        raise ValueError("Palace source identity submodules changed")
    exclude_policy = identity["exclude_policy"]
    if not isinstance(exclude_policy, dict) or set(exclude_policy) != {
            "repository", "global"}:
        raise ValueError("Palace source identity exclude policy is malformed")
    repository_exclude = _git_optional_path(
        source_directory, "rev-parse", "--git-path", "info/exclude",
    )
    if (not isinstance(exclude_policy["repository"], dict)
            or exclude_policy["repository"].get("source_path")
            != str(repository_exclude)):
        raise ValueError("Palace source identity repository exclude was rebound")
    _validate_policy_file(exclude_policy["repository"], label="repository exclude")
    global_exclude = _effective_global_exclude(source_directory)
    if (not isinstance(exclude_policy["global"], dict)
            or exclude_policy["global"].get("source_path")
            != str(global_exclude)):
        raise ValueError("Palace source identity global exclude was rebound")
    _validate_policy_file(exclude_policy["global"], label="global exclude")
    declared = identity["declared_untracked"]
    if not isinstance(declared, list):
        raise ValueError("Palace source identity declaration is malformed")
    for name in declared:
        _source_path(name)
    if declared != sorted(set(declared)):
        raise ValueError("Palace source identity declaration is malformed")
    observed = _path_list(_git_bytes(
        source_directory, "ls-files", "-z", "--others", "--exclude-standard",
    ))
    if observed != declared:
        raise ValueError("Palace source identity untracked files changed")
    tracked_names = _path_list(_git_bytes(
        source_directory, "diff", "--name-only", "-z", "HEAD", "--",
    ))
    tracked = identity["tracked_files"]
    if not isinstance(tracked, dict) or sorted(tracked) != tracked_names:
        raise ValueError("Palace source identity tracked-file roster changed")
    for name, record in tracked.items():
        candidate = source_directory / _source_path(name)
        if record == {"state": "deleted"}:
            if candidate.exists() or candidate.is_symlink():
                raise ValueError(f"Palace tracked source {name} changed")
            continue
        if not isinstance(record, dict) or record.get("state") != "present":
            raise ValueError(f"Palace tracked source {name} record is malformed")
        _validate_file_record(
            {key: value for key, value in record.items() if key != "state"},
            candidate, label=f"tracked source {name}",
        )
    untracked = identity["untracked_files"]
    if not isinstance(untracked, dict) or sorted(untracked) != declared:
        raise ValueError("Palace source identity untracked-file roster changed")
    for name, record in untracked.items():
        if not isinstance(record, dict) or set(record) != {"sha256", "bytes", "blob"}:
            raise ValueError(f"Palace untracked source {name} record is malformed")
        blob = _validate_artifact(
            {"path": record["blob"], "sha256": record["sha256"],
             "bytes": record["bytes"]},
            label=f"untracked blob {name}",
        )
        source = _regular_bytes(
            source_directory / _source_path(name), label=f"untracked source {name}",
        )
        if source != blob:
            raise ValueError(f"Palace untracked source {name} changed")
    return raw
