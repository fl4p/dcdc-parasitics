#!/usr/bin/env python3
"""Palace campaign recovery and adversarial regressions."""
import shutil

from palace_campaign import validate_campaign_identity
from palace_head_authority import CanonicalHeadAuthority
from test_palace_campaign import (
    AUTHORITY_KEY, CanonicalLedgerPublicationV2, CheckpointCampaignLedger,
    Path, accounting, campaign, campaign_v2, canonical_sha256, causal,
    charge_attempt_reservation_v2, create_ledger, deepcopy, finish, json,
    palace_campaign, palace_ledger_v2, projection_v2, pytest,
    reconcile_campaign_attempt_v2, register, registration_recovery,
    registration_transition,
)


def test_campaign_v2_pending_recovery_rederives_successor(tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    predecessor = json.loads(ledger.head_path.read_text())
    entry, successor = registration_transition(campaign, predecessor)
    real_publish = palace_ledger_v2.exclusive_publish_json

    def interrupt_entry(path, value):
        if Path(path).parent == ledger.entries:
            raise OSError("injected crash before entry publication")
        return real_publish(path, value)

    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", interrupt_entry)
    with pytest.raises(OSError, match="before entry publication"):
        ledger._publish_transition(entry, successor)
    monkeypatch.setattr(palace_ledger_v2, "exclusive_publish_json", real_publish)
    pending = json.loads(ledger.pending_path.read_text())
    pending["successor_head"]["prefix"] = 1
    successor_unsigned = dict(pending["successor_head"])
    successor_unsigned.pop("content_sha256")
    pending["successor_head"]["content_sha256"] = canonical_sha256(
        successor_unsigned
    )
    pending_unsigned = dict(pending)
    pending_unsigned.pop("content_sha256")
    pending["content_sha256"] = canonical_sha256(pending_unsigned)
    ledger.pending_path.write_text(json.dumps(pending))
    ledger.close()
    reopened = CanonicalLedgerPublicationV2(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    with pytest.raises(ValueError, match="pending successor is not derived"):
        reopened._reconcile_pending_transition(
            expected_registration=registration_recovery(entry),
        )
    assert authority.read(campaign["content_sha256"])["head_sha256"] == predecessor[
        "content_sha256"
    ]


def test_campaign_v2_publication_reconciles_crash_before_external_cas(
        tmp_path, monkeypatch):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    predecessor = json.loads(ledger.head_path.read_text())
    entry, successor = registration_transition(campaign, predecessor)
    real_cas = CanonicalHeadAuthority.compare_and_swap

    def interrupt_cas(*args, **kwargs):
        raise OSError("injected crash before external CAS")

    monkeypatch.setattr(
        CanonicalHeadAuthority, "compare_and_swap", interrupt_cas,
    )
    with pytest.raises(OSError, match="injected crash"):
        ledger._publish_transition(entry, successor)
    assert authority.read(campaign["content_sha256"])["head_sha256"] == predecessor[
        "content_sha256"
    ]
    monkeypatch.setattr(CanonicalHeadAuthority, "compare_and_swap", real_cas)
    ledger.close()
    ledger = CanonicalLedgerPublicationV2(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    assert ledger._reconcile_pending_transition(expected_registration=registration_recovery(entry))["content_sha256"] == successor["content_sha256"]


def test_campaign_v2_publication_cleans_only_regular_stale_temporaries(tmp_path):
    campaign, workload, authority = campaign_v2(tmp_path)
    ledger = CanonicalLedgerPublicationV2.create(
        tmp_path / "ledger", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    ledger.close()
    root_temp = ledger.root / (".head.json.tmp." + "a" * 32)
    entry_temp = ledger.entries / (".entry.json.tmp." + "b" * 32)
    root_temp.write_bytes(b"partial")
    entry_temp.write_bytes(b"partial")
    reopened = CanonicalLedgerPublicationV2(
        ledger.root, campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    assert not root_temp.exists()
    assert not entry_temp.exists()
    reopened.close()


def test_campaign_v2_publication_rejects_cloned_local_fork(tmp_path):
    campaign, workload, authority = campaign_v2(tmp_path)
    first = CanonicalLedgerPublicationV2.create(
        tmp_path / "first", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    shutil.copytree(first.root, tmp_path / "clone")
    clone = CanonicalLedgerPublicationV2(
        tmp_path / "clone", campaign, authority=authority,
        expected_campaign_sha256=campaign["content_sha256"],
        expected_workload=workload,
    )
    predecessor = json.loads(first.head_path.read_text())
    first_entry, first_head = registration_transition(
        campaign, predecessor, "first-attempt",
    )
    first._publish_transition(first_entry, first_head)
    clone_entry, clone_head = registration_transition(
        campaign, predecessor, "fork-attempt",
    )
    with pytest.raises(ValueError, match="canonical authority"):
        clone._publish_transition(clone_entry, clone_head)
    assert not list(clone.entries.iterdir())


def test_campaign_v2_reconciliation_rejects_exact_retained_disagreement(
        tmp_path, monkeypatch):
    value, workload_value, authority = campaign_v2(tmp_path)
    projection = projection_v2(value, workload_value, checkpoint_root=tmp_path / "checkpoint")
    projection["resource_bounds"]["retained_checkpoint_bytes"] = {
        "lower": 100, "upper": 100,
    }
    monkeypatch.setattr(
        palace_campaign, "validate_palace_execution_witness",
        lambda path: deepcopy(projection),
    )
    monkeypatch.setattr(
        palace_campaign, "validate_native_checkpoint",
        lambda *args, **kwargs: {"prefix": 1, "retained_bytes": 101},
    )
    with pytest.raises(ValueError, match="retained-byte witness mismatch"):
        reconcile_campaign_attempt_v2(
            value, expected_campaign_sha256=value["content_sha256"],
            expected_workload=workload_value,
            expected_authority_identity=authority.identity,
            run_manifest_path=tmp_path / "run.json",
            checkpoint_root=tmp_path / "checkpoint",
        )


def test_campaign_v2_reservation_charge_rejects_observed_overrun():
    reservation = accounting()
    reconciliation = {
        "resource_bounds": {
            name: {"lower": value, "upper": value}
            for name, value in reservation.items()
        }
    }
    reconciliation["resource_bounds"]["peak_rss_bytes"]["lower"] += 1
    reconciliation["resource_bounds"]["peak_rss_bytes"]["upper"] += 1
    with pytest.raises(ValueError, match="exceeded its reservation"):
        charge_attempt_reservation_v2(reconciliation, reservation)


@pytest.mark.parametrize("mutation", ["nan_time", "fractional_integer"])
def test_campaign_v2_reservation_charge_rejects_invalid_numeric_bounds(mutation):
    reservation = accounting()
    bounds = {
        name: {"lower": value, "upper": value}
        for name, value in reservation.items()
    }
    if mutation == "nan_time":
        bounds["wall_time_s"] = {"lower": float("nan"), "upper": None}
    else:
        bounds["solves"] = {"lower": 0.5, "upper": 1.0}
    with pytest.raises(ValueError, match="resource bound is invalid"):
        charge_attempt_reservation_v2({"resource_bounds": bounds}, reservation)


def test_campaign_v2_reconciliation_rejects_rebound_workload(
        tmp_path, monkeypatch):
    value, workload_value, authority = campaign_v2(tmp_path)
    monkeypatch.setattr(
        palace_campaign, "validate_palace_execution_witness",
        lambda path: {"workload_sha256": "0" * 64},
    )
    with pytest.raises(ValueError, match="attempt identity mismatch"):
        reconcile_campaign_attempt_v2(value, expected_campaign_sha256=value["content_sha256"], expected_workload=workload_value, expected_authority_identity=authority.identity, run_manifest_path=tmp_path / "run.json", checkpoint_root=tmp_path / "checkpoint")


def test_campaign_identity_is_strict_and_content_addressed():
    value = campaign()
    assert validate_campaign_identity(value) == value
    changed = deepcopy(value)
    changed["ordered_terminals"][1]["name"] = "A"
    unsigned = dict(changed)
    unsigned.pop("content_sha256")
    changed["content_sha256"] = canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="not unique"):
        validate_campaign_identity(changed)


def test_attempt_chain_requires_external_anchor_and_complete_prefix(tmp_path):
    ledger = create_ledger(tmp_path)
    with pytest.raises(ValueError, match="matrix access"):
        ledger.require_matrix_access()
    register(ledger, "attempt-1", 0)
    finish(ledger, tmp_path, "attempt-1", "wall_timeout", 1)
    register(ledger, "attempt-2", 1)
    finish(ledger, tmp_path, "attempt-2", "completed", 2)
    with pytest.raises(ValueError, match="final-attempt witness v2"):
        ledger.require_matrix_access()
    state = ledger.validate()
    assert state["head"]["prefix"] == 2
    assert len(state["entries"]) == 4


def test_registration_rejects_inadequate_remaining_capacity_before_launch(tmp_path):
    ledger = create_ledger(tmp_path, campaign(attempts=1, wall_time_s=15.0))
    with pytest.raises(ValueError, match="remaining wall_time_s"):
        register(ledger, "attempt-1", 0, accounting(wall=20.0))
    assert ledger.validate()["entries"] == []


def test_terminal_failure_can_record_zero_progress(tmp_path):
    ledger = create_ledger(tmp_path)
    register(ledger, "attempt-1", 0)
    finish(
        ledger, tmp_path, "attempt-1", "terminal_failure", 0,
        accounting(wall=0.0, solves=0),
    )
    with pytest.raises(ValueError, match="matrix access"):
        ledger.require_matrix_access()


def test_wall_timeout_requires_clean_monitor_causality(tmp_path):
    ledger = create_ledger(tmp_path)
    register(ledger, "attempt-1", 0)
    run = tmp_path / "unread-run.json"
    inventory = tmp_path / "unread-inventory.json"
    bad = causal("wall_timeout")
    bad["diagnostics_before_kill"] = ["warning"]
    with pytest.raises(ValueError, match="clean causal"):
        ledger.finish_attempt(
            "attempt-1", outcome="wall_timeout", prefix_after=1,
            run_manifest_path=run, checkpoint_inventory_path=inventory,
            accounting=accounting(), causal_classification=bad,
        )


def test_local_rehash_cannot_rebind_campaign_or_entry(tmp_path):
    ledger = create_ledger(tmp_path)
    register(ledger, "attempt-1", 0)
    campaign_path = ledger.campaign_path
    value = json.loads(campaign_path.read_text())
    value["workload_sha256"] = "f" * 64
    unsigned = dict(value)
    unsigned.pop("content_sha256")
    value["content_sha256"] = canonical_sha256(unsigned)
    campaign_path.chmod(0o600)
    campaign_path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="external anchor|authority"):
        ledger.validate()


def test_external_head_anchor_detects_rollback(tmp_path):
    ledger = create_ledger(tmp_path)
    initial_head = ledger.head_path.read_bytes()
    register(ledger, "attempt-1", 0)
    latest_anchor = ledger.expected_head_sha256
    ledger.head_path.chmod(0o600)
    ledger.head_path.write_bytes(initial_head)
    with pytest.raises(ValueError, match="external anchor"):
        CheckpointCampaignLedger.open(
            ledger.root,
            authority_key=AUTHORITY_KEY,
            expected_campaign_sha256=ledger.expected_campaign_sha256,
            expected_head_sha256=latest_anchor,
        )


def test_artifact_tamper_invalidates_chain(tmp_path):
    ledger = create_ledger(tmp_path)
    register(ledger, "attempt-1", 0)
    finish(ledger, tmp_path, "attempt-1", "terminal_failure", 0,
           accounting(wall=0.0, solves=0))
    run = tmp_path / "attempt-1-run.json"
    run.write_text("tampered")
    with pytest.raises(ValueError, match="identity mismatch"):
        ledger.validate()


def test_orphaned_append_requires_explicit_digest_and_recovers(tmp_path, monkeypatch):
    ledger = create_ledger(tmp_path)
    original = ledger._publish_head
    monkeypatch.setattr(
        ledger, "_publish_head",
        lambda _head: (_ for _ in ()).throw(OSError("injected head crash")),
    )
    with pytest.raises(OSError, match="injected"):
        register(ledger, "attempt-1", 0)
    orphan = next(ledger.entries.iterdir())
    digest = orphan.name.split("-", 1)[1].removesuffix(".json")
    with pytest.raises(ValueError, match="head does not match"):
        ledger.validate()
    monkeypatch.setattr(ledger, "_publish_head", original)
    with pytest.raises(ValueError, match="differs from recovery anchor"):
        ledger.recover_orphan("f" * 64)
    state = ledger.recover_orphan(digest)
    assert state["head"]["active_attempt"] == "attempt-1"


def test_root_symlink_and_replaced_lock_are_rejected(tmp_path):
    ledger = create_ledger(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(ledger.root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        CheckpointCampaignLedger.open(
            alias,
            authority_key=AUTHORITY_KEY,
            expected_campaign_sha256=ledger.expected_campaign_sha256,
            expected_head_sha256=ledger.expected_head_sha256,
        )
    ledger.lock_path.unlink()
    ledger.lock_path.write_text("")
    with pytest.raises(ValueError, match="lock was replaced"):
        register(ledger, "attempt-1", 0)
