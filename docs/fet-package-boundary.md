# FET Package Geometry and SPICE-Model Boundary

This note is the canonical contract between `dcdc-tools/parasitics`,
`dcdc-tools/loss`, and MOSFET SPICE models for FET package inductance.

## Current extractor behavior

The legacy extractor option `--lead-mm` is not a real TO-220 or TO-247
package model. It creates an artificial die-plane extension:

```text
PCB pad node -> straight FastHenry segment -> die-plane node at z=+lead_mm
```

For each FET, `kicad_geom.py` creates drain/source/gate nodes at the same
`x,y` as the KiCad pads and at `z = lead_mm`, then shorts drain to source at
that die plane. The emitted FastHenry geometry is ordinary rectangular
segments:

```text
drain/source riser: width 1.0 mm, height cu_thickness
gate riser:         width 0.5 mm, height cu_thickness
```

With the default copper thickness this means a `0.035 mm` thick vertical
ribbon, not the bent package lead metal on a horizontal through-hole MOSFET.

`lead_mm > 0.5` should therefore be treated as **lead/package-inclusive
legacy extraction**: `L_loop`, `csi_*`, and `L_gate_*` already contain FET
die-plane risers. A small value around `0.1 mm` is the current copper-only
stand-in because literal zero-length FastHenry segments are numerically
fragile.

## SPICE model boundary

A MOSFET subcircuit with lead inductors usually anchors them between the
external package pins and the internal die nodes:

```spice
.SUBCKT IPP024N08NF2S_L0 drain gate source
Ld drain  d1 2.5n
Ls source s1 1.8n
Lg gate   g1 4n
```

This does not describe how the device is mounted on the PCB. It is a lumped
external-pin-to-die path. It normally covers package pin/lead effects
electrically, plus internal leadframe/bond contributions, but it has no
coordinates, bend shape, pad placement, or board-thickness knowledge.

`loss/lib/model_resolver.py` marks such models as `leads_internal=True` when it
detects `Ld`/`Ls`/`Lg`-style inductors on the subcircuit pins.

## Valid combinations

Use one source of package lead inductance, not two:

| Parasitics extraction | MOSFET model | Status |
| --- | --- | --- |
| PCB copper only | vendor model with `leads_internal=True` | Valid default |
| PCB copper only | die-only / lead-free model | Valid if the deck adds package leads |
| Package-inclusive FastHenry geometry | die-only / lead-disabled model | Valid high-fidelity path |
| Package-inclusive FastHenry geometry | vendor model with `leads_internal=True` | Invalid: double-counts package leads |

The invalid combination double-counts the package pin-to-die path. FastHenry has
already put lead/riser inductance into the extracted `L_loop` / `csi_*` /
`L_gate_*`, and the vendor model then adds its own `Ld` / `Ls` / `Lg`.

## Loss-tool rule

The loss workflow should consume **PCB copper-only** parasitics. The FET package
inductance is supplied by either:

- the selected vendor MOSFET model, when `leads_internal=True`; or
- the loss deck's package-lead table / overrides, when the model is lead-free.

Do not feed a lead-inclusive extraction into the loss deck with a lead-internal
vendor model. The deck fails when `meta.lead_mm > 0.5` and any side uses
`leads_internal=True`, because it cannot subtract inductors inside a vendor
SPICE subcircuit. For lead-free models, it can suppress external drain leads
that the deck itself would add, but copper-only re-extraction is still the exact
loss-flow input.

The opposite boundary is also checked. If `meta.lead_mm` is missing or
`<= 0.5` and a side uses a lead-free model, the deck warns that it currently adds
only the package drain lead (`Ld`) from its package table. Source/gate package
inductance must then already be in the extracted `csi_*` / `L_gate_*` values or
the MOSFET model; otherwise a true copper-only extraction plus a die-only model
under-models source/gate package L.

## Package geometry roadmap

`--lead-mm` should be replaced by an explicit `fet_package_geometry` contract:

```yaml
fet_package_geometry:
  mode: copper_only
  copper_only:
    die_plane_epsilon_mm: 0.1
```

Package-inclusive extraction should be opt-in and use reusable preset files:

```yaml
fet_package_geometry:
  mode: preset
  preset_paths:
    - fet-package-geometry/to220_horizontal_front_down.yaml
  sides:
    hs:
      refs: [Q1, Q3]
      preset: to220_horizontal_front_down
    ls:
      refs: [Q2]
      preset: to220_horizontal_front_down
```

Preset files should provide physical lead dimensions and mounting assumptions,
while KiCad pad positions infer the actual mounted lead lengths. A TO-220
horizontal preset should emit L-shaped rectangular lead metal, for example
using `0.8 mm x 0.5 mm` as the default drain/source cross-section with a gate
override, not PCB-copper-thickness vertical risers.

If `fet_package_geometry.included_in_extraction == true`, downstream consumers
must require a die-only/lead-disabled MOSFET model or reject the combination.

The tracking issue for this replacement is:
<https://github.com/fl4p/dcdc-tools/issues/10>.
