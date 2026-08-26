#!/usr/bin/env python3
"""Opt-in extra nets for the meshed set (`extra_nets` / `--extra-nets`).

WHY THIS EXISTS. `kicad_geom.build()` meshes exactly

    power_nets = {sw, vin, gnd}   |   the HS/LS gate nets

because that is the copper the half-bridge commutation loop and the two gate
loops are made of. Everything else on the board is invisible to the extractor —
not "roughly modelled", *absent*. On Fugu2 that means the buck's whole OUTPUT
power path

    coil lug J6.1 (BflowS) -> Q5/Q6/Q7 -> F1 (Bat+ -> BT+) -> J9.1 (BT+)
    return:  J9.2 (GND) -> R26 (0.5 mOhm shunt) -> BuckGND

is unreachable, and a `probe_ports` entry anywhere on it dies with

    probe_ports: out_coil_bflow: pad J6.1 (net 'BflowS') resolved to NO copper
    contact on any of its layers (layer 0: no_same_net_zone_mesh; ...)

which is the RIGHT refusal (the alternative is a bare pad-centre node that
point-injects the current and reports a loop inductance nobody asked for) but
it is not a usable answer. `extra_nets` supplies the missing half: real meshed
copper for those nets, so the probe resolves to a terminal instead of to
nothing. It never relaxes a guard — the guard keeps refusing pads that have no
copper under them; this module's whole job is to make sure copper is there, and
to FAIL LOUDLY when it is not.

THREE THINGS THIS MODULE REFUSES TO DO SILENTLY.

1. **An unknown net name.** `cin_loop_refs` drops a bogus refdes on the floor;
   that posture is wrong here. A misspelt net would mesh nothing, and the user
   would then read the downstream probe failure as "the ROI is too small" and
   chase a margin that can never work.

2. **A net that is on the board but got NO pour mesh inside the ROI.** This is
   the load-bearing one. The ROI is the FET+Cin bounding box grown by
   `--margin` (`kicad_geom._roi`), and extra nets are typically OUTSIDE it — on
   Fugu2, R26 sits 10.8-30.8 mm outside and J10 24-44 mm outside. Extra nets
   deliberately DO NOT extend the ROI (see `require_meshed` for why), so the
   only honest thing to do when the ROI does not reach them is to say so, with
   the margin that would.

3. **A meshed net that no port touches.** `Model.prune()` keeps only
   port-reachable copper, and extra nets are galvanically ISOLATED from the
   power nets — separated by the back-flow FETs, the fuses and the shunt, none
   of which the extractor models. So an extra net with no port on it is dead
   copper: it is meshed, then pruned out of the deck entirely. That costs
   geometry time and buys nothing, and the run must say so rather than let the
   user believe the copper was modelled.

Everything here is pure python (no `pcbnew`), like `probe_ports.py` and
`gate_net_override.py`, so every decision is unit-testable under system python.
The pcbnew-side measurements it consumes (per-net copper extents, the ROI box)
are gathered in `kicad_geom`.
"""


class ExtraNetError(ValueError):
    """Invalid / unusable `extra_nets` declaration.

    ValueError so `kicad_geom.main()`'s existing ValueError -> SystemExit
    conversion applies unchanged, exactly as `probe_ports.ProbeError` does.
    """


def parse_spec(spec):
    """Normalize an `extra_nets` declaration to an ordered, de-duplicated list.

    Accepts the YAML/CLI list form ``[BflowS, "Bat+", GND]`` (and ``None`` /
    ``[]`` -> ``[]``). There is NO packed wire form: net names are arbitrary
    KiCad strings that can legitimately contain ``,``, ``:`` and ``=``, so the
    spec crosses the subprocess boundary as separate argv items
    (``--extra-nets A B C``, the way ``--cin-loop-refs`` does) rather than
    through a separator that a net name could smuggle.

    Duplicates are collapsed rather than refused: `extra_nets` denotes a SET of
    nets to mesh, so a repeat has exactly one possible meaning and rejecting it
    would be noise. An empty or non-string entry IS refused — it cannot name a
    net, and silently dropping it is how a declaration goes missing.
    """
    if spec is None:
        return []
    if isinstance(spec, str):
        # A bare string is almost always a mistake: `extra_nets: GND` in YAML,
        # which would iterate into ['G', 'N', 'D'] if we accepted any iterable.
        raise ExtraNetError(
            f"extra_nets: expected a LIST of net names, got the string {spec!r}. "
            f"Write it as a list, e.g. extra_nets: [{spec}]")
    try:
        items = list(spec)
    except TypeError:
        raise ExtraNetError(
            f"extra_nets: expected a list of net names, got "
            f"{type(spec).__name__}")
    out = []
    for item in items:
        if not isinstance(item, str):
            raise ExtraNetError(
                f"extra_nets: net name {item!r} is {type(item).__name__}, not a "
                f"string; quote it in the YAML (a KiCad net name is always text)")
        if not item.strip():
            raise ExtraNetError(
                "extra_nets: empty net name in the list; remove the entry or "
                "name a net")
        if item not in out:
            out.append(item)
    return out


def classify(requested, power_nets, gate_nets, board_nets):
    """Turn the requested net names into per-net entries, or raise.

    `board_nets` is the set of nets that actually carry MESHABLE copper on this
    board (a filled zone, a track, a via or a pad) — not the netlist. A net that
    exists in the netlist but carries no copper is just as unmeshable as one
    that does not exist, and the caller must hear about it before the run.

    Returns ``[{net, already_meshed, was_gate_net}, ...]`` in declaration order.
    `already_meshed` marks a net the extractor meshes anyway (a power net):
    harmless, a pure set-union no-op, but reported so the user learns the
    declaration did nothing. `was_gate_net` marks a gate net, where the request
    is NOT redundant — gate nets get tracks and vias but deliberately no pour
    mesh, so naming one here really does add its pour.
    """
    board = set(board_nets or ())
    unknown = [n for n in requested if n not in board]
    if unknown:
        # Suggest the nearest real net names. A typo'd net is the single most
        # likely way to reach this, and the board's net list is long enough that
        # "not on this board" alone sends the reader to the wrong place.
        import difflib
        hints = []
        for n in unknown:
            close = difflib.get_close_matches(n, sorted(board), n=3, cutoff=0.6)
            hints.append(f"{n!r}" + (f" (did you mean {', '.join(map(repr, close))}?)"
                                     if close else ""))
        raise ExtraNetError(
            f"extra_nets: {', '.join(hints)} carries no copper on this board — no "
            f"filled zone, track, via or pad is on it, so there is nothing to "
            f"mesh. Net names are case- and punctuation-sensitive and often "
            f"hierarchical (e.g. '/DC/DC/SW_NODE'); copy them from the .kicad_pcb "
            f"rather than from the schematic sheet. This is a hard error, not a "
            f"silent skip: a dropped net would surface later as a probe port "
            f"'resolved to NO copper contact' and be misread as a --margin "
            f"problem.")
    power = set(power_nets or ())
    gates = set(gate_nets or ())
    return [dict(net=n, already_meshed=n in power, was_gate_net=n in gates)
            for n in requested]


def mesh_nets(entries):
    """The net names to hand to `add_zones` / `build_pour_index`, as a set.

    Includes the `already_meshed` ones: they are already in `power_nets`, so the
    union is the same set either way, and filtering them here would only make
    two code paths where one suffices.
    """
    return {e["net"] for e in entries}


def measure_mesh(model, entries):
    """Record, per entry, how much geometry the mesher ACTUALLY produced.

    Measured off the model, not predicted from the ROI box: `add_zones` clips
    the pour to the ROI *and* to the real filled polygon, `add_tracks` drops
    track spans that lie inside their own pour, and a net can be inside the ROI
    yet have no filled copper there at all. Counting nodes is the only statement
    that survives all three.

    `nodes` counts every model node on that net (pour mesh, track endpoints, via
    barrels); `zone_nodes` counts the pour-mesh subset, which is what a pad-land
    terminal needs to bond to — a net with tracks but no pour mesh under the pad
    still fails `_pad_land_terminal` with `no_same_net_zone_mesh`.
    """
    total = {}
    zone = {}
    for name, (net, _lid) in model.meta.items():
        total[net] = total.get(net, 0) + 1
        if name in model.zone_nodes:
            zone[net] = zone.get(net, 0) + 1
    for e in entries:
        e["nodes"] = total.get(e["net"], 0)
        e["zone_nodes"] = zone.get(e["net"], 0)
    return entries


def require_meshed(entries, roi, margin, extents=None):
    """Guard: a requested extra net with NO pour mesh in the ROI is a hard error.

    THE PREDICATE IS `zone_nodes`, NOT `nodes`, and that was measured rather than
    assumed. Of the three meshing stages only `add_zones` is clipped to the ROI:
    `add_tracks` and `add_vias` build their nets board-wide (the ROI there only
    decides whether a track redundant with its own pour may be dropped). So a net
    whose pour is 30 mm outside the ROI still arrives with a full set of track and
    via nodes, and a `nodes > 0` test would pass it — and then the run would die
    much later, in `_pad_land_terminal`, with `no_same_net_zone_mesh`, which is
    the exact failure this guard exists to preempt. `_pad_region_contacts` bonds a
    pad to ZONE nodes; those are the resource, so those are what is counted.

    WHY THIS IS AN ERROR AND NOT A WARNING, and why the ROI is not simply grown
    to fit. Two options were on the table:

      (a) extend the ROI to cover each extra net's copper. Rejected. `GND` on a
          board like Fugu2 spans essentially the whole outline, so an
          auto-extended ROI would silently mesh the entire board on every layer
          — in the one place this tool has a hard cost cliff (each pitch halving
          is ~4x the pour filaments PER LAYER, and FastHenry's solve is
          single-threaded and super-linear). Worse, it would make `L_loop` move
          when a purely diagnostic net is added, because the power-net pour would
          then be meshed over a different region. The tool works hard elsewhere
          to avoid exactly that (`P_out_hs` snaps to an EXISTING pour node
          "so the FastHenry reduction is byte-for-byte unchanged").

      (b) keep `--margin` as the single ROI authority and SAY when it does not
          reach. Chosen.

    Under (b) the failure mode to guard is precise: the user names a net, the
    ROI never touches it, nothing is meshed, and the only symptom is a probe
    port far downstream refusing a pad for "no copper contact" — the very
    message that made the extractor look like it had a pitch problem. So this
    raises HERE, before the FET/cap/probe terminals are even built, and names
    the margin that would reach.

    `extents` is `kicad_geom._net_copper_extents` output; `margin_required_mm`
    there is the smallest `--margin` whose ROI first TOUCHES one of that net's
    filled-zone bounding boxes — a lower bound on what is needed, not a promise
    (touching a fill's bbox is not the same as putting a mesh node inside a
    particular pad, which is why the probe-port guard downstream stays).

    Two sub-cases get different prose because they have different fixes: a net
    whose pour is simply outside the ROI wants a bigger `--margin`; a net with no
    filled zone ANYWHERE wants a different probe position (or a poured board),
    and no margin will ever help it.
    """
    dead = [e for e in entries if not e.get("zone_nodes")]
    if not dead:
        return entries
    extents = extents or {}
    lines = []
    for e in dead:
        info = extents.get(e["net"]) or {}
        nodes = e.get("nodes") or 0
        got = (f"{nodes} track/via node(s) but no pour mesh"
               if nodes else "no geometry at all")
        if not info.get("has_zone"):
            lines.append(
                f"{e['net']!r}: {got}; this net has NO filled zone anywhere on the "
                f"board, so no --margin can help — a pad-land terminal has no pour "
                f"to bond to and a probe_ports entry on it would be refused. Probe "
                f"a position on a poured net, or pour this one.")
            continue
        need = info.get("margin_required_mm")
        zb = info.get("zone_bbox")
        where = (f"its pour spans ({zb[0]:.1f}, {zb[1]:.1f}) .. "
                 f"({zb[2]:.1f}, {zb[3]:.1f}) mm" if zb else
                 "its pour extent is unknown")
        want = (f"--margin >= {need:.1f} mm would first reach it (currently "
                f"{margin:g} mm)" if need is not None else
                "raise --margin until the ROI reaches it")
        lines.append(f"{e['net']!r}: {got}; {where}; {want}")
    roi_txt = (f"({roi[0]:.1f}, {roi[1]:.1f}) .. ({roi[2]:.1f}, {roi[3]:.1f}) mm"
               if roi else "(no ROI: no FET/Cin footprints found)")
    raise ExtraNetError(
        "extra_nets: the following net(s) got NO pour mesh — the meshing ROI "
        f"{roi_txt} does not reach their filled copper:\n  - "
        + "\n  - ".join(lines)
        + "\n\nExtra nets deliberately do NOT extend the ROI: the ROI is what "
          "bounds mesh cost, and letting a diagnostic net move it would also "
          "move the commutation-loop mesh and therefore L_loop. Raise --margin "
          "(and expect the meshed area, hence the solve time, to grow with it), "
          "or drop the net from extra_nets. Refusing rather than continuing: a "
          "net with no pour under its pads produces a probe port that resolves "
          "to no copper, which reads as a pitch/margin bug in a completely "
          "different part of the tool.")


def annotate_reachability(model, entries, seed_label="P_pwr"):
    """Record, per entry, whether any solved port reaches that net's copper.

    Two independent facts are measured, never assumed:

      * `ported` — some surviving port has an endpoint whose node is on this
        net. Without one, `Model.prune()` drops every node of the net from the
        emitted `.inp`: the copper is meshed and then thrown away.
      * `isolated` — NO node of this net is in the seed port's (`P_pwr`'s)
        connected component, i.e. its copper is a galvanically separate island.
        This is the NORMAL state for the output power path, because the back-flow
        FETs / fuse / shunt that join it to the switching cell are components,
        not copper, and the extractor models neither. Measured over the net's
        whole node set, NOT only its ported nodes, so it stays a true statement
        for an unported net — reporting `isolated: false` there would read as
        "connected to the loop", which is the opposite of the truth.

    `retained` counts the nodes that survive the prune because a port seeds
    them, so "meshed 1499 nodes, retained 0" is visible as such in the manifest
    instead of having to be inferred.
    """
    port_nodes = set()
    for _lbl, a, b in model.ports:
        port_nodes.add(a)
        port_nodes.add(b)
    port_nodes.update(getattr(model, "keep_nodes", ()) or ())
    retained = model.component(port_nodes) if port_nodes else set()
    seed = next(((a, b) for lbl, a, b in model.ports if lbl == seed_label), None)
    seed_nets = {model.meta.get(n, (None, None))[0]
                 for n in (model.component(seed) if seed else ())}
    by_net = {}
    for name in retained:
        net = model.meta.get(name, (None, None))[0]
        by_net[net] = by_net.get(net, 0) + 1
    for e in entries:
        e["retained"] = by_net.get(e["net"], 0)
        e["ported"] = any(model.meta.get(n, (None, None))[0] == e["net"]
                          for n in port_nodes)
        e["isolated"] = e["net"] not in seed_nets
    return entries


def run_warnings(entries):
    """Human-readable warnings for a completed `extra_nets` run.

    Deliberately NOT errors:

      * an unported net is a legitimate intermediate state (a user adds
        `extra_nets` first and the `probe_ports` that read them second), and the
        run is still correct — the copper is pruned, so it changes no result. It
        costs geometry time, which is what the warning is about.
      * a redundant net (already a power net) is an exact set-union no-op.

    Both DO change what the user thinks the run modelled, so neither may be
    silent.
    """
    out = []
    redundant = [e["net"] for e in entries if e["already_meshed"]]
    if redundant:
        out.append(
            f"extra_nets: {', '.join(redundant)} is already meshed as a power net "
            f"(sw/vin/gnd), so naming it here changed nothing. Remove it to keep "
            f"the declaration honest about what it adds.")
    unported = [e for e in entries if e["nodes"] and not e["ported"]]
    if unported:
        detail = ", ".join(f"{e['net']} ({e['nodes']} nodes)" for e in unported)
        out.append(
            f"extra_nets: {detail} was meshed but NO port touches it. Extra nets "
            f"are galvanically isolated from the switching cell (FETs, fuses and "
            f"the shunt in between are components, not copper), so prune() drops "
            f"this copper from the FastHenry deck entirely: it is paid for in "
            f"geometry time and appears in no result. Declare a probe_ports entry "
            f"on it, or drop it from extra_nets.")
    ported = [e for e in entries if e["ported"]]
    if ported:
        out.append(
            f"extra_nets: {', '.join(e['net'] for e in ported)} is ported and "
            f"therefore IN the solved deck. Its copper is new mutual coupling to "
            f"the commutation loop, so L_loop from this run is not comparable "
            f"with the same config without extra_nets; the run's own "
            f"probe_ports[].pulled_new_copper flags say which probes pulled it in.")
    islands = [e["net"] for e in entries if e.get("isolated") and e.get("ported")]
    if islands:
        out.append(
            f"extra_nets: {', '.join(islands)} is ported but galvanically ISOLATED "
            f"from the P_pwr commutation loop, so its port measures a "
            f"stand-alone two-terminal path (partial self L, and the copper R "
            f"between the two pads). That is the intended quantity for a "
            f"terminal-path leg; it is NOT part of any loop the reduction reports.")
    return out


def manifest(entries, roi=None, margin=None, extents=None):
    """JSON-safe provenance for the sidecar / `topo` / `meta`.

    Emitted ONLY when `extra_nets` was actually requested, so a run without the
    feature produces byte-identical artifacts to one from before it existed.
    """
    out = []
    for e in entries:
        info = (extents or {}).get(e["net"]) or {}
        out.append(dict(
            net=e["net"],
            already_meshed=e["already_meshed"],
            was_gate_net=e["was_gate_net"],
            nodes=e.get("nodes"),
            zone_nodes=e.get("zone_nodes"),
            retained=e.get("retained"),
            ported=e.get("ported"),
            isolated=e.get("isolated"),
            bbox=list(info["bbox"]) if info.get("bbox") else None,
            has_zone=info.get("has_zone"),
            zone_bbox=list(info["zone_bbox"]) if info.get("zone_bbox") else None,
            margin_required_mm=info.get("margin_required_mm"),
        ))
    return dict(
        nets=out,
        roi=list(roi) if roi else None,
        margin=margin,
        # State the policy in the artifact, not just in the source: a consumer
        # reading `roi` should not have to guess whether the extra nets moved it.
        roi_policy="extra_nets_do_not_extend_roi",
    )
