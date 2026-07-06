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


def test_cli_overrides_yaml_scalars_lists_and_booleans():
    cfg = _yaml("""
pcb: /boards/old.kicad_pcb
sw: OLD_SW
gnd: OLD_GND
out: old-out
pitch: [3.0]
hs_ref: [Q9]
svg: true
""")
    args = extract_parasitics.parse_args([
        "--config", cfg,
        "/boards/new.kicad_pcb",
        "--sw", "SW",
        "--gnd", "GND",
        "--pitch", "2.0", "1.0",
        "--hs-ref", "Q1", "Q3",
        "--no-svg",
        "-o", "new-out",
    ])
    assert args.pcb == "/boards/new.kicad_pcb"
    assert args.sw == "SW"
    assert args.gnd == "GND"
    assert args.out == "new-out"
    assert args.pitch == [2.0, 1.0]
    assert args.hs_ref == ["Q1", "Q3"]
    assert args.svg is False


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


def test_missing_required_merged_args_fail():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
out: out
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_missing_gate_ports_warns_about_zero_csi():
    err = StringIO()
    side = {
        "ports": ["P_pwr", "P_bulk"],
        "topo": {"cin_dropped_ports": ["P_ghs", "P_gls"]},
    }
    with redirect_stderr(err):
        extract_parasitics.warn_missing_gate_ports(side, 2.0)
    msg = err.getvalue()
    assert "missing gate-loop port(s)" in msg
    assert "P_ghs, P_gls" in msg
    assert "0.00 nH" in msg


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
