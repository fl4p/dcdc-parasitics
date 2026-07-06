"""Resolve local or URL KiCad PCB inputs to a local file path."""
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


def resolve_pcb_path(pcb, workdir, downloader=download_url):
    """Download URL PCB inputs to a temp file; local paths pass through."""
    if not is_url(pcb):
        return pcb
    url = normalize_pcb_url(pcb)
    name = os.path.basename(urllib.parse.urlparse(url).path) or "board.kicad_pcb"
    if not name.endswith(".kicad_pcb"):
        name += ".kicad_pcb"
    path = os.path.join(workdir, name)
    downloader(url, path)
    return path
