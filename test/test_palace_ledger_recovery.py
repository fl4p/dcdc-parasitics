#!/usr/bin/env python3
from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "test"))

import palace_ledger_v2  # noqa: E402
import process_monitor  # noqa: E402
from palace_ledger_v2 import (  # noqa: E402
    CanonicalLedgerPublicationV2,
    abort_campaign_attempt_on_error,
    bind_campaign_abort_checkpoint_root,
)
from process_monitor import ProcessCleanupError  # noqa: E402
from test_palace_campaign import (  # noqa: E402
    _zero_accounting,
    campaign_v2,
    ledger_reconciliation,
)


def test_abort_clears_active_registration_and_allows_fresh_attempt(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    monkeypatch.setattr(
        palace_ledger_v2, "validate_native_checkpoint",
        lambda *args, **kwargs: {"prefix": 1},
    )

    @abort_campaign_attempt_on_error
    def fail_after_registration(*, campaign_ledger, attempt_id):
        bind_campaign_abort_checkpoint_root(tmp_path / "checkpoint")
        expected = {
            "resource_decision_sha256": "d" * 64,
            "resource_reservation": _zero_accounting(),
        }
        palace_ledger_v2._ABORT_REGISTRATION.set(expected)
        campaign_ledger._register_attempt(attempt_id, **expected)
        raise OSError("launch failed")

    with pytest.raises(OSError, match="launch failed"):
        fail_after_registration(
            campaign_ledger=ledger, attempt_id="attempt-1",
        )
    state = ledger._validate_local_state()
    assert state["active_registration"] is None
    assert state["head"]["attempts_finished"] == 1
    assert state["last_entry"]["event"] == "attempt_aborted"
    assert state["head"]["prefix"] == 1
    assert state["last_entry"]["reconciliation"]["prefix_after"] == 1
    ledger._register_attempt(
        "attempt-2", resource_decision_sha256="e" * 64,
        resource_reservation=_zero_accounting(),
    )
    assert ledger._validate_local_state()["active_registration"][
        "attempt_id"
    ] == "attempt-2"


def test_pending_abort_recovery_revalidates_checkpoint(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    expected = {
        "resource_decision_sha256": "d" * 64,
        "resource_reservation": _zero_accounting(),
    }
    ledger._register_attempt("attempt-1", **expected)
    inventory = {"prefix": 1}
    monkeypatch.setattr(
        palace_ledger_v2, "validate_native_checkpoint",
        lambda *args, **kwargs: deepcopy(inventory),
    )
    publish = palace_ledger_v2.exclusive_publish_json

    def interrupt_entry(path, value):
        if Path(path).parent == ledger.entries:
            raise OSError("injected abort publication crash")
        return publish(path, value)

    monkeypatch.setattr(
        palace_ledger_v2, "exclusive_publish_json", interrupt_entry,
    )
    with pytest.raises(OSError, match="abort publication crash"):
        ledger.abort_attempt_if_active(
            "attempt-1", reason="launch failed",
            checkpoint_root=tmp_path / "checkpoint",
            expected_registration=expected,
        )
    assert ledger.pending_path.exists()
    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", publish)
    inventory["prefix"] = 2
    with pytest.raises(
            ValueError, match="pending abort checkpoint is stale"):
        ledger.recover_pending_attempt()
    inventory["prefix"] = 1
    monkeypatch.setattr(
        palace_ledger_v2, "derive_attempt_reservation",
        lambda *args, **kwargs: {
            "decision_sha256": "d" * 64,
            "reservation": _zero_accounting(),
        },
    )
    with pytest.raises(
            ValueError, match="abort recovery rejects registration inputs"):
        ledger.recover_pending_attempt(
            resource_decision={}, execution_snapshot={}, trusted_policy={},
        )
    head = ledger.recover_pending_attempt()
    assert head["prefix"] == 1
    assert ledger._validate_local_state()["active_registration"] is None


def test_pending_abort_recovery_pure_replays_canonical_transition(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    expected = {
        "resource_decision_sha256": "d" * 64,
        "resource_reservation": _zero_accounting(),
    }
    ledger._register_attempt("attempt-1", **expected)
    monkeypatch.setattr(
        palace_ledger_v2, "validate_native_checkpoint",
        lambda *args, **kwargs: {"prefix": 1},
    )
    matches = CanonicalLedgerPublicationV2._external_matches
    heads = []

    def interrupt_after_cas(self, head):
        heads.append(head)
        if len(heads) == 2:
            raise OSError("injected post-CAS crash")
        return matches(self, head)

    monkeypatch.setattr(
        CanonicalLedgerPublicationV2, "_external_matches",
        interrupt_after_cas,
    )
    with pytest.raises(OSError, match="post-CAS crash"):
        ledger.abort_attempt_if_active(
            "attempt-1", reason="launch failed",
            checkpoint_root=tmp_path / "checkpoint",
            expected_registration=expected,
        )
    monkeypatch.setattr(
        CanonicalLedgerPublicationV2, "_external_matches", matches,
    )
    assert ledger.pending_path.exists()

    def vanished_root(*args, **kwargs):
        raise ValueError("campaign v2 checkpoint root is missing")

    monkeypatch.setattr(
        palace_ledger_v2, "validate_native_checkpoint", vanished_root,
    )
    head = ledger.recover_pending_attempt()
    assert head["prefix"] == 1
    assert not ledger.pending_path.exists()
    state = ledger._validate_local_state()
    assert state["active_registration"] is None
    assert authority.read(campaign["content_sha256"])["head_sha256"] == head[
        "content_sha256"
    ]


def test_untagged_interrupt_with_live_tree_skips_abort(tmp_path):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )

    @abort_campaign_attempt_on_error
    def fail_with_live_tree(*, campaign_ledger, attempt_id):
        expected = {
            "resource_decision_sha256": "d" * 64,
            "resource_reservation": _zero_accounting(),
        }
        palace_ledger_v2._ABORT_REGISTRATION.set(expected)
        campaign_ledger._register_attempt(attempt_id, **expected)
        bind_campaign_abort_checkpoint_root(tmp_path / "checkpoint")
        process_monitor._TREE_MAY_BE_ALIVE.set(True)
        raise KeyboardInterrupt("escaped untagged")

    with pytest.raises(KeyboardInterrupt):
        fail_with_live_tree(campaign_ledger=ledger, attempt_id="attempt-1")
    assert not process_monitor.solver_tree_may_be_alive()
    assert ledger._validate_local_state()["active_registration"][
        "attempt_id"
    ] == "attempt-1"


def test_abort_reason_bounds_and_decorator_truncation(tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    expected = {
        "resource_decision_sha256": "d" * 64,
        "resource_reservation": _zero_accounting(),
    }
    for reason in ("", "x" * 4097, None):
        with pytest.raises(ValueError, match="abort reason is invalid"):
            ledger.abort_attempt_if_active(
                "attempt-1", reason=reason,
                checkpoint_root=tmp_path / "checkpoint",
                expected_registration=expected,
            )
    monkeypatch.setattr(
        palace_ledger_v2, "validate_native_checkpoint",
        lambda *args, **kwargs: {"prefix": 1},
    )

    @abort_campaign_attempt_on_error
    def fail_verbose(*, campaign_ledger, attempt_id):
        bind_campaign_abort_checkpoint_root(tmp_path / "checkpoint")
        palace_ledger_v2._ABORT_REGISTRATION.set(expected)
        campaign_ledger._register_attempt(attempt_id, **expected)
        raise OSError("boom" * 4096)

    with pytest.raises(OSError, match="boom"):
        fail_verbose(campaign_ledger=ledger, attempt_id="attempt-1")
    entry = ledger._validate_local_state()["last_entry"]
    assert entry["event"] == "attempt_aborted"
    reason = entry["reconciliation"]["reason"]
    assert reason.startswith("OSError: boom")
    assert 0 < len(reason.encode()) <= 4096


def test_publication_size_limit_rejects_oversized_abort_entry(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    expected = {
        "resource_decision_sha256": "d" * 64,
        "resource_reservation": _zero_accounting(),
    }
    ledger._register_attempt("attempt-1", **expected)
    monkeypatch.setattr(
        palace_ledger_v2, "validate_native_checkpoint",
        lambda *args, **kwargs: {
            "prefix": 1, "padding": "x" * (1024 * 1024),
        },
    )
    with pytest.raises(ValueError, match="entry exceeds publication limit"):
        ledger.abort_attempt_if_active(
            "attempt-1", reason="launch failed",
            checkpoint_root=tmp_path / "checkpoint",
            expected_registration=expected,
        )
    assert not ledger.pending_path.exists()
    assert ledger._validate_local_state()["active_registration"][
        "attempt_id"
    ] == "attempt-1"


def test_abort_reconciles_same_attempt_pending_registration(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    expected = {
        "resource_decision_sha256": "d" * 64,
        "resource_reservation": _zero_accounting(),
    }
    publish = palace_ledger_v2.exclusive_publish_json

    def interrupt_entry(path, value):
        if Path(path).parent == ledger.entries:
            raise OSError("injected registration publication crash")
        return publish(path, value)

    monkeypatch.setattr(
        palace_ledger_v2, "exclusive_publish_json", interrupt_entry,
    )
    with pytest.raises(OSError, match="registration publication crash"):
        ledger._register_attempt("attempt-1", **expected)
    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", publish)
    assert ledger.pending_path.exists()
    monkeypatch.setattr(
        palace_ledger_v2, "validate_native_checkpoint",
        lambda *args, **kwargs: {"prefix": 1},
    )
    untrusted = dict(expected, resource_decision_sha256="e" * 64)
    with pytest.raises(
            ValueError, match="abort registration lacks trusted derivation"):
        ledger.abort_attempt_if_active(
            "attempt-1", reason="launch failed",
            checkpoint_root=tmp_path / "checkpoint",
            expected_registration=untrusted,
        )
    assert ledger.abort_attempt_if_active(
        "attempt-1", reason="launch failed",
        checkpoint_root=tmp_path / "checkpoint",
        expected_registration=expected,
    ) is True
    state = ledger._validate_local_state()
    assert state["active_registration"] is None
    assert state["last_entry"]["event"] == "attempt_aborted"


def test_abort_returns_false_on_same_attempt_pending_completion(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    expected = {
        "resource_decision_sha256": "d" * 64,
        "resource_reservation": _zero_accounting(),
    }
    ledger._register_attempt("attempt-1", **expected)
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ab",
    )
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    publish = palace_ledger_v2.exclusive_publish_json

    def interrupt_entry(path, value):
        if Path(path).parent == ledger.entries:
            raise OSError("injected completion publication crash")
        return publish(path, value)

    monkeypatch.setattr(
        palace_ledger_v2, "exclusive_publish_json", interrupt_entry,
    )
    with pytest.raises(OSError, match="completion publication crash"):
        ledger.finish_attempt(
            "attempt-1", run_manifest_path=tmp_path / "run.json",
            checkpoint_root=tmp_path / "checkpoint",
        )
    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", publish)
    assert ledger.abort_attempt_if_active(
        "attempt-1", reason="launch failed",
        checkpoint_root=tmp_path / "checkpoint",
        expected_registration=expected,
    ) is False
    assert ledger.pending_path.exists()


def test_cleanup_failure_keeps_canonical_attempt_active(tmp_path):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )

    @abort_campaign_attempt_on_error
    def fail_cleanup(*, campaign_ledger, attempt_id):
        expected = {
            "resource_decision_sha256": "d" * 64,
            "resource_reservation": _zero_accounting(),
        }
        palace_ledger_v2._ABORT_REGISTRATION.set(expected)
        campaign_ledger._register_attempt(attempt_id, **expected)
        bind_campaign_abort_checkpoint_root(tmp_path / "checkpoint")
        raise ProcessCleanupError("survivor")

    with pytest.raises(ProcessCleanupError, match="survivor"):
        fail_cleanup(campaign_ledger=ledger, attempt_id="attempt-1")
    assert ledger._validate_local_state()["active_registration"][
        "attempt_id"
    ] == "attempt-1"


def test_historical_completion_replay_does_not_reread_live_checkpoint(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ab",
    )
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    ledger.finish_attempt(
        "attempt-1", run_manifest_path=tmp_path / "run.json",
        checkpoint_root=tmp_path / "checkpoint",
    )

    def stale_live_root(*args, **kwargs):
        raise ValueError("live checkpoint advanced")

    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2", stale_live_root,
    )
    assert ledger._validate_local_state()["head"]["prefix"] == 1


def test_public_recovery_completes_interrupted_completion_without_registration_inputs(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    ledger._register_attempt(
        "attempt-1", resource_decision_sha256="d" * 64,
        resource_reservation=_zero_accounting(),
    )
    reconciliation = ledger_reconciliation(
        campaign, tmp_path, outcome="wall_timeout", before=0, after=1,
        witness="ab",
    )
    monkeypatch.setattr(
        palace_ledger_v2, "reconcile_campaign_attempt_v2",
        lambda *args, **kwargs: deepcopy(reconciliation),
    )
    publish = palace_ledger_v2.exclusive_publish_json

    def interrupt_entry(path, value):
        if Path(path).parent == ledger.entries:
            raise OSError("injected completion publication crash")
        return publish(path, value)

    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", interrupt_entry)
    with pytest.raises(OSError, match="completion publication crash"):
        ledger.finish_attempt(
            "attempt-1", run_manifest_path=tmp_path / "run.json",
            checkpoint_root=tmp_path / "checkpoint",
        )
    assert ledger.pending_path.exists()
    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", publish)
    ledger.close()
    reopened = CanonicalLedgerPublicationV2(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    head = reopened.recover_pending_attempt()
    state = reopened._validate_local_state()
    assert state["active_registration"] is None
    assert state["last_registration"]["attempt_id"] == "attempt-1"
    assert head["sequence"] == 2
    assert authority.read(campaign["content_sha256"])["head_sha256"] == head[
        "content_sha256"
    ]
