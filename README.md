# parasitics — KiCad → FastHenry power-stage extractor

Extracts the half-bridge power-stage parasitic inductances and resistances
from a KiCad PCB for:

1. **Switch-node ringing** — the commutation-loop inductance that,
   with the FET Coss and Qrr, sets the SW overshoot and ring frequency.
2. **Gate-drive analysis**: compute gate loop inductance and detect ringing 
   issues. And the gate loop overlaps the high-di/dt commutation path through 
   the FET **source lead**, so that shared **common-source inductance (CSI)**
   feeds power di/dt back into the gate drive.
3. **Power-loss / efficiency modelling** — the extracted **resistances** are the
   copper contribution to the I²R budget, split **per switch** so
   the conduction loss can be weighted by each switch's duty (HS by D, LS by 1−D —
   the LS copper dominates at low duty). The loop carries current at two very
   different frequencies (switching frequency and HF ringing), so it has [two 
   resistances](#two-resistances-hf-ring-vs-lf-conduction).

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

One FastHenry solve with multiple ports yields the full mutual-inductance matrix:

| Port | Across | Gives |
|------|--------|-------|
| `P_pwr` | nearest Cin — **MLCC** (Vin ↔ GND) | commutation-loop L, HF ring R |
| `P_ghs` | HS gate driver-end ↔ HS gate return | HS gate-loop L |
| `P_gls` | LS gate driver-end ↔ LS gate return | LS gate-loop L |
| `P_bulk` | nearest **bulk electrolytic** (Vin ↔ GND) | LF conduction-loop R |
| `P_hs` | Vin(bulk) → SW, via HS leads | HS conduction R |
| `P_ls` | SW → GND(bulk), via LS leads | LS conduction R |

Both FET channels are shorted at the die plane (`.equiv drain_die source_die`)
and each gate is closed to its source there, so `P_pwr` traces the full
`Cin → HS → SW → LS → GND → Cin` shoot-through loop and each gate loop shares that
FET's source lead. **CSI then falls out as the side-specific mutual**
`M(P_hs, P_ghs)` / `M(P_ls, P_gls)` — the shared source-lead partial inductance
that the gate driver actually sees. The older full-loop mutual is still recorded
as `csi_hs_loop` / `csi_ls_loop` in JSON for diagnostics. Whether the gate return
taps the die-source (**Kelvin**, CSI excluded) or the power-source pad
(**non-Kelvin**, full CSI) is encoded by where the gate-return node is placed
(default: non-Kelvin / worst case; force with `--hs-kelvin` / `--ls-kelvin`).

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
warning fires if it is ill-conditioned). The parallel-cap current split
`y = Zc⁻¹·1` is reported per refdes.

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

### Two resistances: HF ring vs LF conduction

The loop resistance is **not one number**, because it carries current at two
frequencies that see different copper and different reference caps:

| R | Freq | Anchored on | Used for |
|---|---|---|---|
| `R_loop` (ring) | ~MHz plateau | nearest **MLCC** (sources the edge) | SW-node ring Q / damping; skin-elevated |
| `R_hs` / `R_ls` (conduction) | ~DC fundamental | nearest **bulk electrolytic** | conduction I²R, split per switch |

At the 39 kHz switching fundamental the **MLCCs are ~open** and carry no conduction
current — the fundamental is sourced and returned by the **bulk electrolytics**. So
the conduction ports (`P_hs`/`P_ls`/`P_bulk`) anchor on the nearest bulk cap, not the
ceramic, and their R is read at the **lowest swept frequency** (skin depth ≫ copper
thickness there, i.e. the near-DC conduction value) rather than at the ring plateau.
`P_hs` drives Vin(bulk) → SW through the HS drain+source leads (the die short routes
it), so its self-R is that switch's true conduction copper; `P_ls` likewise for
SW → GND. The residual `R_loop_cond − R_hs − R_ls` is reported as the **SW-node
spreading R**. In the emitted `.SUBCKT`, `R_loop` is a single solved HF ring
resistance; its HS/LS `Rser` placement is only a damping distribution, split by
the LF `R_hs:R_ls` proportion. A reconstruction check warns if the per-side
conduction R exceeds the LF loop R (a port-polarity/SW-reference tripwire).
Boards with an **all-ceramic** input bank fall back to the nearest ceramic for the
conduction anchor (there the ceramics *do* carry the fundamental); the anchor
refdes and class are recorded in `cond_ref`.

Both `R_loop` and `R_hs`/`R_ls` are read at a single characteristic frequency (the ring
plateau and near-DC respectively). To get the **AC resistance at an arbitrary
frequency**, raise the skin sub-mesh (`--nwinc/--nhinc > 1`); the per-frequency
`L_eff_sweep` in the JSON then shows R rising across the band as skin/proximity effect
crowds the current.

### Input-cap branch network (`--emit-cin-network`)

For the loss tool's **Cin ESR / input-ripple** model, `--emit-cin-network` ports the
**full input bank** (bulk + mlcc) individually (`P_cin_<ref>`, separate from the
MLCC-only HF `P_pwr` so `L_loop` is untouched) and decomposes the port matrix into a
**shared Vin/GND trunk + one private branch per cap**: with a common trunk feeding
every cap, `L[i,i] = L_shared + Lb_i` and `L[i,j] ≈ L_shared`, so `L_shared` is the
off-diagonal mean and `Lb_i = L[i,i] − L_shared` (same for R at the conduction
freq). The result is `cin_branches` + `cin_L_shared`/`cin_R_shared` in the JSON.

If the raw full-bank trunk is larger than the selected HF loop basis, the JSON
keeps the decomposition as `cin_*_raw` diagnostics but clamps the default fields
that consumers read: `cin_L_shared <= L_loop`, `L_loop_switch >= 0`, and
`cin_branches` recomputed from the model shared trunk. The loss deck should keep
using `cin_L_shared`/`cin_branches`; use the `_raw` fields only to debug
cap-selection or basis mismatches.

This is **copper only** — parasitics stays parts-DB-free. The loss tool owns the
complete `cin_network` subckt: it enriches each `ref` with its datasheet C/ESR/ESL
from its parts DB and assembles `Lb_<ref>`(copper) in series with `Cel_<ref>`(bulk)
or `Cmlcc_<ref>`(mlcc). `Rb` is **branch copper**, distinct from dielectric ESR. If
the off-diagonal spread is high (the single-trunk model fits poorly), it warns.

Meshing: tracks → filaments; copper pours → a gridded filament mesh clipped to the
real filled polygon (and to an ROI around the FETs/Cin, so far copper is skipped);
vias → vertical filaments; THT pads and FET leads → vertical stubs to a die plane.
Track widths come from KiCad; segment height is `--cu-thickness` (default 0.035 mm).
Nodes are interned by `(net, layer, snapped-xy)` so coincident same-net endpoints
merge, and every track/via/pad node is bonded to its pour (fixes fragmented
copper); a union-find prune keeps only port-reachable copper. `L = Im(Z)/2πf`,
`R = Re(Z)` read at a low-MHz plateau.

FET package inductance has a strict modeling boundary with the loss tool and
MOSFET SPICE models. The legacy `--lead-mm` path is an artificial die-plane
extension, not a physical bent-lead package model; use copper-only extraction for
loss when the MOSFET model already carries package leads. See
[docs/fet-package-boundary.md](docs/fet-package-boundary.md) for the full contract.

## Usage

```sh
python3 extract_parasitics.py PCB --sw SW_NET --gnd GND_NET \
        [--pitch 2.0 1.0] [--lead-mm 3.0] [--cu-thickness 0.035] [--lf-freq 1e5] [--vin NET] \
        [--cin-parallel 4 | --cin-refs C17 C18 C9 C16] [--include-bulk-cin] \
        [--cin-esl 0.5 --cin-esr 3] [--emit-cin-network] \
        [--hs-ref Q1 Q3 --ls-ref Q2] [--hs-gate NET --ls-gate NET] \
        [--hs-kelvin] [--ls-kelvin] [--weld-tol 0.6] [--zone-mesh grid|polygon] \
        [--terminal-mode padland|single|finite|point] \
        [--margin 8] [--svg] -o OUTDIR
```

All CLI arguments can also be supplied from YAML:

```sh
python3 extract_parasitics.py --config fugu2-parasitics.yaml
```

YAML keys use the argparse destination names (`hs_ref`, `cin_parallel`,
`emit_cin_network`, etc.). Command-line arguments override YAML values, including
boolean options via `--no-svg`, `--no-hs-kelvin`, and the other `--no-*` forms.
`pcb` may be a local path or an HTTPS URL to a public `.kicad_pcb`; GitHub
`blob` URLs are converted to raw downloads automatically. Prefer a commit SHA in
the URL instead of a branch name for reproducible extraction.

```yaml
pcb: https://github.com/org/repo/blob/<commit-sha>/hw/Fugu2/Fugu2.kicad_pcb
sw: SW
gnd: BuckGND
vin: Solar+
hs_ref: [Q1, Q3]
ls_ref: [Q2]
cin_refs: [C9, C16, C17, C18, C21, C22]
pitch: [2.0, 1.0]
emit_cin_network: true
weld_tol: 0.6
zone_mesh: grid
terminal_mode: padland
margin: 8.0
cu_thickness: 0.035
lf_freq: 100000
out: out/
```

`--hs-ref`/`--ls-ref` take **multiple** refdes for paralleled switches (e.g.
`--hs-ref Q1 Q3`). `--weld-tol` fuses same-net nodes within N mm (mesh
de-fragmentation, see below); `--margin` sets the pour-meshing ROI around the
FETs/Cin. `--zone-mesh grid` is the validated/default pour mesher. `--zone-mesh
polygon` is an experimental KiPEX-style cell-edge mesher for cross-checks; it is
not the production default because current simple-hb/Fugu2 checks under-read.
`--terminal-mode padland` is the validated/default pad-to-pour contact model.
`single` is a KiPEX-like one-mesh-node terminal, `finite` is an experimental
finite pad-contact model, and `point` is the legacy/debug pad-center stitch path
for A/B comparisons.

`--parallel-fets per-device` opts into the issue #5 extraction model for
paralleled switches: each physical FET keeps its own die/source/gate branch and
gets its own gate + switch-side ports (`P_ghs_Q1`, `P_hs_Q1`, ...). The default
is `--parallel-fets lumped`, which preserves the historical lumped parallel-FET
model and existing downstream behavior.

Example — Fugu2 (2-layer buck, paralleled HS, explicit HF cap bank):

```sh
python3 extract_parasitics.py .../Fugu2.kicad_pcb --sw SW --gnd BuckGND --vin Solar+ \
        --hs-ref Q1 Q3 --ls-ref Q2 --cin-refs C9 C16 C17 C18 C21 C22 --pitch 2.0 -o out/
```

### Interactive path viewer

`visualize_paths.py` emits a standalone HTML/SVG viewer for inspecting the copper
that participates in the extracted parasitic paths. The first view is the
gate-drive loop: HS/LS paths can be toggled independently, with separate toggles
for driver-output copper, FET-gate copper, source-return copper, source-lead CSI
markers, parts, and top/bottom copper layers.

```sh
python3 visualize_paths.py .../mppt-1210-hus.kicad_pcb \
        --sw "/DCDC power stage/SW_NODE" --gnd GND \
        --vin "/DCDC power stage/SOLAR+" --hs-ref Q1 --ls-ref Q4 \
        -o out/gate-loop-viewer.html
```

It also accepts YAML configs using the same argparse destination names as the
extractor:

```sh
python3 visualize_paths.py --config fugu2-parasitics.yaml
```

Extractor-only keys such as `pitch`, `cin_refs`, and `emit_cin_network` are
accepted and ignored, so the same board/topology config can be reused. If `out`
points to a directory, the viewer writes `gate-loop-viewer.html` inside it.

It uses the same `fet_discovery.py` topology logic as the extractor and re-execs
itself under KiCad's bundled Python if `pcbnew` is not importable from the shell
Python. The output is self-contained and can be opened directly in a browser.

Multiple `--pitch` values run a mesh-convergence sweep (report drift; finest used
for the artifacts). Example (the MPPT test board — a coarse pair for a quick drift
check):

```sh
python3 extract_parasitics.py .../mppt-2420-hc.kicad_pcb \
        --sw "/DC/DC/SW_NODE" --gnd GND --pitch 3.0 2.0 -o out/
```

> ⚠️ **This is a 4-layer board — finer pitch gets expensive fast.** Each pitch
> halving is ~4× the pour filaments *per layer*, and FastHenry is single-threaded
> with a super-linear solve, so `--pitch 1.0` on this board is a **10+ minute**
> run (`2.0` finishes in seconds). Use a coarse pair for a quick convergence check;
> only add `--pitch 1.0` when you need the converged number and can wait, and shrink
> `--margin` to trim the meshed ROI. `--pitch 2.0` alone already lands the validated
> 8.5 nH loop L for this board.

### Outputs (`OUTDIR/`)
- **`parasitics.lib`** — `.SUBCKT pwrstage VIN SW GND HSG LSG HSKEL LSKEL`. CSI is a
  **shared source-lead branch** (`Lscs_hs`/`Lscs_ls`): drive the HS gate between
  `HSG` and `SW` for non-Kelvin (CSI in the loop) or between `HSG` and `HSKEL` for
  Kelvin (CSI excluded). Add your Cin across `VIN–GND` and device models for a
  gate-drive/DPT or SW-overshoot sim.
- **`parasitics.json`** — named parasitics + full port L/R matrix + provenance.
  `meta.pcb_sha256` records the SHA-256 of the resolved `.kicad_pcb` input so
  downstream tools can detect stale extraction artifacts. When extracted through
  `--config`, `meta.extract_config_sha256` records that YAML file's SHA-256 too.
  CSI fields: `csi_hs` / `csi_ls` are side-specific source-lead mutuals used in
  the emitted subckt; `csi_hs_loop` / `csi_ls_loop` are the full-loop mutuals for
  diagnostics.
  Conduction fields: `r_hs`, `r_ls` (per-switch conduction R), `r_loop_cond` (LF
  loop R), `r_sw` (SW spreading residual), `r_cond_freq` (the freq they were read
  at), and `cond_ref` (`{ref, cls}` — the bulk cap they anchor on). With
  `--emit-cin-network`: `cin_branches` (`[{ref, cls, Lb, Rb}]` — per-cap copper
  branch) + `cin_L_shared`/`cin_R_shared` (the model shared Vin/GND trunk);
  `cin_branches_raw`/`cin_L_shared_raw`/`cin_R_shared_raw` preserve the raw
  decomposition when the model trunk is clamped to the selected loop/residual
  basis. The loss tool fills each cap's datasheet C/ESR/ESL from dslib and
  assembles `cin_network` from the model fields.
- **`report.md`** — table + a topology sketch of where CSI sits.
- **`schematic.svg`** (with `--svg`) — a standalone half-bridge drawing of the
  extracted network: each parasitic as a labelled coil, the two common-source
  source-leads in red, and the input-cap bank showing which caps the model ported
  (parallel legs with their current split) vs. board caps left out of the loop.
  The discrete **gate network** (series Rg and any anti-parallel diode, auto-found
  on the gate net) is annotated for context — copper-only FastHenry does not model
  it, so the coil's `R` is the trace resistance (`Cu`), distinct from the gate Rg.

## Layout

`extract_parasitics.py` at the repo root is the extraction CLI. It runs the
two-interpreter pipeline: the geometry step (`lib/kicad_geom.py`) under KiCad's
python, the solve/reduce/emit under system python. `visualize_paths.py` is the
standalone HTML path-viewer exporter.

```
extract_parasitics.py   # CLI entry point (orchestrates the two interpreters)
visualize_paths.py      # -> standalone HTML PCB path viewer
lib/
  kicad_geom.py         # pcbnew -> multiport FastHenry .inp (KiCad python)
  fet_discovery.py      # auto-ID FETs / Vin / gate nets / Cin / gate network
  solve_reduce.py       # run fasthenry, parse Zc.mat -> named parasitics
  emit.py               # -> parasitics.lib / .json / report.md
  emit_svg.py           # -> schematic.svg
test/                   # unit tests (stdlib + numpy)
docs/                   # rendered example(s)
```

Each `lib/` module also has a small `__main__` for standalone/debug use (e.g.
`python3 lib/emit_svg.py parasitics.json > schematic.svg`).

## Requirements & config

- **KiCad** (pcbnew) — the geometry step runs under KiCad's bundled python. Path
  via `$KICAD_PY` (default: the macOS KiCad.app bundled interpreter).
- **FastHenry** — path via `$FASTHENRY` (default `~/dev/vendor/FastHenry2/bin/fasthenry`).
  Build the FastFieldSolvers fork; on a modern clang toolchain use e.g.
  `CFLAGS='-O -DFOUR -m64 -std=gnu89 -fcommon -Wno-implicit-function-declaration
  -Wno-implicit-int -Wno-return-type -Wno-deprecated-non-prototype' make`.
- Python 3 with `numpy` for the solve/reduce step; `PyYAML` is needed when using
  `--config`.

## Paralleled switches

`--hs-ref`/`--ls-ref` accept several refdes (e.g. `--hs-ref Q1 Q3`). By default
(`--parallel-fets lumped`) each paralleled FET contributes its own drain and
source lead stubs; the dies are tied to one node, so between the rails you get
the drain leads in parallel and the source leads in parallel, and **FastHenry
solves the real current split** — the lower-inductance (shorter) device carries
more current, so `L_loop` is the impedance-weighted parallel value, *not* a
naïve `L/2` and *not* the shorter-path-only value. Caveats:

- **Ideal channel.** Tying the dies models `Rds_on = 0`, so the split is set by
  copper/lead impedance alone. That is the dominant term at the commutation edge
  (good for SW-peak/di-dt) but ignores the `Rds_on` that equalises the split at
  low frequency — `L_loop` is the HF, channel-ideal value.
- **CSI is the parallel combination in lumped mode.** The reported `CSI_hs` is
  the paralleled source leads, but each device's gate-return current flows
  through *its own* source lead, so the CSI a single gate driver feels is
  **higher** than reported. In lumped mode only the first FET's gate loop is
  ported, for compatibility with old outputs.
- **Per-device mode is opt-in.** With `--parallel-fets per-device`, parallel
  dies are no longer `.equiv`'d together and each physical FET gets separate
  gate + switch-side ports. `parasitics.json.parallel_devices` carries per-ref
  `L_gate`, `R_gate`, `csi`, `csi_loop`, `L_switch`, and `r_switch`; the side-level
  `L_gate_hs`/`csi_hs` scalars remain as max-per-device compatibility values
  for older consumers. The loss deck consumes the per-ref gate/CSI values directly
  and treats `L_switch` as total per-device switch-path self-L. Because per-device
  CSI is already placed as `Lscs`, the loss deck derives a non-negative drain-side
  residual from `L_switch - csi` and adds only the excess over the lowest residual
  on that side. This preserves total switch-path imbalance without adding the
  source contribution twice.

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
- Solve time is set by mesh density (FastHenry's iterative solver, **single-threaded**
  with a super-linear solve): a ~2 mm pour pitch on a small 2-layer board is a few
  thousand filaments and solves in minutes; drop to `--pitch 3` for a quick look,
  `--pitch 1` (slower) when you need accuracy. **Cost scales steeply with pitch and
  layer count** — each pitch halving is ~4× the pour filaments *per layer*, so
  `--pitch 1` on a 4-layer board (e.g. mppt-2420-hc) is a 10+ minute run. Shrink the
  meshed ROI with a smaller `--margin`, or run a coarse pitch / single pitch, when it
  drags.

## Tests

Stdlib + numpy unit tests for the pure layers (no KiCad/FastHenry) live in
`test/`:

```sh
python3 test/test_extract_config.py  # --config YAML parsing/defaults/overrides
python3 test/test_parasitics.py   # SVG formatting/rendering, reduction, weld
python3 test/test_reduce.py       # --cin-parallel reduction vs closed-form
```

`test_parasitics.py` covers SVG formatting/rendering and the `weld`
de-fragmentation; `test_reduce.py` checks the parallel-cap reduction against
hand-derived answers (shared-path law, coupled 2-port, CSI degeneracy, polarity
and ill-conditioning warnings). The pcbnew/FastHenry geometry+solve path is
covered by running on the real boards below.

## Validation

- `mppt-2420-hc` (4-layer buck; SW `/DC/DC/SW_NODE`, HS `Q1`, LS `Q2`): loop L
  ≈ 8.5 nH, CSI_hs ≈ 0.71 nH, CSI_ls ≈ 1.36 nH, gate Rg `R1/R2 = 3R3` at 2 mm.
- `Fugu2` (2-layer buck, paralleled HS `Q1∥Q3`, LS `Q2`, 6-cap HF bank): loop L
  ≈ 8.2 nH (6 caps ‖), CSI_hs ≈ 0.43 nH, CSI_ls ≈ 1.25 nH, gate Rg `R4/R10 = 4.7`
  — exercises paralleled FETs, the weld pass, and multi-cap reduction.

## Copper power-loss density (`density.py`)

`density.py` renders a **per-copper-layer W/mm² heatmap** from the extracted mesh and a
set of injected port currents. It solves the mesh as a **DC resistive network**
(`R_seg = length/(σ·w·h)`, `.equiv` = short), so it recovers how the current *spreads*
through the pours — a lumped R can't be placed spatially. It is **loss-agnostic**: the
currents (and optional per-phase `norm_W` totals) are just inputs, so it is fully
unit-testable without KiCad/FastHenry/SPICE (see `test/test_density.py`). The extractor
now persists the finest-pitch mesh at `<out>/mesh/model.inp` for this and other consumers.

    density.py <out>/mesh/model.inp --currents SPEC.json --copper <out>/mesh/copper.json -o density/

Pour layers render as a smooth **node-binned field** (`--style field`, default) — the raw
per-filament mesh (`--style filaments`) shows a directional checkerboard, so the field
averages the edges at each node and alpha-ramps with density so hotspots glow over the
faint real-PCB **board overlay** (`--copper`, the same `copper.json` the mesh viewer uses;
persisted by the extractor at `<out>/mesh/copper.json`).

`SPEC.json` names phases: a 2-terminal conduction phase (`{"port":"P_hs","i_rms":..}`) or
a Cin-ripple phase (`{"tap":"P_pwr","cap_currents":{"C18":..}}`, currents summing into the
Vin/GND trunk). `norm_W` rescales a phase so its Σ matches a reference bucket. The loss
tool's `loss/loss_density.py` builds the spec from a real run's SPICE `.raw` and calls this.
Caveat: the DC solve sets the spatial *shape* only — no skin/proximity — so magnitudes
should come from the loss run (`norm_W`); it is a conduction-density map, not an HF ring map.
