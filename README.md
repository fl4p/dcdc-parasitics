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

## Showcase

Half-bridge parasitics extracted from the open-hardware
[LibreSolar MPPT 2420 HC](https://github.com/LibreSolar/mppt-2420-hc) (4-layer
synchronous buck), rendered by `--svg`: each parasitic as a labelled coil, the
two **common-source source-leads in red**, the input-cap bank (the ported cap and
the greyed-out ones), and the auto-detected gate network (Rg).

![Power-stage parasitics of the LibreSolar MPPT 2420 HC](docs/mppt-2420-hc.svg)

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

### Parallel input caps (`--cin-parallel N`)

By default `P_pwr` sits across the **single nearest** ceramic — a *conservative
upper bound* on the loop L, because it ignores the other input caps that share the
commutation current. Since inductances in parallel combine reciprocally, the real
effective loop L is **lower**, so the single-cap number over-estimates SW-node
overshoot. `--cin-parallel N` ports the **N nearest** input ceramics in the same
solve, so FastHenry returns their full mutual matrix, and the reduce step forms the
true effective 2-terminal commutation impedance under a common-voltage drive
(every cap pad pair at the same SW-node voltage, gates open):

```
Z_eff = 1 / (1ᵀ Zc⁻¹ 1)          (Zc = N×N cap-port submatrix)
```

which folds in every branch-to-branch mutual `Mᵢⱼ` exactly (not a naive `1/ΣLᵢ`),
solved as `Zc x = 1` (never an explicit inverse; `cond(Zc)` is reported and a
warning fires if it is ill-conditioned). The CSI mutual is likewise re-weighted by
the parallel-cap current split `y = Zc⁻¹·1`, which is reported per refdes.

**The SW-peak loop L is a bracket, not one number:**

| bound | meaning |
|---|---|
| `L_loop_single` (upper) | nearest single cap alone — pessimistic |
| `L_loop_ideal` (lower) | all N caps ‖, treated as ideal shorts (copper only) |
| `L_loop_physical` | with per-cap ESL/ESR (`--cin-esl`/`--cin-esr`) — the real split |

The truth sits between the bounds, near the lower one when cap ESL ≪ per-cap branch
L. At the ring frequency the ceramics are above SRF and look like ESL (~0.3–1 nH),
which is comparable to the per-cap copper branch L, so pass `--cin-esl` for a
physical number; without it you still bracket the truth. All three land in the
report/JSON. Port polarity is fixed (always Vin→GND) so a reversed cap can't
silently corrupt the mutuals; a spuriously-low effective L still trips a warning.

**Cap selection.** Default is nearest-by-centroid-distance (deterministic, shown in
the manifest as `cin_select`); **bulk electrolytics are excluded by package/type**
(THT can/radial, `CP_`/`Elec`/tantalum/polymer footprints) — above their SRF they
can't source the tens-of-MHz edge. Classification is **by footprint, not value**, so
a 10–22 µF 1210 MLCC stays in the HF set while a small electrolytic stays out; the
per-refdes class is recorded in `cin_class`. Keep the bulk caps with
`--include-bulk-cin` (e.g. a low-frequency ripple-path study). Override selection
entirely with `--cin-refs C17 C18 C9 C16`. If you request more caps than exist, it
warns and solves with what it found rather than silently clamping.

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
        [--cin-parallel 4 | --cin-refs C17 C18 C9 C16] [--include-bulk-cin] \
        [--cin-esl 0.5 --cin-esr 3] \
        [--hs-ref Q1 Q3 --ls-ref Q2] [--hs-gate NET --ls-gate NET] \
        [--hs-kelvin] [--ls-kelvin] [--weld-tol 0.6] [--margin 8] [--svg] -o OUTDIR
```

`--hs-ref`/`--ls-ref` take **multiple** refdes for paralleled switches (e.g.
`--hs-ref Q1 Q3`). `--weld-tol` fuses same-net nodes within N mm (mesh
de-fragmentation, see below); `--margin` sets the pour-meshing ROI around the
FETs/Cin.

Example — Fugu2 (2-layer buck, paralleled HS, explicit HF cap bank):

```sh
python3 extract_parasitics.py .../Fugu2.kicad_pcb --sw SW --gnd BuckGND --vin Solar+ \
        --hs-ref Q1 Q3 --ls-ref Q2 --cin-refs C9 C16 C17 C18 C21 C22 --pitch 2.0 -o out/
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
- **`schematic.svg`** (with `--svg`) — a standalone half-bridge drawing of the
  extracted network: each parasitic as a labelled coil, the two common-source
  source-leads in red, and the input-cap bank showing which caps the model ported
  (parallel legs with their current split) vs. board caps left out of the loop.
  The discrete **gate network** (series Rg and any anti-parallel diode, auto-found
  on the gate net) is annotated for context — copper-only FastHenry does not model
  it, so the coil's `R` is the trace resistance (`Cu`), distinct from the gate Rg.

## Requirements & config

- **KiCad** (pcbnew) — the geometry step runs under KiCad's bundled python. Path
  via `$KICAD_PY` (default: the macOS KiCad.app bundled interpreter).
- **FastHenry** — path via `$FASTHENRY` (default `~/dev/vendor/FastHenry2/bin/fasthenry`).
  Build the FastFieldSolvers fork; on a modern clang toolchain use e.g.
  `CFLAGS='-O -DFOUR -m64 -std=gnu89 -fcommon -Wno-implicit-function-declaration
  -Wno-implicit-int -Wno-return-type -Wno-deprecated-non-prototype' make`.
- Python 3 with `numpy` for the solve/reduce step.

## Paralleled switches

`--hs-ref`/`--ls-ref` accept several refdes (e.g. `--hs-ref Q1 Q3`). Each
paralleled FET contributes its own drain and source lead stubs; the dies are
tied to one node, so between the rails you get the drain leads in parallel and
the source leads in parallel, and **FastHenry solves the real current split** —
the lower-inductance (shorter) device carries more current, so `L_loop` is the
impedance-weighted parallel value, *not* a naïve `L/2` and *not* the
shorter-path-only value. Two caveats:

- **Ideal channel.** Tying the dies models `Rds_on = 0`, so the split is set by
  copper/lead impedance alone. That is the dominant term at the commutation edge
  (good for SW-peak/di-dt) but ignores the `Rds_on` that equalises the split at
  low frequency — `L_loop` is the HF, channel-ideal value.
- **CSI is the parallel combination.** The reported `CSI_hs` is the paralleled
  source leads, but each device's gate-return current flows through *its own*
  source lead, so the CSI a single gate driver feels is **higher** than reported.
  Only the first FET's gate loop is ported (gate skew not modelled). For accurate
  parallel-pair gate-drive CSI, port that device's own source segment.

## Limitations / notes

- FastHenry is magnetoquasistatic (L/R only) — Coss resonance stays in SPICE; the
  emitted subckt is parasitics-only, combine with device models downstream.
- **Mesh de-fragmentation.** Nets with no pour (gate) or split power fills can
  fragment (a track ending inside a pad, touching fills) → an all-NaN solve.
  `Model.weld(--weld-tol, default 0.6 mm)` fuses same-net+layer nodes within that
  radius (< pour pitch, so it never welds across the mesh grid); vias are modelled
  for every net. Needed for 2-layer boards; raise `--weld-tol` if a port stays
  disconnected (the geometry step prints node/segment/port counts).
- **Cap auto-select can mis-pick.** Nearest-by-centroid may land on a far or
  poorly-connected cap (on Fugu2 it chose a 1 µF 13 mm away on an isolated plane
  pocket). Pin the real HF ceramics with `--cin-refs …` when the auto pick looks
  wrong or the loop won't close.
- FET **exposed-lead** length is modelled (`--lead-mm`); package-internal source
  inductance (datasheet) adds to CSI and should be included separately.
- Gate-return **Kelvin detection is not automatic** — defaults to non-Kelvin
  (worst-case CSI); set `--hs-kelvin`/`--ls-kelvin` if the layout Kelvin-senses.
- FET/gate discovery is heuristic; the printed topology shows what was detected —
  override with `--hs-ref/--ls-ref/--hs-gate/--ls-gate/--vin` if wrong.
- Solve time is set by mesh density (FastHenry's iterative solver): a ~2 mm pour
  pitch on a small board is a few thousand filaments and solves in minutes; drop
  to `--pitch 3` for a quick look, `--pitch 1` (slower) when you need accuracy.

## Tests

`python3 test_parasitics.py` — stdlib + numpy unit tests for the pure layers
(SVG formatting/rendering, the reduction maths: single-cap, parallel effective L,
common-source mutual, and the `weld` de-fragmentation). The pcbnew/FastHenry
geometry+solve path is covered by running on the real boards below.

## Validation

- `mppt-2420-hc` (4-layer buck; SW `/DC/DC/SW_NODE`, HS `Q1`, LS `Q2`): loop L
  ≈ 8.5 nH, CSI_hs ≈ 0.71 nH, CSI_ls ≈ 1.36 nH, gate Rg `R1/R2 = 3R3` at 2 mm.
- `Fugu2` (2-layer buck, paralleled HS `Q1∥Q3`, LS `Q2`, 6-cap HF bank): loop L
  ≈ 8.2 nH (6 caps ‖), CSI_hs ≈ 0.43 nH, CSI_ls ≈ 1.25 nH, gate Rg `R4/R10 = 4.7`
  — exercises paralleled FETs, the weld pass, and multi-cap reduction.
