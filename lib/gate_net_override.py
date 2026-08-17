"""In-memory FET gate-net overrides for off-board gate wires."""
import math
import sys

import pcbnew

NM = 1e6  # KiCad internal units (nm) per mm


def _parse(override_str):
    """Parse ``REF=NET`` or ``REF=NET@ANCHOR_REF.ANCHOR_PAD`` pairs, in order.

    The optional ``@ANCHOR`` names the pad the off-board wire physically lands on. It exists
    because the default endpoint search only considers DECLARED, non-overridden sibling FETs,
    which cannot express a board whose gate wire terminates on a NON-POPULATED footprint. On
    flu the single LS FET sits in the D9 land with its gate wired to Q2.1, and Q2 carries no
    device -- so with ``ls_ref: [D9]`` there is no declared sibling to anchor to and the
    override could not be used at all. Declaring ``ls_ref: [Q2, D9]`` to work around that adds
    Q2's lead and die copper to the deck, which is a second, unrelated error.

    Split from the RIGHT: KiCad net names contain parentheses and hyphens
    (``Net-(Q2-G)``) and an ``@`` in a net name is far less likely than one in this syntax,
    but rpartition makes the last ``@`` the separator either way.
    """
    overrides = []
    seen = set()
    for pair in override_str.split(","):
        pair = pair.strip()
        if not pair:
            continue
        ref, sep, net_name = pair.partition("=")
        ref, net_name = ref.strip(), net_name.strip()
        if not sep or not ref or not net_name:
            raise SystemExit(
                f"--gate-net-override: bad pair {pair!r}, expected ref=net "
                f"or ref=net@anchor_ref.anchor_pad")
        anchor = None
        if "@" in net_name:
            net_name, _, anchor = net_name.rpartition("@")
            net_name, anchor = net_name.strip(), anchor.strip()
            if not net_name or not anchor:
                raise SystemExit(
                    f"--gate-net-override: bad pair {pair!r}, expected "
                    f"ref=net@anchor_ref.anchor_pad")
            if anchor.count(".") != 1 or anchor.startswith(".") or anchor.endswith("."):
                raise SystemExit(
                    f"--gate-net-override: bad anchor {anchor!r} in {pair!r}, "
                    f"expected ANCHOR_REF.ANCHOR_PAD (e.g. Q2.1)")
        if ref in seen:
            raise SystemExit(
                f"--gate-net-override: duplicate footprint {ref!r}")
        seen.add(ref)
        overrides.append((ref, net_name, anchor))
    return overrides


def warn_if_lumped(parallel_fets, override_str):
    if override_str and parallel_fets == "lumped":
        sys.stderr.write(
            "WARNING: --gate-net-override is active with --parallel-fets lumped; "
            "lumping collapses the sibling FET gate loops, so the synthetic wire "
            "does not produce a per-device L_gate/CSI result. Use "
            "--parallel-fets per-device for per-ref parasitics.\n")


def apply(board, override_str, fet_refs):
    """Reassign FET pad-1 nets and add synthetic off-board gate tracks.

    ``override_str`` uses ``REF=NET`` pairs. Each overridden pad is connected
    to pad 1 of the nearest declared, non-overridden sibling FET already on the
    target net. Restricting endpoints to declared FETs prevents a nearer gate
    resistor from shortening the modeled wire.

    Returns JSON-safe realized-track provenance keyed by overridden ref.
    """
    overrides = _parse(override_str)
    overridden_refs = {ref for ref, _, _ in overrides}
    declared_fet_refs = set(fet_refs or ())
    footprints = {fp.GetReference(): fp for fp in board.GetFootprints()}
    realized = {}
    for ref, net_name, anchor in overrides:
        net = board.FindNet(net_name)
        if net is None:
            raise SystemExit(
                f"--gate-net-override: net {net_name!r} not found on the board. "
                f"It must be an existing net (e.g. the sibling FET's gate net).")
        fp = footprints.get(ref)
        if fp is None:
            raise SystemExit(f"--gate-net-override: footprint {ref!r} not found")
        gate_pad = next((p for p in fp.Pads() if p.GetNumber() == "1"), None)
        if gate_pad is None:
            raise SystemExit(f"--gate-net-override: {ref} has no pad 1 (gate pin)")
        gate_pad.SetNet(net)
        gate_pos = gate_pad.GetPosition()

        best = None
        best_d2 = float("inf")
        anchor_mode = "sibling_search"

        if anchor:
            # EXPLICIT ANCHOR. Every failure below is hard: a named endpoint that silently
            # fell back to the sibling search would move the synthetic wire somewhere the
            # caller did not ask for, and the only visible symptom would be a different
            # L_gate -- a wrong number with no warning attached.
            anchor_mode = "explicit"
            a_ref, _, a_pad_num = anchor.partition(".")
            a_fp = footprints.get(a_ref)
            if a_fp is None:
                raise SystemExit(
                    f"--gate-net-override: anchor footprint {a_ref!r} (from {anchor!r}) "
                    f"not found on the board")
            if a_ref in overridden_refs:
                raise SystemExit(
                    f"--gate-net-override: anchor {anchor!r} names an overridden ref; "
                    f"chaining one synthetic wire onto another would make the modelled "
                    f"length depend on override order")
            a_pad = next((q for q in a_fp.Pads() if q.GetNumber() == a_pad_num), None)
            if a_pad is None:
                raise SystemExit(
                    f"--gate-net-override: anchor footprint {a_ref!r} has no pad "
                    f"{a_pad_num!r}")
            # The anchor must ALREADY be on the target net. It is the far end of a wire that
            # exists on the physical board, not a place to graft the net onto -- if it is on
            # some other net, the declaration describes a board this is not.
            if a_pad.GetNetname() != net_name:
                raise SystemExit(
                    f"--gate-net-override: anchor {anchor!r} is on net "
                    f"{a_pad.GetNetname()!r}, not the override net {net_name!r}")
            a_pos = a_pad.GetPosition()
            best_d2 = (a_pos.x - gate_pos.x) ** 2 + (a_pos.y - gate_pos.y) ** 2
            best = (a_ref, a_pad, a_pos)

        # Never use another overridden ref as an endpoint: an earlier synthetic
        # source must not win merely because overrides are applied in order.
        sibling_refs = declared_fet_refs - overridden_refs
        if best is None and not sibling_refs:
            raise SystemExit(
                "--gate-net-override: no non-overridden sibling FET refs were "
                "declared; pass the applicable --hs-ref/--ls-ref footprints, or name "
                "the wire's real landing pad explicitly as ref=net@anchor_ref.anchor_pad "
                "(e.g. D9=Net-(Q2-G)@Q2.1) when it terminates on a footprint that carries "
                "no device")
        for sibling_ref in (sibling_refs if best is None else ()):
            sibling = footprints.get(sibling_ref)
            if sibling is None:
                continue
            for pad in sibling.Pads():
                if pad.GetNumber() != "1" or pad.GetNetname() != net_name:
                    continue
                pos = pad.GetPosition()
                d2 = (pos.x - gate_pos.x) ** 2 + (pos.y - gate_pos.y) ** 2
                if d2 < best_d2:
                    best_d2 = d2
                    best = (sibling_ref, pad, pos)
        if best is None:
            raise SystemExit(
                f"--gate-net-override: no pad 1 on declared sibling FET refs "
                f"{sorted(sibling_refs)} is on net {net_name!r}. If the wire lands on a "
                f"footprint that carries no device, name it: ref=net@anchor_ref.anchor_pad")

        target_ref, target_pad, target_pos = best
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(gate_pos)
        track.SetEnd(target_pos)
        track.SetLayer(board.GetLayerID("B.Cu"))
        track.SetNet(net)
        track.SetWidth(int(0.5 * NM))
        board.Add(track)
        length_mm = math.sqrt(best_d2) / NM
        realized[ref] = dict(
            net=net_name,
            gate_pad="1",
            target_ref=target_ref,
            target_pad=target_pad.GetNumber(),
            # WHICH endpoint rule produced this wire. The two modes can pick different pads
            # and therefore different lengths, and length is the whole modelled quantity --
            # so the artifact says which one ran rather than leaving a reader to infer it
            # from the declaration.
            anchor_mode=anchor_mode,
            track_layer="B.Cu",
            track_width_mm=0.5,
            track_length_mm=length_mm,
        )
        sys.stderr.write(
            f"  gate-net-override: {ref} pad 1 -> {net_name!r}, "
            f"synthetic track to {target_ref} pad {target_pad.GetNumber()} "
            f"({length_mm:.1f} mm, {anchor_mode})\n")
    return realized
