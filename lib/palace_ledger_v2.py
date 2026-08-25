#!/usr/bin/env python3
"""Crash-safe local publication against the external Palace head authority."""
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat

if __package__:
    from .palace_campaign import (
        ATTEMPT_ID_RE,
        _accumulate_accounting,
        _validate_accounting,
        charge_attempt_reservation_v2,
        reconcile_campaign_attempt_v2,
        validate_campaign_identity_v2,
    )
    from .palace_head_authority import CanonicalHeadAuthority
    from .palace_reservation import derive_attempt_reservation
    from .provenance import canonical_equal, canonical_sha256, exclusive_publish_json
else:
    from palace_campaign import (
        ATTEMPT_ID_RE,
        _accumulate_accounting,
        _validate_accounting,
        charge_attempt_reservation_v2,
        reconcile_campaign_attempt_v2,
        validate_campaign_identity_v2,
    )
    from palace_head_authority import CanonicalHeadAuthority
    from palace_reservation import derive_attempt_reservation
    from provenance import canonical_equal, canonical_sha256, exclusive_publish_json

try:
    import fcntl
except ImportError:  # pragma: no cover - intentionally POSIX-only
    fcntl = None


HEAD_FORMAT = "palace-checkpoint-campaign-head-v2"
PENDING_FORMAT = "palace-checkpoint-campaign-pending-v2"
ENTRY_FORMAT = "palace-checkpoint-campaign-entry-v2"


def _authority_identity(authority):
    if type(authority) is not CanonicalHeadAuthority:
        raise ValueError("campaign v2 canonical authority type is invalid")
    return CanonicalHeadAuthority.identity.__get__(
        authority, CanonicalHeadAuthority,
    )


PUBLICATION_TEMP_RE = re.compile(r"^\..+\.tmp\.[0-9a-f]{32}$")


def _sha256(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{label} is not a lowercase SHA-256")
    return value


def _strict_document(path, label, *, limit=1024 * 1024):
    path = Path(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError(f"{label} is not a bounded regular file")
        content = b""
        while len(content) < before.st_size:
            chunk = os.read(descriptor, before.st_size - len(content))
            if not chunk:
                raise ValueError(f"{label} is truncated")
            content += chunk
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)):
            raise ValueError(f"{label} changed during read")
    except OSError as error:
        raise ValueError(f"{label} is unavailable or unsafe: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    def strict_object(pairs):
        value = {}
        for name, item in pairs:
            if name in value:
                raise ValueError(f"{label} contains duplicate key")
            value[name] = item
        return value

    try:
        return json.loads(content, object_pairs_hook=strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} JSON is invalid: {error}") from error


def _content_document(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} is malformed")
    unsigned = dict(value)
    digest = unsigned.pop("content_sha256", None)
    _sha256(digest, f"{label} content digest")
    if digest != canonical_sha256(unsigned):
        raise ValueError(f"{label} content digest mismatch")
    return value


def _zero_accounting():
    return {
        "wall_time_s": 0.0, "cpu_time_s": 0.0,
        "iterations": 0, "solves": 0, "bytes_written": 0,
        "bytes_read": 0, "stdout_bytes": 0, "stderr_bytes": 0,
        "checkpoint_write_bytes": 0, "checkpoint_read_bytes": 0,
        "retained_output_bytes": 0, "retained_checkpoint_bytes": 0,
        "peak_rss_bytes": 0,
    }


def initial_head_v2(campaign):
    accounting = _validate_accounting(_zero_accounting())
    payload = {
        "format": HEAD_FORMAT,
        "campaign_sha256": campaign["content_sha256"],
        "sequence": 0,
        "entry_sha256": None,
        "prefix": 0,
        "active_attempt": None,
        "terminal": False,
        "attempts_finished": 0,
        "accounting": accounting,
        "final_attempt_sha256": None,
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}


def validate_head_v2(value, campaign):
    _content_document(value, "campaign v2 head")
    if set(value) != {
            "format", "campaign_sha256", "sequence", "entry_sha256", "prefix",
            "active_attempt", "terminal", "attempts_finished", "accounting",
            "final_attempt_sha256", "content_sha256"}:
        raise ValueError("campaign v2 head schema mismatch")
    terminal_count = len(campaign["ordered_terminals"])
    if (value["format"] != HEAD_FORMAT
            or value["campaign_sha256"] != campaign["content_sha256"]
            or type(value["sequence"]) is not int or value["sequence"] < 0
            or (value["entry_sha256"] is None) != (value["sequence"] == 0)
            or type(value["prefix"]) is not int
            or not 0 <= value["prefix"] <= terminal_count
            or type(value["terminal"]) is not bool
            or type(value["attempts_finished"]) is not int
            or value["attempts_finished"] < 0
            or value["attempts_finished"] > campaign["cumulative_caps"]["attempts"]
            or (value["active_attempt"] is not None
                and (not isinstance(value["active_attempt"], str)
                     or not ATTEMPT_ID_RE.fullmatch(value["active_attempt"])))
            or (value["final_attempt_sha256"] is not None
                and not value["terminal"])):
        raise ValueError("campaign v2 head identity is invalid")
    if value["entry_sha256"] is not None:
        _sha256(value["entry_sha256"], "campaign v2 head entry digest")
    if value["final_attempt_sha256"] is not None:
        _sha256(value["final_attempt_sha256"], "campaign v2 final attempt digest")
    accounting = _validate_accounting(value["accounting"])
    if accounting != value["accounting"]:
        raise ValueError("campaign v2 head accounting is not canonical")
    return value


def validate_entry_v2(value, campaign, predecessor):
    _content_document(value, "campaign v2 entry")
    common = {
        "format", "campaign_sha256", "sequence", "previous_entry_sha256",
        "event", "attempt_id", "content_sha256",
    }
    if (value.get("format") != ENTRY_FORMAT
            or value.get("campaign_sha256") != campaign["content_sha256"]
            or value.get("sequence") != predecessor["sequence"] + 1
            or value.get("previous_entry_sha256") != predecessor["entry_sha256"]
            or not isinstance(value.get("attempt_id"), str)
            or not ATTEMPT_ID_RE.fullmatch(value["attempt_id"])):
        raise ValueError("campaign v2 entry does not extend its predecessor")
    if value.get("event") == "attempt_registered":
        if (set(value) != common | {
                "prefix_before", "resource_decision_sha256",
                "resource_reservation"}
                or predecessor["terminal"]
                or predecessor["active_attempt"] is not None
                or value["prefix_before"] != predecessor["prefix"]):
            raise ValueError("campaign v2 registration schema mismatch")
        _sha256(value["resource_decision_sha256"], "resource decision digest")
        if _validate_accounting(value["resource_reservation"]) != value[
                "resource_reservation"]:
            raise ValueError("campaign v2 resource reservation is not canonical")
    elif value.get("event") == "attempt_finished":
        if (set(value) != common | {"reconciliation", "charged_accounting"}
                or predecessor["terminal"]
                or predecessor["active_attempt"] != value["attempt_id"]):
            raise ValueError("campaign v2 completion schema mismatch")
        reconciliation = _content_document(
            value["reconciliation"], "campaign v2 attempt reconciliation",
        )
        if (reconciliation.get("format")
                != "palace-checkpoint-attempt-reconciliation-v2"
                or reconciliation.get("campaign_sha256")
                != campaign["content_sha256"]):
            raise ValueError("campaign v2 attempt reconciliation identity mismatch")
        if _validate_accounting(value["charged_accounting"]) != value[
                "charged_accounting"]:
            raise ValueError("campaign v2 charged accounting is not canonical")
    elif value.get("event") == "attempt_aborted":
        if (set(value) != common | {"reason", "charged_accounting"}
                or predecessor["terminal"]
                or predecessor["active_attempt"] != value["attempt_id"]
                or not isinstance(value["reason"], str) or not value["reason"]):
            raise ValueError("campaign v2 abort schema mismatch")
        if _validate_accounting(value["charged_accounting"]) != value[
                "charged_accounting"]:
            raise ValueError("campaign v2 abort accounting is not canonical")
    else:
        raise ValueError("campaign v2 entry event is invalid")
    return value


def _validate_pending(value, campaign):
    _content_document(value, "campaign v2 pending record")
    if set(value) != {
            "format", "campaign_sha256", "predecessor_head",
            "successor_head", "entry", "content_sha256"}:
        raise ValueError("campaign v2 pending record schema mismatch")
    predecessor = validate_head_v2(value["predecessor_head"], campaign)
    entry = validate_entry_v2(value["entry"], campaign, predecessor)
    successor = validate_head_v2(value["successor_head"], campaign)
    if (value["format"] != PENDING_FORMAT
            or value["campaign_sha256"] != campaign["content_sha256"]
            or successor["entry_sha256"] != entry["content_sha256"]):
        raise ValueError("campaign v2 pending record identity mismatch")
    return value


class CanonicalLedgerPublicationV2:
    __slots__ = (
        "campaign", "expected_workload", "root", "entries", "head_path",
        "pending_path", "next_head_path", "lock_path", "authority",
        "_lock_fd", "_sealed",
    )

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            if name == "_lock_fd" and value is None:
                object.__setattr__(self, name, value)
                return
            raise AttributeError("canonical ledger instances are immutable")
        object.__setattr__(self, name, value)

    def __init__(self, root, campaign, *, authority, expected_campaign_sha256,
                 expected_workload):
        if fcntl is None or os.name != "posix":
            raise ValueError("campaign v2 publication requires POSIX flock semantics")
        self.campaign = validate_campaign_identity_v2(
            campaign,
            expected_campaign_sha256=expected_campaign_sha256,
            expected_workload=expected_workload,
            expected_authority_identity=_authority_identity(authority),
        )
        self.expected_workload = deepcopy(expected_workload)
        supplied = Path(root)
        if supplied.is_symlink():
            raise ValueError("campaign v2 ledger root must not be a symlink")
        self.root = supplied.absolute()
        self.entries = self.root / "entries"
        self.head_path = self.root / "head.json"
        self.pending_path = self.root / "pending.json"
        self.next_head_path = self.root / "next-head.json"
        self.lock_path = self.root / "writer.lock"
        self.authority = authority
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        self._lock_fd = os.open(self.lock_path, os.O_RDWR | nofollow)
        if not stat.S_ISREG(os.fstat(self._lock_fd).st_mode):
            os.close(self._lock_fd)
            raise ValueError("campaign v2 writer lock is invalid")
        with CanonicalLedgerPublicationV2._lock(self):
            CanonicalLedgerPublicationV2._remove_stale_publication_temps(self)
            CanonicalLedgerPublicationV2._validate_local_state(self)
        self._sealed = True

    @classmethod
    def _ensure_initial_local_state(cls, root, campaign):
        root = Path(root)
        if root.is_symlink():
            raise ValueError("campaign v2 ledger root must not be a symlink")
        try:
            root.mkdir(parents=False)
        except FileExistsError:
            if not root.is_dir() or root.is_symlink():
                raise ValueError("campaign v2 ledger root is invalid")
        entries = root / "entries"
        try:
            entries.mkdir()
        except FileExistsError:
            if not entries.is_dir() or entries.is_symlink():
                raise ValueError("campaign v2 entries directory is invalid")
        lock_path = root / "writer.lock"
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_descriptor = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow, 0o600,
            )
        except FileExistsError:
            lock_descriptor = os.open(lock_path, os.O_RDWR | nofollow)
        try:
            lock_metadata = os.fstat(lock_descriptor)
            path_metadata = os.stat(lock_path, follow_symlinks=False)
            if (not stat.S_ISREG(lock_metadata.st_mode)
                    or (lock_metadata.st_dev, lock_metadata.st_ino)
                    != (path_metadata.st_dev, path_metadata.st_ino)):
                raise ValueError("campaign v2 initialization writer lock is invalid")
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            for directory in (root, entries):
                for path in directory.iterdir():
                    if PUBLICATION_TEMP_RE.fullmatch(path.name):
                        if not path.is_file() or path.is_symlink():
                            raise ValueError(
                                "campaign v2 initialization temporary is unsafe"
                            )
                        path.unlink()
            allowed = {"campaign.json", "entries", "head.json", "writer.lock"}
            if not {path.name for path in root.iterdir()}.issubset(allowed):
                raise ValueError(
                    "campaign v2 initialization root contains unknown files"
                )
            campaign_path = root / "campaign.json"
            if campaign_path.exists() or campaign_path.is_symlink():
                if not canonical_equal(
                        _strict_document(campaign_path, "campaign v2 identity"),
                        campaign):
                    raise ValueError("campaign v2 initialization identity mismatch")
            else:
                exclusive_publish_json(campaign_path, campaign)
            head = initial_head_v2(campaign)
            head_path = root / "head.json"
            if head_path.exists() or head_path.is_symlink():
                if not canonical_equal(
                        _strict_document(head_path, "campaign v2 initial head"),
                        head):
                    raise ValueError(
                        "campaign v2 initialization head is not pristine"
                    )
            else:
                exclusive_publish_json(head_path, head)
            if list(entries.iterdir()):
                raise ValueError("campaign v2 initialization entries are not pristine")
            for directory in (entries, root, root.parent):
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return head
        finally:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)

    @classmethod
    def create(cls, root, campaign, *, authority, expected_campaign_sha256,
               expected_workload):
        if cls is not CanonicalLedgerPublicationV2:
            raise ValueError("campaign v2 ledger subclasses are not authorized")
        campaign = validate_campaign_identity_v2(
            campaign,
            expected_campaign_sha256=expected_campaign_sha256,
            expected_workload=expected_workload,
            expected_authority_identity=_authority_identity(authority),
        )
        root = Path(root)
        head = CanonicalLedgerPublicationV2._ensure_initial_local_state(
            root, campaign,
        )
        CanonicalHeadAuthority.ensure_registered(
            authority, campaign["content_sha256"], head["content_sha256"],
        )
        return CanonicalLedgerPublicationV2(
            root, campaign, authority=authority,
            expected_campaign_sha256=expected_campaign_sha256,
            expected_workload=expected_workload,
        )

    def close(self):
        descriptor = getattr(self, "_lock_fd", None)
        if descriptor is not None:
            os.close(descriptor)
            object.__setattr__(self, "_lock_fd", None)

    @contextmanager
    def _lock(self):
        descriptor = self._lock_fd
        if descriptor is None:
            raise ValueError("campaign v2 writer lock is closed")
        before = os.fstat(descriptor)
        current = os.stat(self.lock_path, follow_symlinks=False)
        if ((before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
                or not stat.S_ISREG(current.st_mode)):
            raise ValueError("campaign v2 writer lock was replaced")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            after = os.stat(self.lock_path, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("campaign v2 writer lock changed during acquisition")
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _remove_stale_publication_temps(self):
        removed = False
        for directory in (self.root, self.entries):
            for path in directory.iterdir():
                if not PUBLICATION_TEMP_RE.fullmatch(path.name):
                    continue
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("campaign v2 publication temporary is unsafe")
                path.unlink()
                removed = True
            if removed:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    def _head(self):
        return validate_head_v2(
            _strict_document(self.head_path, "campaign v2 head"), self.campaign,
        )

    def _registration_successor(self, predecessor, entry):
        reservation = entry["resource_reservation"]
        projected = _accumulate_accounting([
            predecessor["accounting"], reservation,
        ])
        if (predecessor["attempts_finished"]
                >= self.campaign["cumulative_caps"]["attempts"]
                or any(projected[name] > self.campaign["cumulative_caps"][name]
                       for name in projected)):
            raise ValueError("campaign v2 registration exceeds cumulative capacity")
        payload = {
            **predecessor,
            "sequence": entry["sequence"],
            "entry_sha256": entry["content_sha256"],
            "active_attempt": entry["attempt_id"],
        }
        payload.pop("content_sha256")
        return {**payload, "content_sha256": canonical_sha256(payload)}

    def _completion_successor(self, predecessor, entry, registration):
        if (registration is None
                or registration["attempt_id"] != entry["attempt_id"]):
            raise ValueError("campaign v2 completion lacks its registration")
        reconciliation = entry["reconciliation"]
        replayed = reconcile_campaign_attempt_v2(
            self.campaign,
            expected_campaign_sha256=self.campaign["content_sha256"],
            expected_workload=self.expected_workload,
            expected_authority_identity=_authority_identity(self.authority),
            run_manifest_path=reconciliation["execution_witness"]["path"],
            checkpoint_root=reconciliation["checkpoint_root"],
        )
        if not canonical_equal(replayed, reconciliation):
            raise ValueError("campaign v2 attempt reconciliation is stale")
        if (reconciliation["resource_decision_sha256"]
                != registration["resource_decision_sha256"]):
            raise ValueError("campaign v2 attempt resource decision was substituted")
        charged = charge_attempt_reservation_v2(
            reconciliation, registration["resource_reservation"],
        )
        if charged != entry["charged_accounting"]:
            raise ValueError("campaign v2 completion charge is not derived")
        if (reconciliation["prefix_before"] != predecessor["prefix"]
                or reconciliation["prefix_after"] < predecessor["prefix"]):
            raise ValueError("campaign v2 completion prefix is invalid")
        outcome = reconciliation["outcome"]
        if outcome not in {"wall_timeout", "completed", "terminal_failure"}:
            raise ValueError("campaign v2 completion outcome is invalid")
        accounting = _accumulate_accounting([
            predecessor["accounting"], charged,
        ])
        if any(accounting[name] > self.campaign["cumulative_caps"][name]
               for name in accounting):
            raise ValueError("campaign v2 completion exceeds cumulative capacity")
        terminal = outcome in {"completed", "terminal_failure"}
        payload = {
            **predecessor,
            "sequence": entry["sequence"],
            "entry_sha256": entry["content_sha256"],
            "prefix": reconciliation["prefix_after"],
            "active_attempt": None,
            "terminal": terminal,
            "attempts_finished": predecessor["attempts_finished"] + 1,
            "accounting": accounting,
            "final_attempt_sha256": (
                reconciliation["content_sha256"] if terminal else None
            ),
        }
        payload.pop("content_sha256")
        return {**payload, "content_sha256": canonical_sha256(payload)}

    def _abortion_successor(self, predecessor, entry, registration):
        if (registration is None
                or registration["attempt_id"] != entry["attempt_id"]
                or entry["charged_accounting"]
                != registration["resource_reservation"]):
            raise ValueError("campaign v2 abort lacks its exact reservation")
        accounting = _accumulate_accounting([
            predecessor["accounting"], entry["charged_accounting"],
        ])
        if any(accounting[name] > self.campaign["cumulative_caps"][name]
               for name in accounting):
            raise ValueError("campaign v2 abort exceeds cumulative capacity")
        payload = {
            **predecessor,
            "sequence": entry["sequence"],
            "entry_sha256": entry["content_sha256"],
            "active_attempt": None,
            "attempts_finished": predecessor["attempts_finished"] + 1,
            "accounting": accounting,
        }
        payload.pop("content_sha256")
        return {**payload, "content_sha256": canonical_sha256(payload)}

    def _entry_successor(self, predecessor, entry, registration):
        if entry["event"] == "attempt_registered":
            return self._registration_successor(predecessor, entry), entry
        if entry["event"] == "attempt_aborted":
            return self._abortion_successor(predecessor, entry, registration), None
        return self._completion_successor(
            predecessor, entry, registration,
        ), None

    def _validate_local_state(self):
        names = {path.name for path in self.root.iterdir()}
        required = {"campaign.json", "entries", "head.json", "writer.lock"}
        pending_names = required | {"pending.json"}
        if names not in (required, pending_names, pending_names | {"next-head.json"}):
            raise ValueError("campaign v2 ledger root contains unknown files")
        if self.entries.is_symlink() or not self.entries.is_dir():
            raise ValueError("campaign v2 entries directory is invalid")
        stored_campaign = _content_document(
            _strict_document(self.root / "campaign.json", "campaign v2 identity"),
            "campaign v2 identity",
        )
        if not canonical_equal(stored_campaign, self.campaign):
            raise ValueError("campaign v2 stored identity differs from authorization")
        head = self._head()
        pending = None
        if "pending.json" in names:
            pending = _validate_pending(
                _strict_document(self.pending_path, "campaign v2 pending record"),
                self.campaign,
            )
            if head["content_sha256"] not in {
                    pending["predecessor_head"]["content_sha256"],
                    pending["successor_head"]["content_sha256"]}:
                raise ValueError("campaign v2 pending record does not bracket local head")
            if "next-head.json" in names and not canonical_equal(
                    _strict_document(
                        self.next_head_path, "campaign v2 next head"),
                    pending["successor_head"]):
                raise ValueError("campaign v2 next head differs from pending successor")
        files = sorted(self.entries.iterdir(), key=lambda path: path.name)
        predecessor_pending = (pending is not None and
            head["content_sha256"]
            == pending["predecessor_head"]["content_sha256"])
        allowed_counts = {head["sequence"]}
        if predecessor_pending:
            allowed_counts.add(head["sequence"] + 1)
        if len(files) not in allowed_counts:
            raise ValueError("campaign v2 entry inventory differs from local state")
        replay = initial_head_v2(self.campaign)
        active_registration = None
        last_registration = None
        execution_witnesses = set()
        attempt_ids = set()
        last_entry = None
        for index, path in enumerate(files, 1):
            if path.is_symlink() or not path.is_file():
                raise ValueError("campaign v2 ledger entry is unsafe")
            entry = validate_entry_v2(
                _strict_document(path, "campaign v2 ledger entry"),
                self.campaign, replay,
            )
            if path.name != f"{index:06d}-{entry['content_sha256']}.json":
                raise ValueError("campaign v2 ledger entry filename mismatch")
            pending_candidate = (
                pending is not None
                and entry["content_sha256"]
                == pending["entry"]["content_sha256"]
            )
            if entry["event"] == "attempt_registered" and not pending_candidate:
                if entry["attempt_id"] in attempt_ids:
                    raise ValueError("campaign v2 attempt ID was reused")
                attempt_ids.add(entry["attempt_id"])
            if entry["event"] == "attempt_finished":
                witness_sha256 = entry["reconciliation"]["execution_witness"][
                    "sha256"
                ]
                _sha256(witness_sha256, "execution witness digest")
                if not pending_candidate:
                    if witness_sha256 in execution_witnesses:
                        raise ValueError("campaign v2 execution witness was reused")
                    execution_witnesses.add(witness_sha256)
            replay, active_registration = self._entry_successor(
                replay, entry, active_registration,
            )
            if entry["event"] == "attempt_registered":
                last_registration = entry
            last_entry = entry
        expected_replay = (
            pending["successor_head"]
            if pending is not None and len(files) == pending["successor_head"]["sequence"]
            else head
        )
        if not canonical_equal(replay, expected_replay):
            raise ValueError("campaign v2 local head differs from entry replay")
        external = CanonicalHeadAuthority.read(self.authority, (self.campaign["content_sha256"]))
        allowed_external = {head["content_sha256"]}
        if pending is not None:
            allowed_external.update({
                pending["predecessor_head"]["content_sha256"],
                pending["successor_head"]["content_sha256"],
            })
        if external["head_sha256"] not in allowed_external:
            raise ValueError("campaign v2 local state lost canonical authority")
        return {
            "head": head,
            "pending": pending,
            "active_registration": active_registration,
            "last_registration": last_registration,
            "last_entry": last_entry,
            "attempt_ids": attempt_ids,
            "execution_witnesses": execution_witnesses,
        }

    def _external_matches(self, head):
        record = CanonicalHeadAuthority.read(self.authority, (self.campaign["content_sha256"]))
        if (record["head_sha256"] != head["content_sha256"]
                or record["sequence"] != head["sequence"]
                or record["entry_sha256"] != head["entry_sha256"]):
            raise ValueError("campaign v2 local head differs from canonical authority")
        return record

    def register_attempt(self, attempt_id, *, resource_decision,
                         execution_snapshot, trusted_policy):
        receipt = derive_attempt_reservation(
            resource_decision, execution_snapshot,
            expected_workload=self.expected_workload,
            trusted_policy=trusted_policy,
        )
        return self._register_attempt(
            attempt_id,
            resource_decision_sha256=receipt["decision_sha256"],
            resource_reservation=receipt["reservation"],
        )

    def _register_attempt(self, attempt_id, *, resource_decision_sha256,
                          resource_reservation, _allow_existing=False):
        reservation = _validate_accounting(resource_reservation)
        with self._lock():
            state = self._validate_local_state()
            predecessor = state["head"]
            existing = state["active_registration"]
            if existing is not None:
                if _allow_existing:
                    if (existing["attempt_id"] != attempt_id
                            or existing["resource_decision_sha256"]
                            != resource_decision_sha256
                            or existing["resource_reservation"] != reservation):
                        raise ValueError(
                            "campaign v2 active registration differs from retry"
                        )
                    return deepcopy(existing)
                raise ValueError("campaign v2 already has an active attempt")
            payload = {
                "format": ENTRY_FORMAT,
                "campaign_sha256": self.campaign["content_sha256"],
                "sequence": predecessor["sequence"] + 1,
                "previous_entry_sha256": predecessor["entry_sha256"],
                "event": "attempt_registered",
                "attempt_id": attempt_id,
                "prefix_before": predecessor["prefix"],
                "resource_decision_sha256": resource_decision_sha256,
                "resource_reservation": reservation,
            }
            entry = {**payload, "content_sha256": canonical_sha256(payload)}
            successor = self._registration_successor(predecessor, entry)
            self._publish_locked(state, entry, successor)
            return entry

    def abort_attempt_if_active(self, attempt_id, *, reason):
        if not isinstance(reason, str) or not reason:
            raise ValueError("campaign v2 abort reason is invalid")
        with self._lock():
            state = self._validate_local_state()
            if state["pending"] is not None:
                return False
            predecessor = state["head"]
            registration = state["active_registration"]
            if registration is None:
                return False
            if registration["attempt_id"] != attempt_id:
                raise ValueError("campaign v2 active attempt differs from abort")
            payload = {
                "format": ENTRY_FORMAT,
                "campaign_sha256": self.campaign["content_sha256"],
                "sequence": predecessor["sequence"] + 1,
                "previous_entry_sha256": predecessor["entry_sha256"],
                "event": "attempt_aborted",
                "attempt_id": attempt_id,
                "reason": reason,
                "charged_accounting": registration["resource_reservation"],
            }
            entry = {**payload, "content_sha256": canonical_sha256(payload)}
            successor = self._abortion_successor(
                predecessor, entry, registration,
            )
            self._publish_locked(state, entry, successor)
            return True

    def finish_attempt(self, attempt_id, *, run_manifest_path,
                       checkpoint_root):
        with self._lock():
            state = self._validate_local_state()
            predecessor = state["head"]
            registration = state["active_registration"]
            if registration is None or registration["attempt_id"] != attempt_id:
                raise ValueError("campaign v2 attempt is not active")
            reconciliation = reconcile_campaign_attempt_v2(
                self.campaign,
                expected_campaign_sha256=self.campaign["content_sha256"],
                expected_workload=self.expected_workload,
                expected_authority_identity=_authority_identity(self.authority),
                run_manifest_path=run_manifest_path,
                checkpoint_root=checkpoint_root,
            )
            charged = charge_attempt_reservation_v2(
                reconciliation, registration["resource_reservation"],
            )
            payload = {
                "format": ENTRY_FORMAT,
                "campaign_sha256": self.campaign["content_sha256"],
                "sequence": predecessor["sequence"] + 1,
                "previous_entry_sha256": predecessor["entry_sha256"],
                "event": "attempt_finished",
                "attempt_id": attempt_id,
                "reconciliation": reconciliation,
                "charged_accounting": charged,
            }
            entry = {**payload, "content_sha256": canonical_sha256(payload)}
            successor = self._completion_successor(
                predecessor, entry, registration,
            )
            self._publish_locked(state, entry, successor)
            return entry

    def matrix_access_record(self):
        with self._lock():
            state = self._validate_local_state()
            if state["pending"] is not None:
                raise ValueError("campaign v2 matrix access has a pending successor")
            head = state["head"]
            self._external_matches(head)
            entry = state["last_entry"]
            terminal_count = len(self.campaign["ordered_terminals"])
            if (not head["terminal"] or head["active_attempt"] is not None
                    or head["prefix"] != terminal_count
                    or entry is None or entry["event"] != "attempt_finished"):
                raise ValueError("campaign v2 matrix access lacks terminal replay")
            reconciliation = entry["reconciliation"]
            artifacts = reconciliation["final_artifacts"]
            if (reconciliation["outcome"] != "completed"
                    or reconciliation["prefix_after"] != terminal_count
                    or head["final_attempt_sha256"]
                    != reconciliation["content_sha256"]
                    or not isinstance(artifacts, dict)
                    or set(artifacts) != {"raw_matrix", "standard_matrix"}):
                raise ValueError("campaign v2 matrix access lacks a completed final attempt")
            payload = {
                "format": "palace-checkpoint-matrix-access-record-v2",
                "campaign_sha256": self.campaign["content_sha256"],
                "head_sha256": head["content_sha256"],
                "workload_sha256": self.campaign["workload_sha256"],
                "config_sha256": self.expected_workload["config_sha256"],
                "config_manifest_sha256": self.expected_workload[
                    "config_manifest_sha256"
                ],
                "ordered_terminals": deepcopy(self.campaign["ordered_terminals"]),
                "final_attempt_sha256": reconciliation["content_sha256"],
                "matrices": deepcopy(artifacts),
            }
            return {**payload, "content_sha256": canonical_sha256(payload)}

    def _reject_candidate_reuse(self, entry, state):
        if (entry["event"] == "attempt_registered"
                and entry["attempt_id"] in state["attempt_ids"]):
            raise ValueError("campaign v2 attempt ID was reused")
        if entry["event"] == "attempt_finished":
            witness_sha256 = entry["reconciliation"]["execution_witness"][
                "sha256"
            ]
            if witness_sha256 in state["execution_witnesses"]:
                raise ValueError("campaign v2 execution witness was reused")

    def _publish_transition(self, entry, successor_head):
        with self._lock():
            state = self._validate_local_state()
            return self._publish_locked(
                state, entry, successor_head,
            )

    def _publish_locked(self, state, entry, successor_head):
        if state["pending"] is not None:
            raise ValueError("campaign v2 has an unreconciled pending successor")
        predecessor = state["head"]
        self._external_matches(predecessor)
        entry = validate_entry_v2(entry, self.campaign, predecessor)
        self._reject_candidate_reuse(entry, state)
        successor = validate_head_v2(successor_head, self.campaign)
        expected, _ = self._entry_successor(
            predecessor, entry, state["active_registration"],
        )
        if not canonical_equal(successor, expected):
            raise ValueError("campaign v2 successor does not bind its entry")
        pending_payload = {
            "format": PENDING_FORMAT,
            "campaign_sha256": self.campaign["content_sha256"],
            "predecessor_head": predecessor,
            "successor_head": successor,
            "entry": entry,
        }
        pending = {
            **pending_payload,
            "content_sha256": canonical_sha256(pending_payload),
        }
        exclusive_publish_json(self.pending_path, pending)
        return self._reconcile_locked(pending, state)

    def recover_pending_attempt(self, *, resource_decision=None,
                                execution_snapshot=None, trusted_policy=None):
        expected_registration = None
        if resource_decision is not None or execution_snapshot is not None:
            if (resource_decision is None or execution_snapshot is None
                    or trusted_policy is None):
                raise ValueError(
                    "campaign v2 registration recovery requires trusted inputs"
                )
            receipt = derive_attempt_reservation(
                resource_decision, execution_snapshot,
                expected_workload=self.expected_workload,
                trusted_policy=trusted_policy,
            )
            expected_registration = {
                "resource_decision_sha256": receipt["decision_sha256"],
                "resource_reservation": receipt["reservation"],
            }
        return self._reconcile_pending_transition(
            expected_registration=expected_registration,
        )

    def _reconcile_pending_transition(self, *, expected_registration=None):
        with self._lock():
            if not self.pending_path.exists() or self.pending_path.is_symlink():
                raise ValueError("campaign v2 has no safe pending successor")
            state = self._validate_local_state()
            pending = state["pending"]
            if pending is None:
                raise ValueError("campaign v2 has no pending successor")
            entry = pending["entry"]
            if entry["event"] == "attempt_registered":
                actual = {
                    "resource_decision_sha256": entry[
                        "resource_decision_sha256"
                    ],
                    "resource_reservation": entry["resource_reservation"],
                }
                if expected_registration != actual:
                    raise ValueError(
                        "campaign v2 registration recovery lacks trusted derivation"
                    )
            elif expected_registration is not None:
                raise ValueError(
                    "campaign v2 completion recovery rejects registration inputs"
                )
            return self._reconcile_locked(pending, state)

    def _reconcile_locked(self, pending, state):
        predecessor = pending["predecessor_head"]
        predecessor_digest = predecessor["content_sha256"]
        successor = pending["successor_head"]
        entry = pending["entry"]
        self._reject_candidate_reuse(entry, state)
        derived, _ = self._entry_successor(
            predecessor, entry, state["last_registration"],
        )
        if not canonical_equal(derived, successor):
            raise ValueError("campaign v2 pending successor is not derived")
        entry_path = self.entries / (
            f"{entry['sequence']:06d}-{entry['content_sha256']}.json"
        )
        if entry_path.exists() or entry_path.is_symlink():
            if not canonical_equal(
                    _strict_document(entry_path, "campaign v2 ledger entry"), entry):
                raise ValueError("campaign v2 pending entry differs from published entry")
        else:
            exclusive_publish_json(entry_path, entry)
        if self.next_head_path.exists() or self.next_head_path.is_symlink():
            if not canonical_equal(
                    _strict_document(self.next_head_path, "campaign v2 next head"),
                    successor):
                raise ValueError("campaign v2 pending next head differs from successor")
        else:
            exclusive_publish_json(self.next_head_path, successor)
        external = CanonicalHeadAuthority.read(self.authority, (self.campaign["content_sha256"]))
        if external["head_sha256"] == predecessor_digest:
            external = CanonicalHeadAuthority.compare_and_swap(
                self.authority, self.campaign["content_sha256"],
                expected_head_sha256=predecessor_digest,
                successor_head_sha256=successor["content_sha256"],
                successor_sequence=successor["sequence"],
                successor_entry_sha256=entry["content_sha256"],
            )
        elif (external["head_sha256"] != successor["content_sha256"]
              or external["sequence"] != successor["sequence"]
              or external["entry_sha256"] != entry["content_sha256"]):
            raise ValueError("campaign v2 pending successor lost canonical authority")
        local = self._head()
        if local["content_sha256"] == predecessor_digest:
            os.replace(self.next_head_path, self.head_path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        elif not canonical_equal(local, successor):
            raise ValueError("campaign v2 local head is neither pending endpoint")
        final = self._head()
        self._external_matches(final)
        if self.next_head_path.exists() or self.next_head_path.is_symlink():
            next_value = _strict_document(
                self.next_head_path, "campaign v2 stale next head",
            )
            if not canonical_equal(next_value, successor):
                raise ValueError("campaign v2 stale next head differs from successor")
            self.next_head_path.unlink()
        self.pending_path.unlink()
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return final


def abort_campaign_attempt_on_error(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        ledger = kwargs.get("campaign_ledger")
        attempt_id = kwargs.get("attempt_id")
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            if type(ledger) is CanonicalLedgerPublicationV2 and attempt_id is not None:
                try:
                    ledger.abort_attempt_if_active(
                        attempt_id, reason=f"{type(error).__name__}: {error}")
                except Exception as abort_error:
                    raise RuntimeError(
                        f"Palace run failed and campaign abort failed: {abort_error}"
                    ) from error
            raise

    return guarded
