# Review brief: `gate_net_override` for dual-LS Fugu2

## Feature

`gate_net_override` lets a FET footprint (D9, a DNP Schottky TO-220 repurposed
as a 2nd LS MOSFET) have its gate pad net reassigned **in memory** — no PCB file
patch. A synthetic B.Cu track connects the overridden pad to the nearest
pad 1 of a declared sibling FET on the target gate net so FastHenry can mesh
the gate wire.

## Files changed

| File | Change |
|------|-------|
| `parasitics/extract_parasitics.py` | `gate_net_override` in DEFAULTS, `_validate_config` (dict), CLI arg, `run_geom` serialization |
| `parasitics/lib/gate_net_override.py` | pad-1 net reassignment, sibling-FET track selection, lumped-mode warning, realized-track provenance |
| `parasitics/lib/kicad_geom.py` | applies overrides before `fet_discovery.discover()` and stamps their provenance into topology |
| `parasitics/examples/fugu2-dualLS-perDev-noLeads.yaml` | extraction config with `gate_net_override: {D9: "Net-(Q2-G)"}` |
| `loss/examples/fugu2-dualLS.yaml` | loss config pointing at the per-device parasitics |
| `parasitics/test/test_gate_net_override.py` | focused endpoint-selection, refusal, and warning tests |

## Verified working

- Extraction: 2 LS devices (Q2 + D9) in `parallel_devices.ls`, distinct per-device
  parasitics (D9 L_gate=3.51nH vs Q2 2.28nH; D9 L_switch=3.58nH vs Q2 7.38nH).
- Loss sim: 16.68 W total, LS junction 6.74 W (3.37 W x2 dies), budget closes.
- 155 tests pass.

## BUG-1 (resolved): nearest-pad heuristic connected to R8, not Q2

The synthetic track connects D9's gate pad to the **nearest** pad on
`Net-(Q2-G)`, which is R8 (gate resistor, 8.8mm) — not Q2's gate pad (10.4mm).
The physical hand-wire goes to Q2's gate, so the modeled gate-loop L is
~1.6mm short.

**Fix:** the search now considers only pad 1 of declared, non-overridden sibling
FET refs. Real-board validation confirms the endpoint is Q2 pad 1.

## Risks (not bugs)

- **R1:** pad-1 = gate assumption holds for TO-220 but not universally.
- **R2:** SMD pads on F.Cu would leave the B.Cu track floating (not an issue
  for THT TO-220).
- **R3 (resolved):** `parallel_fets: lumped` + `gate_net_override` warns that
  per-device gate-loop results are unavailable.
- **R4 (resolved):** requested and realized override provenance is present in
  `parasitics.json`.
- **R5 (resolved):** focused override tests cover endpoint selection and refusal.

## Completed follow-ups

1. Fixed BUG-1 by scoping endpoint selection to sibling FET refs.
2. Added `gate_net_override` to the topo/sidecar for JSON provenance.
3. Added a warning for `lumped` + `gate_net_override`.
4. Added focused unit tests for the override function.

## Resolution (2026-08-11)

Gate closed. All four follow-ups are implemented:

- Synthetic tracks terminate at pad 1 of the nearest declared, non-overridden
  sibling FET. The real Fugu2 geometry now connects D9 directly to Q2 pad 1;
  R8 is excluded even when it is nearer.
- `topo.gate_net_override` preserves the requested mapping and
  `topo.gate_net_override_tracks` records the realized endpoint, layer, width,
  and length. These fields reach both the mesh sidecar and `parasitics.json`.
- Lumped extraction emits an explicit warning that it cannot produce the
  override's per-device `L_gate`/CSI result.
- Focused tests cover the R8 regression, fail-closed sibling selection, warning,
  YAML validation, and geometry-subprocess forwarding. The complete parasitics
  suite passes: 155 tests.

Real-board validation produced a 10.414 mm, 0.5 mm wide B.Cu synthetic track
from D9 pad 1 to Q2 pad 1. The regenerated extraction reports D9
`L_gate = 3.512 nH`; the downstream dual-LS loss run still closes at 16.680 W
total and 3.366 W per LS die.
