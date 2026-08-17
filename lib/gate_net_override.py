"""In-memory FET gate-net overrides for off-board gate wires."""
import math
import sys

import pcbnew

NM = 1e6  # KiCad internal units (nm) per mm


def _parse(override_str):
    """Parse ``REF=NET`` pairs while preserving their command-line order."""
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
                f"--gate-net-override: bad pair {pair!r}, expected ref=net")
        if ref in seen:
            raise SystemExit(
                f"--gate-net-override: duplicate footprint {ref!r}")
        seen.add(ref)
        overrides.append((ref, net_name))
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
    overridden_refs = {ref for ref, _ in overrides}
    declared_fet_refs = set(fet_refs or ())
    footprints = {fp.GetReference(): fp for fp in board.GetFootprints()}
    realized = {}
    for ref, net_name in overrides:
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

        # Never use another overridden ref as an endpoint: an earlier synthetic
        # source must not win merely because overrides are applied in order.
        sibling_refs = declared_fet_refs - overridden_refs
        if not sibling_refs:
            raise SystemExit(
                "--gate-net-override: no non-overridden sibling FET refs were "
                "declared; pass the applicable --hs-ref/--ls-ref footprints")
        best = None
        best_d2 = float("inf")
        for sibling_ref in sibling_refs:
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
                f"{sorted(sibling_refs)} is on net {net_name!r}")

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
            track_layer="B.Cu",
            track_width_mm=0.5,
            track_length_mm=length_mm,
        )
        sys.stderr.write(
            f"  gate-net-override: {ref} pad 1 -> {net_name!r}, "
            f"synthetic track to {target_ref} pad {target_pad.GetNumber()} "
            f"({length_mm:.1f} mm)\n")
    return realized
