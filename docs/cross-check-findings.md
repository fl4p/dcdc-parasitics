# Parasitics Cross-Check Findings

Date: 2026-07-07

This note captures the Fugu2 and simple-hb cross-checks against KiPEX, PyPEEC,
and synthetic FastHenry decks. The main lesson is that the two boards failed for
different reasons and should not be mixed together.

## Executive Summary

Fugu2:

- KiPEX board-copper loop: ~4.5 nH
- dcdc copper-only loop: ~4.1 nH
- dcdc default loop: 8.21 nH
- conclusion: dcdc and KiPEX agree on copper; the 8.21 nH value included
  die-plane/package-lead risers and caused a lead double-count in the loss deck.

simple-hb:

- KiPEX polygon mesh: ~6.6 nH
- PyPEEC hand-sheet model: 6.90 nH
- hand FastHenry generous-sheet model: 5.12 nH
- dcdc copper-only rectangular-zone mesh: 9.08 nH
- dcdc lead-inclusive run: 14.59 nH
- conclusion: the 9.08 nH dcdc value is already copper-only, so simple-hb has a
  real dcdc copper terminal/meshing over-extraction.

## Fugu2 Result

The authoritative board is:

`/Users/fab/dev/ee/hw/Fugu2/Fugu2.kicad_pcb`

KiPEX was run through a temporary headless adapter under `/private/tmp/kipex-run`
because the normal KiPEX IPC path was not available. The generic same-net
three-leg extraction used:

- `P_vin`: C18.1 -> Q1.2 (`Solar+`)
- `P_sw`: Q1.3 -> Q2.2 (`SW`)
- `P_gnd`: Q2.3 -> C18.2 (`BuckGND`)

KiPEX result at 3.9 MHz:

- `L = 4.51 nH`
- `R = 3.35 mOhm`

dcdc lead sweep:

- `lead_mm=0.1`: `L_loop = 4.13 nH`
- `lead_mm=3.0`: `L_loop = 8.21 nH`
- historical stale `lead_mm=6.0`: `L_loop = 11.70 nH`

Interpretation: KiPEX and dcdc agree on board copper at ~4-4.5 nH. The dcdc
8.21 nH value included FET die-plane risers. The switching loss deck then paired
that lead-inclusive extraction with package/table leads or Infineon `_L0`
models that already carry internal lead inductance, producing a lead
double-count. This is not evidence of a 2x pad-point loop-L inflation on Fugu2.

Current spreading and pad-point injection remain relevant for branch resistance
and possibly branch/trunk decomposition, but they are not the root cause of the
Fugu2 4.5 nH vs 8.21 nH discrepancy.

## simple-hb Result

The board is:

`/Users/fab/dev/ee/hw/simple-hb/simple-hb.kicad_pcb`

Topology:

- Vin `VBUS`, SW `/SW`, GND `GND`
- HS `Q1` TO-220: drain `VBUS`, source `/SW`
- LS `Q2` TO-220: drain `/SW`, source `GND`
- input caps `C1` MLCC and `C2` bulk electrolytic

KiPEX-style three-leg extraction used:

- `P_vin`: C1.1 -> Q1.2
- `P_sw`: Q1.3 -> Q2.2
- `P_gnd`: Q2.3 -> C1.2

KiPEX results:

- coarse quad limits 3.0/1.0: `L = 6.55 nH`, `R = 4.59 mOhm`
- finer quad limits 1.0/0.5: `L = 6.64 nH`, `R = 5.67 mOhm`

dcdc results:

- pitch 1.0, `lead_mm=0.001`: `L_loop = 10.22 nH`
- pitch 0.7, `lead_mm=0.001`: `L_loop = 8.98 nH`
- pitch 0.5, `lead_mm=0.001`: `L_loop = 9.08 nH`
- pitch 1.0, `lead_mm=0.1`: `L_loop = 10.24 nH`
- pitch 1.0, `lead_mm=3.0`: `L_loop = 14.59 nH`

The compared dcdc value, 9.08 nH, is effectively copper-only:
`meta.lead_mm=0.001`. The 0.001 mm to 0.1 mm lead delta at pitch 1.0 is only
~0.024 nH. Therefore simple-hb is not the Fugu2 riser issue repeated.

## Ruled-Out Explanations

The simple-hb gap is not explained by:

- KiPEX three-port reduction: a one-port KiPEX deck with Q1/Q2 D-S `.equiv`
  still gave 6.64 nH.
- dcdc extra ports: a dcdc pwr-only deck still gave 9.08 nH.
- exact C1 pad-center location: moving the dcdc external to stitched zone nodes
  still gave 9.08 nH.
- ROI clipping: increasing dcdc margin to 30 still gave ~9.20 nH.
- weld tolerance: `weld_tol=0.05` reduced segment count but still gave ~9.20 nH.
- dangling TO-220 barrel stubs: removing them still gave 9.08 nH.
- duplicate coincident dcdc segments: deduping reduced 10529 elements to 6958
  but gave 9.15 nH.

Duplicate coincident segments are still a cleanup and performance issue, but not
the main simple-hb loop-L gap.

## Independent Solver Checks

Hand FastHenry sheet model:

- path: `dcdc-tools/parasitics/out-simple-hb-hand-fh/`
- geometry: explicit top-layer VBUS/SW/GND rectangular sheets
- Q1/Q2 D-S modeled as ideal `.equiv`
- one external port across C1
- 0.5 mm mesh, 4464 nodes, 8688 segments
- at 3.9 MHz: `Z = 0.00878421 + j0.125424 ohm`
- `R = 8.784 mOhm`, `L = 5.118 nH`

This is deliberately generous versus the real board polygons, so it should land
below KiPEX. It is a lower sanity anchor, not a final answer.

PyPEEC hand-sheet model:

- path: `dcdc-tools/parasitics/out-simple-hb-pypeec-hand/`
- solver: PyPEEC 5.8.0
- geometry: hand-authored VBUS/SW/GND rectangles
- Q1/Q2 D-S represented as wide copper bridge patches because PyPEEC has no
  FastHenry `.equiv`
- 0.5 mm x 0.5 mm x 35 um voxels
- 3693 used voxels, 7177 electric faces
- at 3.9 MHz: `Z = 0.007405768346179019 + j0.16909137351069287 ohm`
- `R = 7.406 mOhm`, `L = 6.900 nH`

PyPEEC lands on KiPEX and far below dcdc's copper-only 9.08 nH. This strongly
supports simple-hb being a dcdc terminal/meshing issue.

## Synthetic Width Sweep

Path:

`dcdc-tools/parasitics/out-simple-hb-width-sweep/`

Synthetic geometry:

- one-layer U-shaped FastHenry loop
- fixed outer size: 25 mm x 30 mm
- copper thickness: 35 um
- mesh pitch: 1.0 mm
- frequency: 3.9 MHz

Terminal treatments:

- `point`: one `.external` node at each terminal center
- `distributed`: all nodes along the corresponding sheet edge `.equiv`'d to
  the terminal node

Result:

| width mm | point L nH | distributed L nH | point penalty nH | penalty |
|---:|---:|---:|---:|---:|
| 2 | 46.010 | 45.772 | 0.238 | 0.5% |
| 3 | 39.841 | 39.485 | 0.357 | 0.9% |
| 4 | 34.930 | 34.453 | 0.477 | 1.4% |
| 6 | 27.306 | 26.551 | 0.756 | 2.8% |
| 8 | 21.423 | 20.343 | 1.080 | 5.3% |
| 10 | 16.590 | 15.109 | 1.481 | 9.8% |
| 12 | 12.454 | 10.435 | 2.019 | 19.3% |
| 14 | 8.639 | 5.661 | 2.978 | 52.6% |

The monotonic width dependence is the smoking gun: point-injection constriction
is negligible on narrow conductors and becomes nanohenry-scale on wide sheets.
This explains why Fugu2 can agree between KiPEX and dcdc while simple-hb does
not.

## Correct Fix Direction

Do not fix this by shorting a whole pour edge. That is the opposite extreme and
can under-read because it creates a zero-impedance bus across the entire sheet
edge.

The physical terminal is neither a point nor a full-edge bus. It is the actual
pad or via land that injects current into the copper. The extractor should
create terminal regions sized to the real contact geometry:

- MLCC cap pad land or pad span
- MOSFET/source/drain pad or through-hole annular land
- via land when a via is the terminal

Then it should couple the external terminal to mesh nodes in that physical
region. The expected result is bracketed between the current point terminal
overestimate and the full-edge distributed underestimate.

Validation target:

- simple-hb C1/Q1/Q2 should move from dcdc copper-only ~9.08 nH toward
  KiPEX/PyPEEC ~6.6-6.9 nH.
- Fugu2 copper-only should stay near ~4.1-4.5 nH, because its local pours are
  narrow enough that point-vs-pad distribution is a small effect.

Implemented first slice:

- `lib/kicad_geom.py` now uses the real KiCad pad polygon (`GetEffectivePolygon`)
  to find same-net pour mesh nodes inside each pad land. When such nodes exist,
  the pad terminal node is `.equiv`ed to those physical-contact mesh nodes and
  excluded from the old nearest-zone stitch. If no mesh node falls inside the pad
  at the current pitch, it falls back to the historical pad-center node and
  stitch behavior.
- `extract_parasitics.py` carries these pad-land terminal diagnostics into
  `parasitics.json` under `meta.terminal_regions`.
- Review follow-up: `parasitics.json` now also carries
  `meta.terminal_fallbacks`. Fallbacks caused by `polygon_unavailable` or
  `no_mesh_node_inside_pad` emit geometry-step warnings because they can silently
  reintroduce point injection. Expected pads with no same-net zone mesh, such as
  gate pads or unused bottom-side THT lands on a one-layer pour, are recorded as
  `no_same_net_zone_mesh`.
- Clean-HEAD simple-hb baseline at pitch 0.5 mm, `lead_mm=0.001`, C1 only:
  `L_loop = 9.08 nH`.
- Pad-land terminal simple-hb run at the same settings:
  `L_loop = 6.86 nH`, `R_loop = 5.58 mOhm`, with 8 terminal regions and 141
  total zone-node contacts. This lands in the KiPEX/PyPEEC band.
- Historical Fugu2 copper-only guard with C18/C17/C9/C16/C21/C22 was
  `L_loop = 4.13 nH`, but that reference is pre issue-#6 pour-track-skip.
  Current post-issue-#6 point-injection extraction is ~`3.14 nH`.
- Pad-land terminal Fugu2 six-cap guard at the same settings:
  `L_loop = 3.24 nH`, with 27 terminal regions and 93 total contacts in
  `out-fugu2-lead0-grid-default/`. Relative to the current post-issue-#6
  baseline, pad-land moves Fugu2 by only ~0.1 nH, matching the narrow-pour
  prediction and leaving the lead-riser/double-count conclusion unaffected.
  The `cin_L_shared <= L_loop` model clamp has landed, so current Fugu2
  extractions no longer force a negative switch-side residual in the loss deck.
  The committed loss fixture has been refreshed to this 6-cap pad-land/grid
  extraction (`L_loop = 3.24 nH`, `cin_L_shared = 2.96 nH`,
  `L_loop_switch = 0.278 nH`). With datasheet-curve Coss it rings at
  `62.5 MHz`; the old pre issue-#6/pre-padland `4.13 nH` fixture happened to
  ring at `60.0 MHz` and is no longer the current extractor output.

Implemented second slice as guarded/experimental:

- There are three useful comparison states, but only two current CLI mesh modes.
  The legacy grid + pad-center point/stitch path is the clean-HEAD evidence path
  for the `9.08 nH` over-read. It is now also available explicitly as
  `--terminal-mode point` for legacy/debug A/B runs, but it is not the production
  default. Current mesh modes are default `--zone-mesh grid` and experimental
  `--zone-mesh polygon`.
- `lib/kicad_geom.py` now has `--zone-mesh grid|polygon`. `grid` remains the
  default because it is the validated pad-land path above. `polygon` is an
  explicit KiPEX-style experiment: it emits polygon-contained cell-edge
  filaments, uses boundary-inclusive containment, and caps polygon vertex cuts
  so complex KiCad fills do not explode into an unusable x/y cross-product.
- The first uncapped polygon attempts generated tens of thousands of simple-hb
  segments at pitch 0.5 mm (79,657 in the interrupted Codex run), so the
  vertex-cut cap is required for practical runtime.
- With the cap, simple-hb polygon mode at pitch 0.5 mm generated 11,225
  segments and solved to `L_loop = 5.82 nH`, below the KiPEX/PyPEEC 6.6-6.9 nH
  band and therefore more like a lower-bound model than an accepted replacement.
- Fugu2 polygon mode at pitch 1.0 mm generated 5,602 segments and solved to
  `L_loop = 2.65 nH`, below the already-shifted pad-land/grid result
  (`3.24 nH`). This confirms polygon mode needs more calibration before it can
  become the production mesher.
- After metadata plumbing, a geometry-only simple-hb polygon check generated
  3,923 nodes / 11,225 segments and recorded three
  `too_many_polygon_vertex_cuts` `zone_mesh_notes` for `/SW`, `VBUS`, and `GND`.
- The likely remaining issue is not just polygon clipping. Combining a richer
  cell-edge mesh with `.equiv` across every mesh node inside each pad land may
  over-idealize the pad/contact metal. KiPEX uses a polygon mesh but still
  chooses one mesh node inside the pad for `.external`; dcdc's physical terminal
  region is intentionally different, so the next review should decide whether
  pad-land `.equiv` needs a finite pad-metal/contact model.

Fugu2 cap-selection nuance from the pad-land rerun:

- The 6-cap set without C27 reproduces `L_loop = 3.24 nH` under pad-land.
- Adding C27 gives the 7-cap pad-land result `L_loop = 2.54 nH`, a `-0.70 nH`
  change. Under point injection, C27 was effectively invisible.
- C27 is physically far from the other input capacitors and sits on the other
  side of the switch-node region. This makes the `-0.70 nH` shift suspect as a
  cap-selection/basis issue, not an obvious "nearest Cin participates" result.
  Pad-land changes cap-selection sensitivity: far caps that the old detached
  pad-center model treated as free/unused can become real low-impedance parallel
  HF returns. The datasheet-Coss ring A/B resolves C27 for the loss fixture:
  6-cap no-C27 rings at `62.5 MHz`, while 7-cap with C27 rings at `65.0 MHz`,
  farther from the ~60 MHz bench. Treat C27's pad-land contribution as
  over-inclusion for the tight commutation ring and keep `cin_refs` on the
  near 6-cap set.

## Static Code Comparison: KiPEX vs dcdc

The code paths explain why KiPEX and dcdc behave differently on simple-hb.

KiPEX:

- `translator.translate_loop()` runs `stackup()`, `zones()`, `traces()`,
  `vias()`, `footprints()`, then `ports()`.
- `zones()` converts each filled polygon to Shapely, builds a `Quad` tree over
  the polygon bounds, splits intersecting cells down to `quad_lower_mm`, and
  emits FastHenry elements on the inside sides of the quad leaves.
- `Quad.to_inside_sides()` emits side elements only when both endpoints are
  inside the polygon, and each element width is derived from local cell/neighbour
  dimensions rather than a single global pitch.
- `ports()` does not distribute the terminal over a whole pad. It finds the pad
  polygon, then chooses a mesh node inside that polygon nearest the pad center
  and uses that one node for `.external`.

dcdc:

- `build()` runs `add_tracks()`, `add_vias()`, `add_zones()`, `build_fet()`,
  `cin_ports()`, `stitch_zones()`, `weld()`, then adds `.external` ports.
- `add_zones()` creates a rectangular grid over the filled-polygon bounding box,
  keeps grid points whose centers satisfy `poly.Contains(...)`, and connects
  4-neighbours with filaments of width `pitch`.
- `_pad_node_stack()` creates exactly one pad node at the pad center for each
  pad/net/layer.
- `stitch_zones()` later bonds that non-zone pad node to the nearest same-net
  zone-grid node with an added filament of width `pitch`, if it is within
  `3*pitch`.
- Ports are then attached to the original pad-center nodes.

So the static difference is not simply "KiPEX distributes the port and dcdc does
not." KiPEX also uses a single port node, but that node is a node of its
polygon-following quad mesh inside the pad. dcdc uses a separate pad-center node
and then inserts a pitch-wide stitch filament into a center-point rectangular
zone grid. On wide sheets this artificial pad-center-to-grid stitch and the
center-point grid current spreading are plausible sources of the observed
nanohenry-scale over-extraction.

The fix should therefore address both halves of the dcdc path:

- create terminal nodes from the actual pad/via land region, preferably using
  mesh nodes inside the pad polygon rather than a single detached pad-center node
  when a pour mesh exists under the pad;
- avoid adding duplicate or artificial stitch segments where an equivalent
  mesh/pad connection already exists;
- keep terminal distribution limited to the physical contact land, not the whole
  pour edge.

## Artifact Index

- Fugu2 KiPEX temp run: `/private/tmp/kipex-run/fugu2_threeleg/`
- simple-hb KiPEX temp runs:
  `/private/tmp/kipex-run/simple_hb_threeleg/`,
  `/private/tmp/kipex-run/simple_hb_threeleg_q1/`,
  `/private/tmp/kipex-run/simple_hb_oneport_q1/`
- simple-hb dcdc outputs:
  `dcdc-tools/parasitics/out-simple-hb-c1-lead0001/`,
  `dcdc-tools/parasitics/out-simple-hb-c1-p07-lead0001/`,
  `dcdc-tools/parasitics/out-simple-hb-c1-p05-lead0001/`,
  `dcdc-tools/parasitics/out-simple-hb-c1-lead3/`,
  `dcdc-tools/parasitics/out-simple-hb-c1c2-lead0001/`
- simple-hb hand FastHenry:
  `dcdc-tools/parasitics/out-simple-hb-hand-fh/`
- simple-hb PyPEEC:
  `dcdc-tools/parasitics/out-simple-hb-pypeec-hand/`
- synthetic width sweep:
  `dcdc-tools/parasitics/out-simple-hb-width-sweep/`
