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
    # the call's args) and raised nothing (review of 294d476). The literal is also
    # the accepted set only if nothing ADDS to it later: DEFAULTS.update(...),
    # DEFAULTS[k] = ..., |=, a rebinding inside an if/try, or an alias that is then
    # mutated each passed _validate_config while the viewers refused the key
    # (review of 60c5585). Mutation through a function that receives DEFAULTS as an
    # argument is not visible here.
    found, problems = {}, []
    names = ("REQUIRED_ARGS", "DEFAULTS")

    def is_schema(n):
        return isinstance(n, ast.Name) and n.id in names

    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            if is_schema(node.value):
                problems.append(f"line {line}: {node.value.id} is aliased")
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]          # a bare annotation binds nothing
        elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            targets = [i.optional_vars for i in node.items if i.optional_vars]
        elif isinstance(node, ast.NamedExpr):
            targets = [node.target]
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems += [f"line {line}: {n} is declared {type(node).__name__.lower()}"
                         for n in node.names if n in names]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and is_schema(node.func.value)
              and node.func.attr in ("update", "setdefault", "pop", "popitem", "clear",
                                     "__setitem__", "__delitem__", "__ior__")):
            problems.append(f"line {line}: {node.func.value.id}.{node.func.attr}(...)")
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Subscript) and is_schema(sub.value):
                    problems.append(f"line {line}: {sub.value.id}[...] is written")
            if is_schema(t):
                if (isinstance(node, ast.Assign) and node in tree.body
                        and t.id not in found):
                    found[t.id] = node.value
                else:
                    problems.append(f"line {line}: {t.id} is rebound")
    if problems:
        raise SystemExit(f"{path}: the literal REQUIRED_ARGS/DEFAULTS may not be the "
                         f"accepted set ({'; '.join(problems)}); cannot read the "
                         f"extractor's config schema")
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
