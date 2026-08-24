#!/usr/bin/env python3
from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "test"))

import palace_ledger_v2  # noqa: E402
from palace_ledger_v2 import CanonicalLedgerPublicationV2  # noqa: E402
from test_palace_campaign import (  # noqa: E402
    _zero_accounting,
    campaign_v2,
    ledger_reconciliation,
)


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
