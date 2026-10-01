"""lib/extractor_keys.py reads the extractor's accepted config keys for the viewers
(gate_copper.py, visualize_paths.py) that share its YAML. Copied whitelists went
stale and rejected 11 of 13 committed example configs; this pins the shared one."""
import glob
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import extractor_keys  # noqa: E402


def test_every_committed_example_config_uses_only_extractor_keys():
    keys = extractor_keys.extractor_config_keys()
    for path in sorted(glob.glob(os.path.join(ROOT, "examples", "*.yaml"))):
        cfg = yaml.safe_load(open(path)) or {}
        assert set(cfg) <= keys, f"{os.path.basename(path)}: {sorted(set(cfg) - keys)}"


def test_structured_keys_and_required_args_are_included():
    keys = extractor_keys.extractor_config_keys()
    assert {"pcb", "sw", "gnd", "out", "lf_freq", "module", "probe_ports",
            "gate_net_override", "extra_nets"} <= keys


def test_unreadable_schema_refuses_rather_than_guessing(tmp_path):
    empty = tmp_path / "extract_parasitics.py"
    empty.write_text("x = 1\n")
    with pytest.raises(SystemExit, match="cannot tell an extractor key from a typo"):
        extractor_keys.extractor_config_keys(str(empty))
