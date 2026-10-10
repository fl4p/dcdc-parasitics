#!/usr/bin/env python3
"""pcb_rev: read the board as committed at a revision, never the working copy.

The contract (reviews of b8f4779, a9fd448, e2312f6, 8655cb2): for a fixed board path,
pcb_repo and full SHA, NOTHING done to the working copy changes the bytes returned --
edits, deletions, symlinks, nested .git dirs or files, core.worktree, empty
intermediate repositories. The in-commit path is computed from the two given
strings; git never discovers the repository from the board's location. Every
failure refuses; nothing falls back to the working copy.
"""
import os
import shutil
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
    (r / "board" / "deep").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "board" / "b.kicad_pcb").write_text("COMMITTED-1")
    (r / "b.kicad_pcb").write_text("ROOT-BOARD")
    (r / "board" / "deep" / "b.kicad_pcb").write_text("DEEP-BOARD")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "one")
    first = _git(r, "rev-parse", "HEAD")
    (r / "board" / "b.kicad_pcb").write_text("COMMITTED-2")
    _git(r, "commit", "-qam", "two")
    (r / "board" / "b.kicad_pcb").write_text("WORKING-COPY-EDIT")   # uncommitted
    return r, first


def _read(r, rev, tmp_path, pcb=None, name="w"):
    out = tmp_path / name
    out.mkdir(exist_ok=True)
    path, sha, rel = pcb_source.resolve_pinned(str(pcb or (r / "board" / "b.kicad_pcb")),
                                               rev, str(out), str(r))
    return open(path).read(), sha, rel


def test_reads_the_commit_not_the_working_copy(repo, tmp_path):
    r, first = repo
    text, sha, rel = _read(r, first[:7], tmp_path)
    assert (text, sha, rel) == ("COMMITTED-1", first, "board/b.kicad_pcb")
    assert _read(r, "HEAD", tmp_path, name="w2")[0] == "COMMITTED-2"


def test_config_relative_pcb_and_repo_with_the_file_deleted(repo, tmp_path):
    r, first = repo
    cfg = r / "cfg" / "c.yaml"
    cfg.parent.mkdir()
    cfg.write_text("")
    os.remove(r / "board" / "b.kicad_pcb")                  # gone locally, still committed
    out = tmp_path / "w"
    out.mkdir()
    path, _, _ = pcb_source.resolve_pinned("../board/b.kicad_pcb", first, str(out), "..",
                                           config_path=str(cfg), repo_config_path=str(cfg))
    assert open(path).read() == "COMMITTED-1"


@pytest.mark.parametrize("case", ["no-commit", "not-in-commit", "not-a-repo", "url",
                                  "no-repo", "outside-repo", "repo-itself"])
def test_every_failure_refuses_and_never_falls_back(repo, tmp_path, case):
    r, first = repo
    out = tmp_path / "w"
    out.mkdir()
    pcb, rev, rp = str(r / "board" / "b.kicad_pcb"), first, str(r)
    if case == "no-commit":
        rev = "0" * 40
    elif case == "not-in-commit":
        pcb = str(r / "board" / "other.kicad_pcb")
    elif case == "not-a-repo":
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "b.kicad_pcb").write_text("x")
        pcb, rp = str(plain / "b.kicad_pcb"), str(plain)
    elif case == "url":
        pcb = "https://github.com/org/repo/blob/main/b.kicad_pcb"
    elif case == "no-repo":
        rp = None
    elif case == "outside-repo":
        pcb = str(tmp_path / "b.kicad_pcb")
    else:
        pcb = str(r)
    with pytest.raises(SystemExit, match="pcb_rev"):
        pcb_source.resolve_pinned(pcb, rev, str(out), rp)
    assert os.listdir(out) == []


def _clone(r, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "--shared", str(r), str(other)], check=True)
    return other


@pytest.mark.parametrize("case", [
    "edit", "delete", "file-symlink", "dir-symlink-to-other-dir", "root-alias",
    "symlink-to-clone", "nested-git-dir", "git-file", "core-worktree",
    "empty-intermediate-repo"])
def test_nothing_in_the_working_copy_changes_the_bytes_read(repo, tmp_path, case):
    """Every case below changed the result of an earlier version (same path, same
    SHA, a different committed file or the working copy)."""
    r, first = repo
    sha = _git(r, "rev-parse", "HEAD")
    board = r / "board"
    if case == "edit":
        (board / "b.kicad_pcb").write_text("EDITED")
    elif case == "delete":
        os.remove(board / "b.kicad_pcb")
    elif case == "file-symlink":
        os.remove(board / "b.kicad_pcb")
        (board / "b.kicad_pcb").symlink_to(r / "b.kicad_pcb")
    elif case == "dir-symlink-to-other-dir":
        shutil.rmtree(board)
        board.symlink_to(tmp_path)
    elif case == "root-alias":
        shutil.rmtree(board)
        board.symlink_to(".")
    elif case == "symlink-to-clone":
        other = _clone(r, tmp_path)
        shutil.rmtree(board)
        board.symlink_to(other)
    elif case == "nested-git-dir":
        subprocess.run(["git", "-C", str(board), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(board), "fetch", "-q", str(r), sha], check=True)
    elif case == "git-file":
        (board / ".git").write_text(f"gitdir: {r / '.git'}\n")
    elif case == "core-worktree":
        (board / ".git").write_text(f"gitdir: {r / '.git'}\n")
        _git(r, "config", "core.worktree", str(board))
    else:
        subprocess.run(["git", "-C", str(board), "init", "-q"], check=True)
        (board / "deep" / ".git").write_text(f"gitdir: {r / '.git'}\n")
    pcb = board / ("deep" if case == "empty-intermediate-repo" else "") / "b.kicad_pcb"
    want = "DEEP-BOARD" if case == "empty-intermediate-repo" else "COMMITTED-2"
    try:
        text, got_sha, _ = _read(r, sha, tmp_path, pcb=pcb)
    except SystemExit:
        return                                   # refusing is also acceptable
    assert (text, got_sha) == (want, sha)


def test_an_untracked_nested_repository_is_legitimate(repo, tmp_path):
    """Fugu2 lives untracked inside the ~/dev monorepo; that must keep working."""
    r, first = repo
    mono = tmp_path / "mono"
    mono.mkdir()
    subprocess.run(["git", "-C", str(mono), "init", "-q"], check=True)
    nested = mono / "hw"
    os.rename(r, nested)
    text, sha, _ = _read(nested, first, tmp_path)
    assert (text, sha) == ("COMMITTED-1", first)


@pytest.mark.parametrize("value", ["", "   ", None, 7])
def test_an_empty_pin_refuses_instead_of_unpinning(repo, tmp_path, value):
    r, first = repo
    out = tmp_path / "w"
    out.mkdir()
    with pytest.raises(SystemExit, match="pcb_rev"):
        pcb_source.resolve_pinned(str(r / "board" / "b.kicad_pcb"),
                                  value if value is not None else "", str(out), str(r))
    with pytest.raises(SystemExit, match="empty"):
        pcb_source.check_pin(value, "config")


def test_the_extractor_refuses_an_empty_pin_from_yaml_or_cli(tmp_path):
    sys.path.insert(0, ROOT)
    import extract_parasitics as ep
    cfg = tmp_path / "c.yaml"
    cfg.write_text("pcb: b.kicad_pcb\npcb_rev: ''\nsw: SW\ngnd: GND\nout: o\n")
    with pytest.raises(SystemExit, match="empty"):
        ep.parse_args(["--config", str(cfg)])
    cfg.write_text("pcb: b.kicad_pcb\npcb_rev: abc1234\npcb_repo: .\nsw: SW\ngnd: GND\nout: o\n")
    with pytest.raises(SystemExit, match="empty"):
        ep.parse_args(["--config", str(cfg), "--pcb-rev", ""])
    args = ep.parse_args(["--config", str(cfg)])
    assert (args.pcb_rev, args.pcb_repo) == ("abc1234", ".")


def _main_resolution(monkeypatch, argv):
    """(pcb, rev, repo, pcb config_path, repo config_path) the extractor resolves with."""
    sys.path.insert(0, ROOT)
    import extract_parasitics as ep
    seen = {}

    def pinned(pcb, rev, workdir, repo, config_path=None, repo_config_path=None):
        seen.update(pcb=pcb, rev=rev, repo=repo, cfg=config_path, repo_cfg=repo_config_path)
        raise SystemExit("stop")

    def local(pcb, workdir, config_path=None, **kw):
        seen.update(pcb=pcb, rev=None, repo=None, cfg=config_path, repo_cfg=None)
        raise SystemExit("stop")
    monkeypatch.setattr(ep.pcb_source, "resolve_pinned", pinned)
    monkeypatch.setattr(ep.pcb_source, "resolve_pcb_path", local)
    monkeypatch.setattr(sys, "argv", ["extract_parasitics.py", *argv])
    with pytest.raises(SystemExit, match="stop"):
        ep.main()
    return seen


def test_which_directory_each_value_is_relative_to(tmp_path, monkeypatch):
    """A relative value resolves against the config only when the config supplied
    it; a CLI board replaces the config's pin and repository."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"pcb: b.kicad_pcb\npcb_rev: abc1234\npcb_repo: ..\nsw: SW\ngnd: GND\n"
                   f"out: {tmp_path / 'o'}\n")
    monkeypatch.chdir(tmp_path)
    s = _main_resolution(monkeypatch, ["--config", str(cfg)])
    assert (s["rev"], s["repo"], s["cfg"], s["repo_cfg"]) == ("abc1234", "..", str(cfg), str(cfg))
    s = _main_resolution(monkeypatch, ["--config", str(cfg), "x.kicad_pcb"])
    assert (s["pcb"], s["rev"], s["cfg"]) == ("x.kicad_pcb", None, None)
    s = _main_resolution(monkeypatch, ["--config", str(cfg), "x.kicad_pcb",
                                       "--pcb-rev", "def5678", "--pcb-repo", "r"])
    assert (s["pcb"], s["rev"], s["repo"], s["cfg"], s["repo_cfg"]) == \
        ("x.kicad_pcb", "def5678", "r", None, None)
    s = _main_resolution(monkeypatch, ["--config", str(cfg), "x.kicad_pcb",
                                       "--pcb-rev", "def5678"])
    assert s["repo"] is None                     # the config's repo went with its pcb
