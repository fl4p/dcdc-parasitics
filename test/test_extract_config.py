#!/usr/bin/env python3
"""Unit tests for extract_parasitics.py config parsing.

Plain asserts, no framework, matching the rest of parasitics/test.
"""
import os
import sys
import tempfile
from contextlib import redirect_stderr
from io import StringIO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import extract_parasitics  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "lib"))
import pcb_source  # noqa: E402


def _yaml(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


def _expect_exit(fn):
    try:
        with redirect_stderr(StringIO()):
            fn()
    except SystemExit as e:
        return e
    raise AssertionError("expected SystemExit")


def test_yaml_only_supplies_required_args():
    cfg = _yaml("""
pcb: /boards/Fugu2.kicad_pcb
sw: SW
gnd: BuckGND
out: out-par
pitch: [2.0, 1.0]
hs_ref: [Q1, Q3]
ls_ref: [Q2]
emit_cin_network: true
weld_tol: 0.7
margin: 6.5
cu_thickness: 0.07
""")
    args = extract_parasitics.parse_args(["--config", cfg])
    assert args.pcb == "/boards/Fugu2.kicad_pcb"
    assert args.sw == "SW"
    assert args.gnd == "BuckGND"
    assert args.out == "out-par"
    assert args.pitch == [2.0, 1.0]
    assert args.hs_ref == ["Q1", "Q3"]
    assert args.ls_ref == ["Q2"]
    assert args.emit_cin_network is True
    assert args.weld_tol == 0.7
    assert args.margin == 6.5
    assert args.cu_thickness == 0.07


def test_cli_overrides_yaml_scalars_lists_and_booleans():
    cfg = _yaml("""
pcb: /boards/old.kicad_pcb
sw: OLD_SW
gnd: OLD_GND
out: old-out
pitch: [3.0]
hs_ref: [Q9]
svg: true
cu_thickness: 0.035
""")
    args = extract_parasitics.parse_args([
        "--config", cfg,
        "/boards/new.kicad_pcb",
        "--sw", "SW",
        "--gnd", "GND",
        "--pitch", "2.0", "1.0",
        "--hs-ref", "Q1", "Q3",
        "--no-svg",
        "--cu-thickness", "0.07",
        "-o", "new-out",
    ])
    assert args.pcb == "/boards/new.kicad_pcb"
    assert args.sw == "SW"
    assert args.gnd == "GND"
    assert args.out == "new-out"
    assert args.pitch == [2.0, 1.0]
    assert args.hs_ref == ["Q1", "Q3"]
    assert args.svg is False
    assert args.cu_thickness == 0.07


def test_github_blob_url_normalizes_to_raw():
    url = "https://github.com/org/repo/blob/main/hw/Fugu2/Fugu2.kicad_pcb"
    got = pcb_source.normalize_pcb_url(url)
    assert got == "https://github.com/org/repo/raw/main/hw/Fugu2/Fugu2.kicad_pcb"


def test_resolve_pcb_path_downloads_url_to_workdir():
    calls = []
    def fake_download(url, path):
        calls.append((url, path))
        with open(path, "wb") as fh:
            fh.write(b"(kicad_pcb)")

    workdir = tempfile.mkdtemp()
    got = pcb_source.resolve_pcb_path(
        "https://github.com/org/repo/blob/main/hw/board.kicad_pcb",
        workdir,
        downloader=fake_download)

    assert got == os.path.join(workdir, "board.kicad_pcb")
    assert os.path.exists(got)
    assert calls == [("https://github.com/org/repo/raw/main/hw/board.kicad_pcb", got)]


def test_file_sha256_hashes_input_bytes():
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"dcdc-tools")
    got = pcb_source.file_sha256(path)
    assert got == "474d28ed4d32e42f9077c162f3210eb518049d32eb8a2f1fbe1c03e229654f74"


def test_unknown_yaml_key_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
bogus: 1
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_yaml_type_mismatch_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
pitch: 2.0
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_invalid_copper_thickness_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
cu_thickness: 0
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_missing_required_merged_args_fail():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
out: out
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_missing_gate_ports_hard_fails():
    side = {
        "ports": ["P_pwr", "P_bulk"],
        "topo": {"cin_dropped_ports": ["P_ghs", "P_gls"]},
    }
    e = _expect_exit(lambda: extract_parasitics.require_gate_ports(side, 2.0))
    msg = str(e.code)
    assert "missing gate-loop port(s)" in msg
    assert "P_ghs, P_gls" in msg
    assert "0.00 nH" in msg


def test_present_gate_ports_pass():
    side = {"ports": ["P_pwr", "P_ghs", "P_gls"], "topo": {}}
    extract_parasitics.require_gate_ports(side, 2.0)  # no raise


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
