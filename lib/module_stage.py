#!/usr/bin/env python3
"""Declared INTEGRATED-MODULE power stage (both switches inside one package).

WHY THIS EXISTS. Every other path in this extractor closes the input commutation
loop through DECLARED FET DIES: `fet_discovery` finds a high-side FET whose drain
sits on a non-GND rail and a low-side FET bridging SW and GND, `build_fet` raises
each package's pads to a die plane at `z = +lead_mm` and `.equiv`s drain to source
there. That closure is what makes the deck solvable at all -- without it the Vin
copper and the GND copper are two galvanically separate conductors and the Cin
port (`P_pwr`) spans them, which FastHenry answers with

    Number of meshes:             0
    Couldn't create sparse matrix, err 5      (exit 1, no Zc.mat)

measured here 2026-09-16 on a two-wire reproduction (test/fixtures is not needed
to see it; `lib/module_stage.py` docstring is the record). `probe_ports` does NOT
substitute for the closure: a probe is an extra `.external` on copper that is
already in the deck, and the thing missing here is not a port, it is the galvanic
path. Two probes (Cin->VIN pad, GND pad->Cin) would each be solvable on their own
net, but P_pwr still spans nothing and the run still dies -- and nothing in the
tool forms the series reduction that would turn two one-net partial inductances
into a loop.

On a MODULE board (TI TPSM33610S3Q, refdes U1 on ee/hw/buck-tpsm33610) there are
no discrete FETs to declare: the half bridge, its gate drivers and the output
inductor are inside the package. So the loop must be closed at the PACKAGE PADS.

THE IRREDUCIBLE UNKNOWN, AND WHY THIS MODULE REFUSES TO GUESS IT.
For a discrete FET the extractor controls where the die plane sits with
`lead_mm`, and `examples/fugu2-cu.yaml` records what happens when that is wrong:
pairing a lead-INCLUSIVE extraction (lead_mm=3 -> 8.2 nH) with a loss deck that
also supplies the package leads double-counts them and rings ~2x too inductive.
A module has no equivalent knob that can be got right, because the geometry from
the package pads to the dies is **not public** -- TI's SNVSCS7E gives a land
pattern and layout guidance (SS8.5.1/8.5.2) and no internal bond geometry. Any
number the tool invented for that path would be unfalsifiable, and it would be
invisible inside a quantity called `L_loop` that every other config means as
"board copper".

So the closure is an EXPLICIT, RECORDED DECLARATION with no default:

  internal_closure: ideal_pad_plane
      The VIN pad group and the GND pad group are joined by one FastHenry
      `.equiv`. The internal contribution is EXACTLY ZERO by construction, not by
      assumption, and the extracted `L_loop` is board copper only -- the same
      basis as `examples/fugu2-cu.yaml` (lead_mm=0.1). This is a LOWER BOUND on
      the physical loop: the real module adds bondwire/leadframe/die inductance
      that is not in this number.

  internal_closure: declared_internal
      Same solve, byte-for-byte -- FastHenry has no lumped-element primitive, so
      a declared internal inductance cannot enter the field solve. It is carried
      as a SEPARATE, NAMED term and added only in the reduction, where all three
      of `L_loop_board`, `internal_L` and `L_loop_with_internal` are reported
      side by side. `L_loop` itself is NEVER overwritten: it stays board copper
      on both closures, so no consumer can read an assumed internal path as an
      extracted one. `internal_nh` must be accompanied by `internal_source`, a
      free-text provenance string (which datasheet figure, which measurement,
      which date) -- a bare number with no origin is refused.

There is deliberately no third option that estimates the internal path.

GUARD POSTURE (see ~/.claude/CLAUDE.md "Guard review checklist"). Everything in
this file is pure python -- no pcbnew -- so every guard is unit-testable under
system python. Unevaluable input is never OK:
  * `internal_closure` missing        -> hard error (no default, ever)
  * unknown closure name              -> hard error listing the two legal ones
  * `declared_internal` w/o nH        -> hard error
  * `internal_nh` <= 0 or non-finite  -> hard error
  * `internal_nh` on ideal_pad_plane  -> hard error (it would be silently ignored)
  * `declared_internal` w/o source    -> hard error
  * unknown key in the block          -> hard error (a typo'd key is a LOST setting)
The pcbnew-side guards -- refdes/pad/net existence, and the two topological
checks that the closure really sits on the Cin loop -- live in `fet_discovery`
and `kicad_geom`, because they need the board.
"""
import json
import math

IDEAL = "ideal_pad_plane"
DECLARED = "declared_internal"
CLOSURES = (IDEAL, DECLARED)

# Keys the block accepts. Anything else is a hard error: the historical failure
# this prevents is a typo'd `internal_closure:` (say `internal-closure:`) leaving
# the REQUIRED declaration unset while the run looks configured.
_KEYS = {"ref", "vin_pads", "gnd_pads", "sw_pads",
         "internal_closure", "internal_nh", "internal_source",
         "allow_proximity_bond"}


class ModuleError(ValueError):
    """Invalid integrated-module declaration. ValueError so kicad_geom's existing
    ValueError -> SystemExit conversion applies unchanged."""


def _pad_list(name, value):
    """Normalize a declared pad list to a list of pad-number STRINGS.

    KiCad pad 'numbers' are strings (A1, 2, MP), but YAML reads `3` as an int, so
    both have to arrive here and leave as the same thing. An empty list is an
    error, not 'use the default': the user wrote the key, and silently falling
    back to auto-discovery would discard a declaration.
    """
    if value is None:
        return None
    if isinstance(value, (str, int)):
        raise ModuleError(
            f"module: {name}: expected a list of pad numbers, e.g. [3] or ['3'], "
            f"got {value!r}")
    try:
        items = list(value)
    except TypeError:
        raise ModuleError(f"module: {name}: expected a list of pad numbers, got {value!r}")
    if not items:
        raise ModuleError(
            f"module: {name}: declared but empty. Remove the key to auto-discover "
            f"the module's pads on that net, or name the pads; an empty list is "
            f"not 'all pads'.")
    out = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ModuleError(
                f"module: {name}: pad {item!r} is {type(item).__name__}; pad numbers "
                f"are strings or integers")
        out.append(str(item).strip())
    for pad in out:
        if not pad:
            raise ModuleError(f"module: {name}: empty pad number")
    dup = sorted({p for p in out if out.count(p) > 1})
    if dup:
        raise ModuleError(
            f"module: {name}: pad(s) {', '.join(dup)} listed more than once. A "
            f"repeated pad would be terminal-built twice and add parallel "
            f"pad->pour spokes that are not on the board.")
    return out


def parse_spec(spec):
    """Validate a module declaration and return it normalized, or None if absent.

    Accepts the YAML mapping form, or the JSON wire form used to hand the block to
    the pcbnew subprocess. JSON rather than the `a=b:c,d=e` wire format that
    `gate_net_override`/`probe_ports` use, because `internal_source` is free text
    and may legitimately contain , : and =.

    Returns dict(ref, vin_pads|None, gnd_pads|None, sw_pads|None,
                 internal_closure, internal_nh (float, 0.0 for ideal),
                 internal_source|None, allow_proximity_bond).
    """
    if spec is None or spec == "" or spec == {}:
        return None
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as e:
            raise ModuleError(f"module: not valid JSON: {e}")
    if not isinstance(spec, dict):
        raise ModuleError(
            f"module: expected a mapping with at least 'ref' and "
            f"'internal_closure', got {type(spec).__name__}")

    unknown = sorted(set(spec) - _KEYS)
    if unknown:
        raise ModuleError(
            f"module: unknown key(s): {', '.join(unknown)}. Legal keys: "
            f"{', '.join(sorted(_KEYS))}. (A misspelled key is a LOST declaration, "
            f"not a harmless extra.)")

    ref = spec.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise ModuleError(
            f"module: 'ref' must be the module's refdes as a non-empty string "
            f"(e.g. 'U1'), got {ref!r}")
    ref = ref.strip()

    closure = spec.get("internal_closure")
    if closure is None:
        raise ModuleError(
            "module: 'internal_closure' is REQUIRED and has no default. The path "
            "from the package pads to the dies inside an integrated module is not "
            "public geometry, so the extractor will not choose it for you. Declare "
            f"one of: {IDEAL} (internal contribution exactly 0; L_loop is board "
            f"copper only, a LOWER bound on the physical loop), or {DECLARED} "
            "(same board solve, plus a separately-reported internal_nh you must "
            "source).")
    if closure not in CLOSURES:
        raise ModuleError(
            f"module: internal_closure {closure!r} is not one of: "
            f"{', '.join(CLOSURES)}")

    nh = spec.get("internal_nh")
    src = spec.get("internal_source")
    if closure == DECLARED:
        if nh is None:
            raise ModuleError(
                f"module: internal_closure {DECLARED} requires 'internal_nh' (the "
                f"declared pad-to-die internal loop inductance, in nH). Without it "
                f"there is nothing to declare and the run would silently be an "
                f"{IDEAL} extraction under a name that says otherwise.")
        if isinstance(nh, bool) or not isinstance(nh, (int, float)):
            raise ModuleError(f"module: internal_nh must be a number (nH), got {nh!r}")
        nh = float(nh)
        if not math.isfinite(nh) or nh <= 0.0:
            raise ModuleError(
                f"module: internal_nh must be finite and > 0 nH, got {nh!r}. A zero "
                f"or negative declaration is not a measurement; if the internal "
                f"path is being excluded on purpose, say so with "
                f"internal_closure: {IDEAL}.")
        if not isinstance(src, str) or not src.strip():
            raise ModuleError(
                f"module: internal_closure {DECLARED} requires 'internal_source', a "
                f"free-text provenance string for internal_nh (datasheet figure, "
                f"measurement + date, vendor model...). An unsourced internal "
                f"inductance is indistinguishable from an invented one.")
        src = src.strip()
    else:
        # `0` is accepted: it is the NORMALIZED form this function itself emits, so
        # parse_spec must be idempotent (the CLI path re-parses an already-parsed
        # block), and an explicit 0 says the same thing the closure says. Any
        # NON-zero value is refused -- it would be silently discarded.
        if nh is not None and not (isinstance(nh, (int, float))
                                   and not isinstance(nh, bool) and nh == 0):
            raise ModuleError(
                f"module: internal_nh was declared with internal_closure {IDEAL}, "
                f"where the internal contribution is EXACTLY ZERO by construction. "
                f"The value would be silently ignored. Use internal_closure "
                f"{DECLARED} to have it reported, or delete internal_nh.")
        if src is not None:
            raise ModuleError(
                f"module: internal_source was declared with internal_closure "
                f"{IDEAL}, which has no internal term to source. Delete it, or use "
                f"internal_closure {DECLARED}.")
        nh = 0.0
        src = None

    prox = spec.get("allow_proximity_bond", False)
    if not isinstance(prox, bool):
        raise ModuleError(
            f"module: allow_proximity_bond must be true/false, got {prox!r}")

    return dict(
        ref=ref,
        allow_proximity_bond=prox,
        vin_pads=_pad_list("vin_pads", spec.get("vin_pads")),
        gnd_pads=_pad_list("gnd_pads", spec.get("gnd_pads")),
        sw_pads=_pad_list("sw_pads", spec.get("sw_pads")),
        internal_closure=closure,
        internal_nh=nh,
        internal_source=src,
    )


def to_arg(spec):
    """Normalized JSON for `kicad_geom --module-spec`. Re-validates on the way out
    so a malformed block fails in the PARENT, before a subprocess is launched and
    its error has to be read back out of a captured stream."""
    norm = parse_spec(spec)
    if norm is None:
        return ""
    return json.dumps(norm, sort_keys=True)


def internal_henries(spec):
    """Declared internal loop inductance in H (0.0 for the ideal pad-plane closure)."""
    return float((spec or {}).get("internal_nh", 0.0)) * 1e-9


def basis_note(spec):
    """One-line human-readable statement of WHAT the extracted L_loop contains.

    Emitted to the console, the ports sidecar and parasitics.json. It exists so
    that a module result can never be read as a lead-inclusive or
    internals-inclusive loop just because the reader did not open the config.
    """
    if not spec:
        return None
    if spec["internal_closure"] == IDEAL:
        return (f"module {spec['ref']}: L_loop is BOARD COPPER ONLY -- the package "
                f"pads are joined by an ideal .equiv (internal contribution exactly "
                f"0 nH). The physical loop is LARGER by the module's unpublished "
                f"pad-to-die path.")
    return (f"module {spec['ref']}: L_loop is BOARD COPPER ONLY; a declared "
            f"internal {spec['internal_nh']:g} nH is reported SEPARATELY as "
            f"L_loop_with_internal (source: {spec['internal_source']}). The board "
            f"solve is identical to {IDEAL} -- FastHenry has no lumped element.")


def manifest(spec):
    """JSON-friendly provenance block for the sidecar / parasitics.json."""
    if not spec:
        return None
    out = dict(spec)
    out["board_copper_only"] = True
    out["note"] = basis_note(spec)
    return out
