#!/usr/bin/env python3
"""Exclusive content-addressed attempt chains for resumable Palace campaigns."""
from contextlib import contextmanager
from copy import deepcopy
try:
    import fcntl
except ImportError:  # pragma: no cover - E5 is intentionally POSIX-only
    fcntl = None
import hashlib
import hmac
import json
from math import isfinite
import os
from pathlib import Path
import re
import secrets
import stat

if __package__:
    from .palace_head_authority import CanonicalHeadAuthority
    from .palace_attempt import validate_palace_execution_witness
    from .palace_checkpoint import validate_native_checkpoint
    from .provenance import (
        canonical_equal, canonical_sha256, exclusive_publish_bytes, file_sha256,
    )
    from .palace_resources import validate_palace_workload
else:
    from palace_head_authority import CanonicalHeadAuthority
    from palace_attempt import validate_palace_execution_witness
    from palace_checkpoint import validate_native_checkpoint
    from provenance import (
        canonical_equal, canonical_sha256, exclusive_publish_bytes, file_sha256,
    )
    from palace_resources import validate_palace_workload


CAMPAIGN_FORMAT = "palace-checkpoint-campaign-v1"
CAMPAIGN_FORMAT_V2 = "palace-checkpoint-campaign-v2"
ENTRY_FORMAT = "palace-checkpoint-ledger-entry-v1"
HEAD_FORMAT = "palace-checkpoint-ledger-head-v2"
AUTHORITY_FORMAT = "palace-checkpoint-ledger-authority-v1"
ATTEMPT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")


def _canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()


def _authority_key(value):
    if type(value) is not bytes or len(value) < 32:
        raise ValueError("checkpoint campaign authority key must contain at least 32 bytes")
    return value


def _sha256(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_number(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0.0 < float(value) < float("inf")):
        raise ValueError(f"{label} must be finite and positive")
    return float(value)


def _nonnegative_number(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0.0 <= float(value) < float("inf")):
        raise ValueError(f"{label} must be finite and nonnegative")
    return float(value)


def build_campaign_identity(*, workload, ordered_terminals, checkpoint_format_sha256,
                            resource_policy_sha256, cumulative_caps):
    workload = validate_palace_workload(workload)
    terminals = []
    for expected_index, terminal in enumerate(ordered_terminals, 1):
        if (not isinstance(terminal, dict) or set(terminal) != {
                "index", "name", "attribute"}
                or type(terminal["index"]) is not int
                or terminal["index"] != expected_index
                or not isinstance(terminal["name"], str) or not terminal["name"]
                or type(terminal["attribute"]) is not int
                or terminal["attribute"] <= 0):
            raise ValueError("checkpoint campaign terminal identity is invalid")
        terminals.append(dict(terminal))
    if (not terminals
            or len(terminals) != workload["solver_controls"]["terminal_count"]
            or len({item["name"] for item in terminals}) != len(terminals)
            or len({item["attribute"] for item in terminals}) != len(terminals)):
        raise ValueError("checkpoint campaign terminal roster mismatch")
    integer_caps = {
        "iterations", "solves", "bytes_written", "bytes_read",
        "stdout_bytes", "stderr_bytes", "checkpoint_write_bytes",
        "checkpoint_read_bytes", "retained_output_bytes",
        "retained_checkpoint_bytes", "peak_rss_bytes",
    }
    if not isinstance(cumulative_caps, dict) or set(cumulative_caps) != {
            "attempts", "wall_time_s", "cpu_time_s", *integer_caps}:
        raise ValueError("checkpoint campaign cumulative-cap schema mismatch")
    caps = {
        "attempts": _nonnegative_int(cumulative_caps["attempts"], "attempt cap"),
        "wall_time_s": _positive_number(
            cumulative_caps["wall_time_s"], "wall-time cap",
        ),
        "cpu_time_s": _positive_number(
            cumulative_caps["cpu_time_s"], "CPU-time cap",
        ),
        **{
            name: _nonnegative_int(
                cumulative_caps[name], f"{name.replace('_', ' ')} cap",
            )
            for name in integer_caps
        },
    }
    if caps["attempts"] <= 0:
        raise ValueError("checkpoint campaign attempt cap must be positive")
    payload = {
        "format": CAMPAIGN_FORMAT,
        "workload_sha256": workload["content_sha256"],
        "ordered_terminals": terminals,
        "checkpoint_format_sha256": _sha256(
            checkpoint_format_sha256, "checkpoint format SHA-256",
        ),
        "resource_policy_sha256": _sha256(
            resource_policy_sha256, "resource policy SHA-256",
        ),
        "cumulative_caps": caps,
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}


def new_native_campaign_identity():
    return secrets.token_hex(32)


def checkpoint_validator_sha256():
    return file_sha256(Path(__file__).with_name("palace_checkpoint.py"))


def build_campaign_identity_v2(
        *, workload, ordered_terminals, native_campaign_identity,
        checkpoint_validator_digest, checkpoint_partition, canonical_head_authority,
        resource_policy_sha256, cumulative_caps):
    _sha256(native_campaign_identity, "native campaign identity")
    if checkpoint_validator_digest != checkpoint_validator_sha256():
        raise ValueError("checkpoint campaign v2 validator is not trusted")
    legacy = build_campaign_identity(
        workload=workload,
        ordered_terminals=ordered_terminals,
        checkpoint_format_sha256=checkpoint_validator_digest,
        resource_policy_sha256=resource_policy_sha256,
        cumulative_caps=cumulative_caps,
    )
    workload = validate_palace_workload(workload)
    topology = workload["topology_workload"]
    if (not isinstance(checkpoint_partition, dict)
            or set(checkpoint_partition) != {
                "process_count", "global_true_dofs", "local_true_dofs"}
            or type(checkpoint_partition["process_count"]) is not int
            or checkpoint_partition["process_count"] != topology["process_count"]
            or type(checkpoint_partition["global_true_dofs"]) is not int
            or checkpoint_partition["global_true_dofs"]
            != topology["h1_true_dofs_finest"]
            or not isinstance(checkpoint_partition["local_true_dofs"], list)
            or len(checkpoint_partition["local_true_dofs"])
            != checkpoint_partition["process_count"]
            or any(type(value) is not int or value < 0
                   for value in checkpoint_partition["local_true_dofs"])
            or sum(checkpoint_partition["local_true_dofs"])
            != checkpoint_partition["global_true_dofs"]):
        raise ValueError("checkpoint campaign v2 partition identity is invalid")
    if type(canonical_head_authority) is not CanonicalHeadAuthority:
        raise ValueError("checkpoint campaign v2 canonical authority is invalid")
    authority = CanonicalHeadAuthority.identity.__get__(
        canonical_head_authority, CanonicalHeadAuthority,
    )
    if (not isinstance(authority, dict) or set(authority) != {
            "authority_id", "root", "root_sha256", "instance_sha256",
            "key_fingerprint", "client_uid"}):
        raise ValueError("checkpoint campaign v2 canonical authority is invalid")
    payload = {
        **{key: value for key, value in legacy.items()
           if key not in {"format", "checkpoint_format_sha256", "content_sha256"}},
        "format": CAMPAIGN_FORMAT_V2,
        "native_campaign_identity": native_campaign_identity,
        "checkpoint_contract": {
            "validator_sha256": _sha256(
                checkpoint_validator_digest, "checkpoint validator SHA-256",
            ),
            "completion_format": (
                "palace-electrostatic-checkpoint-completion-v1"
            ),
            "shard_format": "palace-electrostatic-checkpoint-shard-v1",
            "response_format": "palace-electrostatic-checkpoint-response-v1",
        },
        "checkpoint_partition": deepcopy(checkpoint_partition),
        "canonical_head_authority": deepcopy(authority),
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}


def validate_campaign_identity_v2(
        value, *, expected_campaign_sha256, expected_workload,
        expected_authority_identity):
    if not isinstance(value, dict) or value.get("format") != CAMPAIGN_FORMAT_V2:
        raise ValueError("checkpoint campaign v2 format mismatch")
    expected_keys = {
        "format", "workload_sha256", "ordered_terminals",
        "native_campaign_identity", "checkpoint_contract", "checkpoint_partition",
        "canonical_head_authority", "resource_policy_sha256",
        "cumulative_caps", "content_sha256",
    }
    if set(value) != expected_keys:
        raise ValueError("checkpoint campaign v2 schema mismatch")
    unsigned = dict(value)
    digest = unsigned.pop("content_sha256")
    if (digest != canonical_sha256(unsigned)
            or digest != _sha256(
                expected_campaign_sha256, "expected campaign SHA-256")):
        raise ValueError("checkpoint campaign v2 content hash mismatch")
    _sha256(value["native_campaign_identity"], "native campaign identity")
    contract = value["checkpoint_contract"]
    if (not isinstance(contract, dict) or contract != {
            "validator_sha256": contract.get("validator_sha256"),
            "completion_format": "palace-electrostatic-checkpoint-completion-v1",
            "shard_format": "palace-electrostatic-checkpoint-shard-v1",
            "response_format": "palace-electrostatic-checkpoint-response-v1",
            }):
        raise ValueError("checkpoint campaign v2 format contract mismatch")
    if (_sha256(contract["validator_sha256"], "checkpoint validator SHA-256")
            != checkpoint_validator_sha256()):
        raise ValueError("checkpoint campaign v2 validator identity is stale")
    workload = validate_palace_workload(expected_workload)
    if value["workload_sha256"] != workload["content_sha256"]:
        raise ValueError("checkpoint campaign v2 workload identity mismatch")
    legacy_shape = {
        "format": CAMPAIGN_FORMAT,
        "workload_sha256": value["workload_sha256"],
        "ordered_terminals": value["ordered_terminals"],
        "checkpoint_format_sha256": contract["validator_sha256"],
        "resource_policy_sha256": value["resource_policy_sha256"],
        "cumulative_caps": value["cumulative_caps"],
    }
    validate_campaign_identity({
        **legacy_shape,
        "content_sha256": canonical_sha256(legacy_shape),
    })
    partition = value["checkpoint_partition"]
    if (not isinstance(partition, dict) or set(partition) != {
            "process_count", "global_true_dofs", "local_true_dofs"}
            or type(partition["process_count"]) is not int
            or partition["process_count"] <= 0
            or type(partition["global_true_dofs"]) is not int
            or partition["global_true_dofs"] <= 0
            or not isinstance(partition["local_true_dofs"], list)
            or len(partition["local_true_dofs"]) != partition["process_count"]
            or any(type(item) is not int or item < 0
                   for item in partition["local_true_dofs"])
            or sum(partition["local_true_dofs"]) != partition["global_true_dofs"]):
        raise ValueError("checkpoint campaign v2 partition identity is invalid")
    topology = workload["topology_workload"]
    if (partition["process_count"] != topology["process_count"]
            or partition["global_true_dofs"] != topology["h1_true_dofs_finest"]):
        raise ValueError("checkpoint campaign v2 partition differs from workload")
    authority = value["canonical_head_authority"]
    if (not isinstance(expected_authority_identity, dict)
            or not canonical_equal(authority, expected_authority_identity)):
        raise ValueError("checkpoint campaign v2 canonical authority is invalid")
    return deepcopy(value)


def load_campaign_identity_v2(
        path, *, expected_campaign_sha256, expected_workload,
        expected_authority_identity):
    path = Path(path)
    descriptor = None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise ValueError("checkpoint campaign v2 file is not a bounded regular file")
        content = b""
        while len(content) < before.st_size:
            chunk = os.read(descriptor, before.st_size - len(content))
            if not chunk:
                raise ValueError("checkpoint campaign v2 file is truncated")
            content += chunk
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)):
            raise ValueError("checkpoint campaign v2 file changed during read")
    except OSError as error:
        raise ValueError(
            f"checkpoint campaign v2 file is unavailable or unsafe: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    def strict_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("checkpoint campaign v2 contains a duplicate key")
            result[key] = item
        return result
    try:
        value = json.loads(content, object_pairs_hook=strict_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"checkpoint campaign v2 JSON is invalid: {error}") from error
    return validate_campaign_identity_v2(
        value,
        expected_campaign_sha256=expected_campaign_sha256,
        expected_workload=expected_workload,
        expected_authority_identity=expected_authority_identity,
    )


def charge_attempt_reservation_v2(reconciliation, reservation):
    reservation = _validate_accounting(reservation)
    bounds = reconciliation.get("resource_bounds")
    if not isinstance(bounds, dict) or set(bounds) != set(reservation):
        raise ValueError("checkpoint campaign v2 resource-bound schema mismatch")
    charged = {}
    for name, reserved in reservation.items():
        bound = bounds[name]
        lower = bound.get("lower") if isinstance(bound, dict) else None
        upper = bound.get("upper") if isinstance(bound, dict) else None
        time_field = name.endswith("_s")
        lower_number = (
            float(lower)
            if not isinstance(lower, bool) and isinstance(lower, (int, float))
            else float("nan")
        )
        valid_lower = (
            isinstance(lower, (int, float) if time_field else int)
            and not isinstance(lower, bool)
            and isfinite(lower_number) and lower_number >= 0.0
        )
        valid_upper = (
            upper is None or (
                not isinstance(upper, bool)
                and isinstance(upper, (int, float) if time_field else int)
                and isfinite(float(upper))
                and float(upper) >= lower_number
            )
        )
        if (not isinstance(bound, dict) or set(bound) != {"lower", "upper"}
                or not valid_lower or not valid_upper):
            raise ValueError("checkpoint campaign v2 resource bound is invalid")
        charge = reserved if bound["upper"] is None else bound["upper"]
        if float(bound["lower"]) > float(reserved) or float(charge) > float(reserved):
            raise ValueError("checkpoint campaign v2 attempt exceeded its reservation")
        if name.endswith("_s"):
            charged[name] = float(charge)
        elif type(charge) is not int:
            raise ValueError("checkpoint campaign v2 integer charge is invalid")
        else:
            charged[name] = charge
    return charged


def reconcile_campaign_attempt_v2(
        campaign, *, expected_campaign_sha256, expected_workload,
        expected_authority_identity,
        run_manifest_path, checkpoint_root):
    campaign = validate_campaign_identity_v2(
        campaign,
        expected_campaign_sha256=expected_campaign_sha256,
        expected_workload=expected_workload,
        expected_authority_identity=expected_authority_identity,
    )
    if (campaign["checkpoint_contract"]["validator_sha256"]
            != checkpoint_validator_sha256()):
        raise ValueError("checkpoint campaign v2 validator identity is stale")
    projection = validate_palace_execution_witness(run_manifest_path)
    expected_native_digest = hashlib.sha256(
        campaign["native_campaign_identity"].encode()
    ).hexdigest()
    expected_checkpoint = {
        "path": str(Path(checkpoint_root).resolve()),
        "native_campaign_identity": campaign["native_campaign_identity"],
    }
    if (projection["workload_sha256"] != campaign["workload_sha256"]
            or projection.get("checkpoint_enabled") is not True
            or not canonical_equal(projection.get("checkpoint"), expected_checkpoint)
            or projection.get("native_campaign_digest") != expected_native_digest
            or not canonical_equal(
                projection.get("ordered_terminals"),
                campaign["ordered_terminals"])):
        raise ValueError("checkpoint campaign v2 attempt identity mismatch")
    partition = campaign["checkpoint_partition"]
    inventory = validate_native_checkpoint(
        checkpoint_root,
        campaign_identity=campaign["native_campaign_identity"],
        ordered_terminal_indices=[
            terminal["index"] for terminal in campaign["ordered_terminals"]
        ],
        process_count=partition["process_count"],
        global_true_dofs=partition["global_true_dofs"],
        partition=partition["local_true_dofs"],
    )
    expected_prefix = max(
        projection["prefix_before"], projection["prefix_after"],
    )
    terminal_count = len(campaign["ordered_terminals"])
    if (inventory["prefix"] != expected_prefix
            or projection["prefix_before"] > terminal_count
            or projection["prefix_after"] > terminal_count
            or (projection["outcome"] == "completed"
                and expected_prefix != terminal_count)
            or (projection["outcome"] != "completed"
                and projection["final_artifacts"] is not None)):
        raise ValueError("checkpoint campaign v2 attempt prefix is inconsistent")
    bounds = deepcopy(projection["resource_bounds"])
    retained_bound = bounds["retained_checkpoint_bytes"]
    if (retained_bound["upper"] is not None and retained_bound != {
            "lower": inventory["retained_bytes"],
            "upper": inventory["retained_bytes"]}):
        raise ValueError("checkpoint campaign v2 retained-byte witness mismatch")
    bounds["retained_checkpoint_bytes"] = {
        "lower": inventory["retained_bytes"],
        "upper": inventory["retained_bytes"],
    }
    payload = {
        "format": "palace-checkpoint-attempt-reconciliation-v2",
        "campaign_sha256": campaign["content_sha256"],
        "execution_witness": projection["run_manifest"],
        "workload_sha256": projection["workload_sha256"],
        "resource_decision_sha256": _sha256(
            projection["resource_decision_sha256"],
            "attempt resource decision SHA-256",
        ),
        "outcome": projection["outcome"],
        "prefix_before": projection["prefix_before"],
        "observed_prefix_after": projection["prefix_after"],
        "prefix_after": inventory["prefix"],
        "loaded_rhs_indices": projection["loaded_rhs_indices"],
        "new_rhs_indices": projection["new_rhs_indices"],
        "resource_bounds": bounds,
        "causal_classification": projection["causal_classification"],
        "checkpoint_root": expected_checkpoint["path"],
        "checkpoint_inventory": inventory,
        "final_artifacts": projection["final_artifacts"],
    }
    return {**payload, "content_sha256": canonical_sha256(payload)}


def validate_campaign_identity(value):
    if not isinstance(value, dict) or set(value) != {
            "format", "workload_sha256", "ordered_terminals",
            "checkpoint_format_sha256", "resource_policy_sha256",
            "cumulative_caps", "content_sha256"}:
        raise ValueError("checkpoint campaign identity schema mismatch")
    if value["format"] != CAMPAIGN_FORMAT:
        raise ValueError("checkpoint campaign identity format mismatch")
    unsigned = dict(value)
    content_sha256 = unsigned.pop("content_sha256")
    _sha256(value["workload_sha256"], "workload SHA-256")
    if content_sha256 != canonical_sha256(unsigned):
        raise ValueError("checkpoint campaign identity content hash mismatch")
    _sha256(value["checkpoint_format_sha256"], "checkpoint format SHA-256")
    _sha256(value["resource_policy_sha256"], "resource policy SHA-256")
    terminals = value["ordered_terminals"]
    if not isinstance(terminals, list) or not terminals:
        raise ValueError("checkpoint campaign terminal roster is missing")
    for expected_index, terminal in enumerate(terminals, 1):
        if (not isinstance(terminal, dict) or set(terminal) != {
                "index", "name", "attribute"}
                or terminal["index"] != expected_index
                or type(terminal["index"]) is not int
                or not isinstance(terminal["name"], str) or not terminal["name"]
                or type(terminal["attribute"]) is not int
                or terminal["attribute"] <= 0):
            raise ValueError("checkpoint campaign terminal identity is invalid")
    if (len({item["name"] for item in terminals}) != len(terminals)
            or len({item["attribute"] for item in terminals}) != len(terminals)):
        raise ValueError("checkpoint campaign terminal roster is not unique")
    caps = value["cumulative_caps"]
    integer_caps = {
        "iterations", "solves", "bytes_written", "bytes_read",
        "stdout_bytes", "stderr_bytes", "checkpoint_write_bytes",
        "checkpoint_read_bytes", "retained_output_bytes",
        "retained_checkpoint_bytes", "peak_rss_bytes",
    }
    if not isinstance(caps, dict) or set(caps) != {
            "attempts", "wall_time_s", "cpu_time_s", *integer_caps}:
        raise ValueError("checkpoint campaign cumulative-cap schema mismatch")
    _nonnegative_int(caps["attempts"], "attempt cap")
    if caps["attempts"] <= 0:
        raise ValueError("checkpoint campaign attempt cap must be positive")
    _positive_number(caps["wall_time_s"], "wall-time cap")
    _positive_number(caps["cpu_time_s"], "CPU-time cap")
    for name in integer_caps:
        _nonnegative_int(caps[name], name.replace("_", " "))
    return deepcopy(value)


def _validate_accounting(accounting):
    integer_fields = {
        "iterations", "solves", "bytes_written", "bytes_read",
        "stdout_bytes", "stderr_bytes", "checkpoint_write_bytes",
        "checkpoint_read_bytes", "retained_output_bytes",
        "retained_checkpoint_bytes", "peak_rss_bytes",
    }
    if not isinstance(accounting, dict) or set(accounting) != {
            "wall_time_s", "cpu_time_s", *integer_fields}:
        raise ValueError("checkpoint attempt accounting schema mismatch")
    return {
        "wall_time_s": _nonnegative_number(accounting["wall_time_s"], "wall time"),
        "cpu_time_s": _nonnegative_number(accounting["cpu_time_s"], "CPU time"),
        **{
            name: _nonnegative_int(accounting[name], name.replace("_", " "))
            for name in integer_fields
        },
    }


ADDITIVE_ACCOUNTING = {
    "wall_time_s", "cpu_time_s", "iterations", "solves", "bytes_written",
    "bytes_read", "stdout_bytes", "stderr_bytes", "checkpoint_write_bytes",
    "checkpoint_read_bytes",
}
MAXIMUM_ACCOUNTING = {
    "retained_output_bytes", "retained_checkpoint_bytes", "peak_rss_bytes",
}


def _accumulate_accounting(records):
    totals = {name: 0.0 if name.endswith("_s") else 0
              for name in ADDITIVE_ACCOUNTING | MAXIMUM_ACCOUNTING}
    for record in records:
        for name in ADDITIVE_ACCOUNTING:
            totals[name] += record[name]
        for name in MAXIMUM_ACCOUNTING:
            totals[name] = max(totals[name], record[name])
    return totals


def _atomic_write(path, value, *, exclusive=False):
    path = Path(path)
    content = _canonical_bytes(value) + b"\n"
    if exclusive:
        exclusive_publish_bytes(path, content)
        return
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    temporary = f".{path.name}.tmp.{secrets.token_hex(16)}"
    descriptor = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600, dir_fd=directory,
        )
        view = memoryview(content)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short write while publishing ledger head")
            view = view[count:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.chmod(temporary, 0o444, dir_fd=directory, follow_symlinks=False)
        os.replace(
            temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


class CheckpointCampaignLedger:
    def __init__(self, root, *, authority_key, expected_campaign_sha256,
                 expected_head_sha256):
        if fcntl is None:
            raise ValueError("checkpoint campaigns require POSIX flock semantics")
        supplied = Path(root)
        if supplied.is_symlink():
            raise ValueError("checkpoint campaign root must not be a symlink")
        self.root = supplied.absolute()
        self.campaign_path = self.root / "campaign.json"
        self.authority_path = self.root / "authority.json"
        self.entries = self.root / "entries"
        self.head_path = self.root / "head.json"
        self.lock_path = self.root / "writer.lock"
        self._authority_key = _authority_key(authority_key)
        self.expected_campaign_sha256 = _sha256(
            expected_campaign_sha256, "expected campaign SHA-256",
        )
        self.expected_head_sha256 = _sha256(
            expected_head_sha256, "expected head SHA-256",
        )
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        self._lock_fd = os.open(self.lock_path, os.O_RDWR | nofollow)
        if not stat.S_ISREG(os.fstat(self._lock_fd).st_mode):
            os.close(self._lock_fd)
            raise ValueError("checkpoint campaign writer lock is invalid")

    def __del__(self):
        descriptor = getattr(self, "_lock_fd", None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._lock_fd = None

    def _mac(self, kind, value):
        content = kind.encode() + b"\0" + _canonical_bytes(value)
        return hmac.new(self._authority_key, content, hashlib.sha256).hexdigest()

    def _signed(self, kind, value):
        signed = {**value, "authority_mac": self._mac(kind, value)}
        return {**signed, "content_sha256": canonical_sha256(signed)}

    def _validate_signed(self, kind, value):
        if not isinstance(value, dict):
            raise ValueError(f"checkpoint campaign {kind} is malformed")
        unsigned = dict(value)
        digest = unsigned.pop("content_sha256", None)
        authority_mac = unsigned.pop("authority_mac", None)
        _sha256(digest, f"{kind} content SHA-256")
        _sha256(authority_mac, f"{kind} authority MAC")
        if (not hmac.compare_digest(authority_mac, self._mac(kind, unsigned))
                or digest != canonical_sha256({**unsigned, "authority_mac": authority_mac})):
            raise ValueError(f"checkpoint campaign {kind} authority mismatch")
        return unsigned

    @classmethod
    def create(cls, root, campaign, *, authority_key, trusted_campaign_sha256):
        campaign = validate_campaign_identity(campaign)
        if campaign["content_sha256"] != _sha256(
                trusted_campaign_sha256, "trusted campaign SHA-256"):
            raise ValueError("checkpoint campaign differs from trusted authorization")
        root = Path(root)
        if root.is_symlink():
            raise ValueError("checkpoint campaign root must not be a symlink")
        root.mkdir(parents=False)
        entries = root / "entries"
        entries.mkdir()
        lock_path = root / "writer.lock"
        lock_path.touch(mode=0o600, exist_ok=False)
        parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        temporary = object.__new__(cls)
        temporary._authority_key = _authority_key(authority_key)
        campaign_auth = {
            "format": AUTHORITY_FORMAT,
            "campaign_sha256": campaign["content_sha256"],
            "authority_mac": temporary._mac("campaign", campaign),
        }
        _atomic_write(root / "campaign.json", campaign, exclusive=True)
        _atomic_write(root / "authority.json", campaign_auth, exclusive=True)
        head_payload = {
            "format": HEAD_FORMAT,
            "campaign_sha256": campaign["content_sha256"],
            "sequence": 0,
            "entry_sha256": None,
            "prefix": 0,
            "active_attempt": None,
            "terminal": False,
        }
        head = temporary._signed("head", head_payload)
        _atomic_write(root / "head.json", head, exclusive=True)
        ledger = cls(
            root,
            authority_key=authority_key,
            expected_campaign_sha256=campaign["content_sha256"],
            expected_head_sha256=head["content_sha256"],
        )
        ledger.validate()
        return ledger

    @classmethod
    def open(cls, root, *, authority_key, expected_campaign_sha256,
             expected_head_sha256):
        ledger = cls(
            root,
            authority_key=authority_key,
            expected_campaign_sha256=expected_campaign_sha256,
            expected_head_sha256=expected_head_sha256,
        )
        ledger.validate()
        return ledger

    @contextmanager
    def _lock(self):
        lock_fd = self._lock_fd
        if lock_fd is None:
            raise ValueError("checkpoint campaign writer lock is closed")
        descriptor_stat = os.fstat(lock_fd)
        path_stat = os.stat(self.lock_path, follow_symlinks=False)
        if (not stat.S_ISREG(path_stat.st_mode)
                or (descriptor_stat.st_dev, descriptor_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)):
            raise ValueError("checkpoint campaign writer lock was replaced")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        locked_stat = os.stat(self.lock_path, follow_symlinks=False)
        if ((descriptor_stat.st_dev, descriptor_stat.st_ino)
                != (locked_stat.st_dev, locked_stat.st_ino)):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            raise ValueError("checkpoint campaign writer lock changed during acquisition")
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _load_json(self, path, label):
        path = Path(path)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | nofollow)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(f"checkpoint campaign {label} is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                return json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"checkpoint campaign {label} is invalid: {error}") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _open_contained_file(self, path, label):
        path = Path(path).absolute()
        try:
            relative = path.relative_to(self.root.parent)
        except ValueError as error:
            raise ValueError(f"checkpoint attempt {label} escapes containment") from error
        if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError(f"checkpoint attempt {label} path is invalid")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(
            self.root.parent, os.O_RDONLY | os.O_DIRECTORY | nofollow,
        )
        try:
            for component in relative.parts[:-1]:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | nofollow,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = child
            return path, os.open(
                relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=directory,
            )
        except OSError as error:
            raise ValueError(
                f"checkpoint attempt {label} is unavailable or unsafe: {error}"
            ) from error
        finally:
            os.close(directory)

    def _artifact_identity(self, path, label):
        path, descriptor = self._open_contained_file(path, label)
        digest = hashlib.sha256()
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(f"checkpoint attempt {label} is not a regular file")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(descriptor)
        return {"path": str(path), "sha256": digest.hexdigest()}

    def _artifact_size(self, path, label):
        _, descriptor = self._open_contained_file(path, label)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"checkpoint attempt {label} is not a regular file")
            return metadata.st_size
        finally:
            os.close(descriptor)

    def _load_content_document(self, path, label):
        _, descriptor = self._open_contained_file(path, label)
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                value = json.load(stream)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"checkpoint attempt {label} is invalid: {error}") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not isinstance(value, dict) or "content_sha256" not in value:
            raise ValueError(f"checkpoint attempt {label} lacks content identity")
        unsigned = dict(value)
        digest = unsigned.pop("content_sha256")
        _sha256(digest, f"{label} content SHA-256")
        if digest != canonical_sha256(unsigned):
            raise ValueError(f"checkpoint attempt {label} content identity mismatch")
        return value

    def _validate_attempt_documents(
            self, *, run_path, inventory_path, campaign, attempt_id,
            prefix_before, prefix_after, outcome, accounting, causal):
        inventory = self._load_content_document(
            inventory_path, "checkpoint inventory",
        )
        if (set(inventory) != {
                "format", "campaign_sha256", "prefix", "terminal_indices",
                "files", "retained_bytes", "content_sha256"}
                or inventory["format"] != "palace-checkpoint-inventory-v1"
                or inventory["campaign_sha256"] != campaign["content_sha256"]
                or inventory["prefix"] != prefix_after
                or inventory["terminal_indices"]
                != list(range(1, prefix_after + 1))
                or not isinstance(inventory["files"], list)
                or (prefix_after > 0 and not inventory["files"])):
            raise ValueError("checkpoint attempt inventory schema or prefix is invalid")
        retained = 0
        seen_paths = set()
        for record in inventory["files"]:
            if (not isinstance(record, dict)
                    or set(record) != {"path", "sha256", "bytes"}
                    or record["path"] in seen_paths
                    or type(record["bytes"]) is not int or record["bytes"] < 0):
                raise ValueError("checkpoint attempt inventory file record is invalid")
            seen_paths.add(record["path"])
            identity = self._artifact_identity(record["path"], "checkpoint file")
            if identity != {"path": record["path"], "sha256": record["sha256"]}:
                raise ValueError("checkpoint attempt inventory file identity mismatch")
            size = self._artifact_size(record["path"], "checkpoint file")
            if size != record["bytes"]:
                raise ValueError("checkpoint attempt inventory file size mismatch")
            retained += size
        if (type(inventory["retained_bytes"]) is not int
                or inventory["retained_bytes"] != retained
                or retained != accounting["retained_checkpoint_bytes"]):
            raise ValueError("checkpoint attempt retained checkpoint accounting mismatch")
        run = self._load_content_document(run_path, "run manifest")
        if (set(run) != {
                "format", "campaign_sha256", "attempt_id", "prefix_before",
                "prefix_after", "outcome", "loaded_rhs_indices",
                "new_rhs_indices", "accounting", "causal_classification",
                "checkpoint_inventory_sha256", "witnesses", "content_sha256"}
                or run["format"] != "palace-checkpoint-attempt-v1"
                or run["campaign_sha256"] != campaign["content_sha256"]
                or run["attempt_id"] != attempt_id
                or run["prefix_before"] != prefix_before
                or run["prefix_after"] != prefix_after
                or run["outcome"] != outcome
                or run["loaded_rhs_indices"] != list(range(1, prefix_before + 1))
                or run["new_rhs_indices"]
                != list(range(prefix_before + 1, prefix_after + 1))
                or run["accounting"] != accounting
                or run["causal_classification"] != causal
                or run["checkpoint_inventory_sha256"]
                != self._artifact_identity(
                    inventory_path, "checkpoint inventory",
                )["sha256"]
                or not isinstance(run["witnesses"], list)):
            raise ValueError("checkpoint attempt run manifest is invalid or unreconciled")
        roles = set()
        witness_bytes = {}
        for record in run["witnesses"]:
            if (not isinstance(record, dict)
                    or set(record) != {"role", "path", "sha256", "bytes"}
                    or record["role"] in roles
                    or record["role"] not in {
                        "stdout", "stderr", "event_log", "resource_telemetry",
                    }
                    or type(record["bytes"]) is not int or record["bytes"] < 0):
                raise ValueError("checkpoint attempt witness record is invalid")
            roles.add(record["role"])
            identity = self._artifact_identity(record["path"], record["role"])
            size = self._artifact_size(record["path"], record["role"])
            if (identity != {"path": record["path"], "sha256": record["sha256"]}
                    or size != record["bytes"]):
                raise ValueError("checkpoint attempt witness identity mismatch")
            witness_bytes[record["role"]] = size
        if (roles != {"stdout", "stderr", "event_log", "resource_telemetry"}
                or witness_bytes["stdout"] != accounting["stdout_bytes"]
                or witness_bytes["stderr"] != accounting["stderr_bytes"]):
            raise ValueError("checkpoint attempt witness accounting mismatch")

    def _entry_path(self, sequence, digest):
        return self.entries / f"{sequence:06d}-{digest}.json"

    def _append(self, head, payload):
        entry_payload = {
            "format": ENTRY_FORMAT,
            "campaign_sha256": head["campaign_sha256"],
            "sequence": head["sequence"] + 1,
            "previous_entry_sha256": head["entry_sha256"],
            **payload,
        }
        entry = self._signed("entry", entry_payload)
        digest = entry["content_sha256"]
        _atomic_write(self._entry_path(entry["sequence"], digest), entry, exclusive=True)
        return digest, entry

    def _publish_head(self, head):
        signed = self._signed("head", head)
        _atomic_write(self.head_path, signed)
        self.expected_head_sha256 = signed["content_sha256"]
        return signed

    def register_attempt(self, attempt_id, *, prefix_before,
                         resource_decision_sha256, resource_reservation):
        if not isinstance(attempt_id, str) or not ATTEMPT_ID_RE.fullmatch(attempt_id):
            raise ValueError("checkpoint attempt ID is invalid")
        _sha256(resource_decision_sha256, "resource decision SHA-256")
        reservation = _validate_accounting(resource_reservation)
        with self._lock():
            state = self.validate()
            head = state["head"]
            campaign = state["campaign"]
            if head["terminal"] or head["active_attempt"] is not None:
                raise ValueError("checkpoint campaign cannot register another attempt")
            if type(prefix_before) is not int or prefix_before != head["prefix"]:
                raise ValueError("checkpoint attempt prefix does not match ledger head")
            if attempt_id in state["attempt_ids"]:
                raise ValueError("checkpoint attempt ID is already registered")
            finished = [entry["accounting"] for entry in state["entries"]
                        if entry["event"] == "attempt_finished"]
            if len(finished) >= campaign["cumulative_caps"]["attempts"]:
                raise ValueError("checkpoint campaign attempt cap is exhausted")
            projected = _accumulate_accounting([*finished, reservation])
            for name, value in projected.items():
                if value > campaign["cumulative_caps"][name]:
                    raise ValueError(
                        f"checkpoint campaign remaining {name} capacity is inadequate"
                    )
            digest, entry = self._append(head, {
                "event": "attempt_registered",
                "attempt_id": attempt_id,
                "prefix_before": prefix_before,
                "resource_decision_sha256": resource_decision_sha256,
                "resource_reservation": reservation,
            })
            self._publish_head({
                **head,
                "sequence": entry["sequence"],
                "entry_sha256": digest,
                "active_attempt": attempt_id,
            })
            return digest, entry

    def finish_attempt(self, attempt_id, *, outcome, prefix_after,
                       run_manifest_path, checkpoint_inventory_path,
                       accounting, causal_classification):
        if outcome not in {"wall_timeout", "completed", "terminal_failure"}:
            raise ValueError("checkpoint attempt outcome is invalid")
        normalized = _validate_accounting(accounting)
        if (not isinstance(causal_classification, dict)
                or set(causal_classification) != {
                    "version", "monitor_initiated", "limit",
                    "diagnostics_before_kill"}
                or causal_classification["version"] != "palace-causal-timeout-v1"
                or type(causal_classification["monitor_initiated"]) is not bool
                or (causal_classification["limit"] is not None
                    and not isinstance(causal_classification["limit"], str))
                or not isinstance(
                    causal_classification["diagnostics_before_kill"], list,
                )
                or any(not isinstance(value, str) or not value
                       for value in causal_classification["diagnostics_before_kill"])):
            raise ValueError("checkpoint attempt causal classification is invalid")
        if outcome == "wall_timeout" and causal_classification != {
                "version": "palace-causal-timeout-v1",
                "monitor_initiated": True,
                "limit": "wall_time",
                "diagnostics_before_kill": [],
        }:
            raise ValueError("wall-time outcome lacks a clean causal witness")
        run_manifest = self._artifact_identity(run_manifest_path, "run manifest")
        checkpoint_inventory = self._artifact_identity(
            checkpoint_inventory_path, "checkpoint inventory",
        )
        with self._lock():
            state = self.validate()
            head = state["head"]
            campaign = state["campaign"]
            if head["active_attempt"] != attempt_id:
                raise ValueError("checkpoint attempt is not the active ledger writer")
            registration = state["entries"][-1]
            reservation = registration["resource_reservation"]
            if any(normalized[name] > reservation[name] for name in normalized):
                raise ValueError("checkpoint attempt exceeded its preregistered reservation")
            terminal_count = len(campaign["ordered_terminals"])
            if (type(prefix_after) is not int or prefix_after < head["prefix"]
                    or prefix_after > terminal_count
                    or (prefix_after == head["prefix"]
                        and outcome != "terminal_failure")):
                raise ValueError("checkpoint attempt did not advance a valid contiguous prefix")
            if outcome == "completed" and prefix_after != terminal_count:
                raise ValueError("completed checkpoint attempt lacks the full prefix")
            if outcome == "wall_timeout" and prefix_after == terminal_count:
                raise ValueError("wall-time checkpoint attempt cannot claim complete prefix")
            self._validate_attempt_documents(
                run_path=run_manifest_path,
                inventory_path=checkpoint_inventory_path,
                campaign=campaign,
                attempt_id=attempt_id,
                prefix_before=head["prefix"],
                prefix_after=prefix_after,
                outcome=outcome,
                accounting=normalized,
                causal=causal_classification,
            )
            finished = [entry["accounting"] for entry in state["entries"]
                        if entry["event"] == "attempt_finished"]
            totals = _accumulate_accounting([*finished, normalized])
            caps = campaign["cumulative_caps"]
            for name, value in totals.items():
                if value > caps[name]:
                    raise ValueError(f"checkpoint campaign cumulative {name} cap exceeded")
            digest, entry = self._append(head, {
                "event": "attempt_finished",
                "attempt_id": attempt_id,
                "outcome": outcome,
                "prefix_before": head["prefix"],
                "prefix_after": prefix_after,
                "run_manifest": run_manifest,
                "checkpoint_inventory": checkpoint_inventory,
                "causal_classification": deepcopy(causal_classification),
                "accounting": normalized,
            })
            self._publish_head({
                **head,
                "sequence": entry["sequence"],
                "entry_sha256": digest,
                "prefix": prefix_after,
                "active_attempt": None,
                "terminal": outcome in {"completed", "terminal_failure"},
            })
            return digest, entry

    def validate(self, *, _entry_limit=None):
        if not self.root.is_dir() or self.root.is_symlink():
            raise ValueError("checkpoint campaign root is unavailable")
        expected_root = {
            "campaign.json", "authority.json", "entries", "head.json",
            "writer.lock",
        }
        if {path.name for path in self.root.iterdir()} != expected_root:
            raise ValueError("checkpoint campaign root contains unknown files")
        if not self.entries.is_dir() or self.entries.is_symlink():
            raise ValueError("checkpoint campaign entries directory is invalid")
        campaign = validate_campaign_identity(
            self._load_json(self.campaign_path, "identity"),
        )
        if campaign["content_sha256"] != self.expected_campaign_sha256:
            raise ValueError("checkpoint campaign differs from external anchor")
        authority = self._load_json(self.authority_path, "authority")
        if (not isinstance(authority, dict) or set(authority) != {
                "format", "campaign_sha256", "authority_mac"}
                or authority["format"] != AUTHORITY_FORMAT
                or authority["campaign_sha256"] != campaign["content_sha256"]
                or not isinstance(authority["authority_mac"], str)
                or not hmac.compare_digest(
                    authority["authority_mac"], self._mac("campaign", campaign),
                )):
            raise ValueError("checkpoint campaign authority is invalid")
        signed_head = self._load_json(self.head_path, "head")
        if signed_head.get("content_sha256") != self.expected_head_sha256:
            raise ValueError("checkpoint campaign head differs from external anchor")
        head = self._validate_signed("head", signed_head)
        if set(head) != {
                "format", "campaign_sha256", "sequence", "entry_sha256",
                "prefix", "active_attempt", "terminal"}:
            raise ValueError("checkpoint campaign head schema mismatch")
        if (head["format"] != HEAD_FORMAT
                or head["campaign_sha256"] != campaign["content_sha256"]
                or type(head["sequence"]) is not int or head["sequence"] < 0
                or type(head["prefix"]) is not int or head["prefix"] < 0
                or head["prefix"] > len(campaign["ordered_terminals"])
                or type(head["terminal"]) is not bool):
            raise ValueError("checkpoint campaign head identity is invalid")
        files = sorted(self.entries.iterdir(), key=lambda path: path.name)
        if _entry_limit is not None:
            if (type(_entry_limit) is not int or _entry_limit < 0
                    or len(files) != _entry_limit + 1):
                raise ValueError("checkpoint campaign orphan recovery shape is invalid")
            files = files[:_entry_limit]
        entries = []
        previous = None
        prefix = 0
        active = None
        active_reservation = None
        terminal = False
        attempt_ids = set()
        finished_accounting = []
        for sequence, path in enumerate(files, 1):
            if path.is_symlink():
                raise ValueError("checkpoint campaign ledger entry is invalid")
            signed_entry = self._load_json(path, "ledger entry")
            digest = signed_entry.get("content_sha256")
            _sha256(digest, "ledger entry SHA-256")
            if path.name != f"{sequence:06d}-{digest}.json":
                raise ValueError("checkpoint campaign ledger entry filename mismatch")
            entry = self._validate_signed("entry", signed_entry)
            if (entry.get("format") != ENTRY_FORMAT
                    or entry.get("campaign_sha256") != campaign["content_sha256"]
                    or entry.get("sequence") != sequence
                    or entry.get("previous_entry_sha256") != previous):
                raise ValueError("checkpoint campaign ledger chain is invalid")
            event = entry.get("event")
            attempt_id = entry.get("attempt_id")
            if event == "attempt_registered":
                if set(entry) != {
                        "format", "campaign_sha256", "sequence",
                        "previous_entry_sha256", "event", "attempt_id",
                        "prefix_before", "resource_decision_sha256",
                        "resource_reservation"}:
                    raise ValueError("checkpoint campaign registration schema mismatch")
                reservation = _validate_accounting(entry["resource_reservation"])
                if (active is not None or terminal or attempt_id in attempt_ids
                        or not isinstance(attempt_id, str)
                        or not ATTEMPT_ID_RE.fullmatch(attempt_id)
                        or entry.get("prefix_before") != prefix):
                    raise ValueError("checkpoint campaign registration sequence is invalid")
                _sha256(entry.get("resource_decision_sha256"), "resource decision SHA-256")
                projected = _accumulate_accounting([
                    *finished_accounting, reservation,
                ])
                if any(projected[name] > campaign["cumulative_caps"][name]
                       for name in projected):
                    raise ValueError("checkpoint campaign registration exceeds remaining caps")
                active = attempt_id
                active_reservation = reservation
                attempt_ids.add(attempt_id)
            elif event == "attempt_finished":
                if set(entry) != {
                        "format", "campaign_sha256", "sequence",
                        "previous_entry_sha256", "event", "attempt_id", "outcome",
                        "prefix_before", "prefix_after", "run_manifest",
                        "checkpoint_inventory", "causal_classification",
                        "accounting"}:
                    raise ValueError("checkpoint campaign completion schema mismatch")
                if active != attempt_id or entry.get("prefix_before") != prefix:
                    raise ValueError("checkpoint campaign completion sequence is invalid")
                after = entry.get("prefix_after")
                outcome = entry.get("outcome")
                terminal_count = len(campaign["ordered_terminals"])
                if (type(after) is not int or after < prefix or after > terminal_count
                        or (after == prefix and outcome != "terminal_failure")
                        or outcome not in {"wall_timeout", "completed", "terminal_failure"}
                        or (outcome == "completed" and after != terminal_count)
                        or (outcome == "wall_timeout" and after == terminal_count)):
                    raise ValueError("checkpoint campaign prefix or outcome is invalid")
                for name in ("run_manifest", "checkpoint_inventory"):
                    identity = entry[name]
                    if (not isinstance(identity, dict)
                            or set(identity) != {"path", "sha256"}
                            or identity != self._artifact_identity(
                                identity["path"], name.replace("_", " "),
                            )):
                        raise ValueError(f"checkpoint campaign {name} identity mismatch")
                causal = entry["causal_classification"]
                if outcome == "wall_timeout" and causal != {
                        "version": "palace-causal-timeout-v1",
                        "monitor_initiated": True,
                        "limit": "wall_time",
                        "diagnostics_before_kill": [],
                }:
                    raise ValueError("checkpoint campaign wall timeout is not causal")
                accounting = _validate_accounting(entry["accounting"])
                self._validate_attempt_documents(
                    run_path=entry["run_manifest"]["path"],
                    inventory_path=entry["checkpoint_inventory"]["path"],
                    campaign=campaign,
                    attempt_id=attempt_id,
                    prefix_before=prefix,
                    prefix_after=after,
                    outcome=outcome,
                    accounting=accounting,
                    causal=causal,
                )
                if active_reservation is None or any(
                        accounting[name] > active_reservation[name]
                        for name in accounting):
                    raise ValueError("checkpoint campaign accounting exceeds reservation")
                finished_accounting.append(accounting)
                totals = _accumulate_accounting(finished_accounting)
                if any(totals[name] > campaign["cumulative_caps"][name]
                       for name in totals):
                    raise ValueError("checkpoint campaign cumulative cap exceeded")
                if len(finished_accounting) > campaign["cumulative_caps"]["attempts"]:
                    raise ValueError("checkpoint campaign attempt cap exceeded")
                prefix = after
                active = None
                active_reservation = None
                terminal = outcome in {"completed", "terminal_failure"}
            else:
                raise ValueError("checkpoint campaign ledger event is unknown")
            entries.append(entry)
            previous = digest
        if (head["sequence"] != len(entries)
                or head["entry_sha256"] != previous
                or head["prefix"] != prefix
                or head["active_attempt"] != active
                or head["terminal"] != terminal):
            raise ValueError("checkpoint campaign head does not match ledger replay")
        return {
            "campaign": campaign,
            "head": head,
            "head_sha256": signed_head["content_sha256"],
            "entries": entries,
            "attempt_ids": attempt_ids,
            "accounting": _accumulate_accounting(finished_accounting),
        }

    def recover_orphan(self, orphan_sha256):
        orphan_sha256 = _sha256(orphan_sha256, "orphan entry SHA-256")
        with self._lock():
            signed_head = self._load_json(self.head_path, "head")
            head = self._validate_signed("head", signed_head)
            state = self.validate(_entry_limit=head["sequence"])
            files = sorted(self.entries.iterdir(), key=lambda path: path.name)
            orphan_path = files[-1]
            signed_orphan = self._load_json(orphan_path, "orphan ledger entry")
            if (signed_orphan.get("content_sha256") != orphan_sha256
                    or orphan_path.name != (
                        f"{head['sequence'] + 1:06d}-{orphan_sha256}.json"
                    )):
                raise ValueError("checkpoint campaign orphan differs from recovery anchor")
            orphan = self._validate_signed("entry", signed_orphan)
            if (orphan.get("sequence") != head["sequence"] + 1
                    or orphan.get("previous_entry_sha256") != head["entry_sha256"]
                    or orphan.get("campaign_sha256")
                    != state["campaign"]["content_sha256"]):
                raise ValueError("checkpoint campaign orphan does not extend the trusted head")
            if orphan.get("event") == "attempt_registered":
                recovered_head = {
                    **head,
                    "sequence": orphan["sequence"],
                    "entry_sha256": orphan_sha256,
                    "active_attempt": orphan.get("attempt_id"),
                }
            elif orphan.get("event") == "attempt_finished":
                recovered_head = {
                    **head,
                    "sequence": orphan["sequence"],
                    "entry_sha256": orphan_sha256,
                    "prefix": orphan.get("prefix_after"),
                    "active_attempt": None,
                    "terminal": orphan.get("outcome") in {
                        "completed", "terminal_failure",
                    },
                }
            else:
                raise ValueError("checkpoint campaign orphan event is unknown")
            self._publish_head(recovered_head)
            return self.validate()

    def require_matrix_access(self):
        self.validate()
        raise ValueError(
            "checkpoint campaign matrix access requires final-attempt witness v2"
        )
