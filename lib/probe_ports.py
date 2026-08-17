#!/usr/bin/env python3
"""User-declared two-terminal probe ports (`REF.PAD` <-> `REF.PAD`).

WHY THIS EXISTS. The extractor's ports are all *derived*: the HS/LS device
terminals, the Cin branches, the two gate loops and bulk. A component mounted
anywhere else on those nets -- an added ring-identification capacitor between
SW and BuckGND at Q2.3/J3.1, at the D9 device tabs, or on the R11/C8 snubber
land -- is outside every one of them, so its **mount-loop inductance can only be
guessed**. `probe_ports` lets an extraction config name such a position and have
its loop L *extracted*:

    probe_ports:
      cap_at_d9:    [D9.2, D9.3]
      cap_q2_j3:    [J3.1, Q2.3]

Each entry becomes one extra FastHenry `.external` labelled ``P_probe_<name>``,
appended to the existing `ports` list (every consumer looks ports up by name, so
appending is safe), plus a derived convenience block in `parasitics.json`.

GUARD POSTURE. `extract_parasitics` today fails closed on structure and type but
fails OPEN on ref-set membership: a bogus `cin_loop_refs` entry is silently
dropped. Probe ports must not inherit that -- a probe that silently vanishes
turns into a `None` that a caller reads as a measurement. Every failure below is
a hard error, and the one thing that cannot be made impossible (a probe pulling
previously-unported copper into the deck, which can shift `L_loop`) is
*measured and reported*, never assumed away.

Everything in this module is pure python -- no pcbnew -- so the guards are
unit-testable under system python. The pcbnew-side pad resolution lives in
`kicad_geom._probe_pad_node_stack`.
"""
import re

PORT_PREFIX = "P_probe_"

# kicad_geom.SNAP, quoted in a message only. Kept as a literal so this module
# stays importable without pcbnew; the authority is kicad_geom.
SNAP_MM = 0.02

# Probe names must survive label generation unchanged. `_port_ref`-style
# sanitizing (`[^A-Za-z0-9_]+` -> `_`) would silently map `cap-a` and `cap.a` and
# `cap_a` onto ONE label, i.e. two declared probes could quietly become one port
# -- exactly the silent-loss failure this module exists to prevent. So the
# sanitizer is applied AND required to be a no-op.
_SANITIZE = re.compile(r"[^A-Za-z0-9_]+")

# Endpoint syntax: REF.PAD. Refdes never contains a dot; pad "numbers" are
# strings in KiCad (A1, 2, MP...). Reject the characters used by the
# comma/colon/equals wire format so a name can never smuggle a separator.
_ENDPOINT = re.compile(r"^([A-Za-z0-9_+\-]+)\.([A-Za-z0-9_+\-]+)$")


class ProbeError(ValueError):
    """Invalid probe-port declaration. ValueError so kicad_geom's existing
    ValueError -> SystemExit conversion applies unchanged."""


def port_label(name):
    """FastHenry port label for a probe name. Requires an already-valid name."""
    return PORT_PREFIX + _SANITIZE.sub("_", name)


def is_probe_label(label):
    return isinstance(label, str) and label.startswith(PORT_PREFIX)


def _check_name(name):
    if not isinstance(name, str) or not name:
        raise ProbeError(f"probe_ports: probe name must be a non-empty string, got {name!r}")
    if _SANITIZE.sub("_", name) != name:
        raise ProbeError(
            f"probe_ports: probe name {name!r} is not usable as a port label; use only "
            f"[A-Za-z0-9_]. (Sanitizing it would map distinct names onto one port label "
            f"and silently merge two declared probes into one.)")
    return name


def _check_endpoint(name, text):
    if not isinstance(text, str):
        raise ProbeError(
            f"probe_ports: {name}: endpoint must be a 'REF.PAD' string, got {text!r}")
    m = _ENDPOINT.match(text.strip())
    if not m:
        raise ProbeError(
            f"probe_ports: {name}: endpoint {text!r} is not 'REF.PAD' "
            f"(e.g. 'D9.2'); refdes and pad number, separated by one dot")
    return m.group(1), m.group(2)


def parse_spec(spec):
    """Normalize a probe-port declaration into a list of dicts, in order.

    Accepts either the YAML mapping form ``{name: [REF.PAD, REF.PAD]}`` or the
    wire form used to hand the spec to the pcbnew subprocess,
    ``'name=REF.PAD:REF.PAD,name2=...'`` -- mirroring how `gate_net_override`
    crosses the same boundary.

    Returns ``[{name, label, a_ref, a_pad, b_ref, b_pad, a, b}, ...]``.
    Raises ProbeError on anything malformed. `None`/empty -> ``[]``.
    """
    if spec is None or spec == "" or spec == {}:
        return []
    if isinstance(spec, str):
        pairs = _parse_wire(spec)
    elif isinstance(spec, dict):
        pairs = []
        for name, ends in spec.items():
            if isinstance(ends, str):
                raise ProbeError(
                    f"probe_ports: {name!r}: expected a two-element list "
                    f"[REF.PAD, REF.PAD], got a string {ends!r}")
            try:
                ends = list(ends)
            except TypeError:
                raise ProbeError(
                    f"probe_ports: {name!r}: expected a two-element list "
                    f"[REF.PAD, REF.PAD], got {ends!r}")
            if len(ends) != 2:
                raise ProbeError(
                    f"probe_ports: {name!r}: a probe port has exactly TWO terminals, "
                    f"got {len(ends)}: {ends!r}")
            pairs.append((name, ends[0], ends[1]))
    else:
        raise ProbeError(
            "probe_ports: expected a mapping of name -> [REF.PAD, REF.PAD], got "
            f"{type(spec).__name__}")

    out = []
    seen = {}
    for name, a_text, b_text in pairs:
        _check_name(name)
        if name in seen:
            raise ProbeError(f"probe_ports: duplicate probe name {name!r}")
        seen[name] = True
        a_ref, a_pad = _check_endpoint(name, a_text)
        b_ref, b_pad = _check_endpoint(name, b_text)
        if (a_ref, a_pad) == (b_ref, b_pad):
            raise ProbeError(
                f"probe_ports: {name}: both terminals are {a_ref}.{a_pad} — a port "
                f"across one pad measures nothing and makes FastHenry's Zc singular")
        out.append(dict(name=name, label=port_label(name),
                        a_ref=a_ref, a_pad=a_pad, b_ref=b_ref, b_pad=b_pad,
                        a=f"{a_ref}.{a_pad}", b=f"{b_ref}.{b_pad}"))
    labels = [p["label"] for p in out]
    dup = sorted({l for l in labels if labels.count(l) > 1})
    if dup:
        raise ProbeError(f"probe_ports: probe name(s) collide on port label(s): {dup}")
    return out


def _parse_wire(text):
    pairs = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, ends = item.partition("=")
        if not sep:
            raise ProbeError(
                f"--probe-ports: bad entry {item!r}, expected name=REF.PAD:REF.PAD")
        a_text, sep2, b_text = ends.partition(":")
        if not sep2:
            raise ProbeError(
                f"--probe-ports: bad entry {item!r}, expected name=REF.PAD:REF.PAD")
        pairs.append((name.strip(), a_text.strip(), b_text.strip()))
    return pairs


def to_arg(spec):
    """Serialize a spec (mapping or already-parsed list) to the wire form."""
    probes = spec if (isinstance(spec, list) and spec and isinstance(spec[0], dict)) \
        else parse_spec(spec)
    return ",".join(f"{p['name']}={p['a']}:{p['b']}" for p in probes)


# --------------------------------------------------------------------------- #
# guards over the built model
# --------------------------------------------------------------------------- #
def require_unique_labels(ports):
    """Guard 4: no two ports may share a label.

    There is no global label-collision check in the extractor today (only
    per-device labels are checked). Two ports with the same label make the
    sidecar's `ports` list ambiguous: `idx = {p: i for i, p in enumerate(ports)}`
    in solve_reduce keeps the LAST one, so one of the two measurements is
    silently replaced by the other.
    """
    labels = [lbl for lbl, _, _ in ports]
    dup = sorted({l for l in labels if labels.count(l) > 1})
    if dup:
        raise ProbeError(
            f"probe_ports: port label collision with existing extractor ports: "
            f"{', '.join(dup)}. Rename the probe; generated labels include P_pwr*, "
            f"P_bulk, P_ghs/P_gls (or P_g{{hs,ls}}_<ref>), P_hs/P_ls (or "
            f"P_{{hs,ls}}_<ref>), P_cin_<ref> and P_sw_residual*.")


def require_distinct_node_pairs(ports):
    """Guard 5: no two ports may sit on the SAME node pair.

    Two `.external` on one node pair makes FastHenry's Zc singular ("Error on
    factor") — the whole solve is lost, not just the probe. This fires for a
    `cap_at_d9`-style probe whenever the same device is already a declared FET
    (its device port is across those very tabs), which is exactly the intended
    flu configuration, so the message has to say what to read instead.
    """
    by_pair = {}
    for lbl, a, b in ports:
        by_pair.setdefault(frozenset((a, b)), []).append(lbl)
    for pair, labels in sorted(by_pair.items(), key=lambda kv: sorted(kv[1])):
        if len(labels) < 2:
            continue
        probes = [l for l in labels if is_probe_label(l)]
        if not probes:
            continue  # pre-existing duplicate, not ours to diagnose here
        others = [l for l in labels if not is_probe_label(l)]
        raise ProbeError(
            f"probe_ports: {', '.join(sorted(probes))} resolves to the SAME node pair as "
            f"{', '.join(sorted(others)) or 'another probe'}. Two .external on one node "
            f"pair make FastHenry's Zc singular ('Error on factor'), so this cannot be "
            f"emitted. Drop the probe and read "
            f"{(sorted(others) or sorted(probes))[0]} from port_L/port_R instead — it "
            f"already measures that pad pair.")


def require_not_dropped(probes, dropped, ports):
    """Guard 6: a probe port that got dropped as floating is a hard failure.

    `drop_floating_ports` exists so ONE disconnected port cannot NaN the entire
    FastHenry solve, and it only warns. For a probe that is the wrong trade: the
    label vanishes from `ports`, and a `.get()`-style consumer reads the missing
    entry as `None` — i.e. as a measurement that was never made.
    """
    ds = set(dropped or ())
    labels = {lbl for lbl, _, _ in ports}
    lost = [p["label"] for p in probes if p["label"] in ds or p["label"] not in labels]
    if lost:
        raise ProbeError(
            f"probe_ports: {', '.join(lost)} is not connected to the commutation loop in "
            f"the modeled copper and was dropped — it would silently read as no "
            f"measurement at all. Its pad never bonded into the meshed pour: lower "
            f"--pitch, raise --weld-tol / --margin, or check that the declared REF.PAD "
            f"is really on the extracted nets.")


def annotate_perturbation(model, probes):
    """Guard 7: report — never assume — how much copper each probe pulled in.

    `Model.prune()` keeps only copper reachable from port endpoints, so a probe in
    a previously-unported region ADDS copper to the deck and can shift `L_loop`.
    The `P_out_hs` aux port dodges this deliberately (it snaps to an existing pour
    node "so the FastHenry reduction is byte-for-byte unchanged"); a probe cannot
    always dodge it, because its whole point is to reach a mounting position that
    no existing port covers.

    Each probe carries `pulled_new_copper`, the OR of three measured signals:

      * `terminal_segs_added` / `terminal_nodes_added` — geometry this probe's own
        pad-land terminals created, as a delta around their construction;
      * `endpoint_new_in_probe_phase` — either endpoint sits on a node that no
        NON-probe consumer had created. This is the ORDER-FREE signal: when two
        probes share one previously-unterminated pad, the deltas above credit the
        whole cost to whichever ran first, and the second would otherwise report
        "added nothing";
      * `retained_nodes_added` — nodes `prune()` now keeps only because this probe
        seeds them. **In a successful build this is structurally zero**: the caller
        runs `drop_floating_ports` first, so every surviving probe is already inside
        the seed port's connected component, hence inside `base`. It is kept
        because it is the correct general statement of the question and it is the
        signal that fires in the states the drop guard would otherwise have to
        catch — but the terminal-geometry signals are the operative ones, and this
        one must not be read as independent corroboration.

    If the baseline cannot be evaluated at all (no non-probe ports), every probe
    reports `pulled_new_copper: true` — the reported-perturbation direction, never
    "fine".
    """
    base_seeds = set()
    for lbl, a, b in model.ports:
        if is_probe_label(lbl):
            continue
        base_seeds.add(a)
        base_seeds.add(b)
    base_seeds.update(getattr(model, "keep_nodes", ()) or ())
    base = model.component(base_seeds) if base_seeds else set()
    for p in probes:
        own = model.component({p["a_node"], p["b_node"]})
        retained = len(own - base)
        p["retained_nodes_added"] = retained
        p["pulled_new_copper"] = bool(
            retained
            or p.get("terminal_segs_added")
            or p.get("terminal_nodes_added")
            or p.get("endpoint_new_in_probe_phase")
            or not base_seeds)
        p["perturbation_basis"] = "measured" if base_seeds else "no_baseline_ports"
    return probes


def require_not_shorted(model, probes):
    """A probe must not span a SYNTHESIZED SHORT.

    This is the canonical anti-monotone shape, and none of the other guards see
    it: the terminals resolve, nothing floats, no label or node pair collides, no
    copper is pulled — and the port still measures nothing, because the extractor
    itself shorted its two endpoints.

    MEASURED on Fugu2 with `lead_mm: 0` and a probe across the LS device tabs
    (D9.2 SW -> D9.3 BuckGND): FastHenry returned **1.702e-15 H** (0.0017 nH)
    against 3.575e-09 H for that device's own port. The path is
    `pad --0.001 mm--> drain die --.equiv--> source die --0.001 mm--> pad`: with
    lead_mm=0 the die-plane closure shorts drain to source AT THE PADS. A
    near-zero dressed as a measurement, in exactly the position the feature exists
    to measure. Refuse it — 0.0017 nH is not a small mount loop, it is the absence
    of one.

    The node-pair collision guard cannot catch this: the two nodes really are
    distinct, they are merely shorted THROUGH the closure.
    """
    for p in probes or ():
        if p["b_node"] in model.ideal_link_component(p["a_node"]):
            raise ProbeError(
                f"probe_ports: {p['name']}: {p['a']} and {p['b']} are SHORTED "
                f"together in the extracted model — every link between them is an "
                f"ideal `.equiv` or a sub-{SNAP_MM} mm coincident segment, so the "
                f"port spans no copper and would report a near-zero inductance "
                f"(measured: 1.7e-15 H for this exact case) that is not a "
                f"measurement. The usual cause is a probe across a MOSFET's own "
                f"drain-source tabs with `lead_mm: 0`, where the die-plane closure "
                f"(`.equiv drain_die source_die`) lands directly on the pads. "
                f"There is no board copper to measure at that position: the mount "
                f"loop there is the capacitor's own ESL. Probe a position with "
                f"real copper between the terminals, or extract on a basis that "
                f"does not close the device.")


def require_not_across_device_closure(probes, device_nodes):
    """A probe must not span ONE device's own synthesized closure.

    `require_not_shorted` catches this whenever `lead_mm` is ~0, because the
    closure then collapses onto the pads. With real leads the same port is no
    longer a short — it reads the two lead stubs plus the die `.equiv` — but it is
    still not a MOUNTING LOOP: it reports package geometry the extractor
    synthesized, under a name that promises board copper. Refuse it under any
    `lead_mm`, so the verdict does not silently depend on an unrelated knob.

    `device_nodes` is {refdes: {node, ...}} of each device's drain/source pad and
    die nodes.
    """
    for p in probes or ():
        for ref, nodes in sorted(device_nodes.items()):
            if p["a_node"] in nodes and p["b_node"] in nodes:
                raise ProbeError(
                    f"probe_ports: {p['name']}: {p['a']} and {p['b']} are both "
                    f"terminals of device {ref}, whose drain-source closure the "
                    f"extractor synthesizes (`.equiv drain_die source_die`). A "
                    f"port across them reports that closure — package lead stubs "
                    f"and an ideal die short — not the board copper a mounting "
                    f"loop is made of. Read {ref}'s own device port instead, or "
                    f"probe a position whose terminals are joined by board copper.")


def require_no_production_cin_network(probes, emit_cin_network, cin_network_model):
    """A perturbed deck must not also emit the production input-cap network.

    `cin_branches` / `cin_matrix` are consumed downstream by the loss deck as THE
    input-cap model. They are reduced from the same deck the probes perturbed, and
    the loss tool has no way to know a diagnostic probe added terminal copper to
    it. Warning here would be a mute button on a contaminated production artifact,
    so this is a hard failure — and it costs only the geometry build, since
    FastHenry has not run yet.

    Note it is NOT enough to strip probes from the cap_only/switch_residual legs
    of a matrix run: the combine subtracts an unprobed leg from a probed one,
    which contaminates the difference directly rather than cancelling.
    """
    pulled = [p["name"] for p in probes or () if p.get("pulled_new_copper")]
    if not pulled or not emit_cin_network:
        return
    raise ProbeError(
        f"probe port(s) {', '.join(pulled)} added terminal copper to the deck, and "
        f"this run also emits the Cin network (--emit-cin-network, model "
        f"{cin_network_model!r}) — which would be reduced from the perturbed deck "
        f"and consumed downstream as a production input-cap model. Refusing. "
        f"Either drop --emit-cin-network from the probe run (probes are a "
        f"diagnostic; run them in their own extraction), or declare probes only on "
        f"pads that already carry a port terminal, which add nothing.")


def manifest(probes):
    """JSON-safe provenance for the sidecar / parasitics.json topo."""
    keys = ("name", "label", "a", "b", "a_ref", "a_pad", "b_ref", "b_pad",
            "a_net", "b_net", "a_terminal", "b_terminal",
            "a_bond", "b_bond", "a_proximity", "b_proximity",
            "a_proximity_inherited", "b_proximity_inherited",
            "terminal_segs_added", "terminal_nodes_added",
            "endpoint_new_in_probe_phase", "retained_nodes_added",
            "pulled_new_copper", "perturbation_basis")
    return [{k: p[k] for k in keys if k in p} for p in probes]
