# parasitics — KiCad → FastHenry power-stage extractor

Extracts the **half-bridge power-stage parasitic inductances and resistances**
straight from a KiCad `.kicad_pcb`, for two jobs:

1. **HS gate-drive / shoot-through analysis** — the high-side gate loop overlaps
   the high-di/dt commutation path through the FET **source lead**, so that
   shared **common-source inductance (CSI)** feeds power di/dt back into the gate
   drive. This tool measures it.
2. **Switch-node peak-voltage / ringing** — the commutation-loop inductance that,
   with the FET Coss, sets the SW overshoot and ring frequency.

You give it the **switch-node net** and the **GND net**; everything else (HS/LS
FETs, Vin rail, gate nets, input caps) is auto-discovered from connectivity, with
overrides for every guess.

## How it works

One FastHenry solve with three ports yields the full mutual-inductance matrix:

| Port | Across | Gives |
|------|--------|-------|
| `P_pwr` | nearest Cin (Vin ↔ GND) | commutation-loop L, R |
| `P_ghs` | HS gate driver-end ↔ HS gate return | HS gate-loop L |
| `P_gls` | LS gate driver-end ↔ LS gate return | LS gate-loop L |

Both FET channels are shorted at the die plane (`.equiv drain_die source_die`)
and each gate is closed to its source there, so `P_pwr` traces the full
`Cin → HS → SW → LS → GND → Cin` shoot-through loop and each gate loop shares that
FET's source lead. **CSI then falls out as the mutual** `M(P_pwr, P_gate)` — the
shared source-lead partial inductance. Whether the gate return taps the die-source
(**Kelvin**, CSI excluded) or the power-source pad (**non-Kelvin**, full CSI) is
encoded by where the gate-return node is placed (default: non-Kelvin / worst case;
force with `--hs-kelvin` / `--ls-kelvin`).

Meshing: tracks → filaments; copper pours → a gridded filament mesh clipped to the
real filled polygon (and to an ROI around the FETs/Cin, so far copper is skipped);
vias → vertical filaments; THT pads and FET leads → vertical stubs to a die plane.
Nodes are interned by `(net, layer, snapped-xy)` so coincident same-net endpoints
merge, and every track/via/pad node is bonded to its pour (fixes fragmented
copper); a union-find prune keeps only port-reachable copper. `L = Im(Z)/2πf`,
`R = Re(Z)` read at a low-MHz plateau.

## Usage

```sh
python3 extract_parasitics.py PCB --sw SW_NET --gnd GND_NET \
        [--pitch 2.0 1.0] [--lead-mm 3.0] [--vin NET] \
        [--hs-ref Q1 --ls-ref Q2] [--hs-gate NET --ls-gate NET] \
        [--hs-kelvin] [--ls-kelvin] -o OUTDIR
```

Multiple `--pitch` values run a mesh-convergence sweep (report drift; finest used
for the artifacts). Example (the MPPT test board):

```sh
python3 extract_parasitics.py .../mppt-2420-hc.kicad_pcb \
        --sw "/DC/DC/SW_NODE" --gnd GND --pitch 2.0 1.0 -o out/
```

### Outputs (`OUTDIR/`)
- **`parasitics.lib`** — `.SUBCKT pwrstage VIN SW GND HSG LSG HSKEL LSKEL`. CSI is a
  **shared source-lead branch** (`Lscs_hs`/`Lscs_ls`): drive the HS gate between
  `HSG` and `SW` for non-Kelvin (CSI in the loop) or between `HSG` and `HSKEL` for
  Kelvin (CSI excluded). Add your Cin across `VIN–GND` and device models for a
  gate-drive/DPT or SW-overshoot sim.
- **`parasitics.json`** — named parasitics + full port L/R matrix + provenance.
- **`report.md`** — table + a topology sketch of where CSI sits.

## Requirements & config

- **KiCad** (pcbnew) — the geometry step runs under KiCad's bundled python. Path
  via `$KICAD_PY` (default: the macOS KiCad.app bundled interpreter).
- **FastHenry** — path via `$FASTHENRY` (default `~/dev/vendor/FastHenry2/bin/fasthenry`).
  Build the FastFieldSolvers fork; on a modern clang toolchain use e.g.
  `CFLAGS='-O -DFOUR -m64 -std=gnu89 -fcommon -Wno-implicit-function-declaration
  -Wno-implicit-int -Wno-return-type -Wno-deprecated-non-prototype' make`.
- Python 3 with `numpy` for the solve/reduce step.

## Limitations / notes

- FastHenry is magnetoquasistatic (L/R only) — Coss resonance stays in SPICE; the
  emitted subckt is parasitics-only, combine with device models downstream.
- FET **exposed-lead** length is modelled (`--lead-mm`); package-internal source
  inductance (datasheet) adds to CSI and should be included separately.
- Gate-return **Kelvin detection is not automatic** — defaults to non-Kelvin
  (worst-case CSI); set `--hs-kelvin`/`--ls-kelvin` if the layout Kelvin-senses.
- FET/gate discovery is heuristic; the printed topology shows what was detected —
  override with `--hs-ref/--ls-ref/--hs-gate/--ls-gate/--vin` if wrong.
- Solve time is set by mesh density (FastHenry's iterative solver): a ~2 mm pour
  pitch on a small board is a few thousand filaments and solves in minutes; drop
  to `--pitch 3` for a quick look, `--pitch 1` (slower) when you need accuracy.

## Validation

Developed/validated against `mppt-2420-hc` (4-layer buck; SW `/DC/DC/SW_NODE`,
HS `Q1`, LS `Q2`): loop L ≈ 7.8 nH, CSI_hs ≈ 0.65 nH, CSI_ls ≈ 1.3 nH at 2 mm
pitch — physically sane magnitudes for a compact half-bridge.
