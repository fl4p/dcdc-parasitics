"""lib/extractor_keys.py reads the extractor's accepted config keys for the viewers
(gate_copper.py, visualize_paths.py) that share its YAML. Copied whitelists went
stale and rejected 11 of 13 committed example configs; this pins the shared one."""
import ast
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


def _rebind(src, name, wrap):
    """Replace the module-level assignment to `name` by wrap(value_node)."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            node.value = wrap(node.value)
            return ast.unparse(tree)
    raise AssertionError(f"no {name} in the extractor")


def _call(fn):
    return lambda v: ast.Call(func=ast.Name(fn, ast.Load()), args=[v], keywords=[])


def _spread(v):
    v.keys.insert(0, None)
    v.values.insert(0, ast.Name("BASE", ast.Load()))
    return v


def _renamed(src):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "DEFAULTS":
                    t.id = "DEFAULTS_"
    return ast.unparse(tree)


# Each mutant is VALID Python: a syntax error would be refused for the wrong reason
# and prove nothing about the shape check.
@pytest.mark.parametrize("mutate", [
    lambda s: _rebind(s, "DEFAULTS", _call("dict")),
    _renamed,
    lambda s: _rebind(s, "DEFAULTS", _spread),
    lambda s: _rebind(s, "REQUIRED_ARGS", _call("tuple")),
    lambda s: s + "\nDEFAULTS = {}\n",
], ids=["dict-call", "renamed", "spread", "required-non-literal", "reassigned"])
def test_a_schema_it_cannot_read_whole_refuses_instead_of_returning_part(tmp_path, mutate):
    """Each of these returned a PARTIAL key set and raised nothing (dict(...) gave 4
    keys, a non-literal REQUIRED_ARGS gave 50) -- review of 294d476."""
    src = open(extractor_keys.EXTRACTOR).read()
    bad = mutate(src)
    compile(bad, "mutant", "exec")
    assert bad != src
    p = tmp_path / "extract_parasitics.py"
    p.write_text(bad)
    with pytest.raises(SystemExit, match="config schema"):
        extractor_keys.extractor_config_keys(str(p))


# The literal is the accepted set only if nothing adds to it afterwards; each of
# these passed _validate_config with the extra key while the reader omitted it
# (review of 60c5585). Valid Python, appended after the unchanged literal.
@pytest.mark.parametrize("tail", [
    "DEFAULTS.update(extra_key=1)",
    "DEFAULTS['extra_key'] = 1",
    "DEFAULTS |= {'extra_key': 1}",
    "if True:\n    DEFAULTS = {**DEFAULTS, 'extra_key': 1}",
    "try:\n    pass\nfinally:\n    DEFAULTS.setdefault('extra_key', 1)",
    "_d = DEFAULTS\n_d['extra_key'] = 1",
    "def _f():\n    global DEFAULTS\n    DEFAULTS = {}",
], ids=["update", "subscript", "ior", "if-rebind", "setdefault", "alias", "global"])
def test_a_later_mutation_of_the_schema_refuses(tmp_path, tail):
    src = open(extractor_keys.EXTRACTOR).read() + "\n" + tail + "\n"
    compile(src, "mutant", "exec")
    p = tmp_path / "extract_parasitics.py"
    p.write_text(src)
    with pytest.raises(SystemExit, match="config schema"):
        extractor_keys.extractor_config_keys(str(p))


def test_a_bare_annotation_is_not_a_second_assignment(tmp_path):
    """`DEFAULTS: dict` binds nothing; it was refused as "assigned more than once"
    (review of 60c5585, finding 2)."""
    src = open(extractor_keys.EXTRACTOR).read().replace(
        "DEFAULTS = {", "DEFAULTS: dict\nDEFAULTS = {", 1)
    p = tmp_path / "extract_parasitics.py"
    p.write_text(src)
    assert extractor_keys.extractor_config_keys(str(p)) == extractor_keys.extractor_config_keys()
