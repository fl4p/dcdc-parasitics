#!/usr/bin/env python3
"""External canonical-head compare-and-swap authority for Palace campaigns."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat

if __package__:
    from .provenance import canonical_sha256
else:
    from provenance import canonical_sha256

try:
    import fcntl
except ImportError:  # pragma: no cover - intentionally POSIX-only
    fcntl = None


FORMAT = "palace-checkpoint-canonical-head-v1"
INSTANCE_FORMAT = "palace-checkpoint-authority-instance-v1"
INSTANCE_FILE = ".authority-instance.json"


def _digest(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"canonical head {label} is not a lowercase SHA-256")
    return value


def _key(value):
    if type(value) is not bytes or len(value) < 32:
        raise ValueError("canonical head authority key must contain at least 32 bytes")
    return value


def _canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()


def _strict_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("canonical head record contains a duplicate key")
        value[name] = item
    return value


def _client_cannot_mutate(metadata, client_uid):
    return (metadata.st_uid != client_uid
            and not stat.S_IMODE(metadata.st_mode) & 0o022)


def _validate_root_boundary(root, client_uid):
    cursor = Path(root).absolute()
    while True:
        metadata = cursor.lstat()
        if (stat.S_ISLNK(metadata.st_mode)
                or not _client_cannot_mutate(metadata, client_uid)):
            raise ValueError("canonical head authority root is mutable by its client UID")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent


class CanonicalHeadAuthority:
    def __init__(self, root, *, authority_id, authority_key, client_uid):
        if fcntl is None or os.name != "posix":
            raise ValueError("canonical head authority requires POSIX flock semantics")
        if (not isinstance(authority_id, str) or not authority_id
                or len(authority_id) > 128):
            raise ValueError("canonical head authority ID is invalid")
        if type(client_uid) is not int or client_uid <= 0:
            raise ValueError("canonical head authority client UID is invalid")
        supplied = Path(root)
        if supplied.is_symlink():
            raise ValueError("canonical head authority root must not be a symlink")
        self.root = supplied.absolute()
        self.authority_id = authority_id
        self.client_uid = client_uid
        self._key = _key(authority_key)
        self.key_fingerprint = hashlib.sha256(self._key).hexdigest()
        _validate_root_boundary(self.root, client_uid)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            self._root_fd = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | nofollow,
            )
        except OSError as error:
            raise ValueError(f"canonical head authority root is unavailable: {error}") from error
        root_metadata = os.fstat(self._root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode):
            os.close(self._root_fd)
            raise ValueError("canonical head authority root is invalid")
        self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        self._instance = self._load_or_create_instance()

    def close(self):
        descriptor = getattr(self, "_root_fd", None)
        if descriptor is not None:
            os.close(descriptor)
            self._root_fd = None

    def __del__(self):
        try:
            self.close()
        except OSError:
            pass

    def _verify_root(self):
        _validate_root_boundary(self.root, self.client_uid)
        metadata = self.root.stat(follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != self._root_identity:
            raise ValueError("canonical head authority root was replaced")

    def _instance_mac(self, value):
        return hmac.new(
            self._key, b"authority-instance\0" + _canonical_bytes(value),
            hashlib.sha256,
        ).hexdigest()

    def _load_or_create_instance(self):
        payload = {
            "format": INSTANCE_FORMAT,
            "authority_id": self.authority_id,
            "root_sha256": hashlib.sha256(str(self.root).encode()).hexdigest(),
            "key_fingerprint": self.key_fingerprint,
            "client_uid": self.client_uid,
            "instance_nonce": secrets.token_hex(32),
        }
        if not (self.root / INSTANCE_FILE).exists():
            with_mac = {**payload, "authority_mac": self._instance_mac(payload)}
            self._publish(
                INSTANCE_FILE,
                {**with_mac, "content_sha256": canonical_sha256(with_mac)},
                exclusive=True,
            )
        value = self._read_document(INSTANCE_FILE, "instance")
        if not isinstance(value, dict) or set(value) != {
                "format", "authority_id", "root_sha256", "key_fingerprint",
                "client_uid", "instance_nonce", "authority_mac", "content_sha256"}:
            raise ValueError("canonical head authority instance schema mismatch")
        unsigned = dict(value)
        digest = unsigned.pop("content_sha256")
        authority_mac = unsigned.pop("authority_mac")
        if (unsigned["format"] != INSTANCE_FORMAT
                or unsigned["authority_id"] != self.authority_id
                or unsigned["root_sha256"] != payload["root_sha256"]
                or unsigned["key_fingerprint"] != self.key_fingerprint
                or unsigned["client_uid"] != self.client_uid
                or not isinstance(unsigned["instance_nonce"], str)
                or len(unsigned["instance_nonce"]) != 64
                or not hmac.compare_digest(
                    authority_mac, self._instance_mac(unsigned))
                or digest != canonical_sha256({**unsigned, "authority_mac": authority_mac})):
            raise ValueError("canonical head authority instance mismatch")
        return value

    @property
    def identity(self):
        return {
            "authority_id": self.authority_id,
            "root": str(self.root),
            "root_sha256": hashlib.sha256(str(self.root).encode()).hexdigest(),
            "instance_sha256": self._instance["content_sha256"],
            "key_fingerprint": self.key_fingerprint,
            "client_uid": self.client_uid,
        }

    def _names(self, campaign_sha256):
        campaign = _digest(campaign_sha256, "campaign digest")
        return campaign, f"{campaign}.head.json", f"{campaign}.lock"

    def _mac(self, value):
        return hmac.new(
            self._key, b"canonical-head\0" + _canonical_bytes(value), hashlib.sha256,
        ).hexdigest()

    def _signed(self, payload):
        with_mac = {**payload, "authority_mac": self._mac(payload)}
        return {**with_mac, "content_sha256": canonical_sha256(with_mac)}

    def _validate(self, value, campaign_sha256):
        if not isinstance(value, dict) or set(value) != {
                "format", "authority_id", "campaign_sha256", "sequence",
                "head_sha256", "entry_sha256", "authority_mac", "content_sha256"}:
            raise ValueError("canonical head record schema mismatch")
        unsigned = dict(value)
        content = unsigned.pop("content_sha256")
        authority_mac = unsigned.pop("authority_mac")
        if (value["format"] != FORMAT
                or value["authority_id"] != self.authority_id
                or value["campaign_sha256"] != campaign_sha256
                or type(value["sequence"]) is not int or value["sequence"] < 0
                or (value["entry_sha256"] is None) != (value["sequence"] == 0)
                or not isinstance(authority_mac, str)
                or not hmac.compare_digest(authority_mac, self._mac(unsigned))
                or content != canonical_sha256({**unsigned, "authority_mac": authority_mac})):
            raise ValueError("canonical head record authority mismatch")
        _digest(value["head_sha256"], "head digest")
        if value["entry_sha256"] is not None:
            _digest(value["entry_sha256"], "entry digest")
        return dict(value)

    @contextmanager
    def _lock(self, lock_name, *, create):
        if self._root_fd is None:
            raise ValueError("canonical head authority is closed")
        self._verify_root()
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        flags = os.O_RDWR | nofollow | (os.O_CREAT if create else 0)
        try:
            descriptor = os.open(lock_name, flags, 0o600, dir_fd=self._root_fd)
        except OSError as error:
            raise ValueError(f"canonical head lock is unavailable: {error}") from error
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = os.stat(
                lock_name, dir_fd=self._root_fd, follow_symlinks=False,
            )
            if (not stat.S_ISREG(descriptor_stat.st_mode)
                    or (descriptor_stat.st_dev, descriptor_stat.st_ino)
                    != (path_stat.st_dev, path_stat.st_ino)):
                raise ValueError("canonical head lock was replaced")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked_stat = os.stat(
                lock_name, dir_fd=self._root_fd, follow_symlinks=False,
            )
            if ((descriptor_stat.st_dev, descriptor_stat.st_ino)
                    != (locked_stat.st_dev, locked_stat.st_ino)):
                raise ValueError("canonical head lock changed during acquisition")
            self._verify_root()
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read_document(self, record_name, label):
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                record_name, os.O_RDONLY | nofollow, dir_fd=self._root_fd,
            )
        except OSError as error:
            raise ValueError(f"canonical head {label} is unavailable: {error}") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
                raise ValueError(f"canonical head {label} is not a bounded regular file")
            content = b""
            while len(content) < metadata.st_size:
                chunk = os.read(descriptor, metadata.st_size - len(content))
                if not chunk:
                    raise ValueError(f"canonical head {label} is truncated")
                content += chunk
            after = os.fstat(descriptor)
            if ((metadata.st_dev, metadata.st_ino, metadata.st_size)
                    != (after.st_dev, after.st_ino, after.st_size)):
                raise ValueError(f"canonical head {label} changed during read")
        finally:
            os.close(descriptor)
        try:
            value = json.loads(content, object_pairs_hook=_strict_object)
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"canonical head {label} JSON is invalid: {error}") from error
        return value

    def _read(self, record_name, campaign_sha256):
        return self._validate(
            self._read_document(record_name, "record"), campaign_sha256,
        )

    def _publish(self, record_name, value, *, exclusive):
        self._verify_root()
        content = _canonical_bytes(value) + b"\n"
        temporary = f".{record_name}.tmp.{secrets.token_hex(16)}"
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = None
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600, dir_fd=self._root_fd,
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short canonical head write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.chmod(temporary, 0o400, dir_fd=self._root_fd, follow_symlinks=False)
            if exclusive:
                os.link(
                    temporary, record_name,
                    src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=self._root_fd)
            else:
                os.replace(
                    temporary, record_name,
                    src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd,
                )
            os.fsync(self._root_fd)
            self._verify_root()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass

    def register(self, campaign_sha256, initial_head_sha256):
        campaign, record_name, lock_name = self._names(campaign_sha256)
        _digest(initial_head_sha256, "initial head digest")
        with self._lock(lock_name, create=True):
            try:
                os.stat(record_name, dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                self._read(record_name, campaign)
                raise ValueError("canonical head campaign is already registered")
            record = self._signed({
                "format": FORMAT,
                "authority_id": self.authority_id,
                "campaign_sha256": campaign,
                "sequence": 0,
                "head_sha256": initial_head_sha256,
                "entry_sha256": None,
            })
            self._publish(record_name, record, exclusive=True)
            return record

    def ensure_registered(self, campaign_sha256, initial_head_sha256):
        campaign, record_name, lock_name = self._names(campaign_sha256)
        _digest(initial_head_sha256, "initial head digest")
        with self._lock(lock_name, create=True):
            try:
                os.stat(record_name, dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                record = self._signed({
                    "format": FORMAT,
                    "authority_id": self.authority_id,
                    "campaign_sha256": campaign,
                    "sequence": 0,
                    "head_sha256": initial_head_sha256,
                    "entry_sha256": None,
                })
                self._publish(record_name, record, exclusive=True)
                return record
            current = self._read(record_name, campaign)
            if (current["sequence"] != 0
                    or current["head_sha256"] != initial_head_sha256
                    or current["entry_sha256"] is not None):
                raise ValueError(
                    "canonical head campaign registration differs from initial head"
                )
            return current

    def read(self, campaign_sha256):
        campaign, record_name, lock_name = self._names(campaign_sha256)
        with self._lock(lock_name, create=False):
            return self._read(record_name, campaign)

    def compare_and_swap(
            self, campaign_sha256, *, expected_head_sha256,
            successor_head_sha256, successor_sequence, successor_entry_sha256):
        campaign, record_name, lock_name = self._names(campaign_sha256)
        _digest(expected_head_sha256, "expected head digest")
        _digest(successor_head_sha256, "successor head digest")
        _digest(successor_entry_sha256, "successor entry digest")
        with self._lock(lock_name, create=False):
            current = self._read(record_name, campaign)
            if (current["head_sha256"] != expected_head_sha256
                    or type(successor_sequence) is not int
                    or successor_sequence != current["sequence"] + 1):
                raise ValueError("canonical head compare-and-swap predecessor mismatch")
            successor = self._signed({
                "format": FORMAT,
                "authority_id": self.authority_id,
                "campaign_sha256": campaign,
                "sequence": successor_sequence,
                "head_sha256": successor_head_sha256,
                "entry_sha256": successor_entry_sha256,
            })
            self._publish(record_name, successor, exclusive=False)
            return successor
