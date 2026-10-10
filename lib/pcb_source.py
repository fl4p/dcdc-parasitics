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


def board_at_rev(path, rev, workdir):
    """Write `path` as committed at git revision `rev` into `workdir`; return
    (new path, full commit sha). `path` locates the repository and the file in
    it; the working copy's bytes are never read, so uncommitted edits to the
    board cannot leak into an extraction pinned to a commit.

    The path is taken LEXICALLY inside the repository: a symlink -- the board
    file itself, or a directory between the repository root and it -- would let
    the working copy choose which committed file is read, and a target outside
    the repository became "../x" for `git show`, which Git resolves against its
    invocation directory (review of b8f4779). Both are refused, as is a path that
    is a symlink or not a regular file IN THE COMMIT. Symlinks above the
    repository root (macOS /tmp -> /private/tmp) are fine.

    Fails hard -- the path is not in a git checkout, the revision does not exist
    (rebased away, never fetched), or the file is not in that commit -- rather
    than fall back to the working copy, which is exactly what the pin excludes."""
    path = os.path.abspath(path)                       # lexical: no symlink resolution
    where = os.path.dirname(path)
    while not os.path.isdir(where):        # the file may not exist in the working copy
        parent = os.path.dirname(where)
        if parent == where:
            break
        where = parent

    def git(*args, text=True):
        try:
            return subprocess.run(["git", "-C", where, *args], capture_output=True,
                                  text=text)
        except OSError as e:
            raise SystemExit(f"pcb_rev {rev!r}: cannot run git: {e}")

    top = git("rev-parse", "--show-toplevel")
    if top.returncode:
        raise SystemExit(f"pcb_rev {rev!r}: {path} is not inside a git checkout "
                         f"({top.stderr.strip()})")
    root_real = os.path.realpath(top.stdout.strip())
    # The lexical repository root: the HIGHEST ancestor of `where` that is the
    # root directory. Taking the first match from below let an in-repo alias
    # (`board -> .`) pose as the root, so its own symlink was never checked and
    # the same path and pin read another committed file (review of a9fd448).
    # Identity is by inode, not by string: on a case-insensitive volume
    # `mainrepo` and `MainRepo` are one directory, not a symlink.
    lex_root = _lexical_root(where, root_real)
    if lex_root is None:
        raise SystemExit(f"pcb_rev {rev!r}: {path} reaches its repository "
                         f"{root_real} through a symlinked directory; give the "
                         f"real path")
    _refuse_shadowing_repo(lex_root, rev, path)
    rel = os.path.relpath(path, lex_root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise SystemExit(f"pcb_rev {rev!r}: {path} is outside the repository {root_real}")
    probe = lex_root
    for part in rel.split(os.sep):
        probe = os.path.join(probe, part)
        if os.path.islink(probe):
            raise SystemExit(f"pcb_rev {rev!r}: {probe} is a symlink in the working "
                             f"copy, so it would decide which committed file is read; "
                             f"give the real path inside the repository")
    rel = rel.replace(os.sep, "/")
    sha = git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if sha.returncode:
        raise SystemExit(f"pcb_rev {rev!r}: no such commit in {root_real}")
    sha = sha.stdout.strip()
    entry = git("ls-tree", "--full-tree", sha, "--", rel)
    mode = entry.stdout.split()[0] if entry.returncode == 0 and entry.stdout.strip() else None
    if mode is None:
        raise SystemExit(f"pcb_rev {rev!r}: {rel} is not in commit {sha[:12]} of {root_real}")
    if mode not in ("100644", "100755"):
        raise SystemExit(f"pcb_rev {rev!r}: {rel} is not a regular file in commit "
                         f"{sha[:12]} (git mode {mode}{', a symlink' if mode == '120000' else ''})")
    blob = git("cat-file", "blob", f"{sha}:{rel}", text=False)
    if blob.returncode:
        raise SystemExit(f"pcb_rev {rev!r}: cannot read {rel} at {sha[:12]}: "
                         f"{blob.stderr.decode(errors='replace').strip()}")
    out = os.path.join(workdir, f"{sha[:12]}-{os.path.basename(path)}")
    with open(out, "wb") as fh:
        fh.write(blob.stdout)
    return out, sha


def _lexical_root(start, root_real):
    """The HIGHEST lexical ancestor of `start` that is the directory `root_real`,
    or None. Identity is by inode, not by string: on a case-insensitive volume
    `mainrepo` and `MainRepo` are one directory, not a symlink."""
    root_st = os.stat(root_real)
    found, probe = None, start
    while True:
        try:
            st = os.stat(probe)
            if (st.st_dev, st.st_ino) == (root_st.st_dev, root_st.st_ino):
                found = probe
        except OSError:
            pass
        parent = os.path.dirname(probe)
        if parent == probe:
            return found
        probe = parent


def _refuse_shadowing_repo(lex_root, rev, path):
    """Refuse a repository root that sits inside ANOTHER work tree at a place
    that outer tree tracks. A nested `.git`, a `.git` file pointing at the outer
    git dir, or the board directory swapped for a symlink to another clone all
    made `git rev-parse` find a different repository from the working copy, so
    the same path and pin read a different committed file (review of e2312f6).
    An untracked nested repository (Fugu2 inside the ~/dev monorepo) and a
    registered submodule (gitlink) are legitimate and pass."""
    parent = os.path.dirname(lex_root)
    if parent == lex_root:
        return
    try:
        top = subprocess.run(["git", "-C", parent, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    except OSError as e:
        raise SystemExit(f"pcb_rev {rev!r}: cannot run git: {e}")
    if top.returncode:
        return                                  # not inside another work tree
    outer_real = os.path.realpath(top.stdout.strip())
    outer_lex = _lexical_root(parent, outer_real)
    if outer_lex is None:
        raise SystemExit(f"pcb_rev {rev!r}: {path}: the repository {lex_root} sits in "
                         f"{outer_real}, reached through a symlink; give the real path")
    rel = os.path.relpath(lex_root, outer_lex).replace(os.sep, "/")
    entries = []
    for args in (["ls-files", "-s", "--", rel], ["ls-tree", "-r", "HEAD", "--", rel]):
        r = subprocess.run(["git", "-C", outer_lex, *args], capture_output=True, text=True)
        if r.returncode == 0:
            entries += [ln.split(None, 1)[0] for ln in r.stdout.splitlines() if ln.strip()]
    if entries and any(mode != "160000" for mode in entries):
        raise SystemExit(f"pcb_rev {rev!r}: {lex_root} acts as a repository root but "
                         f"{outer_lex} tracks files under it; a nested .git, a .git "
                         f"file or a symlinked directory there would decide which "
                         f"committed board is read. Give the path in the real repository")


def resolve_pinned(pcb, rev, workdir, config_path=None):
    """(path, full commit sha) of `pcb` as committed at `rev`; see board_at_rev.

    The path is resolved WITHOUT looking at the working copy: absolute as given,
    else relative to the config file when the config supplied it, else to the
    cwd. Callers pass config_path=None for a board named on the command line: a
    relative CLI board means the invocation directory (review of a9fd448). The
    unpinned resolver's "cwd first, then config dir, and compare the two files"
    read live files, so deleting or editing one changed which committed board the
    same config selected (review of b8f4779)."""
    rev = check_pin(rev, "pcb_rev")
    if is_url(pcb):
        raise SystemExit(f"pcb_rev {rev!r} applies to a local board in a git "
                         f"checkout, not to the URL {pcb}; pin the commit in the URL")
    if os.path.isabs(pcb):
        path = pcb
    elif config_path:
        path = os.path.join(os.path.dirname(os.path.abspath(config_path)), pcb)
    else:
        path = os.path.abspath(pcb)
    return board_at_rev(path, rev, workdir)


def resolve_pcb_path(pcb, workdir, config_path=None, downloader=download_url, rev=None):
    """Download URL PCB inputs to a temp file; local paths resolve against the
    current working directory first, then — when a YAML config supplied the
    path — against the config file's own directory. If both resolve to a real
    file but to *different* boards (by SHA-256), fail hard rather than guess.

    With `rev` (a commit hash or tag; YAML `pcb_rev`), the resolved local path
    only names the repository and file: the board is read from that commit, see
    board_at_rev. A URL with a rev is refused -- pin the URL itself instead."""
    if rev is not None:                     # "" is refused by check_pin, never "unpinned"
        return resolve_pinned(pcb, rev, workdir, config_path)[0]
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
