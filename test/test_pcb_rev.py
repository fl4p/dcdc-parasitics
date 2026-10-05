#!/usr/bin/env python3
"""pcb_rev: read the board as committed at a revision, never the working copy.

Calibration: the working copy is edited after the commit (as another session's
uncommitted Fugu2 edits were, 2026-10-05: 971ab77 then dropped Net-(Q2-G)), and
the pinned read must return the COMMITTED bytes. Every failure (no repo, no such
commit, file absent from it, URL) must refuse, never fall back to the working copy.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import pcb_source  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "hw"
    (r / "board").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    (r / "board" / "b.kicad_pcb").write_text("COMMITTED-1")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "one")
    first = _git(r, "rev-parse", "HEAD")
    (r / "board" / "b.kicad_pcb").write_text("COMMITTED-2")
    _git(r, "commit", "-qam", "two")
    (r / "board" / "b.kicad_pcb").write_text("WORKING-COPY-EDIT")   # uncommitted
    return r, first


def test_reads_the_commit_not_the_working_copy(repo, tmp_path):
    r, first = repo
    out = tmp_path / "w"; out.mkdir()
    path, sha = pcb_source.resolve_pinned(str(r / "board" / "b.kicad_pcb"), first[:7], str(out))
    assert open(path).read() == "COMMITTED-1" and sha == first
    path = pcb_source.resolve_pcb_path(str(r / "board" / "b.kicad_pcb"), str(out), rev="HEAD")
    assert open(path).read() == "COMMITTED-2"


def test_config_relative_path_and_a_file_deleted_from_the_working_copy(repo, tmp_path):
    r, first = repo
    cfg = r / "cfg.yaml"; cfg.write_text("")
    os.remove(r / "board" / "b.kicad_pcb")                  # gone locally, still committed
    out = tmp_path / "w"; out.mkdir()
    path, _ = pcb_source.resolve_pinned("board/b.kicad_pcb", first, str(out), str(cfg))
    assert open(path).read() == "COMMITTED-1"


@pytest.mark.parametrize("case", ["no-commit", "not-in-commit", "not-a-repo", "url"])
def test_every_failure_refuses_and_never_falls_back(repo, tmp_path, case):
    r, first = repo
    out = tmp_path / "w"; out.mkdir()
    pcb, rev = str(r / "board" / "b.kicad_pcb"), first
    if case == "no-commit":
        rev = "0" * 40
    elif case == "not-in-commit":
        pcb = str(r / "board" / "other.kicad_pcb")
    elif case == "not-a-repo":
        plain = tmp_path / "plain"; plain.mkdir()
        (plain / "b.kicad_pcb").write_text("x"); pcb = str(plain / "b.kicad_pcb")
    elif case == "url":
        pcb = "https://github.com/org/repo/blob/main/b.kicad_pcb"
    with pytest.raises(SystemExit, match="pcb_rev"):
        pcb_source.resolve_pinned(pcb, rev, str(out))
    assert os.listdir(out) == []
