# Implementation plan — KiCad → FastHenry power-stage parasitic extractor

## Context

Generalize the Fugu2 FastHenry commutation-loop extractor into a reusable
`dcdc-tools` tool. Given any `.kicad_pcb` + the **GND net** and **switch-node
net**, compute the half-bridge power-stage parasitic **L and R**, useful for:

- **HS gate-drive analysis with common-source inductance (CSI)** — the HS gate
  loop overlaps the shoot-through high-current path through the FET source lead,
  so the shared source inductance couples power di/dt into the gate loop.
- **Switch-node peak-voltage / ringing** — commutation loop L (+ Coss).

Primary output: a generated SPICE subcircuit (+ JSON), plus a Markdown report.
Scope: full half-bridge (HS + LS gate loops and both common-source inductances).

## Approach (as built)

One FastHenry solve, three ports → full mutual-inductance matrix:

| Port | Across | Gives |
|------|--------|-------|
| `P_pwr` | nearest Cin (Vin↔GND) | commutation loop L, R |
| `P_ghs` | HS gate driver-end ↔ HS gate return | HS gate-loop L |
| `P_gls` | LS gate driver-end ↔ LS gate return | LS gate-loop L |

FET channels shorted at the die plane and gates closed to source there, so
`P_pwr` traces the full `Cin→HS→SW→LS→GND→Cin` shoot-through loop and each gate
loop shares that FET's source lead. **CSI = mutual `M(P_pwr, P_gate)`** — the
shared source-lead partial inductance. Gate-return node position encodes Kelvin
(CSI excluded) vs non-Kelvin (CSI included). Emitted subckt expresses CSI as a
**shared source-lead branch** (`Lscs_*`).

Meshing: tracks→filaments; pours→gridded mesh clipped to the filled polygon and to
an ROI around the FETs/Cin; vias→vertical filaments; THT pads + FET leads→vertical
stubs to a die plane. Nodes interned by `(net, layer, snapped-xy)` (coincident-
node fix); every track/via/pad bonded to its pour; union-find prune to
port-reachable copper. `L = Im(Z)/2πf`, `R = Re(Z)` at a low-MHz plateau.
`nwinc/nhinc` default to 1 (fast; L-plateau barely affected) — raise for HF R.

## Files

- `extract_parasitics.py` — CLI orchestrator (KiCad-python geom + system-python
  solve); the only entry point at the repo root.
- `lib/kicad_geom.py` — pcbnew → multiport FastHenry `.inp` (mesh engine).
- `lib/fet_discovery.py` — pcbnew auto-ID of HS/LS FETs, Vin, gate nets, Cin, Kelvin.
- `lib/solve_reduce.py` — run fasthenry, parse Zc.mat, reduce to named parasitics.
- `lib/emit.py` — parasitics → `parasitics.lib` (shared-branch CSI) + `.json` + `report.md`.
- `lib/emit_svg.py` — parasitics → `schematic.svg`.

## Verification

Validated on `mppt-2420-hc` (4-layer buck). **Corrected 2026-08-12** — the paragraph here
previously named the wrong nets, the wrong LS device, and numbers from a different board.
Against `examples/mppt-2420-hc.yaml` and `out/out-mppt-2420-hc/parasitics.json`:

| | this doc said | actual |
|---|---|---|
| SW net | `/DC/DC/SW_NODE` | `/DCDC power stage/SW_NODE` |
| Vin net | `/DCDC_HV+` | `/DCDC power stage/DCDC_IN` |
| LS ref | `Q2` | **`Q4`** |
| loop L @ 2 mm | ≈ 8.5 nH | **7.380 nH** (−13 %) |
| CSI_hs | ≈ 0.7 nH | 0.685 nH ✓ |
| CSI_ls | ≈ 1.4 nH | **6.228 nH** (4.4×) |

The quoted triple (8.5 / 0.7 / 1.4 nH) is a much better match for **`mppt-1210-hus`**
(8.841 / 0.728 / 1.123 nH) — a different board, same `hs_ref: [Q1]` / `ls_ref: [Q4]`. Treat it as
having been recorded against that one.

"All three ports connected (no NaN)" holds. Note also the config's `pcb:` URL points at
`mppt-2420-**lc**`, not `-hc`, so the output directory name and the board it fetches disagree.

Follow-ups — **both already done** (2026-08-12), left here only as a record: golden regression on
the Fugu2 board (16+ Fugu2 extractions exist plus two committed fixtures, `loss/examples/fugu2`
and `loss/examples/fugu2-dualLS`) and the mesh-convergence sweep at finer pitch (pitch 1.0 / 0.7 /
0.5 runs are retained, and `sweep.py` drives it). Automatic Kelvin detection remains open.
