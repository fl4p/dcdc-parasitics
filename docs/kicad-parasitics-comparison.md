# KiCad-Parasitics / KiPEX / gerber2ems Comparison

Date: 2026-07-07

Compared local `dcdc-tools/parasitics` against
`https://github.com/Steffen-W/KiCad-Parasitics` at upstream commit `d493a93`
(`2026-07-05 fix(release): correct plugin metadata and harden ZIP packaging
script`).

Also compared adjacent tooling:

- `https://github.com/tobiglaser/KiPEX`, via a temporary local headless adapter
  for FastHenry cross-checks.
- `https://github.com/antmicro/gerber2ems` at upstream `main`
  `9eaf3033f8adb0b468045f7177523162b388b020`, plus
  `https://github.com/antmicro/kicad-si-simulation-wrapper` at
  `1caf8fb15f5b73bed6c2d916fc5efcca6ab6f76b`.

## Bottom Line

The projects solve different problems.

`KiCad-Parasitics` is a general KiCad PCB-editor plugin. The normal workflow is:
select two same-net pads or vias, build a copper graph for that net, estimate
DC resistance and optional AC impedance, then present the result in a GUI.

`dcdc-tools/parasitics` is a topology-specific half-bridge extractor. The normal
workflow is: provide a KiCad board plus SW/GND nets, discover or override the
power-stage topology, build a multiport FastHenry model, reduce the full port
impedance matrix into named parasitics, and emit SPICE/JSON/report artifacts for
switching-loss and gate-drive simulations.

So `KiCad-Parasitics` is stronger as a plugin/product reference. Our extractor is
the better fit for Fugu2-style SMPS commutation-loop, CSI, and loss-model work.

This note also folds in the separate KiPEX and Antmicro flow comparisons. KiPEX
is a closer technical comparator for our purposes because it emits
FastHenry-style polygon meshes rather than a shortest-path same-net impedance
graph. `gerber2ems` is less direct, but useful as an openEMS/FDTD validation and
workflow reference.

## KiPEX Cross-Check Summary

KiPEX was used as an independent FastHenry sanity check on two boards:

- Authoritative Fugu2:
  `/Users/fab/dev/ee/hw/Fugu2/Fugu2.kicad_pcb`
- simple-hb:
  `/Users/fab/dev/ee/hw/simple-hb/simple-hb.kicad_pcb`

The temp headless adapter lived under `/private/tmp/kipex-run/`. It adapted
KiPEX's core translator logic to local `pcbnew` because the normal KiPEX `kipy`
IPC path was not available from Codex.

Results:

| Board / setup | Result |
| --- | ---: |
| Fugu2 KiPEX three-leg board-copper loop at 3.9 MHz | 4.51 nH |
| Fugu2 dcdc lead-inclusive historical default, `lead_mm=3` | 8.21 nH |
| Fugu2 dcdc copper-only pre issue-#6 reference, `lead_mm=0.1` | 4.13 nH |
| Fugu2 current dcdc point-injection, post issue-#6 pour-track-skip | ~3.14 nH |
| Fugu2 current dcdc pad-land/grid | ~3.24 nH |
| simple-hb KiPEX finer run | 6.64 nH |
| simple-hb PyPEEC hand-sheet cross-check | 6.90 nH |
| simple-hb dcdc legacy grid + point/stitch injection | 9.08 nH |
| simple-hb dcdc default grid + pad-land injection | 6.86 nH |
| simple-hb dcdc experimental polygon + pad-land injection | 5.82 nH |

Conclusions:

- Fugu2's original `8.21 nH` discrepancy was not pad-point injection. It was
  copper plus FET die-plane risers, and later deck ringing was a package-lead
  double count. Current pad-land injection changes Fugu2 only about `0.1 nH`
  relative to the current post issue-#6 extractor baseline.
- simple-hb was a real pad/current-injection problem. Its deliberately wide
  one-layer sheets made the old pad-center-to-one-grid-node stitch create a
  nonphysical current funnel. Replacing that with physical pad-land terminal
  distribution moves dcdc from `9.08 nH` to `6.86 nH`, matching KiPEX/PyPEEC.
- Experimental dcdc polygon mode currently under-reads. It is useful for
  cross-checks, not a production replacement for the default grid mesher.

## KiPEX vs dcdc Implementation Difference

KiPEX:

- Converts each filled polygon to Shapely geometry.
- Builds an adaptive quad tree over the polygon bounds.
- Splits intersecting cells down to a configured lower size.
- Emits FastHenry elements along inside sides of quad leaves.
- Uses a single `.external` mesh node inside the pad polygon, chosen near the
  pad center. It does not distribute the terminal over the whole pad.

dcdc, before the pad-land fix:

- Built a rectangular center-point grid over each filled-polygon bounding box.
- Kept grid points whose centers were inside the filled polygon.
- Connected 4-neighbours with filaments of width `pitch`.
- Created one detached pad-center node per pad/net/layer.
- Later stitched that pad node to the nearest same-net zone-grid node.

That old dcdc terminal path looked like:

```text
external port -> pad center point -> one stitch segment -> one pour mesh node
```

On wide sheets this forces all current through a point-like constriction and
overestimates loop inductance.

The fixed default path is:

```text
external port -> pad terminal -> same-net mesh nodes inside the real pad polygon
```

The terminal region is limited to the actual pad/via/device land, not the whole
pour edge.

## Current dcdc Modes

The current extractor has two runtime mesh modes:

- `--zone-mesh grid`: validated/default pour mesher.
- `--zone-mesh polygon`: guarded experimental cell-edge polygon mesher.

And four terminal modes for experiments:

- `--terminal-mode padland`: validated/default terminal model; distributes over
  same-net mesh nodes inside the real KiCad pad polygon.
- `--terminal-mode single`: KiPEX-like terminal model; chooses one mesh node
  inside the pad polygon near the pad center.
- `--terminal-mode finite`: experimental finite pad/contact model; connects the
  pad center to pad-land mesh nodes with finite copper spokes instead of
  zero-ohm `.equiv`.
- `--terminal-mode point`: legacy/debug model; uses the old pad-center node plus
  nearest-zone stitch so old over-read results can be reproduced without
  checking out old code.

Production default remains:

```yaml
zone_mesh: grid
terminal_mode: padland
```

Polygon-mode terminal comparisons should use:

```yaml
zone_mesh: polygon
terminal_mode: single   # KiPEX-like
```

```yaml
zone_mesh: polygon
terminal_mode: padland  # current distributed zero-ohm pad-land bound
```

```yaml
zone_mesh: polygon
terminal_mode: finite   # finite pad/contact experiment
```

```yaml
zone_mesh: polygon
terminal_mode: point    # legacy/debug point-stitch comparison
```

## gerber2ems / si-wrapper Comparison

`gerber2ems` is an openEMS/FDTD signal-integrity flow that consumes fabrication
outputs: Gerbers, Excellon drill files, stackup JSON, pick-and-place positions,
and a `simulation.json` file describing ports, frequency range, mesh, and traces.
It generates an openEMS/CSXCAD geometry, runs wideband port simulations, and
post-processes S-parameters and impedance plots.

`kicad-si-simulation-wrapper` is the companion workflow tool. It creates board
slices, places simulation-port footprints, includes neighboring nets, and emits
configs for the `gerber2ems` flow.

Useful ideas for dcdc-tools:

- Adaptive grid and ROI policy. Antmicro's 2025 flow densifies the grid around
  relevant copper and sparsifies the rest, which is a useful reference for our
  pour-meshing and ROI heuristics.
- Slice-generation ergonomics. The wrapper's automatic trace/neighbor selection
  and simulation-port placement are relevant to "extract only the power-stage
  neighborhood" UX.
- Visualization and validation. openEMS field dumps, impedance plots,
  S-parameters, and VNA comparison are useful independent HF sanity checks if
  FastHenry/LTspice and bench data disagree.
- Gerber/net parsing fallback. Its grid-generation path parses Gerber
  net/vector information, which could be useful if a fabrication-output fallback
  is ever needed.

Limits for dcdc-tools:

- It is an FDTD signal-integrity simulator, not a quasi-static multiport R/L
  extractor.
- Its openEMS copper geometry path rasterizes Gerbers to PNG and triangulates
  the image into polygons. That is not a good source of truth for low-nH
  commutation-loop extraction compared with KiCad filled polygons or a PEEC mesh.
- Copper is modeled as ideal metal in openEMS (`AddMetal`), so the flow does not
  emit the HF/LF copper resistance, partial-inductance matrix, or branch/trunk
  decomposition needed by the loss deck.
- Ports are microstrip/lumped SI ports, not the FastHenry multiport matrix used
  for commutation-loop `L_loop`, common-source-inductance mutuals, and Cin
  common-voltage reduction.
- The `gerber2ems` README explicitly says capacitors are not simulated; for
  high-frequency SI they are approximated by shorting them. That conflicts with
  the dcdc-tools Cin branch/trunk model, where physical MLCC ESL/ESR and per-cap
  branch copper are load-bearing.

Verdict: do not integrate `gerber2ems` as an extraction backend. Mine it for
adaptive grid, ROI/slicing, port-placement UX, and openEMS/VNA validation ideas.
For extraction cross-checks, KiPEX/PyPEEC remain closer matches to the
power-loop problem.

## Capability Matrix

| Area | KiCad-Parasitics | dcdc-tools/parasitics |
| --- | --- | --- |
| User surface | KiCad action plugin with GUI; pcbnew and KiCad 9 IPC paths | CLI/YAML batch extractor |
| Input selection | Two selected same-net pads/vias | SW net, GND net, optional topology overrides |
| Main solver | ngspice network of analytical R/L/C elements; optional bfieldtools path inductance | FastHenry PEEC multiport solve |
| Primary geometry | Tracks/vias/pads/zones normalized into a graph | Tracks, pours, vias, pads, FET lead stubs emitted as FastHenry conductors |
| Pours/zones | Low-resistance graph links between touched nodes | Meshed copper sheets clipped to filled polygons and ROI |
| Inductance model | Shortest path only; no mutual coupling or plane influence per upstream README | Full port matrix with mutual inductance, CSI, cap-bank coupling |
| Power-stage topology | Not modeled | HS/LS FETs, gates, Vin, Cin, Kelvin/source return |
| Parallel input caps | Not modeled as coupled ports | Exact matrix reduction `1 / (1^T Zc^-1 1)` plus current split |
| Output | GUI report/details | `parasitics.lib`, `parasitics.json`, `report.md`, mesh/SVG viewers |
| Tests found | No local test directory in the inspected clone | Pure-Python tests for reduction/config/SVG/model helpers |

## Upstream Strengths

- Product integration is much more complete: KiCad plugin metadata, GUI dialog,
  classic `pcbnew` entry, and KiCad 9 IPC/kipy support.
- It has a useful normalized board-element model: `WIRE`, `VIA`, `PAD`, `ZONE`,
  `NetworkElement`, and `CuLayer`.
- It includes substantial stackup and transmission-line formula work in
  `plugins/impedance.py`, including microstrip, stripline, coplanar, and via
  approximations.
- Its ngspice wrapper writes explicit measurement netlists and has both DC and
  AC impedance paths.
- The optional bfieldtools inductance path is a convenient interactive sanity
  checker for simple trace paths.

## Upstream Limits For Our Use Case

- The same-net, two-terminal workflow does not match a synchronous-buck
  commutation loop across Vin, SW, and GND.
- Zones are not emitted as physical sheet conductors. In the inspected code,
  zone-connected nodes are tied with a fixed `1 mOhm` resistor, which is useful
  for graph connectivity but not for current spreading or loop inductance.
- Pads are treated mainly as connection points rather than physical copper
  terminal regions.
- The inductance calculator follows the shortest path and explicitly ignores
  parallel routes, adjacent coupling, and plane influence.
- The solver path is not a FastHenry/PEEC backend, so it does not produce a
  partial-inductance matrix suitable for CSI, parallel Cin reduction, or SPICE
  parasitic subcircuit emission.
- License is GPL-3.0. Direct code copying into differently licensed tooling
  needs a deliberate licensing decision.

## Local Strengths

- `lib/kicad_geom.py` builds one multiport FastHenry model with commutation,
  gate-loop, conduction, and optional full-Cin-bank ports.
- `lib/fet_discovery.py` discovers HS/LS FETs, gate nets, Vin, Cin, and default
  gate-return assumptions by connectivity, with CLI overrides.
- `lib/solve_reduce.py` reduces the full complex port matrix into physical
  quantities used downstream:
  - commutation-loop L and HF ring R;
  - HS/LS gate-loop L;
  - side-specific common-source inductance;
  - LF conduction R split into HS/LS/SW residual;
  - effective parallel-Cin loop impedance including mutuals;
  - optional full input-cap branch/trunk network for the loss tool.
- The extractor keeps separate HF ring and LF conduction bases. This matters
  because MLCCs source the fast edge while bulk capacitors source the switching
  fundamental.
- The emitted `.SUBCKT` and JSON are designed for downstream switching/loss
  simulations, not just an interactive report.
- Local regression coverage exists for the reduction math. On this comparison
  pass, `python3 dcdc-tools/parasitics/test/test_reduce.py` passed all cases.

## Practical Reuse Guidance

Use `KiCad-Parasitics` as a reference for:

- KiCad plugin packaging and GUI integration;
- KiCad IPC support;
- normalized element/stackup schemas;
- simple same-net path sanity checks;
- analytical trace/via impedance formulas.

Use `gerber2ems` / `si-wrapper` as a reference for:

- adaptive grid and ROI selection;
- board slicing and simulation-port placement UX;
- openEMS field/impedance/S-parameter visualization;
- optional HF validation against VNA or bench data.

Do not use `KiCad-Parasitics`' parasitic solver path as the backend for
Fugu2-style power-loop extraction. The local FastHenry path should remain the
core backend because the critical quantities are geometry-dependent mutuals and
multiport reductions, not shortest-path same-net impedance.

Do not use `gerber2ems` as the backend either. It targets SI/FDTD behavior and
does not produce the copper R/L ownership, Cin branch/trunk network, CSI mutuals,
or SPICE-ready parasitic subcircuit that the loss tools consume.

## Follow-Up Ideas

- If a GUI is needed, wrap the local extractor behind a KiCad action plugin using
  upstream's plugin/IPC structure as an architecture reference.
- Consider importing only concepts, not code, from upstream unless the local tool
  is explicitly made GPL-compatible.
- Keep `KiCad-Parasitics` in the validation toolbox for simple trace/path
  sanity checks, but compare it against local FastHenry only on intentionally
  simple same-net fixtures.
- Keep `gerber2ems` as a later openEMS validation route only when a full-wave or
  S-parameter view is useful enough to justify the setup cost.
