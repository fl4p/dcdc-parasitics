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


# ---- review of b8f4779 -------------------------------------------------------
@pytest.mark.parametrize("case", ["file-symlink", "dir-symlink", "outside-target"])
def test_a_symlink_in_the_working_copy_cannot_choose_the_board(repo, tmp_path, case):
    """A symlinked board or directory let the working copy pick WHICH committed file
    was read; a target outside the repo became '../x', which git show resolves
    against its own cwd and returned an unrelated committed file."""
    r, first = repo
    (r / "other").mkdir()
    (r / "other" / "b.kicad_pcb").write_text("OTHER")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "other")
    out = tmp_path / "w"; out.mkdir()
    if case == "file-symlink":
        link = r / "link.kicad_pcb"; link.symlink_to(r / "other" / "b.kicad_pcb")
    elif case == "dir-symlink":
        (r / "alias").symlink_to(r / "other"); link = r / "alias" / "b.kicad_pcb"
    else:
        outside = tmp_path / "elsewhere.kicad_pcb"; outside.write_text("x")
        link = r / "board" / "out.kicad_pcb"; link.symlink_to(outside)
    with pytest.raises(SystemExit, match="is a symlink in the working copy"):
        pcb_source.resolve_pinned(str(link), "HEAD", str(out))
    assert os.listdir(out) == []


def test_pinned_relative_path_ignores_the_working_copy(repo, tmp_path, monkeypatch):
    """Config-relative, always: a same-named file in the cwd -- even byte-identical in
    the working copy -- must not decide which committed board is read."""
    r, first = repo
    cfg = r / "board" / "cfg.yaml"; cfg.write_text("")
    cwd = tmp_path / "cwd"; cwd.mkdir()
    (cwd / "b.kicad_pcb").write_text("WORKING-COPY-EDIT")        # same bytes as r's edit
    monkeypatch.chdir(cwd)
    out = tmp_path / "w"; out.mkdir()
    path, _ = pcb_source.resolve_pinned("b.kicad_pcb", first, str(out), str(cfg))
    assert open(path).read() == "COMMITTED-1"


@pytest.mark.parametrize("value", ["", "   ", None, 7])
def test_an_empty_pin_refuses_instead_of_unpinning(repo, tmp_path, value):
    r, first = repo
    out = tmp_path / "w"; out.mkdir()
    with pytest.raises(SystemExit, match="pcb_rev"):
        pcb_source.resolve_pcb_path(str(r / "board" / "b.kicad_pcb"), str(out),
                                    rev=value if value is not None else "")
    with pytest.raises(SystemExit, match="empty"):
        pcb_source.check_pin(value, "config")


def test_the_extractor_refuses_an_empty_pin_from_yaml_or_cli(tmp_path):
    sys.path.insert(0, ROOT)
    import extract_parasitics as ep
    cfg = tmp_path / "c.yaml"
    cfg.write_text("pcb: b.kicad_pcb\npcb_rev: ''\nsw: SW\ngnd: GND\nout: o\n")
    with pytest.raises(SystemExit, match="empty"):
        ep.parse_args(["--config", str(cfg)])
    cfg.write_text("pcb: b.kicad_pcb\npcb_rev: abc1234\nsw: SW\ngnd: GND\nout: o\n")
    with pytest.raises(SystemExit, match="empty"):
        ep.parse_args(["--config", str(cfg), "--pcb-rev", ""])
    args = ep.parse_args(["--config", str(cfg)])
    assert args.pcb_rev == "abc1234"
