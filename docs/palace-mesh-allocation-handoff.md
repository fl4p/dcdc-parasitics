# Handoff: Palace mesh element allocation & canary convergence

Written 2026-08-26 at the end of a pi session, for a fresh agent in another harness.
Everything below is either measured or explicitly flagged as unverified. Read
§6 before deriving anything — this task has burned four wrong conclusions, all
from the same mistake.

Repo root for all paths and commands: `/Users/fab/dev/pv/ee/dcdc-tools/parasitics`

---

## 0. Session 2 result: step 1 answered NO, and the re-diagnosis (2026-08-26)

§5 step 1 said: read `b20u`; if a fine in-band z step does not flip the
board/air split, **the §2 diagnosis is incomplete — stop and re-diagnose.**
It did not flip. Re-diagnosis follows. **§2, §4 and §5 below are superseded by
this section**; they are kept because their measurements are all still correct —
only their explanation was wrong.

### 0.1 `b20u` (area 2e-6, vstep 2e-5, band `[-0.0016, 0]`)

Mesh finished; the run's own manifest step was still going, so this was read
straight off `pcb.msh` with `meshsplit.awk` (validated by reproducing `b4lat`
exactly first).

| | b4lat (vstep 4e-4) | b20u (vstep 2e-5) |
|---|---|---|
| tets | 4,027,236 | 76,346,856 |
| z-levels | 123 (11 band / 112 air) | 2,297 (83 band / 2,214 air) |
| F.Mask | 1,116 | **1,116** |
| dielectric 1 (core) | 3,348 | 43,524 |
| B.Mask | 1,116 | **1,116** |
| outer air | 99.8614% | **99.9401%** |

A 20× finer in-band step made the air share **worse**. The masks are single-layer
(27.5 µm, below every step tried), so mask tets ÷ 3 is the number of triangles
covering the board footprint: **372 in both meshes.** The core tracks z alone —
3,348/(372·3) = exactly 3 layers, 43,524/(372·3) = exactly 39.

**The board's lateral footprint is pinned at 372 triangles by the geometry, and
no mesh knob moves it.**

### 0.2 Root cause: the board dielectric is a 50 µm picture frame

`lib/kicad_palace_dump.py:314` builds the board region with

```python
board.ConvertBrdLayerToPolygonalContours(pcbnew.Edge_Cuts, outline)
```

That API **strokes the Edge.Cuts graphics at their line width**; it does not
return the region the outline encloses. The canary's outline is four `gr_line`
segments of the KiCad-default 0.1 mm width, so the dump stores four capsules
with 0.05 mm round caps (`board_outlines` shells begin `137.476903, 67.490433`
— the cap arc). `lib/kicad_palace.py:369` unions them into a picture frame, and
`_prism_polygon = Polygon(rings[0], rings[1:])` turns the inner stroke edge into
a **hole**.

Measured on the canary geometry:

- core polygon: exterior 1804.252 mm², hole 1795.752 mm², **filled 8.499 mm² (0.47%)**
- `polygon.contains(board centre)` → **False** — that is the exact predicate
  `_cell_region` uses
- so F.Mask, the εr = 4.5 core and B.Mask exist only as a 50 µm rim, and **the
  whole board interior is solved as εr = 1 air**

This is a physics-correctness defect, not a meshing inefficiency. The mesher
put 99.9% of its elements in air because the board *is* 99.5% air on input.

**Blast radius** (`scan_hollow_outlines.py`): 22 of 23 volume dumps are hollow —
every `simple-hb*` at 0.47% filled, every `fugu2-*` at 30.13/4420.98 = 0.68%.
The single exception, `fugu2-q3-q2-c27-source-v1`, is 100% filled; its source
outline is one closed shape rather than N stroked segments, which is the control
showing the rest of the pipeline is sound.

### 0.3 The fix, measured

`GetBoardPolygonOutlines()` chains the segments and returns the enclosed region.
Both APIs exist in the KiCad 10.0.5 the dumper runs under; `outline_api_compare.py`:

| board | as shipped | `GetBoardPolygonOutlines` | edge bbox |
|---|---|---|---|
| simple-hb canary | 4 outlines, 8.507 mm² | 1 outline, **1800.000 mm²** | 1804.252 mm² |
| Fugu2 | 8 outlines, 30.223 mm² | 1 outline, **4399.265 mm²** | 4420.978 mm² |

Fugu2 here is the authoritative `/Users/fab/dev/ee/hw/Fugu2/Fugu2.kicad_pcb`,
sha `32906787…` — see §0.8 for the §7 reconciliation.

1800.000 mm² is exactly 45 × 40 mm. The correction is 212× on the canary and
146× on Fugu2.

Note the existing guard at `kicad_palace_dump.py:316` raises "KiCad board has no
**closed** Edge.Cuts outline" on an empty result — but the stroking API returns
non-empty for any segment, closed or not. It verifies existence, not the property
its message claims (Guard-review-checklist items 3 and 8).

### 0.4 Second, independent bug: the band only half works

The band added last session (`_refine_levels`, uncommitted) compares the gap edge
to the band edge with exact `<=`. Measured (`band_boundary_probe.py`):

```
material z span : -0.0015999999999999999 .. -0.0
band as passed  : -0.0016              .. 0.0
lo <= band[0]   : False     (lo - band[0] = 2.168e-19 m)
hi >= band[1]   : True      (-0.0 >= 0.0)
```

The board's bottom level sits **0.2 attometres** above the band edge, so the gap
*below* the board fails the skip test and is subdivided at the board's own step.
The top gap is skipped only by the accident that `-0.0 >= 0.0`.

This reproduces both meshes exactly: ceil(44.25 mm / 0.4 mm) = 111, +1 for the
box top = **112** air levels in b4lat; ceil(44.25 / 0.02) = 2,213, +1 = **2,214**
in b20u. b20u's levels run −45.850 … −1.620 mm at a uniform 20.0 µm, with a
single level above the band.

So the band never bought the ~20× it was supposed to — only the ~2× from skipping
one of the two air gaps. It fails **open** (silently does the expensive thing).

### 0.5 §2's lateral axis is not a defect — do not build the §5 step 2 fix

§2 says `max_planar_area_m2` spreads the budget over the full 90×88 mm box so
"~78% of every layer is air triangles". The triangulation does not do that.
Measured directly (`planar_probe.py`, 2D only, seconds not minutes), at
area 2e-6:

```
tris=11,114   board=6,184 (mean 0.292 mm²)   air=4,930 (mean 1.281 mm²)
```

The board footprint is 22.2% of the area and gets **55.6% of the triangles**, at
4.4× finer mean area. The lateral allocation is already board-biased. Only 372 of
those 6,184 board-footprint triangles land in a board *volume*, and §0.2 is why.

**A board-footprint refinement region would therefore have fixed nothing.** This
would have been the fifth instance of the §6 trap, and the first one that cost a
schema change to a shared gated component. `quality_meshing=False` is still real
(no shape-quality floor at all) but it is a separate accuracy question, not the
allocation cause.

### 0.6 What this means for every number in this document

Every canary and Fugu figure to date was measured on a board whose dielectric was
99.5% missing. That includes all three candidate canary limits in §4, and the
Fugu +35.8 pF. The 18 cross-terminal tets in §4 are real and still worth a gate,
but the extraction must be repeated on corrected geometry before the +35.8 pF is
attributed to anything.

### 0.8 Both fixes landed and re-measured (same session)

Fab chose both fixes plus guards, then a re-dump. All four steps of §0.7 that do
not need a solve are done; the suite is **942 passed, 1 skipped** (was 926/1) and
`ruff` is clean on the three touched files.

**Code.** `lib/kicad_palace_dump.py` now calls
`GetBoardPolygonOutlines(outline, False)` — infer off, so an unclosed outline
raises instead of being silently replaced by the Edge.Cuts bounding box — and
`_check_board_outline_fill` rejects a board region thinner than
`MIN_OUTLINE_FILL_FRACTION` (10%) of its own Edge.Cuts bounding box. The old
guard's message promised "closed" and only checked non-empty; this one measures
the property. `lib/palace_plc_mesh.py` `_refine_levels` compares band edges at
`max(1e-15, 1e-10 * span)` — the tolerance `_coalesce_levels` already uses to
decide two levels are the same level — and closes each refined gap with its
exact `high` rather than an interpolated point that could land an ulp beyond it.

**Guard calibrated against the real defect**, not only a fixture:

```
canary v2  (defective): REJECTED  encloses 8.507 mm2, 0.47% of 1804.252 mm2
canary v3  (fixed)    : ACCEPTED  fill=99.76%   area=1800.000 mm2
fugu2  v13 (defective): REJECTED  encloses 30.223 mm2, 0.68% of 4420.978 mm2
fugu2  v14 (fixed)    : ACCEPTED  fill=99.51%   area=4399.265 mm2
```

**§7's SHA discrepancy is reconciled.** Two distinct boards exist:
`/Users/fab/dev/ee/hw/Fugu2/Fugu2.kicad_pcb` (sha `32906787…`, 1,736,678 B,
2026-07-27) and `/Users/fab/dev/pv/ee/modulekit/fugu2/board/Fugu2.kicad_pcb`
(sha `b7fe4720…`, 1,497,721 B, 2026-07-04). **All 16 Fugu dumps used the former**,
which is also the `ee/hw/Fugu2` §7 names as authoritative. Use it; the modulekit
copy is older. (They share an Edge.Cuts outline, so the §0.3 area happens to
agree, but they are not the same board.)

**New dumps.** `simple-hb-user-space-geometry-v3` and
`fugu2-plc-source-bound-v14`, from the same PCBs as v2 / v13. In both, the
copper `records`, `drills` and `groups` are **byte-identical** to the old dumps —
only `board_outlines` changed, from 4 and 8 stroked capsules to one enclosed
region each.

**Allocation, baseline mesh, identical knobs, only the dump differs:**

| | v2 (defective) | v3 (fixed) |
|---|---|---|
| tets | 98,178 | 97,314 |
| F.Mask | 120 (0.12%) | 14,565 (14.97%) |
| dielectric 1 | 180 (0.18%) | 23,043 (23.68%) |
| B.Mask | 120 (0.12%) | 19,062 (19.59%) |
| outer air | 97,758 (**99.57%**) | 40,644 (**41.77%**) |

Board share 0.43% → **58.23%** at the same total element count. **The mesher was
never mis-allocating anything** — §2's premise is now disproved directly, not
just argued from §0.5.

**Allocation with refinement, b4lat's exact knobs (area 2e-6, vstep 4e-4,
band [-0.0016, 0]):**

| | b4lat (both defects) | v3b4lat (both fixed) |
|---|---|---|
| tets | 4,027,236 | **351,252** |
| z-levels | 123 (11 band / **112 air**) | 13 (11 band / **2 air**) |
| dielectric 1 | 3,348 (0.08%) | 76,770 (21.86%) |
| board total | 5,580 (0.14%) | 124,260 (**35.38%**) |
| outer air | 99.86% | 64.62% |

The band now skips **both** air gaps, as it was always meant to. The mesh is
**11.5× smaller** while carrying **22× more board elements**. This one fits the
solve budget with room to spare, so the ladder no longer needs the 2-concurrent-
solve rationing in §7.

**Still open:** no canary value has been computed on corrected geometry yet — §4
still has no trustworthy number, and §0.6 stands. The ladder (§0.7 step 3) and
the Fugu re-extraction (step 4) are the remaining work.

### 0.9 The ladder on corrected geometry: still NOT CONVERGED

Five rungs halving `max_planar_area_m2` and `max_vertical_step_m` together, on
v3 geometry with the fixed band, order 1 + BoomerAMG, 8 ranks. Every rung has
exactly **2 air levels**. Grade with
`out/palace-qualification/simple-hb-zladder-v1/grade_v3.py -boomeramg`.

| rung | area m² | vstep m | nodes | tets | trace pF | Δrel | pos_off | recip F | s | RSS GB |
|---|---|---|---|---|---|---|---|---|---|---|
| v3r1 | 8e-6 | 8e-4 | 33,610 | 154,206 | 151.2072 | — | 0 | 8.9e-24 | 6.6 | 1.48 |
| v3r2 | 4e-6 | 4e-4 | 50,098 | 241,248 | 145.7398 | −3.75% | 0 | 1.7e-23 | 9.3 | 1.91 |
| v3r3 | 2e-6 | 2e-4 | 92,731 | 472,644 | 138.3096 | −5.37% | 0 | 4.4e-23 | 17.5 | 3.16 |
| v3r4 | 1e-6 | 1e-4 | 215,289 | 1,158,066 | 133.3821 | −3.69% | 0 | 3.0e-23 | 43.5 | 7.07 |
| v3r5 | 5e-7 | 5e-5 | 597,037 | 3,346,722 | 129.1146 | −3.31% | 0 | 1.9e-23 | 101.6 | 13.27 |

**Matrix health is perfect** — zero positive off-diagonals and reciprocity at
1e-23 F on every rung, against the Fugu +35.8 pF that motivated the gate in §5
step 4. **Cost is no longer a constraint:** the whole five-rung ladder is under
three minutes of solve time and peaks at 13 GB, so §7's "at most 2 concurrent
solves" and 1800 s budget no longer bind.

**But the finest pair is 3.31% / 4267.55 fF against the 2% + 1 fF band: NOT
CONVERGED.** The gate stays as it is.

**No value may be quoted, including an extrapolated one.** The observed
convergence order, `log2(Δprev/Δnext)`, is **−0.44, +0.59, +0.21** across
successive windows, against ~2 expected for order-1 elements. Aitken over a
sliding three-rung window gives **166.44 → 123.68 → 101.52 pF** — a 30% swing
per window. The sequence is not in an asymptotic regime, so extrapolation is
meaningless here. (This is worth remembering against §4's p-ladder Aitken of
11.706 pF, which was quoted as a limit.)

**Why: see §0.10.** An earlier draft of this section blamed element shape
quality, on the strength of the isotropy measure `12(3V)^(2/3)/Σedge²`, which
runs a median of 0.039–0.202 across the rungs with a tail at 1e-6. That was the
wrong metric and the wrong conclusion — see §0.10.1. Element quality is fine.

### 0.10 Which axis: measured, and it is the lateral one

#### 0.10.1 Element quality is not the problem — the isotropy metric was

The isotropy measure penalises *every* thin element, but a flat well-formed
element in a 27.5 µm mask layer is perfectly sound for FEM. What actually breaks
the interpolation error bound is the **maximum angle** approaching 180°
(Babuška–Aziz), not the aspect ratio. Max dihedral angle on v3r5:

| volume | tets | median | p95 | p99 | max | >170° | >178° |
|---|---|---|---|---|---|---|---|
| outer | 120,038 | 90.00 | 104.11 | 163.46 | 179.98 | 0.72% | 0.00% |
| dielectric 1 | 42,943 | 90.69 | 152.88 | 166.65 | 179.98 | 0.17% | 0.04% |
| B.Mask | 2,521 | 90.26 | 139.29 | 163.38 | 179.98 | 0.20% | 0.12% |
| F.Mask | 1,834 | 90.18 | 131.85 | 163.65 | 173.10 | 0.05% | 0.00% |

Median ~90°, under 0.72% above 170°, essentially nothing above 178°. **The mesh
is sound.** A quality floor would have been a schema change to a shared gated
component to fix an axis that is not broken — the §6 trap, caught one step
before implementation.

#### 0.10.2 The vertical axis is already converged

Halve exactly one knob from v3r4 (1e-6, 1e-4):

| mesh | trace pF | Δ from v3r4 | share |
|---|---|---|---|
| v3r4 (1e-6, 1e-4) | 133.3821 | — | — |
| **v3lat** (5e-7, **1e-4**) | 129.1868 | **−4195.3 fF** | **98.3%** |
| **v3vert** (1e-6, **5e-5**) | 133.3127 | **−69.4 fF** | **1.6%** |
| v3r5 (5e-7, 5e-5) | 129.1146 | −4267.5 fF | 100% |

The singles sum to −4264.7 fF against −4267.5 for both, so the axes separate
cleanly with no interaction term. **Vertical is done: 69 fF on 133 pF is 0.05%,
far inside the band.** The z-ladder, the band, and b20u all refined the axis
carrying 1.6% of the error. §2 had the two axes exactly the wrong way round.

#### 0.10.3 The lateral ladder is clean, and hopeless

Pure lateral at fixed vstep 1e-4, order 1 + BoomerAMG:

| area m² | tets | trace pF | Δrel | Δabs fF | pos_off | recip F |
|---|---|---|---|---|---|---|
| 2.0e-6 | 715,428 | 138.0156 | — | — | 0 | 1.6e-23 |
| 1.0e-6 | 1,158,066 | 133.3821 | −3.47% | −4633.4 | 0 | 3.0e-23 |
| 5.0e-7 | 2,048,832 | 129.1868 | −3.25% | −4195.3 | 0 | 1.2e-23 |
| 2.5e-7 | 3,806,928 | 125.3696 | −3.04% | −3817.2 | 0 | 7.5e-24 |

Isolating one axis finally gives an asymptotic sequence: difference ratios
**0.905, 0.910** and observed order **0.14, 0.14**, with Aitken now *stable* at
**89.01 → 86.84 pF** against the 30% swings of the joint ladder (§0.9). The
joint ladder was erratic because it varied two axes at once, not because
anything was wrong with the solve.

But **p ≈ 0.14 is unusable**. At 0.91 per area-halving, reaching 2% of an
eventual ~100 pF needs 6.9 more halvings — **4.4e8 tets**, at a 66 µm element
edge. Uniform lateral refinement is not a route to this number. And note the
gap: 125.37 pF measured against an ~87 pF extrapolated limit.

#### 0.10.4 Why, and what it implies

66 µm is the tell: it is the **copper thickness, 35 µm**, to within a factor of
two. The remaining error is the field singularity at conductor edges, whose
length scale is the copper thickness, and the finest lateral element tried is
**707 µm — 20× too coarse there**. Uniform refinement reaches that scale
everywhere, at absurd cost; grading reaches it only where it is needed.

Two things this is **not**, both checked rather than assumed:

- **Not element quality** (§0.10.1).
- **Not elements bridging conductor gaps.** Tets touching two terminal surfaces
  hold flat at **10,270 / 10,631 / 10,524 / 10,715** across the 5.3× lateral
  ladder, over 32–35 pairs. They are resolution-independent — conductors
  adjacent in the PLC share nodes, and `allow_volume_steiner=False` preserves
  those segments at every refinement. This also means §4's "an XY-refined mesh
  at 4e-7 m² has zero cross-terminal tets" needs re-checking; it does not hold
  on the canary.

So **§5 step 2 was right after all, for a reason nobody had**: a lateral
refinement region is the only affordable route. §0.5's argument against it was
about *allocation*, and was correct about allocation — the hollow board was the
allocation defect. Once the board is solid, the lateral axis is the
convergence-limiting one and it needs grading, not uniform refinement. The
target is now quantitative: **element size approaching the 35 µm copper
thickness near conductor edges, and only there.**

### 0.11 Graded lateral refinement: 79× cheaper, still not converged

Implemented as `conductor_edge_band_m` + `conductor_edge_max_planar_area_m2`
(commit `86891c7`). Within the band of a conductor boundary, triangles are
capped at the finer area through Triangle's `-u` callback, and conductor
boundary *segments* are re-split at `sqrt(2 · edge area)` — not optional, since
`allow_volume_steiner=False` forbids Triangle from splitting a segment, so the
conductor polyline would otherwise keep the global spacing however small the cap.

**Against the uniform lateral ladder, at matched cost:**

| mesh | tets | trace pF |
|---|---|---|
| uniform 2.5e-7 | 3,806,928 | 125.3696 |
| **graded** 1e-6 base, 200 µm band @ 1e-8 | **3,256,260** | **106.8595** |

14% fewer elements, 18.5 pF further down a sequence that is monotone from above.
Reaching 106.86 pF by uniform refinement would take ~2.6e8 tets — an **79×
saving**, and confirmation that the error really is at the conductor edges.

**The graded ladder** (base 1e-6, vstep 1e-4, band 200 µm, halving the cap):

| edge area m² | edge length | tets | trace pF | Δrel | pos_off |
|---|---|---|---|---|---|
| 4.0e-8 | 283 µm | 1,591,332 | 122.4429 | — | 0 |
| 2.0e-8 | 200 µm | 2,037,024 | 113.3958 | −7.98% | 0 |
| 1.0e-8 | 141 µm | 3,256,260 | 106.8595 | −6.12% | 0 |
| 5.0e-9 | 100 µm | 5,808,024 | 99.5917 | −7.30% | 0 |

**NOT CONVERGED** at 7.30% / 7267.9 fF, and the sequence is erratic — ratios
0.722 then **1.112**, so the differences are still *growing*. Aitken swings
89.84 → 171.79 pF. We are still pre-asymptotic, which is consistent with the
diagnosis: the finest edge element is 100 µm against a 35 µm copper thickness,
so the singularity is not resolved yet.

Two things confound this ladder and should be fixed before it is trusted:

- The **band is fixed at 200 µm** while the cap shrinks, so it spans 0.71
  element layers at the coarsest rung and 2.0 at the finest. The refined region
  is not a fixed multiple of the local element size.
- The **base area is fixed at 1e-6**, freezing the bulk error at a constant
  offset while only the edge term moves.

Matrix health stays perfect throughout: zero positive off-diagonals, reciprocity
at 1e-23 F on all four rungs. Cost is comfortable — the finest is 5.8M tets,
230 s, 14.2 GB.

**Trend, not a value:** 122.44 → 113.40 → 106.86 → 99.59 pF is heading into the
~87 pF region the uniform lateral ladder extrapolated to (§0.10.3), which is
mild corroboration and nothing more. §4 still stands: **no trustworthy canary
value.**

### 0.12 The acceptance rule was passing ladders that do not converge

**The gate was the defect.** Every mesh carries `convergence_ladder_required:
True`, but nothing in `lib/` discharged it — the rule lived as a copy-pasted
"finest pair within 2% + 1 fF" in each campaign script under `out/`. That rule
reads two points out of a sequence, so an oscillating ladder passes it as soon
as two adjacent rungs happen to land close.

A ladder built this session did exactly that. Scaling the refinement band with
the element size (band = 2 × edge length, base held at 1e-6) gave:

| rung | edge µm | band µm | tets | trace pF | Δrel |
|---|---|---|---|---|---|
| `v3e05` | 100.0 | 200.0 | 5,808,024 | 99.5917 | — |
| `v3s25` | 70.71 | 141.4 | 6,875,268 | 105.4212 | **+5.53%** |
| `v3s12` | 50.00 | 100.0 | 9,643,944 | 102.9801 | −2.37% |
| `v3s06` | 35.36 | 70.7 | 12,886,656 | 104.6627 | **+1.61%** |

The finest pair is 1.61%, inside the band. The old rule reports CONVERGED on a
sequence that is visibly ringing. Solver residuals are ~1e-12 against a 1e-10
target on identical binary and config, so this is not a solver artefact.

Two traps in fixing it, both real:

- **Contraction alone is not enough.** That oscillating sequence contracts by
  0.419 then 0.689. A sequence bouncing about a centre with decaying amplitude
  contracts beautifully while saying nothing about where it is going.
- **Sign alone is not enough.** The fixed-band ladder is monotone at −9047.04,
  −6536.29, −7267.88 fF; only the growing magnitude gives it away.

`lib/palace_convergence.py` requires both, suspending each for a correction
already inside the band, and is tri-state: too few solved rungs, a rung that
varied more than one axis, or a noise floor wider than the band all report
`unevaluable`. Both real sequences are pinned as known-bad calibration in
`test/test_palace_convergence.py`. The 2% + 1 fF band is untouched — this only
ever refuses more (§6).

**The scaled ladder above is not a ladder.** The gate rejects it on
preconditions before reading a number: the band varies along with the element
size, so two axes moved. Worse, the band *shrinks* — 200 → 70.7 µm — which
un-refines a shell of material at each rung. Coarsening raises the FEM energy
and so raises C, which is exactly the +5.53% jump. Holding the band constant in
*element layers* is not the same as holding it constant, and only the latter is
a ladder. **The valid family is fixed-band.**

Run through the gate, the fixed-band family is:

```
rung       elem_um      tets   trace_pF   d_abs_fF    d_rel
v3e40       282.84   1591332   122.4429          -        -
v3e20       200.00   2037024   113.3958   -9047.04   -7.98%
v3edge      141.42   3256260   106.8595   -6536.29   -6.12%
v3e05       100.00   5808024    99.5917   -7267.88   -7.30%
NOT_CONVERGED: the correction at rung v3e05 did not contract (ratio 1.112 > 0.9)
```

Note the Aitken limit this ladder implies is **171.79 pF** — above every rung
that produced it. Earlier sessions quoted Aitken figures from this family; they
are extrapolations from a sequence that is not contracting and mean nothing.
`grade_gate.py` now labels them so.

**Retraction:** §0.11's "graded refinement is 79× cheaper" compared a graded
mesh against a uniform one at matched trace. The cost ratio still stands, but it
was stated alongside a convergence claim resting on the finest-pair rule. No
graded ladder has yet converged under the corrected gate.

#### 0.12.1 Why the lateral ladder stalls — **RETRACTED, measured false**

> **The hypothesis below is wrong.** Halving the vertical step at held lateral
> refinement moves the trace by **−290 fF (−0.29%)**: `v3e05` 99.5917 pF →
> `v3v50` 99.3013 pF. The lateral axis moves it 7.3% per rung. The vertical
> axis is not co-dominant and cannot explain the stall, so **§0.10.2 stands
> after all** and this section's reasoning does not.
>
> The measurement is cleaner than it looks. `_planar_mesh` does not take
> `max_vertical_step_m`, so the two meshes share a **bit-identical lateral
> triangulation** — 42,486 distinct xy points in both — and the §0.13 re-meshing
> noise floor, which was measured by perturbing the base area, does not apply to
> this comparison at all.
>
> One caveat kept: the z-levels are **not** nested between the two rungs (15 of
> 25 coarse levels are absent from the fine set), because `_refine_levels`
> divides each gap into `ceil(gap/step)` *equal* parts, so halving the step
> reshuffles the interior levels instead of bisecting them. Part of that 290 fF
> is therefore re-levelling perturbation rather than refinement, which only makes
> the true vertical effect smaller. This is also direct evidence for the
> bisection requirement in §0.13.1 — the current scheme demonstrably does not
> nest in z either.
>
> What was right in it: the geometric observation that copper is 2 elements
> thick while the adjacent core layer is 94 µm. That is true, and it simply does
> not dominate.

Original hypothesis, retained for the record:

The `v3e05` mesh has 25 z-levels. Copper runs `-0.0450 → -0.0275 → -0.0100` mm,
so it is 2 elements of 17.5 µm through its 35 µm thickness — but **the core
layer adjacent to it is 94 µm thick**, and that is where the fringing field at a
conductor edge lives. With the lateral edge element now at 100 µm, the two axes
are within 6% of each other and both are ~2.7× the copper thickness.

Refining one axis while the other is co-dominant stalls: the error left by the
frozen axis floors the sequence. This makes §0.10.2's "the vertical axis is
already converged" **stale** — it was measured when the lateral element was
1.4 mm, so 94 µm vertical looked converged by comparison. Under test as `v3v50`
/ `v3v25` (vstep 5e-5, 2.5e-5, lateral held at `v3e05`).

### 0.13 Re-meshing noise, and why the ladder design itself is wrong

Three meshes at nominally identical resolution (base area perturbed ±1%, so the
resolution is unchanged but Triangle returns a different triangulation):

| tag | base m² | tets | trace pF |
|---|---|---|---|
| `v3n99` | 9.9e-7 | 5,800,470 | 99.7826 |
| `v3e05` | 1.0e-6 | 5,808,024 | 99.5917 |
| `v3n101` | 1.01e-6 | 5,797,908 | 99.5455 |

**Trace spread: 237 fF, 0.238%** — well inside the band. On that number alone
the ladder is readable and the §0.12 stall is real signal, not luck.

**Entry by entry it is not.** 45 of the 171 entries move more than the entire
2% + 1 fF band under nothing but regeneration:

```
  i   j      |C| fF   spread fF   spread %
  0   6       99.72       63.59      63.77
  0  12       69.13       34.85      50.41
  4  12        4.86        1.89      38.79
  4  11        4.62        1.74      37.69
  4   6        5.82        2.13      36.65
```

The trace hid this for exactly the reason it hides cancelling drift (§ the
entrywise gate): it is a sum, and the re-meshing errors in it cancel. This is a
lower bound — segment splitting is deterministic at fixed edge area, so
perturbing the base understates how much the edge zone can move.

Run against the fixed-band ladder with these floors, the canary is
**UNEVALUABLE, not NOT_CONVERGED**:

```
45 of 171 entries move more than the 2% + 1 fF band when the mesh is merely
regenerated at the same resolution, worst C[6][6] at 152.53 fF; no amount of
refinement can demonstrate convergence against that
```

That is the more accurate verdict and it is worse news than non-convergence,
because it is not fixable by refining harder.

#### 0.13.1 The consequence: rungs must be nested

Every ladder run so far — z, lateral, joint, graded, scaled — built each rung as
an **independent mesh**. That is the defect. Two consequences follow, and both
are structural rather than tuning problems:

1. **Rayleigh–Ritz does not apply.** The monotone-from-above guarantee holds for
   *nested* function spaces. Independent triangulations are not nested, so
   monotonicity was never owed to us, and §0.10.3's reading of monotone decrease
   as evidence of approach was reading a coincidence.
2. **Re-meshing noise never cancels.** Each rung carries an independent
   O(60%)-per-entry perturbation. Differencing two rungs adds their noise
   instead of removing it.

Under nested refinement both problems vanish by construction: the coarse space
is a subspace of the fine one, energy decreases monotonically, and the shared
node positions mean the entry-level noise is common-mode rather than
independent.

This is a mesher feature that does not exist yet. `_planar_mesh` calls Triangle
fresh each time; nesting needs Triangle's refine mode over the previous
triangulation, plus a z-level set built by bisecting the previous one so the
coarse levels remain a subset. **No canary number should be quoted as converged
until the ladder is nested.**

#### 0.13.2 `meshpy.triangle.refine` segfaults — measured, with the workaround

Tested before designing on it, and the obvious call does not work:

| call | result |
|---|---|
| `t.refine(m)` | **SIGSEGV** (exit 139, no traceback) |
| `t.refine(m, refinement_func=...)` | **SIGSEGV** (exit 139) |
| `t.refine(m)` after `m.element_volumes.setup()` | works |

`refine()` hands the mesh to Triangle's `-r` mode, which reads a per-element
area-constraint array that MeshPy never allocates. The `refinement_func=`
keyword is accepted and still crashes, so it is **not** a substitute — the
existing `refinement_func` grading in `_planar_mesh` cannot simply be carried
over to the refine path. Grading has to be expressed as per-element targets:

```python
m.element_volumes.setup()
for i in range(len(m.elements)):
    m.element_volumes[i] = target_area_for(m, i)   # grading goes here
r = t.refine(m)
```

The crash is silent under output filtering — the last `print` before the call is
the only clue, which is how it first looked like a hang.

**Vertex nesting confirmed**, not assumed: every coarse point survives into the
fine mesh (`set(coarse) <= set(fine)`, 0 lost; 5 points/4 triangles → 177/319 at
target 0.005). Caveat worth carrying: this is *vertex* nesting, not strict
element nesting — Delaunay may still flip an edge between two surviving
vertices. It should make the re-meshing noise largely common-mode, which is what
§0.13 needs, but it is not a proof of nested subspaces and the noise floor must
be re-measured on nested rungs rather than assumed to vanish.

Filed as `~/dev/kb/tooling/meshpy-triangle-refine-needs-element-volumes.md`.

#### 0.13.3 …and it survives the real canary PLC

A unit square proves little here — the canary PLC has hundreds of conductor
outlines and holes, and `-YY` forbidding segment splits. Run through
`refine_probe.py` at 8e-6 → 2e-6:

```
coarse: 3156 points, 6218 triangles, 2533 segments
fine:   17063 points, 33950 triangles, 7546 segments
coarse vertices preserved: True (0 lost of 3156)
PLC segments kept verbatim: 2101 of 2533
triangles over target: 0 of 33950 (max 1.998e-06 vs target 2.000e-06)
```

Vertices nest exactly and the area target is honoured with nothing over it.

**The segment count is the catch, and it is a provenance problem, not a
geometry one.** `refine()` takes no `allow_volume_steiner`, so it splits
segments the `-YY` build forbade: 432 of 2533 no longer appear verbatim. Checked
rather than assumed — total segment length goes 1.127103013 m → 1.127103013 m,
a relative change of **+8.7e-15**. The splits are collinear subdivisions, so no
boundary moved and no geometry was invented or lost.

Two consequences to carry into the implementation:

1. `mesh_parameters` records `allow_volume_steiner: false`, which will be a
   **lie** on a refined rung. A nested mesh must record how it was produced and
   that segment splitting was permitted, or the manifest asserts a property the
   mesh does not have.
2. `_resplit_conductor_segments` exists precisely because `-YY` would not split
   conductor-boundary segments. On the refine path Triangle will split them
   itself, so that pre-split is redundant there — but the *coarsest* rung is
   still a `build()`, so it cannot simply be deleted.

### 0.14 The vertical ladder converges — entrywise — and shows why

```
rung       elem_um      tets   trace_pF   d_abs_fF    d_rel
v3e05       100.00   5808024    99.5917          -        -
v3v50        50.00   9585594    99.3013    -290.33   -0.29%
v3v25        25.00  17140734    99.2100     -91.37   -0.09%

trace:  CONVERGED  (contraction 0.315, observed order 1.67, Aitken 99.1680 pF)
matrix: CONVERGED  all 171 entries; worst finest step 0.60 fF (0.19%) at C[6][9]
```

**This is the first ladder in the project to pass anything**, and the first to
pass entrywise — the same gate that reports the lateral ladder UNEVALUABLE
because 45 of its 171 entries are noise-dominated.

The reason is the whole diagnosis in one line: **`_planar_mesh` does not take
`max_vertical_step_m`, so all three rungs share a bit-identical lateral
triangulation** (42,486 xy points each). They are accidentally nested in the
axis that carries the noise, so nothing had to cancel — there was no
perturbation to begin with. Change the lateral mesh instead and 45 entries move
by more than the band; hold it fixed and every one of the 171 settles.

That is direct evidence that §0.13.1's prescription works: when rungs are nested,
the entrywise gate becomes readable. It is the strongest corroboration available
short of running the nested lateral ladder itself.

Two things this does **not** say:

- **The canary is not converged.** This is one axis. The lateral axis still
  moves 7.3% a rung and is the one that matters (§0.10.4). A converged vertical
  axis on an unconverged lateral one is not a converged matrix, and 99.21 pF is
  a measurement, not a limit.
- **z nesting was not what saved it.** The z-levels are *not* nested across
  these rungs — 15 of 25 coarse levels are absent from `v3v50` (§0.12.1). The
  ladder converged anyway, which places the re-meshing noise overwhelmingly in
  the lateral triangulation rather than the z re-levelling. Bisection (§cba5176)
  is still right, but it is the smaller of the two effects.

### 0.7 Next steps (replacing §5)

1. Fix `kicad_palace_dump.py:314` to `GetBoardPolygonOutlines`, and tighten the
   §0.3 guard so it verifies the outline is closed and encloses a plausible area
   (e.g. filled ≥ some fraction of the edge bbox) — fail-closed, since the
   current failure is silent. **Re-dump both geometries.**
2. Fix the `_refine_levels` band comparison to use a tolerance rather than exact
   `<=`, and add a known-bad test: a band edge perturbed by 1 ulp must still skip
   the gap. Assert the resulting air-level count, not just that it ran.
3. Only then rebuild the z ladder. With the core solid and the band actually
   skipping both air gaps, re-measure before designing any lateral change.
4. Re-extract Fugu at 82 terminals; keep the positive cross-terminal stiffness
   gate from §5 step 4 regardless.

Reproduce §0 with, from the repo root:

```
PYTHONPATH=out/palace-qualification/venv/lib/python3.14/site-packages python3 \
  out/palace-qualification/simple-hb-refined-mesh-v1/<probe>.py
awk -f out/palace-qualification/simple-hb-refined-mesh-v1/meshsplit.awk <mesh.msh>
```

probes: `dielectric_fill_probe.py`, `scan_hollow_outlines.py`,
`band_boundary_probe.py`, `planar_probe.py`; and `outline_api_compare.py`, which
must run under KiCad's own interpreter
(`/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3`).

---

## 1. Goal

Produce **converged, passive, reciprocal** capacitance matrices for Fugu2
(82 terminals) from Palace electrostatic FEM, gated fail-closed at **2% + 1 fF**.

Gate ordering is deliberate: the **simple-hb canary must demonstrate convergence
first**; Fugu qualification is downstream of it. Do not reorder.

## 2. The core finding

> **Superseded by §0.2 and §0.5.** The measurements here are correct; the
> explanation is not. The vertical axis is real but secondary (§0.4); the
> lateral axis is not a defect at all (§0.5).

`lib/palace_plc_mesh.py` spends essentially the entire element budget on air.
This single defect has **two independent axes**, and it explains both open
problems (canary non-convergence *and* the Fugu positive off-diagonal).

**Vertical axis — two bugs found and fixed; effect confirmed only in unit tests.**
`_refine_levels()` applied `max_vertical_step_m` to *every* z gap. The level set
spans the whole outer box (90 mm) for a 1.6 mm board, so the two ~44 mm air gaps
got the same step as the stackup. Measured on the old mesh: **233 z-levels, 8 in
the board and 225 in air; 99.93% of tets in outer air**.

Adding a band exposed a second, nastier bug that **failed open**: band edges are
round numbers but levels derive from the stackup, so the board bottom lands at
`-0.0015999999999999999` — 2.2e-19 m above a band edge of `-0.0016`. An exact
comparison un-skipped the *lower* 44 mm air gap and tiled it at the board's own
step, while the upper gap at `0.0` compared equal and was skipped correctly.
That asymmetry is the "exactly 2x" air-refinement signature seen in every
pre-fix mesh. Nothing reported it; the mesh was just ~20x larger than intended.
Both the tolerance and an endpoint-interpolation ulp bug are now fixed at
`_refine_levels` (line 303).

**Lateral axis — NOT fixed, no code written.**
`max_planar_area_m2` is applied across the full **90×88 mm** footprint while the
board is **45×40 mm — 22% of the area**. So ~78% of *every* layer is air
triangles. Confining z alone cannot flip the allocation. The triangulation call
(`lib/palace_plc_mesh.py:190`) also passes **`quality_meshing=False`**, so there
is no shape-quality floor at all.

## 3. What is done (uncommitted, in the working tree)

`lib/palace_plc_mesh.py` (+43/−5) adds `vertical_refinement_band_m=(lo, hi)`:

| line | what |
|---|---|
| 303 | `_refine_levels(levels, maximum_step, band=None)` — skips gaps not overlapping the band |
| 319 | `_validate_vertical_refinement_band()` — fail-closed |
| 572, 591, 616 | parameter on `generate_palace_plc_mesh`, validated, passed through |
| 664 | recorded in provenance `mesh_parameters` |
| 789, 1112, 1147 | reload/rebuild path + validated key set + revalidation |

`test/test_kicad_palace.py` (+210). Suite: **942 passed, 1 skipped**
(was 912 before this work). Total diff `lib/palace_plc_mesh.py` +65/−6.

Also uncommitted and **mine**: `lib/palace_source_policy.py`,
`test/test_palace_build_v2.py` — the authorized-build swap to
`palace-build-qualification-direct-make` (see §7).

**Not mine — leave alone.** `lib/emit.py`, `test/test_reduce.py`,
`test/test_cin_warning_classification.py`, `examples/*`. Another agent works
this tree concurrently. Stage file-by-file; never `git add -A` or `git add <dir>`.
HEAD moved twice mid-session (now `02f1d5b`); re-read `git log` before any
history operation.

## 4. Verified vs unverified

> **See §0.6.** Everything listed here was measured on geometry whose dielectric
> was 99.5% missing. The measurements stand as measurements; none of them
> characterises the intended board.

**Verified by measurement:**
- Old allocation 99.93% air; board core 3,348 tets.
- **Every mesh currently on disk is STALE** — `b4lat`, `b20u` and all `z*` dirs
  were built by pre-fix code and must be regenerated before use. Their numbers
  below are evidence *of the bug*, not of the geometry.
- `b4lat` (band `[-0.0016, 0]`, vstep 4e-4), pre-fix: 123 z-levels, 11 in band,
  112 in air; board core still exactly 3,348 tets; air 99.86%.
- `b20u` (band, vstep 2e-5), pre-fix: **2,297 z-levels — 83 in band (correct:
  1.6 mm/2e-5 ≈ 80) but 2,214 in air**, 12,976,693 nodes / 77,349,724 elements,
  and no manifest (provenance never completed). The 2,214 air levels match
  44.25 mm / 2e-5 = 2,212 — i.e. exactly the one un-skipped lower gap. This is
  the cleanest evidence of the fail-open bug, and it is now fixed.
- Correct framing, from the subagent: **the band stops waste, it does not add
  resolution.** A fine *in-band* step must also be requested.
- Solver conditioning is solved and is **not** a constraint: BoomerAMG at order 1
  matches a SuperLU direct solve to 3e-15 and is ~2× faster. The historical
  stalls were an order≥2 Chebyshev-smoother problem.
- Fugu `Bat+`↔`Net-(D11-A)` = **+3.5793e-11 F** is caused by **18 coarse
  tetrahedra straddling both terminals**, contributing +3.5934e-11 F = **100.4%**
  of it. Terminals share no nodes/overlap/UUIDs; copper is 1.2 mm apart. An
  XY-refined mesh at `4e-7 m²` has **zero** cross-terminal tets.

**Unverified / open:**
- Whether a fine in-band z step flips allocation. The decisive mesh (`b20u`:
  area 2e-6, vstep **2e-5**, band `[-0.0016, 0]`) was **still building when this
  session ended** — check for
  `out/palace-qualification/simple-hb-refined-mesh-v1/b20u/pcb.msh.manifest.json`.
  It has been killed twice already by a 1800 s inner timeout; give it ≥90 min.
- Any converged canary value. **There is currently no trustworthy number.**
  Three mutually inconsistent limits exist — z-only ladder ~12.72 pF, p-ladder
  Aitken 11.706 pF, `z4lat` 10.5484 pF — and **all three are suspect** because
  every one was measured on an air-dominated mesh. Truth is plausibly ≤10 pF.
  Do not quote any of them as a reference.

## 5. Next steps

> **Superseded by §0.7.** In particular do not build step 2 — see §0.5.

1. **Regenerate a mesh with the fixed code and re-measure the allocation** —
   e.g. `regenerate.py 2e-6 2e-5 b20v -0.0016 0.0`. Expect ~83 in-band levels
   and ~2 air levels, i.e. roughly 85 total rather than 2,297, and a board/air
   tet split that has actually flipped. Delete the stale `b4lat`/`b20u` dirs.
   If the split still does not flip, the lateral axis (§2) is the reason —
   do not proceed to ladders until it does.
2. **Design the lateral fix** — the real remaining work. Options: a board-
   footprint refinement region (lateral analogue of the band), or enabling
   Triangle quality meshing with a local area constraint near conductors.
   This is a second provenance-schema change to a shared gated component
   (`mesh_parameters` key set at line 1112) — **Fab's call, not the agent's.**
3. Rebuild a **joint** lateral+z ladder, ≥3 rungs, halving both per rung,
   order 1, BoomerAMG. Grade successive rungs at 2% + 1 fF. Record trace,
   positive off-diagonal count, reciprocity, time, RSS per rung.
4. Re-extract Fugu at 82 terminals on a properly refined mesh. The +35.8 pF
   should vanish; add a gate rejecting positive cross-terminal local stiffness
   so this class fails at assembly, not in the final matrix.
5. Commit in focused pieces with evidence in the bodies.

## 6. Traps — read before deriving

**The recurring error, four times in one session: vary an axis that cannot move
the quantity, observe invariance, conclude the wrong cause.**

- The p-ladder "converged" → invalid, measured at fixed under-resolved mesh.
- The z-only ladder passed 2% + 1 fF → invalid, lateral moved it −29%.
- "Lateral refinement is a negative result" → wrong, an artifact of z error
  dominating.
- "The Fugu +35.8 pF is mesh-independent, so it's an extraction bug" → wrong.
  The z20 refinement kept exactly the 18 offending tets because a 20 mm vertical
  limit never subdivides a 1.51 mm slab.

So: **before calling anything mesh-independent, confirm the refinement you varied
actually subdivides the elements responsible.** A gate going green is not
evidence; the previous convergence claim passed its gate and was still wrong.
Never weaken the 2% + 1 fF gate to make a ladder pass.

Other traps:
- Mesh manifests record refinement under `mesh_parameters`, **not** under
  `source_identity` (probes print `null` there — not a bug).
- Mesh format is legacy **MSH 2.2**, not 4.1: `$Nodes` lines are `id x y z`;
  in `$Elements`, type `4` is a tetrahedron and the physical tag is `parts[3]`.
- Killing a campaign leaves stale `checkpoint/` and `native-campaign-identity.json`.
- `.config.json.execution.*/` dirs are read-only: `chmod -R u+w` before `rm -rf`.
- `out/` is gitignored by design; probe/campaign scripts live there deliberately.

## 7. Environment

- Palace (authorized): `/Users/fab/dev/vendor/palace-build-qualification-direct-make/bin/palace`,
  `v0.17.0-302-g0ca2de94`.
  **Deprecated:** `palace-build-qualification-make/bin/palace` is `86810b19-dirty`
  and no longer authorized — a false attestation caught this session; the swap was
  proven provenance-only (identical numerics, `z4` reproduced 13.6240 pF exactly).
- Build manifest: `docs/artifacts/palace-e4-build-v4/palace-build-v2.8e0c1516e52742b5baf1a8f5c798c1a86319e0fca96a1314dab6d7a8c4b1dcb9.json`
- Always prefix: `PYTHONPATH=out/palace-qualification/venv/lib/python3.14/site-packages`
- Tests: `PYTHONPATH=... python3 -m pytest test/ -q`
- `ruff` repo-wide has ~57 pre-existing errors; lint only branch-touched files.
- Machine: 11 cores (5P+6E), 36 GiB. Budget 1800 s / 24 GiB per solve attempt.
  A 7.7M-tet solve floors at ~16 GiB regardless of rank count (~2.1 KB/tet), so
  **at most 2 concurrent solves**. Mesh builds take 25+ min — set timeouts
  accordingly; this killed two runs.

**Scripts** (args are positional):
- `out/palace-qualification/simple-hb-refined-mesh-v1/regenerate.py MAX_AREA MAX_VSTEP TAG [BAND_LO BAND_HI]` — `1e30` disables lateral
- `out/palace-qualification/simple-hb-refined-mesh-v1/meshsplit.py` — per-volume tet split
- `out/palace-qualification/simple-hb-zladder-v1/probe.py TAG [ORDER] [SOLVER] [RANKS]`
- `out/palace-qualification/simple-hb-zladder-v1/grade.py`

**Geometry:** F.Mask 0.01, F.Cu 0.035, core `dielectric 1` 1.51 mm (εr 4.5),
B.Cu 0.035, B.Mask 0.01; board 45×40 mm; outer box 90×88×90 mm
(`outer_scale` 2.0); outer z −0.04585 … +0.04425 m. Physical tags: 1 = outer air,
2/4 = masks, 3 = dielectric core.

**Fugu:** D11 = SS310 Schottky, `PowerSupply.kicad_sch:4805`, nets 44/74.
Pads D12.2 (`fadef423…`, `Fugu2.kicad_pcb:29161`) and D11.2 (`7a846fdd…`,
`Fugu2.kicad_pcb:31435`). Attributes 107 (`Bat+`) / 130 (`Net-(D11-A)`).
Authoritative board is `ee/hw/Fugu2` — one run manifest referenced a copy whose
SHA differs; reconcile before trusting numbers.
