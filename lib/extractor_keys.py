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
    keys = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if names & {"REQUIRED_ARGS", "DEFAULTS"}:
            v = node.value
            elts = v.keys if isinstance(v, ast.Dict) else getattr(v, "elts", [])
            keys |= {e.value for e in elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    if not keys:
        raise SystemExit(f"found no REQUIRED_ARGS/DEFAULTS in {path}; cannot tell an "
                         f"extractor key from a typo")
    return keys
