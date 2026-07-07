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


def resolve_pcb_path(pcb, workdir, config_path=None, downloader=download_url):
    """Download URL PCB inputs to a temp file; local paths resolve against the
    current working directory first, then — when a YAML config supplied the
    path — against the config file's own directory. If both resolve to a real
    file but to *different* boards (by SHA-256), fail hard rather than guess."""
    if is_url(pcb):
        url = normalize_pcb_url(pcb)
        name = os.path.basename(urllib.parse.urlparse(url).path) or "board.kicad_pcb"
        if not name.endswith(".kicad_pcb"):
            name += ".kicad_pcb"
        path = os.path.join(workdir, name)
        downloader(url, path)
        return path

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
