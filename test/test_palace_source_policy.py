#!/usr/bin/env python3
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import palace_source_policy  # noqa: E402


IDENTITY = ROOT / "docs/artifacts/palace-e4-source-v5" / (
    "palace-source-identity."
    "9a50e1ad35a4b6b644afdff791f312c75b8d58ff7bc01cc5669fe7faa16be94b.json"
)


def test_reviewed_source_identity_is_authorized():
    manifest = palace_source_policy.validate_authorized_palace_source_identity(IDENTITY)
    assert manifest["content_sha256"] in (
        palace_source_policy.AUTHORIZED_PALACE_SOURCE_IDENTITIES
    )


def test_unreviewed_source_identity_is_rejected(monkeypatch):
    monkeypatch.setattr(
        palace_source_policy, "validate_palace_source_identity",
        lambda path: {"content_sha256": "f" * 64},
    )
    with pytest.raises(ValueError, match="not authorized"):
        palace_source_policy.validate_authorized_palace_source_identity("candidate.json")
