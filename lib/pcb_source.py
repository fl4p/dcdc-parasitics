"""Resolve local or URL KiCad PCB inputs to a local file path."""
import hashlib
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request


def is_url(path):
    return urllib.parse.urlparse(path).scheme in ("http", "https")


def normalize_pcb_url(url):
    """Return a direct-download URL for supported PCB URL forms."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return url
    if parsed.netloc == "github.com" and "/blob/" in parsed.path:
        path = parsed.path.replace("/blob/", "/raw/", 1)
        return urllib.parse.urlunparse(parsed._replace(path=path))
    return url


def download_url(url, path):
    headers = {"User-Agent": "dcdc-tools-parasitics"}
    if urllib.parse.urlparse(url).netloc in ("github.com", "raw.githubusercontent.com"):
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as out:
            out.write(resp.read())
    except (urllib.error.URLError, OSError) as e:
        cmd = ["curl", "-fL", "--connect-timeout", "20", "--max-time", "120",
               "-A", headers["User-Agent"]]
        if "Authorization" in headers:
            cmd += ["-H", f"Authorization: {headers['Authorization']}"]
        cmd += ["-o", path, url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except OSError:
            raise SystemExit(f"{url}: failed to download PCB: {e}")
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or str(e)).strip()
            raise SystemExit(f"{url}: failed to download PCB: {detail}")


def file_sha256(path):
    """Return the SHA-256 hex digest of a local file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_pin(value, source):
    """A pcb_rev that is GIVEN must name something. `pcb_rev: ""`, `pcb_rev: null`
    and `--pcb-rev ''` read as false and silently selected the working copy -- an
    empty command-line value even overrode a valid YAML pin (review of b8f4779).
    Omit the key to leave a board unpinned; never pass an empty one."""
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"{source}: pcb_rev is given but empty ({value!r}); omit it "
                         f"to leave the board unpinned, or name a commit or tag")
    return value.strip()


def board_at_rev(path, rev, workdir, repo):
    """Write `path` as committed at git revision `rev` in the repository rooted at
    `repo` into `workdir`; return (new path, full commit sha, in-commit path).

    The in-commit path is `path` relative to `repo`, computed LEXICALLY from the
    two given strings. Neither the board nor any directory on its path is looked
    at, git does not DISCOVER the repository from the board's location (pcb_repo
    must itself be the root), and every object read is re-hashed: every
    working-copy trick that moved that discovery -- a symlinked directory, a nested
    `.git` or `.git` file, `core.worktree`, an empty intermediate repository --
    worked by changing which repository root, and therefore which in-commit path,
    the same board path meant (reviews of b8f4779, a9fd448, e2312f6, 8655cb2). With
    the in-commit path fixed by text, a full commit SHA selects the same blob in
    any object store that has the commit (git is content-addressed). A tag or a
    short hash is resolved in `repo`; meta records the full SHA it resolved to.

    Fails hard -- `repo` is not a git repository, the revision does not exist,
    the path leaves `repo`, or the file is not a regular file in that commit --
    rather than fall back to the working copy, which is exactly what the pin
    excludes."""
    path = os.path.abspath(path)                        # lexical: no symlink resolution
    repo = os.path.abspath(repo)
    rel = os.path.relpath(path, repo)
    if rel in (os.curdir, os.pardir) or rel.startswith(os.pardir + os.sep):
        raise SystemExit(f"pcb_rev {rev!r}: {path} is not inside pcb_repo {repo}")
    rel = rel.replace(os.sep, "/")

    def git(*args, text=True):
        # No replacement objects and no inherited GIT_* variables: `git replace`,
        # GIT_REPLACE_REF_BASE or a redirected GIT_DIR made the same full SHA return
        # other bytes (review of 4e4a605). The hash walk below re-checks regardless.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        try:
            return subprocess.run(["git", "--no-replace-objects", "-C", repo, *args],
                                  capture_output=True, text=text, env=env)
        except OSError as e:
            raise SystemExit(f"pcb_rev {rev!r}: cannot run git: {e}")

    # pcb_repo must BE the repository (work-tree root, or the git dir of a bare
    # repository): `git -C` searches upward, so pcb_repo=repo/sub silently read
    # repo's b.kicad_pcb for repo/sub/b.kicad_pcb (review of 4e4a605).
    bare = git("rev-parse", "--is-bare-repository")
    if bare.returncode:
        raise SystemExit(f"pcb_rev {rev!r}: pcb_repo {repo} is not a git repository")
    which = "--absolute-git-dir" if bare.stdout.strip() == "true" else "--show-toplevel"
    root = git("rev-parse", which).stdout.strip()
    try:
        same = bool(root) and os.path.samefile(root, repo)
    except OSError:
        same = False
    if not same:
        raise SystemExit(f"pcb_rev {rev!r}: pcb_repo {repo} is not a repository root "
                         f"(git resolves it to {root or '?'}); give the root itself")

    sha = git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if sha.returncode:
        raise SystemExit(f"pcb_rev {rev!r}: no such commit in pcb_repo {repo}")
    sha = sha.stdout.strip()
    data = _verified_blob(git, rev, sha, rel)
    out = os.path.join(workdir, f"{sha[:12]}-{os.path.basename(path)}")
    with open(out, "wb") as fh:
        fh.write(data)
    return out, sha, rel


def _verified_blob(git, rev, sha, rel):
    """The bytes of `rel` in commit `sha`, with EVERY object on the way re-hashed
    here: commit -> tree per path component -> blob. git cat-file does not verify
    what it returns, so an object store that answers a full SHA with other content
    (replacement objects, a swapped alternate, a forged loose object) is caught
    here and refused instead of being extracted under a correct-looking pin."""
    algo = {40: hashlib.sha1, 64: hashlib.sha256}.get(len(sha))
    if algo is None:
        raise SystemExit(f"pcb_rev {rev!r}: unexpected object id {sha!r}")

    def obj(kind, oid):
        r = git("cat-file", kind, oid, text=False)
        if r.returncode:
            raise SystemExit(f"pcb_rev {rev!r}: cannot read {kind} {oid[:12]}: "
                             f"{r.stderr.decode(errors='replace').strip()}")
        body = r.stdout
        if algo(f"{kind} {len(body)}".encode() + b"\0" + body).hexdigest() != oid:
            raise SystemExit(f"pcb_rev {rev!r}: {kind} {oid[:12]} does not hash to its "
                             f"id in this repository; refusing the pinned read")
        return body

    commit = obj("commit", sha)
    first = commit.split(b"\n", 1)[0]
    if not first.startswith(b"tree "):
        raise SystemExit(f"pcb_rev {rev!r}: commit {sha[:12]} has no tree line")
    oid, mode = first[5:].decode(), b"40000"
    raw = len(sha) // 2
    parts = rel.split("/")
    for k, name in enumerate(parts):
        if mode != b"40000":
            raise SystemExit(f"pcb_rev {rev!r}: {'/'.join(parts[:k])} is not a directory "
                             f"in commit {sha[:12]}")
        tree, pos, found = obj("tree", oid), 0, None
        while pos < len(tree):
            sp = tree.index(b" ", pos)
            nul = tree.index(b"\0", sp)
            if tree[sp + 1:nul].decode(errors="surrogateescape") == name:
                found = (tree[pos:sp], tree[nul + 1:nul + 1 + raw].hex())
                break
            pos = nul + 1 + raw
        if found is None:
            raise SystemExit(f"pcb_rev {rev!r}: {rel} is not in commit {sha[:12]}")
        mode, oid = found
    if mode not in (b"100644", b"100755"):
        kind = ", a symlink" if mode == b"120000" else ""
        raise SystemExit(f"pcb_rev {rev!r}: {rel} is not a regular file in commit "
                         f"{sha[:12]} (git mode {mode.decode()}{kind})")
    return obj("blob", oid)


def _anchor(value, config_path):
    """Absolute as given, else relative to the config file when it supplied the
    value, else to the cwd. Purely lexical."""
    if os.path.isabs(value):
        return value
    if config_path:
        return os.path.join(os.path.dirname(os.path.abspath(config_path)), value)
    return os.path.abspath(value)


def resolve_pinned(pcb, rev, workdir, repo, config_path=None, repo_config_path=None):
    """(path, full commit sha, in-commit path) of `pcb` as committed at `rev` in
    `repo`; see board_at_rev. `config_path` / `repo_config_path` is the config
    file that supplied `pcb` / `repo` (None when the command line did)."""
    rev = check_pin(rev, "pcb_rev")
    if is_url(pcb):
        raise SystemExit(f"pcb_rev {rev!r} applies to a local board in a git "
                         f"checkout, not to the URL {pcb}; pin the commit in the URL")
    if not isinstance(repo, str) or not repo.strip():
        raise SystemExit(f"pcb_rev {rev!r} needs pcb_repo (--pcb-repo): the repository "
                         f"root the board path is taken relative to. It is not "
                         f"discovered from the working copy, which could change it")
    return board_at_rev(_anchor(pcb, config_path), rev, workdir,
                        _anchor(repo, repo_config_path))


def merge_pin(yaml_args, cli_args, merged, warn=None):
    """The board/pin merge shared by all three tools; mutates `merged`.

    - A pin that is given must name something (check_pin).
    - A board named on the command line replaces the YAML's `pcb` AND its pin and
      repository: the explicit file is used as given unless the same command line
      pins it (loop_inductance_guard.py relies on this).
    - Which source supplied `pcb` and `pcb_repo` is recorded, because a relative
      value resolves against the config's directory only when the config gave it
      (reviews of a9fd448, e2312f6)."""
    for src, d in (("config", yaml_args), ("--pcb-rev", cli_args)):
        if "pcb_rev" in d:
            d["pcb_rev"] = check_pin(d["pcb_rev"], src)
    if "pcb" in cli_args and "pcb_rev" not in cli_args:
        if yaml_args.get("pcb_rev") and warn:
            warn(f"WARNING: board {cli_args['pcb']!r} given on the command line; the "
                 f"config's pcb_rev {yaml_args['pcb_rev']!r} pins the config's own pcb "
                 f"and is not applied to it. Pass --pcb-rev and --pcb-repo to pin it.\n")
        merged["pcb_rev"] = None
    if "pcb" in cli_args and "pcb_repo" not in cli_args:
        merged["pcb_repo"] = None             # the config's repository went with its pcb
    merged["pcb_from_cli"] = "pcb" in cli_args
    merged["pcb_repo_from_cli"] = "pcb_repo" in cli_args


def resolve_board(args, workdir):
    """(local board path, full pinned sha or None, in-commit path or None)."""
    cfg = getattr(args, "config", None)
    pcb_cfg = None if getattr(args, "pcb_from_cli", False) else cfg
    rev = getattr(args, "pcb_rev", None)
    if rev is not None:
        repo_cfg = None if getattr(args, "pcb_repo_from_cli", False) else cfg
        return resolve_pinned(args.pcb, rev, workdir, getattr(args, "pcb_repo", None),
                              config_path=pcb_cfg, repo_config_path=repo_cfg)
    return resolve_pcb_path(args.pcb, workdir, config_path=pcb_cfg), None, None


def resolve_pcb_path(pcb, workdir, config_path=None, downloader=download_url, rev=None,
                     repo=None):
    """Download URL PCB inputs to a temp file; local paths resolve against the
    current working directory first, then — when a YAML config supplied the
    path — against the config file's own directory. If both resolve to a real
    file but to *different* boards (by SHA-256), fail hard rather than guess.

    With `rev` (a commit hash or tag; YAML `pcb_rev`), the resolved local path
    only names the repository and file: the board is read from that commit, see
    board_at_rev. A URL with a rev is refused -- pin the URL itself instead."""
    if rev is not None:                     # "" is refused by check_pin, never "unpinned"
        return resolve_pinned(pcb, rev, workdir, repo, config_path)[0]
    if is_url(pcb):
        url = normalize_pcb_url(pcb)
        name = os.path.basename(urllib.parse.urlparse(url).path) or "board.kicad_pcb"
        if not name.endswith(".kicad_pcb"):
            name += ".kicad_pcb"
        path = os.path.join(workdir, name)
        downloader(url, path)
        return path

    return _resolve_local(pcb, config_path)


def _resolve_local(pcb, config_path):
    """cwd first, then the config's own directory; see resolve_pcb_path."""
    cwd_path = pcb if os.path.isabs(pcb) else os.path.abspath(pcb)
    cfg_path = None
    if config_path and not os.path.isabs(pcb):
        cfg_path = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(config_path)), pcb))

    cwd_exists = os.path.isfile(cwd_path)
    cfg_exists = cfg_path is not None and os.path.isfile(cfg_path)

    if cwd_exists and cfg_exists and cwd_path != cfg_path:
        if file_sha256(cwd_path) != file_sha256(cfg_path):
            raise SystemExit(
                f"PCB path {pcb!r} resolves to two different boards:\n"
                f"  cwd-relative:    {cwd_path}\n"
                f"  config-relative: {cfg_path}\n"
                f"Both files exist but differ (SHA-256 mismatch). Refusing to "
                f"guess which board to extract; remove one or pass an absolute path.")
        return cwd_path
    if cwd_exists:
        return cwd_path
    if cfg_exists:
        return cfg_path
    return cwd_path
