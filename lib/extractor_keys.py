"""The config keys extract_parasitics.py accepts, for the viewers that share its YAML.

Read from the extractor's SOURCE with ast rather than imported: gate_copper.py and
visualize_paths.py run under KiCad's Python, which need not have the extractor's
numpy stack. Copied whitelists went stale -- by 2026-10-01 both viewers rejected 11
of the 13 committed example configs (lf_freq, cin_loop_refs, module, ...), though
each promises the extractor's YAML works unchanged (merge review of d70128c).
Stdlib only.
"""
import ast
import os

EXTRACTOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "extract_parasitics.py")


def extractor_config_keys(path=EXTRACTOR):
    """REQUIRED_ARGS | DEFAULTS of extract_parasitics.py -- exactly the set its own
    _validate_config accepts. Raises SystemExit if the schema cannot be read: a
    viewer that cannot tell an extractor key from a typo must not guess."""
    try:
        tree = ast.parse(open(path).read(), path)
    except (OSError, SyntaxError) as e:
        raise SystemExit(f"cannot read the extractor's config schema from {path}: {e}")
    # Fail closed on ANY shape but the two literals: a partial set is worse than
    # none, because a missing key reads as a typo and a viewer refuses a valid
    # config. Wrapping DEFAULTS in dict(...) returned 4 keys (the str values of
    # the call's args) and raised nothing (review of 294d476).
    found = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id in ("REQUIRED_ARGS", "DEFAULTS"):
                if t.id in found:
                    raise SystemExit(f"{path}: {t.id} is assigned more than once; "
                                     f"cannot tell which is the extractor's config schema")
                found[t.id] = value
    keys = set()
    for name, kinds in (("REQUIRED_ARGS", (ast.Tuple, ast.List)), ("DEFAULTS", (ast.Dict,))):
        v = found.get(name)
        if not isinstance(v, kinds):
            raise SystemExit(f"{path}: {name} is "
                             f"{'missing' if v is None else 'not a literal ' + kinds[0].__name__.lower()}; "
                             f"cannot read the extractor's config schema, so cannot tell an "
                             f"extractor key from a typo")
        elts = v.keys if isinstance(v, ast.Dict) else v.elts
        bad = [e for e in elts if not (isinstance(e, ast.Constant) and isinstance(e.value, str))]
        if bad:
            raise SystemExit(f"{path}: {name} has {len(bad)} key(s) that are not string "
                             f"literals (e.g. a **spread or a computed key); cannot read "
                             f"the extractor's config schema")
        keys |= {e.value for e in elts}
    return keys
