# Handoff: Palace mesh element allocation & canary convergence

Written 2026-08-26 at the end of a pi session, for a fresh agent in another harness.
Everything below is either measured or explicitly flagged as unverified. Read
§6 before deriving anything — this task has burned four wrong conclusions, all
from the same mistake.

Repo root for all paths and commands: `/Users/fab/dev/pv/ee/dcdc-tools/parasitics`

---

## STATE AS OF 2026-08-27 — read this before anything below

The document grew by fifteen subsections on 2026-08-26/27 and several of its
earlier conclusions were overturned by later ones. This block is the current
position; §0.20 onward are the sections that still stand.

**The canary is NOT qualified.** Two ladders now pass the *trace* gate (§0.24,
§0.25); no ladder passes the *entrywise* gate that §1 actually states. The best
result is 8 of 171 entries outside 2% + 1 fF, all of them on the board's three
0.6 mm SOIC pads.

**Tightest upper bound on the trace: 18.1573 pF** (`v3k1` at order 3, an accepted
run; see §0.26 on why its cost figure understates what it took). Every accepted trace is a bound, because `C_ii` is the discrete Ritz energy
— verified, not assumed (§0.20). Extrapolations from both axes at both orders
land between 17.8 and 20.1 pF. **Quote the bound, not the extrapolations.**

**Four things settled this session, each overturning something above:**

| finding | overturns |
|---|---|
| The prism split must order by coordinate, not vertex index (§0.17) | a ~21% numbering dependence in every earlier answer-class mesh |
| Every trace is a Ritz energy, so an upper bound (§0.20) | §0.18's order-1 Aitken limit of 90.14 pF, excluded by 4.9× |
| The air box is the dominant axis, ~9:1 over lateral (§0.21) | §0.10, "which axis: measured, and it is the lateral one" |
| Grading the vertical band takes observed order 0.72 → 1.83 (§0.24, §0.25) | the conclusion that no ladder could converge |
| A peak-RSS ceiling cannot bound a swapping process (§0.26, §0.29) | every cost figure in §0.9–§0.25, which understates what its run took |
| BoomerAMG at order 2 never stalled; §0.19 misread an iteration cap (§0.31) | §0.19 route 1 and §0.30's "measured closed" escape-route list |

**Two library defects fixed, both fail-open:**
`_tetrahedralize` chose prism diagonals from vertex indices (`5d533c8`), and
`_refine_levels` subdivided any base gap that *overlapped* the refinement band
rather than clipping to it, so every band narrower than the 44 mm air gap
silently did nothing (`cab1d39`).

**Reading the numbers below.** Everything in §0.9–§0.19 that came from an
index-ordered mesh carries the §0.17 numbering uncertainty and has not been
re-run. §2, §4 and §5 were already superseded by §0. The 2026-08-27 grid
(§0.23–§0.25) is all SuperLU direct solves at 8 ranks, so no rung of it carries
iterative-solver error.

**Next steps** are listed at the end of §0.25. The blocking one is a fifth rung
of the combined-grading ladder. What blocks it has changed twice: the 1800 s
wall was raised on evidence and was never binding (§0.27, §0.28); memory then
was, once the ceiling started measuring memory rather than RSS (§0.29), and it
put the ladder's *existing* top rung over budget at 24.49 GB. That is the cost
of the **direct factorisation**, not of the rung — §0.31 shows the iterative
route it was believed to have closed is open, and was misdiagnosed from a single
summary statistic.

**Cost discipline.** These are full-system direct factorisations on a 36 GB
machine and chaining them kernel-panicked it once (§0.22). Check free memory
before each run; do not chain them.

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

> **CORRECTION (same session).** The paragraph below is wrong, and the nested
> ladder failed on it. `refine()` does not merely split PLC segments; it drops
> the constrained status of some. Measured against triangle edges: 355 of 2533
> source segments no longer appear verbatim, 305 of those are honest collinear
> subdivisions, and **50 are up to 95% uncovered** — 1.93 mm missing from a
> 2.03 mm segment. A conductor boundary that is not an edge of the
> triangulation breaks the point-in-polygon material assignment.
>
> The 8.7e-15 length figure below measured the *facet list's* internal
> consistency, not whether source segments survive as edges of the mesh. It was
> the wrong measurement for the question and it read as reassurance.
>
> `_validate_plc_mesh_topology` catches this, so a nested build fails closed
> rather than producing a wrong mesh: `v3nest1..3` all refuse with "Palace PLC
> mesh omits a noded source boundary segment: missing=50".
>
> **SECOND CORRECTION — the sentence that stood here, "the nested path is
> blocked until Triangle's -Y can be passed through refine", was also wrong,
> and it named the wrong flag.** The missing flag is `-p`, not `-Y`.
> `meshpy.triangle.refine()` decides whether to pass `p` by testing
> `input_p.faces` — the *output* edge array — while the PLC segments live in
> `facets`. On a mesh from `build()` that test is always false, so `-r` runs
> **without the segments ever being declared**. They are not destroyed by
> refinement; they are never presented to it.
>
> Measured on a square-in-a-square PLC, one parent mesh refined under each
> option string, scoring each source segment by whether it is still covered by
> triangle edges:
>
> ```
> razjQ   (what MeshPy sends)   4 of 8 segments 100% uncovered
> razjpQ  (this)                0 of 8 uncovered, 0 parent vertices lost
> ```
>
> `-Y` is in fact the **wrong** fix and is deliberately left out. It forbids
> Steiner points on segments, so the conductor polyline would freeze at
> whatever spacing rung zero got while the interior kept refining —
> reintroducing on every rung above zero exactly the under-resolved conductor
> edge `_resplit_conductor_segments` exists to prevent. Segment subdivision is
> harmless because the material assignment accepts a segment covered by
> collinear pieces; **losing** the segment is what breaks it, which is why
> coverage and not verbatim survival is the property to measure.
>
> `_refine_segment_conforming` now calls Triangle directly with `razjpQ`. The
> rung that failed with `missing=50` builds: 97841 nodes, 497934 tets.
>
> The standing instruction survives both corrections: do not relax the topology
> guard to get past a segment failure. It was right twice.

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

### 0.15 The nested ladder, finally built — and it is not Triangle that builds it

§0.13.3 said the nested path was blocked on Triangle's `-Y`. That was wrong twice
over, and the way it was wrong is the useful part.

**First correction: the missing flag was `-p`, not `-Y`.**
`meshpy.triangle.refine()` decides whether to pass `p` by testing `input_p.faces`
— the *output* edge array — while the PLC segments live in `facets`. On a mesh
from `build()` that test is always false, so `-r` runs without the segments ever
being declared. They were never destroyed by refinement; they were never
presented to it.

```
razjQ   (what MeshPy sends)   4 of 8 segments 100% uncovered
razjpQ  (segments declared)   0 of 8 uncovered, 0 parent vertices lost
```

**Second correction: `-p` is necessary and not sufficient.** With the segments
properly declared Triangle still drops them once the mesh is refined far enough:

```
seed 8e-6, rounds 0-4    0 uncovered      round 5    2 uncovered
seed 5e-8, rounds 0-1    0 uncovered      round 2   23 uncovered
                                          round 3   77 uncovered
                                          round 4  108 uncovered
```

They are gone from Triangle's own segment list. It is not the input's fault: of
2532 source segments exactly one pair meets anywhere other than a shared
endpoint, and that pair is a duplicate, so the PSLG is valid. Dropping `j` does
not change the count; **adding `Y` does not change the count**; quality meshing
makes it far worse (46 uncovered). Triangle's `-r` mode cannot be trusted to
carry a PLC to this depth however it is invoked, so it is no longer used for
nesting.

**What builds the ladder instead: uniform 1-to-4 subdivision.** No mesher is
involved, so none of that surface exists.

- Every parent vertex is a child vertex and every parent triangle is the union
  of four children → the planar mesh is exactly nested, so a rung is a
  deterministic refinement of its parent rather than an unrelated triangulation.
- Every parent edge becomes two collinear halves → a source segment that was an
  edge stays covered by edges, by construction.
- The four children are similar to the parent → shape quality is exactly
  preserved and a graded seed keeps its grading.
- Element size halves *exactly* each rung, which is a cleaner ladder than
  halving an area (a √2 step in length).

Verified on the written meshes, not merely in a unit test:

```
v3u0  77794 nodes   383106 tets    3156 planar xy
v3u1 293348 nodes  1532424 tets   12529 planar xy
parent nodes missing from child: 0     parent xy missing: 0
z levels 25 -> 25, none lost           tets 383106 -> 1532424 = exactly 4.00x
```

3156 vertices + 9373 edges = 12529, which is the subdivision arithmetic being
exactly what it claims. The z levels are identical between rungs, so this is a
pure lateral ladder with the vertical axis held.

The price is 4× the triangles per rung rather than 2×. That is the right trade:
an inexactly nested ladder measures re-meshing noise, and on this model that
noise moved 45 of 171 matrix entries by more than the whole acceptance band.

#### 0.15.2 CORRECTION — the 3D spaces are not nested, so monotonicity is not owed

The bullet above originally read "the rungs are exactly nested, and
Rayleigh–Ritz monotonicity is owed rather than hoped for". **The nesting claim
is 2D and the FEM is 3D.**

`_tetrahedralize` splits each prism by the ordering of its triangle's *global*
vertex indices, which subdivision renumbers — but the mismatch is structural,
not a consequence of that rule. A corner sub-prism has positive area on both the
top and bottom planes, while each of the parent prism's three tets degenerates
to a point or an edge on one of them, so **no parent tet is a union of child
tets under any diagonal convention**. Measured on one prism:

```
parent tets 3, child tets 12
child tets straddling more than one parent tet: 11 of 12
```

So the parent P1 space is not a subspace of the child P1 space, the energy is
not obliged to decrease, and **a rung that moves the trace upward is not by
itself evidence of a defect**. This matters immediately: the first uniform
ladder did exactly that.

```
rung   elem_um       tets   trace_pF   d_abs_fF    d_rel
v3u0   4000.00     383106   146.0835          -        -
v3u1   2000.00    1532424   166.8052   20721.70   +12.42%
```

Two things are worth separating here. The +12.42% is *permitted* by the above —
it is not proof of a broken mesh. But it is also not a usable ladder: a 4 mm
element on a 45 mm board is far outside the asymptotic range, and the sign of a
correction there carries no information either way. The seed was simply too
coarse, which §0.15.3 addresses.

What nesting still buys, and the reason to keep it, is the removal of the
**re-meshing perturbation** — the thing actually measured hurting the ladder
(45 of 171 entries beyond the band). It does not buy monotonicity, and this
document should not have said it did.

One residual worth flagging rather than assuming away: because the prism
diagonals are re-chosen at every rung, some *re-tetrahedralization* difference
survives between rungs. It is far more local than a full re-mesh — the vertices
and the planar triangulation are identical — but it has not been measured. The
honest statement is that the noise floor on a nested ladder is **unmeasured, not
zero**; §0.13.3's caveat still stands in a narrower form.

Regression check on the mechanism, which is sound: `v3nest0` (built before any
of this work) and `v3u0` (built after) are the same mesh and give a bit-identical
trace of 146.0835 pF.

#### 0.15.1 Three topology guards were measuring length where they meant distance

Getting a nested mesh past the validator exposed the same bug in three places,
and it is worth stating in general terms because it will recur.

**A segment lying along an edge that is bent off it by one ULP shares only a
measure-zero set with it.** So `difference` returns *the whole segment* and
`covers` returns False. By length that is indistinguishable from a segment that
was dropped outright:

```
covered but bent   689 segments report 100% uncovered, straying <= 2.794e-17 m
genuinely dropped  50 segments, 25%-100% uncovered, straying 1.9e-3 m
```

Nine orders apart, identical under a length test. The crossing guard had the
same flaw and reported a **1.765e-3 m penetration into a cell 1e-3 m across** —
an impossible depth, which is what gave it away.

Fixes, all measured rather than reasoned about:

1. Anything the exact test flags is re-tested against grid-snapped geometry.
   **Snapping is strictly a fallback.** On a 2e-3 m fixture a segment whose raw
   residue is exactly `0.0` comes back 100% uncovered when snapped to 1.11e-16,
   so the exact test keeps the last word when it says "covered".
2. **The grid must be a power of ten.** GEOS snaps by scaling by `1/gridSize`,
   so a grid that is not exactly representable pushes collinear points off each
   other. The comparison that leaves 0 uncovered at 1e-13 leaves **160 at
   2.05e-13 and 141–193 at every power of two from 2⁻⁴⁶ to 2⁻³⁶**. Powers of
   two being worse than powers of ten is the counter-intuitive part; the source
   coordinates are themselves a decimal grid. This cost two wrong diagnoses
   before it was measured.
3. When no grid clears both bars — above the rounding, below the geometry
   quantum — `_coverage_grid_m` returns `None` and the unsnapped verdict stands,
   which fails closed.
4. The failure messages now report the **stray distance**, because that is the
   one number that separates "dropped" from "covered but bent". Neither the
   residue length nor the count can.

Runtime cost of the guard work, measured: 4.2 s at 383k tets, 17.2 s at 1.53M —
4.1× for 4× the mesh, so linear, with nothing superlinear introduced.

**None of the guards were weakened to get a mesh through.** Every one of them
was correct to refuse what it refused; three of them were reporting the wrong
number about it.

### 0.16 The coarse ladder was measuring the tetrahedralization, not the mesh

§0.15.2 flagged the nested-ladder noise floor as *unmeasured, not zero*, because
the prism diagonals are re-chosen at every rung. Measured, and it is not small.

`_tetrahedralize` picks each prism's diagonal from `sorted(triangle)` — the
**global vertex indices**. Permuting the planar point numbering therefore leaves
every coordinate, every triangle, every z level and every material assignment
exactly as it was, and changes only which diagonal each prism gets. Two such
permutations of the coarse seed:

```
v3u0     trace 146.0835 pF      (natural ordering out of Triangle)
v3u0p1   trace 288.7963 pF      (seed 1)
v3u0p2   trace 275.7444 pF      (seed 2)
trace spread 142712.70 fF = 49.42%
entries beyond the 2% + 1 fF band: 272 of 324
worst entry C[14][12]: 185.54 fF with 183.07 fF of spread (98.67%)
```

Checked rather than assumed, because a 49% spread invites disbelief:

- identical node sets (77794 nodes, set equality)
- identical element histogram across every material and all 18 terminal tags
- identical total volume to machine precision (7.289605946e-04 m³ both)
- genuinely different meshes: only 28.1% of tets are shared
- comparable element quality (median 0.1065 / 0.1058 / 0.1060; 9.15% / 9.17% /
  9.16% below 0.01), so this is **not** a quality artefact
- different mesh SHA-1s in the two solve directories, so each solve really did
  read its own mesh
- **both meshes are conforming**, which was asserted before it was checked:
  identical face histograms, every interior face shared by exactly 2 tets,
  58866 boundary faces in each (`{1: 58866, 2: 736779}` for both)
- every written surface triangle is a genuine face of a tet in the same mesh
  (0 orphans in all three), so the terminal surfaces Palace integrates over are
  not broken by the relabelling

The second graded ladder then reproduced the pattern independently — a
different seed, different grading, same shape:

```
v3u0 -> v3u1   +12.42%   (uniform, base 4000 -> 2000 um)
v3g0 -> v3g1   +13.02%   (graded, base 4000 um / edge 800 -> 400 um)
```

**Consequence.** At this resolution the answer is a function of the vertex
numbering. The v3u ladder's 146 → 167 → 140 pF was not measuring refinement; a
single relabelling moves the trace further than any of its rungs did. The
oscillation §0.12 has been chasing is at least partly this.

**What this does not say.** It was measured on a 4 mm-lateral / 100 µm-vertical
seed — 40:1 anisotropic, 9% of tets below quality 0.01. It says nothing yet
about the noise floor at the resolutions the project actually quotes (v3e05,
5.8M tets, base 1414 µm). **That measurement is still owed**, and until it
exists no ladder on this mesher should be believed, including the ones already
in this document. The permutation test is the way to take it: same geometry,
same materials, different diagonals.

### 0.17 The numbering sensitivity reaches answer-class meshes — and is now fixed

§0.16 measured the vertex-numbering sensitivity on a deliberately absurd seed
and said explicitly that the same measurement at the resolutions this project
quotes was still owed. Taken now, on **v3e20** — 2.04 M tets, graded base
1414 µm / edge 200 µm, one of the meshes this document has quoted numbers from:

```
v3e20     113.3958 pF     (natural ordering)
v3e20p1   143.4699 pF
v3e20p2   140.2250 pF
trace spread 30074.02 fF = 20.96%
entries beyond the 2% + 1 fF band: 128 of 171
worst C[9][13]: 391.61 fF with 366.24 fF of spread (93.52%)
```

**113.3958 pF is a rung of §0.11's ladder.** Relabelling the vertices — same
geometry, same materials, same z levels — moves it by 21%. Every capacitance in
this document that came from an index-ordered mesh carries that uncertainty,
and the ladders built from them were differencing rungs whose individual values
were less certain than the differences being read.

#### The cause, and why it is mechanical rather than statistical

`_tetrahedralize` chose each prism's diagonal from `sorted(triangle)`, i.e. from
**global vertex indices**. Subdivision appends midpoints at the end of the point
list, so every midpoint outranks every original vertex. For a parent edge A→B
with A < B, the sub-edge A–AB keeps the parent's orientation while AB–B flips,
because AB now outranks B. Half the sub-edges invert on every rung, and which
half is decided by the numbering rather than the geometry:

```
sub-edges inheriting their parent's diagonal
  index order        18618 / 37236   (50.0%, exactly what the argument predicts)
  coordinate order   37226 / 37236   (99.97%)
```

That is also why two independent nested ladders oscillated in step — same
artefact, not a shared physical effect:

```
v3u   146.0835 -> 166.8052 (+12.42%) -> 139.5821 (-19.50%)   uniform
v3g   140.2311 -> 161.2289 (+13.02%) -> 136.6850 (-17.96%)   graded
```

#### The fix, verified rather than argued

Ordering the split by **coordinate** instead of index makes a midpoint sort
between its own endpoints, so every sub-edge inherits its parent's diagonal and
the tetrahedralization becomes a function of the geometry alone. Conformity is
unaffected: it holds for any total order, because two prisms sharing a quad
face derive its diagonal from the same pair.

The verification is exact, not statistical. Two arbitrarily different index
permutations of the same 2.04 M-tet geometry:

```
coordinate order   2037024 of 2037024 tets shared (100.0000%), set equality True
index order         563640 of 2037024 tets shared (27.7%)
```

Under coordinate ordering there is no noise floor to measure, because there is
nothing left to vary. `prism_split` in provenance becomes
`freudenthal-3-lexicographic`; the old value is still accepted so existing
meshes keep validating, but **a mesh recording `freudenthal-3` should be treated
as carrying the §0.16 uncertainty**.

#### What is still open

This removes a mechanism that was demonstrably injecting 20–50% swings. It does
not follow that the ladder now converges — that is being measured, and the
answer is not in yet. Every ladder in this document predates the fix and should
be re-run before any of its conclusions are relied on.

### 0.18 With the numbering artefact gone, the ladder converges — but is not converged

The A/B is clean: identical geometry, identical seed, identical rungs, identical
solver. Only the prism diagonal rule differs.

```
index order (v3u)                          coordinate order (v3l)
rung  elem_um      tets   trace_pF         rung  elem_um      tets   trace_pF
v3u0  4000.00   383106   146.0835          v3l0  4000.00   383106   175.6309
v3u1  2000.00  1532424   166.8052 +12.42%  v3l1  2000.00  1532424   130.8634 -34.21%
v3u2  1000.00  6129696   139.5821 -19.50%  v3l2  1000.00  6129696   109.5380 -19.47%

contraction   1.314                        contraction   0.476
observed order -0.39                       observed order 1.07
sign flip at rung 2                        monotone
```

**This is the first ladder in the project whose corrections all share a sign and
contract.** The contraction of 0.476 is essentially ½, which is what first-order
convergence gives when *h* halves; the observed order of 1.07 agrees. For a
problem with field singularities at conductor edges, first order is what P1
elements are expected to deliver.

**It is still NOT_CONVERGED, and the gate is right.** The finest step is 19.47%,
an order of magnitude outside the 2% + 1 fF band, and 91 of 171 entries fail.
The ladder is converging; it has not converged. The Aitken figure of 90.14 pF is
reported and is *not* a result — the gate marks it meaningless precisely because
the ladder did not pass, and it disagrees with the ~99.6 pF that earlier
index-ordered ladders drifted toward, which is itself a reason to trust neither
until a ladder passes.

At a contraction of 0.476 per rung, closing 19.47% down to 2% takes roughly
three more halvings — 24M, 98M, 392M tets uniformly, which is not reachable.
Only graded refinement can get there, which is what the conductor edge band
exists for, and a graded ladder under the fixed ordering is the next measurement.

#### Reproduced on a second, independent mesh family

The graded ladder was rebuilt under the fixed ordering, same A/B:

```
index order (v3g)                          coordinate order (v3gl)
140.2311                                   179.1552
161.2289  +13.02%                          136.7797  -30.98%
136.6850  -17.96%                          115.8193  -18.10%
contraction 1.169, order -0.23, sign flip  contraction 0.495, order 1.02, monotone
```

Side by side with the uniform ladder:

```
                 rung 0     rung 1     rung 2   contraction  order  finest step
v3l  (uniform)  175.6309   130.8634   109.5380     0.476     1.07     19.47%
v3gl (graded)   179.1552   136.7797   115.8193     0.495     1.02     18.10%
```

Two mesh families, different seeds, different grading, both contracting at
essentially ½ with an observed order of 1.0. That is first-order convergence
reproduced rather than asserted, and it is the strongest evidence so far that
this model can be converged at all.

It is also why neither Aitken figure should be quoted: they are 90.14 pF and
95.30 pF, 5.7% apart. Two ladders that agree on their *rate* to within 4% still
disagree on their *limit* by 5.7%, because both are extrapolating from ~18%
outside the band. The gate calls them meaningless and the gate is right.

One more thing this pins down. v3u0 and v3l0 are the *same geometry at the same
resolution*, differing only in diagonal choice, and their quality distributions
are indistinguishable (p50 0.10654 vs 0.10653; 9.15% vs 9.18% of tets below
0.01). They differ by 20% in trace. So the discretisation error at 4 mm lateral
elements is at least that large however the prisms are cut, and no ordering rule
was ever going to rescue a mesh this coarse — the fix removes the arbitrariness,
not the error.

### 0.19 Where this leaves the canary, and what to do next

**State.** Two independent nested ladders converge at first order (contraction
0.476 and 0.495, observed order 1.07 and 1.02) and neither has converged: the
finest steps are 19.47% and 18.10% against a 2% + 1 fF band. The canary still
has **no trustworthy value**, and after §0.17 it has fewer defensible ones than
before — every capacitance in this document that came from an index-ordered mesh
carries a ~21% numbering uncertainty.

**Uniform refinement cannot close it.** At a contraction of 0.476 per rung:

```
+1 rung   9.27%    24.5M tets
+2 rung   4.41%    98.1M tets
+3 rung   2.10%   392.3M tets
+4 rung   1.00%  1569.3M tets
```

Halving the vertical step count buys a factor of 4 and does not change that
conclusion. More tetrahedra is not the answer.

**Three candidate routes, cheapest first.**

1. **Raise the element order.** At O(h²) the contraction becomes ~0.25, so
   19.47% → 4.9% → 1.2% closes in two rungs rather than four. Order 2 on
   `v3l0`, the *coarsest* rung at 383k tets and 39 s at order 1:

   ```
   PCG did NOT converge in 500 iterations, avg. reduction factor 9.489e-01
   killed at the 1800 s wall limit, exit -9, outcome "rejected"
   0.26 GB per rank -- not memory-bound
   ```

   > **CORRECTION (§0.31, 2026-08-27).** Everything this subsection went on to
   > conclude from that reduction factor was wrong, and the paragraph that
   > stood here — "the preconditioner is doing essentially nothing at order 2,
   > this is not a size problem and starting coarser will not fix it" — was a
   > misreading of a single number. The solve did not stall. It reduced the
   > residual by 9.4 orders of magnitude, monotonically, and Palace's own
   > *explicit* residual `norm(Ax-b)/norm(b)` was **4.2e-12** on every solve,
   > against the 1e-10 this workflow accepts. `9.489e-01` is not a diagnosis;
   > it is `(r_500/r_0)^(1/500)` — an arithmetic identity that a perfectly
   > healthy solve stopped at an iteration cap also satisfies. Read §0.31
   > before using anything below about BoomerAMG, and do not repeat the
   > inference: **a per-iteration reduction factor cannot distinguish a stalled
   > solve from a converging one that was cut off. Only the residual history
   > can.**

   Route 1 also costs nothing extra as a **direct** solve — which is what
   `probe.py` defaults to, and the reason its docstring gives is exactly this: a
   direct solve means "the reported capacitance carries no iterative-solver
   error and the only varying quantity across rungs is [the mesh]".

   **Measured, and it is cheap.** The same rung, order 2, SuperLU:

   ```
   BoomerAMG   1831 s   rejected at the 500-iteration cap (NOT a stall -- §0.31)
   SuperLU       53 s   completed, no failures, 8.4 GB
   ```

   The route is open *as a solve*, and the earlier note here calling it closed
   was wrong — it generalised from the iterative solver to the method. But the
   scaling closes it again, and this time measured rather than inferred:

   ```
   order 2, SuperLU    383k tets     53 s    8.4 GB   completed
                      1.53M tets  >1800 s   14.1 GB   killed, exit -9
   ```

   A 4× mesh makes the direct solve **more than 34× slower** and it does not
   finish inside the 1800 s wall. Note what the kill was *not*: peak RSS was
   14.07 GB against the resource class's 24 GiB ceiling, so memory had 10 GB of
   headroom and the binding constraint was wall time alone. The 34× is a lower
   bound on the true cost, not a measurement of it.

   **This does not close p-refinement, it relocates it.** A three-rung *p*=2
   ladder would sit at roughly 24k / 96k / 383k tets, all of which are
   affordable. Whether that is useful is an open and cheap question: p=2 on the
   coarsest mesh already moved the trace from 175.63 pF to 57.35 pF, so a p=2
   ladder over coarse meshes may converge where p=1 over fine ones does not.
   The meshes do not exist yet — it needs a seed about 16× coarser than `v3l0`.
   That is the next experiment, and it is a build job rather than a solve job.
   **§0.20 ran that build job. The seed does not exist and cannot be built:
   the PLC floors the lateral mesh long before 24k tets.** Read §0.20 before
   acting on this paragraph.
   The p-ladder scripts under `out/` were written for this and are pinned to
   refused v2 geometry — repoint them at v3 rather than writing new ones. Each
   pins it in exactly one line (`GEOMETRY = Path(...simple-hb-user-space-
   geometry-v2)`), so the repoint is a one-line change per script, and
   `simple-hb-p1-superlu-probe-v1`, `simple-hb-p3-v1` and
   `simple-hb-p4-superlu-v1` already carry the SuperLU direct-solver settings
   that high order needs.
2. **Red–green local refinement.** Subdivide only inside the conductor edge
   band, where the singularity is, and bisect the neighbours to restore
   conformity. Keeps vertex nesting, and spends elements where the error lives
   instead of tiling the whole board. It breaks the exactly-4ᵏ triangle guard in
   `_nested_refinement`, which would need a per-region count instead.
3. **Split the axes.** Converge laterally at a deliberately coarse vertical
   step, then apply the vertical correction separately — §0.14 already did the
   vertical half this way. Not a proof of joint convergence, but the gate is
   per-axis and this is the standard practice.

**A sobering cross-check, and a warning about every extrapolation here.** The
same coarse mesh solved at order 1 and order 2:

```
v3l0 order 1 (BoomerAMG)   175.6309 pF   reciprocity 1.19e-23
v3l0 order 2 (SuperLU)      57.3542 pF   reciprocity 5.82e-26
```

A factor of **three**, with reciprocity three orders better at order 2. That is
a direct measure of how far from converged the order-1 solution is on this mesh,
and it sits badly with both extrapolations in play: the order-1 h-ladder
contracts toward ~90 pF and the earlier index-ordered work drifted toward
~99.6 pF. If the order-2 value is anywhere near right, the order-1 ladder has
much further to run than its contraction ratio suggests — and if it is not, then
the order-2 solution is itself unconverged in *h* and equally unquotable. The
two cannot both be trusted, and nothing here settles which. **Quote neither.**

**Before any of that, re-run what is already here.** Every ladder in this
document predates the coordinate-ordering fix. §0.11's graded ladder, §0.14's
vertical ladder, and the §0.9 and §0.10 measurements were all built on
index-ordered meshes and are contaminated to the tune of §0.17's 21%. The
vertical ladder in particular *passed* the entrywise gate, and that pass should
not be relied on until it is reproduced under the fixed ordering — a ladder can
pass for the wrong reason as easily as it can fail for one.

**Do not** reorder the gate: the canary must demonstrate convergence before
Fugu is requalified, per §1.

### 0.20 Every trace in this document is an upper bound — and the best one is 24 pF

§0.19 sent the next agent to build a seed "about 16× coarser than `v3l0`" for a
three-rung order-2 ladder at 24k / 96k / 383k tets. That build job was run. It
fails, and the reason it fails is worth more than the ladder would have been.

**The lateral mesh is floored by the PLC, not by the area constraint.**
`max_planar_area_m2` stops doing anything once it exceeds the size the conductor
outlines already force. Same geometry, same vertical settings as `v3l0`, only
the area target moved:

```
max_planar_area_m2   nodes    tets      vs v3l0
8e-6   (v3l0)        77794    383106      1.00x
1.28e-4 (16x)        58781    279186      1.37x coarser
5.12e-4 (64x)        22506     97842      3.92x
2.048e-3 (256x)      22406     97422      3.93x   <- saturated
```

A 256× larger area target buys 3.93× fewer tetrahedra and then stops: the last
two rows differ by 0.4%. Roughly 2350 planar points are not a resolution choice,
they are the conductor outlines themselves. There is no 24k-tet mesh of this
board, so there is no three-rung order-2 h-ladder. Route 1 as §0.19 framed it is
closed, and this time for a structural reason rather than a cost one.

**The floor mesh.** Dropping `max_vertical_step_m` as well (levels at material
interfaces only) gives the smallest mesh this geometry admits:

```
v3cz   area 1.28e-4, no vertical step   22886 nodes   99546 tets
```

**p-refinement does not need three meshes.** The mesh is a PLC extrusion: every
conductor and dielectric boundary is exactly a facet, at every order. Raising
the polynomial order on a *fixed* mesh therefore converges to the true solution
of the same geometry with no geometric error to chase — and, unlike the h-ladder
of §0.15.2, the spaces really are nested, because they share the mesh. On `v3cz`,
SuperLU direct, 8 ranks:

```
p    trace_pF     delta_pF    d_rel    contraction   wall     RSS
1    215.9080            -        -              -    5.0 s   1.5 GB
2     63.9914    -151.9166  -237.40%             -   12.0 s        -   REJECTED
3     38.0788     -25.9126   -68.05%         0.171   28.1 s  13.4 GB
4     28.6703      -9.4085   -32.82%         0.363   78.8 s  14.8 GB
5     24.1859      -4.4845   -18.54%         0.477  523.4 s  20.9 GB
```

**The diagonal is the energy — verified, not assumed.** The claim that these are
upper bounds rests on `C_ii` being the Ritz energy rather than an independently
computed surface flux, so it was checked rather than argued:

```
2 * E_elec[i] / C_raw[i][i] = 376.7303134   for all 18 terminals
  p3 spread 6.1e-13   p4 1.6e-12   p5 2.1e-12
```

That constant is the free-space impedance, i.e. the solver's nondimensionalisation.
`C_ii` **is** twice the discrete field energy over a fixed constant, to twelve
digits. The same identity, the same constant, holds on the BoomerAMG h-ladder
runs (`v3l0/1/2`, `v3gl0/1/2`, spreads 1.7e-13 to 6.9e-13), so it is a property
of the extraction and not of the direct solver.

Rayleigh–Ritz then applies with no caveats: over nested spaces the discrete
energy decreases monotonically toward the true energy from above. Therefore

> **every capacitance trace in this document is an upper bound on the true
> trace, and the smallest one measured is 24.19 pF.**

**This refutes the order-1 extrapolations outright.** §0.18's ladder contracts
toward an Aitken limit of 90.14 pF, and the earlier index-ordered work drifted
toward 99.6 pF. Both sit **3.7× above a proven upper bound**. They were never
estimates of the limit. §0.19 said "quote neither"; the reason is now stronger
than the caution was — they are not merely untrustworthy, they are excluded.

**And it says the h-ladder is the wrong axis by a wide margin.** Compare the
best h result against a coarse-mesh p result:

```
v3l2  order 1   6129696 tets   109.5380 pF     <- 6.1M tets
v3cz  order 3     99546 tets    38.0788 pF     <- 62x fewer, 2.9x better bound
v3cz  order 5     99546 tets    24.1859 pF     <- 62x fewer, 4.5x better bound
```

Sixty-two times fewer elements and a bound 4.5× tighter. Every tetrahedron spent
on the order-1 h-ladder bought less than raising the order on the smallest mesh
the geometry allows.

**What the p-ladder does not do is converge either.** The contraction ratio is
*worsening* — 0.171, 0.363, 0.477 — which is the signature of algebraic rather
than exponential convergence, and it is heading for roughly the same ~0.48 the
h-ladder settled at. A geometric tail at 0.477 puts the limit near 20.1 pF, but
a rising ratio makes even that an optimistic read. The finest step is 18.54%
against a 2% + 1 fF band. **The canary still has no value, only a much better
bound on one.**

Two candidate causes, and they are distinguishable cheaply:

- **Conductor-edge singularity.** The potential goes as r^α at a re-entrant
  copper edge; p-refinement on a fixed mesh converges only algebraically against
  a singularity sitting at an element vertex. If this dominates, the fix is
  grading toward the edges (`v3gl*` already exists) or hp, not more p.
- **Air-box aspect ratio.** `v3cz` spans ~44 mm of air in a handful of elements,
  and every h-ladder mesh confines its vertical refinement to
  `[-0.0016, 0]` — the board — so the air's vertical resolution has never been
  varied by *any* ladder in this document. That is an unexplored axis, and the
  fact that p (which enriches within those tall elements) moves the answer so
  much further than h (which does not touch them) points at it.

The discriminating experiment is one mesh and one solve: rebuild `v3cz` with the
vertical band covering the whole domain instead of the board, and compare at
fixed order. If the trace drops sharply, the air box was the error.

**One rung was rejected, and correctly.** Order 2 produced 16 positive
off-diagonal entries, the largest `C[2][13] = +0.0127 fF` against a 2.92 pF
diagonal — 4 parts per million, i.e. discretisation noise — and the run gate
refused the whole run and quarantined the CSVs. Orders 1, 3, 4 and 5 on the same
mesh have none. This is the guard behaving as designed (a non-physical matrix is
not a small error to be tolerated), but it does mean a p-ladder can lose an
interior rung, and `check_convergence_ladder` will not let the survivors be
joined into one apparent step. Read the trace above from the quarantined copy
under `postpro/.quarantine.*/`; the numbers are recorded, the run is not
accepted.

Campaign: `out/palace-qualification/simple-hb-pladder-v1/probe.py TAG ORDER`,
one output directory per order so no grader can conflate them.

### 0.21 The dominant axis is the air box, and no ladder here has ever driven it

§0.20 named two candidate causes for the p-ladder's algebraic convergence and
said the discriminating experiment was one mesh and one solve. It is cheaper
than that: a 2×2, all at order 1, all seconds.

The board sits in an air box spanning z = −45.8 mm to +44.2 mm. Every mesh in
this document sets `vertical_refinement_band_m = [-0.0016, 0]` — **the board** —
so the 44.2 mm of air above and the 44.2 mm below have always been one element
tall. Measured on the meshes themselves:

```
v3l0   z levels 25   min gap 10.0 um   max gap 44.200 mm
v3cz   z levels 10   min gap 10.0 um   max gap 44.200 mm
```

Lateral refinement subdivides that 44.2 mm element sideways, so every rung of
the h-ladder made the air elements *more* anisotropic rather than smaller: 11:1
at `v3l0`, 44:1 at `v3l2`. The ladder was refining the one direction that was
already resolved.

**The 2×2.** Vary lateral element size and air z-resolution independently, with
the same PLC and — verified — the same worst tetrahedron:

```
mesh   lateral   air z     tets   trace_pF   pos_off   worst_ppm
v3cz    16 mm     none    99546   215.9080         0           -
v3lz     4 mm     none   137856   200.6603         0           -
v3a3    16 mm     5 mm   328026    76.6165        50      7656.8
v3al     4 mm     5 mm   436320    67.9962        50      2219.5
```

```
lateral 16 mm -> 4 mm, no air levels     215.91 -> 200.66     -7.1%
lateral 16 mm -> 4 mm, air at 5 mm        76.62 ->  68.00    -11.2%
air none -> 5 mm, lateral 16 mm          215.91 ->  76.62    -64.5%
air none -> 5 mm, lateral 4 mm           200.66 ->  68.00    -66.1%
```

The two axes are nearly separable and the air axis is worth roughly **nine
times** the lateral one at comparable element cost. The air ladder alone, at
order 1:

```
v3cz    99546 tets   215.9080 pF   air: material interfaces only
v3a1   156666 tets   120.7545 pF   air step 20 mm
v3a2   213786 tets    95.3932 pF   air step 10 mm
v3a3   328026 tets    76.6165 pF   air step  5 mm
```

328k tetrahedra reach a better bound than the lateral ladder reached with
6.13M — **19× fewer elements**.

**This corrects §0.10.** That section is titled "Which axis: measured, and it is
the lateral one", and its measurement was sound within the family it varied.
But every mesh in that family held the air at a single 44.2 mm element, so it
identified the best of the axes it drove, not the best axis. The lateral axis
wins among mesh parameters that were on the table; the one that was not on the
table beats it ninefold. Nothing in §0.10's arithmetic is wrong and its
conclusion does not survive.

**Mesh quality is not the explanation.** All five meshes report an identical
`minimum_tetrahedron_determinant_m3` of 2.850e-18, set by the 10 µm copper
layer, and the minimum z gap is 10 µm in every one. Adding air levels created no
slivers; it removed a 44.2 mm one.

**But the air-refined meshes do not pass the gate.** Every rung with air levels
was rejected for positive off-diagonal entries, and the violation grows with air
refinement rather than shrinking:

```
v3a1   24 entries   worst  601 ppm of the smaller diagonal
v3a2   40 entries   worst  453 ppm
v3a3   50 entries   worst 7657 ppm
v3al   50 entries   worst 2220 ppm
```

Lateral refinement improves it (7657 → 2220 ppm at fixed air resolution) but
does not remove it. A positive Maxwell off-diagonal is non-physical, and the
run gate is right to refuse the run; the traces above come from the quarantined
CSVs and are diagnostic only. They remain valid *upper bounds* — the trace is
the Ritz energy per §0.20 regardless of off-diagonal sign — but the matrices
are not usable and the entrywise gate cannot be run on them.

So the axis that matters is identified and is not yet usable. **Diagnosing the
positive off-diagonals is now the blocking item**, ahead of any further ladder.
The likely mechanism is the loss of the M-matrix property on obtuse tetrahedra,
which extruded prism splitting produces freely once the vertical and lateral
scales are comparable; on `v3cz` at order 1 there are none, and orders 3, 4 and
5 there have none either, so it is not a simple function of resolution. Do not
relax the gate to get past this — see §6.

### 0.22 The air axis is usable after all — the positive off-diagonals are an order-1 artefact

§0.21 left the dominant axis identified but blocked: every air-refined rung was
rejected for positive off-diagonal entries. That blocker is gone, and it was
never a property of the mesh.

**Raise the order and they disappear.** Same air-refined mesh (`v3a1`, 156666
tets), direct solve, orders 1 to 4:

```
p    trace_pF     delta_pF     d_rel   contraction   pos_off   wall      RSS
1    120.7545            -         -             -        24    7.3 s       -
2     38.2289     -82.5256   -215.87%            -         0   17.8 s   9.3 GB
3     25.3968     -12.8321    -50.53%        0.155         0   54.5 s  12.6 GB
4     21.1762      -4.2207    -19.93%        0.329         0  360.2 s  18.0 GB
```

Orders 2, 3 and 4 produce **no positive entries at all**. The gate accepts them.

**Why**, and it was visible in §0.21's own data. Track one offending entry as
the discretisation improves:

```
C[15][8] fF   v3cz p1  -256.5   v3a1 p1  -40.9   v3a2 p1  -7.1   v3a3 p1  +15.8
              v3cz p3    -7.8   v3cz p5   -7.0
```

The order-1 solution on the coarse mesh overestimates this coupling **35-fold**.
As accuracy improves the error shrinks through the true value (near −7 fF) and,
for entries whose true magnitude is small, overshoots into positive territory.
The positive off-diagonals are order-1 error on weak couplings, not a mesh
defect — which is why they appear on the *more* accurate meshes and vanish when
the order rises rather than when the mesh coarsens.

That also explains the anti-monotone behaviour §0.21 recorded: the violation
"growing with air refinement" (601 → 453 → 7657 ppm) was the true couplings
shrinking toward their correct small values while the order-1 error floor stayed
put. The median off-diagonal magnitude falls from 9.96 fF on `v3cz` to 1.62 fF
on `v3a3` — a 6× shrink of the signal against a static error.

**A caution on reading the gate.** For as long as a campaign runs at order 1, the
positive-off-diagonal gate systematically rejects its *better* meshes and accepts
its worse ones. That is not a reason to weaken it — a non-physical matrix is not
a small error — but it is a reason never to read "no positive off-diagonals" as
evidence of accuracy. On this geometry it is closer to the opposite. The M-matrix
property that would guarantee the sign has never held here: measured across
`v3cz`, `v3lz`, `v3a3` and `v3al`, 21–26% of assembled P1 stiffness edges carry a
positive off-diagonal (`mmatrix.py`), including on every mesh that passes. The
discretisation cannot promise a physical matrix; it has only been delivering one.

**Best bound to date, and a second ladder that agrees on the shape.**

```
                     p1        p2       p3       p4       p5
v3cz   99546 tets  215.91    63.99    38.08    28.67    24.19
v3a1  156666 tets  120.75    38.23    25.40    21.18        -
```

21.18 pF is now the tightest upper bound on the canary trace, against §0.18's
order-1 Aitken limit of 90.14 pF — which is **4.3×** above it. Both ladders
contract in the same worsening pattern (0.171, 0.363 and 0.155, 0.329):
algebraic, not exponential, with the finest step still ~20% against a 2% + 1 fF
band. Neither has converged. The canary still has no value.

**One unexplained result.** `v3cz` at order 2 produced 16 positive entries
(largest 4 ppm) while orders 1, 3, 4 and 5 on that same mesh produced none.
`v3a1` shows the opposite pattern — order 1 bad, 2 onward clean. A single-order
anomaly on one mesh is not explained by the error-floor account above, and it is
recorded here unresolved rather than smoothed over.

**Cost, and a hard constraint on whoever runs this next.** These are full-system
direct factorisations on a 36 GB machine:

```
v3cz  order 3   13.4 GB      v3a1  order 2    9.3 GB
v3cz  order 4   14.8 GB      v3a1  order 3   12.6 GB
v3cz  order 5   20.9 GB      v3a1  order 4   18.0 GB
```

Running these back to back **kernel-panicked the machine** on 2026-08-26
(`watchdog timeout: no checkins from watchdogd in 92 seconds`), losing nothing
but costing a reboot. Two limits failed to prevent it and both should be
understood before the next campaign:

- `RESOURCE_LIMITS["pcb_diagnostic"]` permits 24 GiB — two thirds of this
  machine's RAM — and `process_monitor` enforces it by *polling*, so it reports
  an overshoot after the fact and cannot refuse one. It is a reporting threshold,
  not a guard.
- The system-wide OOM killer at `~/dev/crypto/jnb/apps/guards/macos_oom_guard.py`
  did not fire. Its swap trigger needs 58 GB (the panic came at 18.3 GB of swap);
  its level trigger needs two *consecutive* samples below 10 and resets its strike
  count on any sample above, which a burst allocation never satisfies; and its own
  once-a-minute heartbeat stopped roughly three minutes before the panic, so it
  was starved by the freeze it exists to prevent.

Do not run above order 3 without checking free memory first, and do not chain
these runs.

### 0.23 A real ladder in the real axis: 4 rungs, physical matrices, still not converged

§0.22 established that the air axis works at order 2 and above. This is the
ladder that follows from it — the first one in this document driven along the
axis that actually dominates, and the first whose every rung the run gate
accepts.

**Air ladder, order 2, four rungs** (`grade_p.py --axis vertical`), halving the
vertical step each time with the lateral mesh and the board stackup held:

```
rung          z step      tets   trace_pF   d_rel   pos_off   recip_F
v3a1-p2       20.0 mm   156666    38.2289       -         0  1.70e-26
v3a2-p2       10.0 mm   213786    32.3091  -18.32%        0  1.16e-26
v3a3-p2        5.0 mm   328026    28.2234  -14.48%        0  1.17e-26
v3a4-p2        2.5 mm   585066    25.7497   -9.61%        0  8.89e-27

trace:  NOT_CONVERGED — finest step 9.61% against 2% + 1 fF
        contraction 0.690, 0.605      observed order 0.72
matrix: NOT_CONVERGED — 56 of 171 entries outside the band
```

Reciprocity is 1e-26, eight orders inside the 1e-18 slack, and no rung has a
single positive off-diagonal. This is a clean ladder that simply has not
converged, which is a better position than any previous section reached.

**The observed order is 0.72, and that is the interesting number.** Halving the
element size in the driven direction should buy far more than that. Sub-linear
order has an obvious candidate here: at 2.5 mm vertical against a 16 mm lateral
element, the *lateral* mesh is now the coarse direction, so the ladder could be
converging to the other axis's error floor rather than to the answer. If true it
would invalidate the per-axis gate that §0.19's route 3 assumes.

**Tested, and it is not that.** The same ladder rebuilt on a 4× finer lateral
mesh (`max_planar_area_m2` 8e-6 rather than 1.28e-4):

```
                        lateral   contraction   observed order
v3a1 / v3a2 / v3a3       16 mm          0.690             0.54
v3b1 / v3b2 / v3al        4 mm          0.687             0.54
```

Identical to three digits in the ratio. Sixteen times the lateral elements
changes the air axis's convergence *rate* not at all — it only shifts the whole
curve down. So the axes are separable in rate, the sub-linear order is intrinsic
to the vertical direction, and route 3's per-axis gate is not invalidated. That
is a negative result and it is worth as much as the ladder: it was the cheapest
available reason to distrust every single-axis ladder in this document, and it
does not hold.

Note the order is also *improving* as the ladder refines — contraction 0.690 then
0.605, i.e. order 0.54 then 0.72. The ladder is still pre-asymptotic at 585k
tetrahedra.

**The grid, and what it says the answer is.** Mesh against element order, every
cell a completed solve, `!of` = rejected for positive off-diagonals, `!RS` =
rejected at the 24 GiB ceiling:

```
mesh    air z    tets           p1           p2        p3        p4        p5
v3cz     none   99546     215.91       63.99!of    38.08     28.67     24.19
v3lz     none  137856     200.66            -         -         -         -
v3a1    20 mm  156666     120.75!of     38.23     25.40     21.18         -
v3a2    10 mm  213786      95.39!of     32.31     22.69     19.70!RS      -
v3a3     5 mm  328026      76.62!of     28.22     20.95         -         -
v3a4   2.5 mm  585066           -       25.75         -         -         -
v3al     5 mm  436320      68.00!of     26.37         -         -         -
```

Extrapolating each row in *p* and each column in air step, independently:

```
p-ladders      v3cz 20.10   v3a1 19.11   v3a2 18.34   v3a3 19.67  pF
air-ladders    order 2 19.12   order 3 17.85   (4 mm lateral) 17.75  pF
```

Seven independent extrapolations across two axes land between **17.8 and
20.1 pF**. None is a bound and none passes the gate; what makes them worth
recording is that they were reached along different axes at different orders and
they agree.

**The one number that is a bound**: 20.9544 pF, from `v3a3` at order 3, an
accepted run. By §0.20 every accepted trace bounds the truth from above, and
this is the smallest one measured. Against §0.18's order-1 Aitken limit of
90.14 pF, which the h-ladder was converging toward, that is a factor of 4.3.

**Why neither axis can close the gate on this machine.** At the measured
contraction ratios, and with the memory each rung costs:

```
air axis at order 2, contraction ~0.605:  9.61% -> 5.8 -> 3.5 -> 2.1 -> 1.3%
   four more rungs; v3a4 already costs 21.3 GB of a 24 GiB ceiling
p axis at fixed mesh, contraction ~0.31:  needs p5-p6
   v3a2 at p4 was killed at the ceiling
```

Both run out of memory before they run out of ladder. `v3a4-p2` at 21.3 GB and
`v3al-p2` at 20.2 GB are the largest solves this resource class admits, and
neither axis is within four rungs of the band.

**A note on reading rejected runs.** `v3a2-p4` produced a complete matrix
(19.6965 pF) and was then killed for exceeding the RSS ceiling; the harness
quarantined the CSV. That number is in the grid above marked `!RS` and it is not
a result. `grade_p.py` refuses to grade a rejected run at all rather than reading
the quarantined copy, because reading it back is precisely how a refused result
gets laundered into a ladder. An earlier draft of the grid here reported
`v3a2-p4` as the tightest bound available, having keyed on the matrix rather
than on the run's own verdict; that was wrong and this is the correction.

### 0.24 The trace converges — grading the vertical band was the whole difference

§0.23 left both axes needing four more rungs than memory allows. The reason was
not the axis, it was that the elements were in the wrong place, and the API that
would have put them in the right place was broken.

**The band was a no-op.** `_refine_levels` skipped a gap only if it lay entirely
outside `vertical_refinement_band_m`; a gap that merely *overlapped* was
subdivided over its whole length. The base levels come from the stackup, so the
canary's air gap is a single 44 mm span, and therefore **every band narrower than
44 mm did nothing at all** — silently, no error. A ±20 mm band produced a level
set identical to no band: 39 levels either way. Fixed in `cab1d39` so the band
clips to its own extent; a band that misses the model now raises rather than
reporting a refined mesh that was never refined. All 14 existing canary meshes
still validate against their stored hashes, because every band used before this
was either stackup-aligned or full-domain.

**The error is near-board, and there is an optimum.** Same element budget, spent
four ways, order 2:

```
mesh           band        z step      tets   trace_pF
v3a4      full 90 mm      2.50 mm    585066    25.7497
v3g1       +/- 20 mm      1.84 mm    385146    25.3213
v3g2       +/-  5 mm      0.50 mm    378234    20.6158   <- optimum
v3g3       +/-  2 mm      0.20 mm    369018    21.1804
```

A graded 385k-tetrahedron mesh beats a uniform 585k one, and the ±5 mm band is
better than both its neighbours — so the field structure that matters extends
about 5 mm from a 1.6 mm board, roughly three board thicknesses, and everything
beyond that can be a single element 25 mm tall. Narrowing further starves the
transition and gets worse again.

**The graded ladder, order 2, five rungs, ±5 mm band:**

```
rung          z step       tets   trace_pF    d_rel   pos_off   recip_F
v3h1-p2       2.000 mm   170946    25.2223        -         0  2.71e-26
v3h2-p2       1.000 mm   240042    21.8779  -15.29%        0  8.78e-27
v3g2-p2       0.500 mm   378234    20.6158   -6.12%        0  7.57e-27
v3h4-p2       0.250 mm   656922    20.2321   -1.90%        0  7.47e-27
v3h5-p2       0.125 mm  1214298    20.0760   -0.78%        0  1.33e-26

trace:  CONVERGED — finest step 0.78%, inside the 2% + 1 fF band
        contraction 0.377, 0.304, 0.407     observed order 1.30
        Aitken limit 19.9691 pF
matrix: NOT_CONVERGED — 8 of 171 entries (was 37 at four rungs)
```

**This is the first ladder in this document to pass anything.** Against the same
axis run uniformly, which reached observed order 0.72 and a 9.61% finest step,
grading alone moved the order to 1.30 and the finest step to 0.78%. Nothing else
changed: same geometry, same lateral mesh, same solver, same order.

Order 3 on the same band is tighter still and one rung shorter for the memory:

```
v3h1-p3   2.000 mm   170946   19.8193
v3h2-p3   1.000 mm   240042   18.8054   -5.39%
v3g2-p3   0.500 mm   378234   18.3979   -2.22%     contraction 0.402, order 1.32
```

**18.3979 pF is now the tightest accepted upper bound on the canary trace**,
against §0.18's order-1 Aitken limit of 90.14 pF — a factor of 4.9 — and its own
Aitken limit of 18.1240 pF sits inside the 17.8–20.1 pF band that §0.23's seven
independent extrapolations pointed at.

**What the last 8 entries are, and why this ladder cannot close them.** They are
not spread over the matrix; they sit on terminals 6, 8, 9 and 15, which are the
smallest conductors on the board:

```
Net-(U1-VCCI-Pad3)      2.35 x 6.95 mm
unconnected-(U1-DIS-Pad5)  1.95 x 0.60 mm
unconnected-(U1-DT-Pad6)   1.95 x 0.60 mm
unconnected-(U1-NC-Pad7)   1.95 x 0.60 mm
```

0.6 mm SOIC pads in a mesh whose free lateral element is 16 mm. That predicts
they are lateral-limited, and the matched pair `v3a3`/`v3al` (identical but for a
4× lateral refinement) confirms it for the worst of them:

```
                 16 mm lat    4 mm lat   change
C[8][9]           -217.71f    -105.30f    51.6%   still failing
C[8][8]            599.22f     404.66f    32.5%   still failing
C[0][0]           5685.66f    5249.54f     7.7%   converged
C[1][3]          -1169.75f   -1073.37f     8.2%   converged
```

The failing entries move 32–52% under lateral refinement against 7–19% for
converged ones. A vertical ladder cannot close them however far it runs.
(`C[6][15]` moves 0.1% and is the exception; whatever limits that one is neither
axis and is not yet identified.)

**Next**: graded *lateral* refinement around the small pads, which the library
already supports via `conductor_edge_band_m` / `conductor_edge_max_planar_area_m2`
and which the `v3gl*` meshes already exercise — combined with the ±5 mm vertical
band. That is the remaining axis, and it is the one the last 8 entries name.

### 0.25 Both axes graded: the trace passes twice, and the entrywise gate does not

§0.24 named graded lateral refinement as the remaining axis, because the last 8
failing entries all sat on 0.6 mm SOIC pads. This is that ladder, and the state
it leaves the canary in.

**Graded in both axes** — 4 mm lateral base with a 0.2 mm conductor-edge band at
3.2e-7, plus the ±5 mm vertical band, order 2:

```
rung          z step       tets   trace_pF    d_rel   pos_off   recip_F
v3k0-p2       2.000 mm   266994    23.4470        -         0  3.80e-26
v3k1-p2       1.000 mm   371790    20.3231  -15.37%        0  3.31e-26
v3k3-p2       0.500 mm   581382    19.2266   -5.70%        0  1.56e-26
v3k4-p2       0.250 mm  1002870    18.9184   -1.63%        0  9.49e-27

trace:  CONVERGED — finest step 1.63%, contraction 0.351, 0.281
        observed order 1.83      Aitken limit 18.7979 pF
matrix: NOT_CONVERGED — 37 of 171 entries
```

Observed order 1.83, the best of any ladder here, against 0.72 for the same axis
run uniformly. **Two ladders now pass the trace gate** — this one and §0.24's
five-rung vertical ladder — where before §0.24 none ever had.

**The fifth rung of this ladder does not fit, and it is time rather than
memory.** `v3k5` at 1845846 tetrahedra was killed at the 1800 s wall with peak
RSS 22.23 GB, comfortably inside the 24 GiB ceiling. The finer lateral mesh
costs fill-in: `v3h5` at 1.21M tets solved in 344 s, `v3k5` at 1.85M did not
finish in 1800 s. That fifth rung is what took the vertical ladder from 37
failing entries to 8, so the combined ladder is one affordable rung short of its
best result, and the binding limit is the resource class's wall clock.

**Where the canary stands.**

```
                          rungs   finest step   observed order   failing entries
uniform vertical, p2         4        9.61%             0.72        56 of 171
vertical graded, p2          5        0.78%             1.30         8 of 171
vertical graded, p3          3        2.22%             1.32        43 of 171
both axes graded, p2         4        1.63%             1.83        37 of 171
```

Thirty accepted runs. The tightest upper bound on the trace is **18.3979 pF**
(`v3g2` at order 3), and every extrapolation from every axis and order now lands
between 17.8 and 20.1 pF. For comparison, §0.18's order-1 h-ladder was
converging toward 90.14 pF, which §0.20 showed to be excluded by a factor of
nearly five.

**The gate is still not passed and the canary is still not qualified.** The
entrywise rule is the one §1 states, the trace is only a summary, and no ladder
has brought all 171 entries inside 2% + 1 fF. What has changed is that the
failure is now specific and small rather than general: 8 entries, all on the
board's smallest conductors, in a ladder whose other 163 entries converge.

**Next, in order:**

1. The fifth rung of the combined ladder. It is a 1800 s wall, not memory, and
   the run was progressing steadily at 22.2 GB when it was cut. Deciding whether
   to extend that budget is a resource-policy call and is left to the owner —
   note the wall is what stopped a *healthy* run, unlike the 24 GiB ceiling,
   which stopped `v3a2-p4` and `v3k5`'s memory never approached.
2. Combined grading at order 3, which reached the tightest bound per rung of
   anything measured (`v3g2-p3`, 378k tets, 18.3979 pF, 21.5 GB).
3. ~~`C[6][15]` remains unexplained.~~ **Withdrawn the same day.** Tracing it
   across the whole vertical ladder rather than reading its final percentage
   shows it converging cleanly:

   ```
   C[6][15]  -159.417  -118.932  -102.731  -97.853  -95.824 fF
   deltas      +40.485   +16.201    +4.878   +2.029
   ratios                   0.400     0.301    0.416
   ```

   Its finest step is 2.12% against a 2% band — the most marginal entry in the
   matrix, not an anomalous one. Being unmoved by lateral refinement is not a
   puzzle either: it means the entry is already converged in that axis. I called
   it unexplained on the strength of two numbers without looking at the
   sequence.

**And the combined ladder is doing what it was built to do.** Comparing the same
entries across the two ladders shows lateral grading working on exactly the
entries §0.24 predicted:

```
              vertical graded, 5 rungs    both axes graded, 4 rungs
C[8][9]       -67.378 fF, step 8.01%      -47.243 fF, step 2.70%
C[6][8]       -64.112 fF                  -54.718 fF
C[6][15]      -95.824 fF, step 2.12%      -95.165 fF
```

`C[8][9]` — the worst entry in the matrix and a 0.6 mm pad — improves from 8.01%
to 2.70% with one *fewer* rung once the lateral mesh is graded, while `C[6][15]`,
which was already lateral-converged, lands on the same value from both. That is
the signature of the diagnosis being right, and it puts the fifth rung of the
combined ladder within reach of the entrywise band rather than merely closer to
it.

**Do not** read the trace passing as the canary passing, and do not reorder the
gate: Fugu stays downstream of an entrywise pass, per §1.

### 0.26 The 24 GiB ceiling does not bound these runs, because RSS is the wrong metric

Combined grading at order 3 is the tightest result per rung so far — `v3k1-p3`,
371790 tets, **18.1573 pF**, now the tightest accepted upper bound on the canary
trace, and the order gap is closing (p3-p2 is -4.37 pF at the 2 mm rung, -2.17 at
1 mm). The third rung, `v3k3-p3` at 581382 tets, does not exist and should not be
attempted again as configured.

**What happened.** It ran about 25 minutes and was killed with no rejection
manifest — neither Palace's resource monitor nor the system OOM guard stopped
it. The reason is in the guard's own log, and is only visible there because the
heartbeat was changed on 2026-08-27 to carry the interval's peak rather than an
instantaneous reading:

```
02:46:42  level=35  swap=31.0GB   peak swap 31.0GB
02:57:47  level=35  swap=36.9GB   peak swap 37.1GB
03:01:48  level=34  swap=37.2GB   peak swap 37.8GB
03:02:48  level=83  swap= 6.0GB   peak swap 38.1GB   <- run gone
```

**38.1 GB of swap on a 36 GB machine, held for 25 minutes.** More swap than
physical memory, sustained. That is the condition the OOM guard exists to
prevent, and by its own rules it was right not to fire: `memorystatus_level`
never dropped below 32, and 38.1 GB is 0.9 GB short of the 39 GB absolute swap
arm. It came within a gigabyte.

**The ceiling that should have stopped it measures the wrong thing.**
`RESOURCE_LIMITS["pcb_diagnostic"]` caps *peak RSS* at 24 GiB. A process being
swapped keeps its resident set below that cap indefinitely while its actual
memory demand goes into swap — the resident set is precisely the part that stays
in RAM. So the cap is not merely late (§0.22: it samples, so it reports rather
than prevents), it is measuring a quantity that **falls** as the situation gets
worse. The system OOM guard's own docstring says this in as many words: "RSS of a
swapped hog collapses; phys_footprint is the honest one." The Palace resource
policy limits the one that collapses.

Every peak-RSS figure in §0.23-§0.25 should be read with that in mind. They are
real, and they are not a measure of how close a run came to the machine's limit.
`v3k1-p3` is reported at 22.56 GB peak RSS and completed — but the swap window
above opens at 02:38, which covers it, so it too was running against swap. Its
*number* is unaffected (swapping costs time, not accuracy, and the solve was a
direct factorisation run to completion), but its cost classification was wrong.

**Consequences for anyone continuing this work:**

- Do not treat "peak RSS under 24 GiB" as evidence a run was affordable. Check
  `sysctl vm.swapusage` during the run, or read the guard's peak-swap heartbeat
  afterwards.
- Combined grading at order 3 is affordable to about 372k tetrahedra on this
  host and not at 581k. The order-2 combined ladder reaches 1.00M (`v3k4`,
  21.4 GB, no swap excursion) and its fifth rung at 1.85M is wall-limited.
- The right fix for the resource policy is to bound `ri_phys_footprint` rather
  than RSS, which is the metric jetsam and Activity Monitor use. That is a change
  to `lib/process_monitor.py`, shared with other campaigns, and is left to the
  owner rather than made here.

### 0.27 The wall is raised, on evidence, and only the wall

§0.25 left the fifth rung of the order-2 combined ladder (`v3k5`, 1.85M tets)
blocked and called extending the budget an owner decision. The owner made it.
This section records what was changed and why that particular number.

**The rung was not expensive, it was slow.** `v3k5-p2` run-01 was killed at
1801.9 s with `peak_rss_bytes` 22.23 GB against a 24 GiB ceiling and
`limit_failures: ["wall time exceeded 1800s"]` — no memory failure, no solver
failure, five of eighteen right-hand sides solved and every one of them at
`explicit_relative_residual` ~1e-15 against a 1e-10 target. It is the only run in
this campaign that was stopped by the clock while healthy.

**The wall is a measurement.** Its own `palace_progress_events` give the
projection directly:

| interval | seconds |
| --- | --- |
| launch → `setup_end` | 30.8 |
| → `field_solve_started` | 57.4 |
| → terminal 1 solved (this interval *is* the factorisation) | 866.5 |
| per terminal thereafter (2,3,4,5) | 215.5, 219.3, 219.6, 207.4 |

The per-terminal cost is flat across four terminals, so the run was not
degrading — 866.5 + 17 × 215 = **4522 s**. That is 2.5× the old wall and the
reason nothing was going to finish inside it.

**What landed** (`lib/palace.py`): a new resource class `pcb_convergence`, wall
7200 s, **every other bound identical to `pcb_diagnostic`** — 24 GiB peak RSS,
10 GiB output, same mesh envelope. 7200/4522 = 1.59, above the policy's
`RESOURCE_MINIMUM_HEADROOM_RATIO` of 1.1. `probe.py` takes the class as its
fifth argument and defaults to `pcb_diagnostic`.

**Why a new class and not a wider old one.**
`_validate_palace_run_manifest_unattested` re-derives `expected_resource_limits` from the live
`RESOURCE_LIMITS[resource_class]` table and rejects any manifest that disagrees
(`lib/palace.py:839`). Widening `pcb_diagnostic` would therefore have failed
every accepted run already on disk — the whole ladder, retroactively. The table
is part of each run's identity, and that is deliberate.

**Guard review checklist** (global rule; a limit is a guard):

1. *Unevaluable input.* Unchanged. A run that produces no manifest is still
   rejected; the wall is a ceiling on an observed elapsed time, and elapsed time
   is always observable.
2. *Monotonicity.* A longer run is still worse: `elapsed_s > wall_time_s` fails,
   and `bounded_values` in `validate_palace_run_manifest` independently refuses
   any accepted manifest whose `elapsed_s` exceeds the class ceiling. There is no
   input at which more wall time becomes a PASS.
3. *Preconditions.* The class must exist in **both** `RESOURCE_LIMITS` and
   `MESH_LIMITS`, or `_validate_palace_run_manifest_unattested` raises `KeyError`
   rather than rejecting. Both were added.
4. *Source of truth.* The limits are read from the module, not copied into the
   manifest and trusted; the manifest's own copy is compared against the live
   table on every validation.
5. *Persistence.* Nothing is cached. run-01's rejection manifest stays on disk
   next to run-02; `grade_p.py` reads the newest run and would refuse it if it
   were rejected.
6. *Provenance.* `resource_class` is recorded in every run manifest and in
   `probe.py`'s output, so a rung run under the wider wall is identifiable as
   such forever. No run is reclassified.
7. *Known-bad calibration.* The known-bad case is run-01 itself: 1801.9 s under a
   1800 s wall, rejected, and still rejected — it is not re-graded by this
   change. `test_the_convergence_wall_covers_the_measured_projection` asserts the
   old class does **not** cover the projection, which is the fact that made the
   new one necessary.
8. *Fix vs mute.* This is the distinction that matters here. Raising a wall on a
   run that was still solving correctly changes the quantity (18 solved terminals
   instead of 5). Raising the *memory* ceiling would have been the mute button —
   the machine has already been lost once to swap, and §0.26 shows the RSS metric
   collapses exactly when things get bad — so the memory ceiling did not move,
   and a test pins it equal to `pcb_diagnostic`'s.

*Runtime cost of the change itself:* none. It adds one entry to a dict read once
per run.

**One thing this does not fix.** The wall now permits a run that swaps for two
hours. The RSS ceiling still cannot see that (§0.26), so the wall's new length is
underwritten by the OOM guard's peak-swap heartbeat and by checking
`kern.memorystatus_level` before launch — not by the resource policy. Launch
conditions for run-02 were level 87, swap 3.4 GB of 5 GB, guard running.

### 0.28 The wall was never the binding constraint — memory was, and still is

The rung was launched under the new class at 07:00 and stopped externally at
07:29, 4 of 18 terminals in. Two facts come out of those 29 minutes, and the
second one matters more than the first.

**The wall projection was right.** Four terminals at 1636 s against run-01's
1520.8 s for the same four — 7% slower, flat, no degradation trend. Extrapolated
completion ~4800 s, comfortably inside 7200 s. If time had been the only cost,
§0.27's budget would have delivered the rung.

**The machine could not afford the duration.** The OOM guard's peak-swap
heartbeat, the only instrument that can see this at all:

```
07:23:28  level=35  swap=34.5GB   peak swap 36.4GB
07:27:29  level=35  swap=35.0GB   peak swap 37.3GB
07:28:30  level=34  swap=35.2GB   peak swap 38.5GB   <- stopped here
```

**38.5 GB of swap on a 36 GB machine — above the 38.1 GB in §0.26 that preceded
the kernel panic.** Peak RSS across the eight ranks read about 6 GB at the time,
against a 24 GiB ceiling, because the rest was swapped out. The ceiling saw a run
using a quarter of its budget while the machine was in the worst memory state it
has ever been measured in.

**What this changes about §0.27.** Nothing in it is wrong — the wall really was
what stopped run-01, the projection really does check out, and a longer wall is
the correct fix for a run that is merely slow. But it was the wrong *first* fix,
because it lengthens exposure to a hazard the resource policy cannot measure.
The order of operations should have been: bound `ri_phys_footprint` first, then
extend the wall. A two-hour budget is only spendable once the thing spending it
can be stopped for the right reason.

**So the fifth rung is not blocked on wall time and never was.** It is blocked on
`lib/process_monitor.py` bounding a metric that does not collapse under swap
(§0.26), or on running this rung somewhere with more physical memory. The
`pcb_convergence` class stands and is correct on its own terms; it is simply not
sufficient, and nothing should be launched under it on this host until the
memory metric is fixed.

Empty run directory and its evidence: `v3k5-p2/run-02/WHY-THIS-RUN-IS-EMPTY.txt`.

### 0.29 The memory ceiling now measures memory

§0.26 identified the metric defect and left it to the owner because
`lib/process_monitor.py` is shared. The owner called it. This is what changed.

**The measurement.** `_tree_rss` became `_tree_memory` and sums the platform's
memory *footprint* instead of resident set size — on Darwin `ri_phys_footprint`
from `proc_pid_rusage`, the quantity jetsam kills on and Activity Monitor labels
"Memory"; on Linux `VmRSS + VmSwap`. Neither falls when pages leave RAM, which
is the entire property RSS lacked.

**What did not change: the serialized field is still `peak_rss_bytes`.** That is
deliberate and it is the one thing to know before reading any number here. The
name is baked into the resource-sample schema of every run manifest on disk, all
of which are validated against an exact key set, so renaming it would have
failed every accepted run in this campaign retroactively — the same trap §0.27
avoided with the resource class. The field name is legacy; `MEMORY_METRIC` in
`process_monitor` names the quantity, the failure string is now "peak memory
footprint exceeded", and this paragraph is the third place it is written down.

**Peak figures across the change do not compare.** Footprint excludes clean
file-backed pages, so a healthy run measures somewhat *below* its RSS; it
includes compressed and swapped pages, so a swapping run measures far above.
Every peak in §0.23–§0.26 is RSS. Do not put them in a column with anything
measured after this commit.

**Unevaluable now raises.** The old loop caught a failed read per member and
carried on, returning a smaller total — fail-open at exactly the moment the
machine is least able to answer questions about processes. A member that cannot
be measured now raises `MemoryMetricUnavailable`, the tree is killed, and no
witness is written at all. `ESRCH` is the single error mapped to zero, because a
process that has exited occupies nothing.

**Guard review checklist:**

1. *Unevaluable input.* Raises. A run whose peak memory is unknown has not been
   shown to fit under a ceiling — it has only failed to be shown to exceed one.
   Test: `test_an_unreadable_member_raises_rather_than_counting_as_zero`.
2. *Monotonicity.* `peak_memory` is a running max and the witness validator
   already rejects non-monotonic samples. A worse memory state cannot produce a
   smaller number — which is precisely what RSS could do.
3. *Preconditions.* `require_memory_metric()` is called before `Popen`, so a
   platform with no footprint metric is a launch error rather than a run that
   quietly went unbounded. Test:
   `test_an_unmeasurable_platform_refuses_to_launch_at_all`.
4. *Source of truth.* The struct offset is the thing most likely to be silently
   wrong — a wrong one returns a plausible number from `ri_wired_size` or
   `ri_pageins` next door. `test_the_struct_offset_really_is_the_footprint_field`
   reads the neighbouring `ri_resident_size` and requires it to reproduce
   psutil's RSS exactly, which pins the layout.
5. *Persistence.* Nothing is cached; every sample is a fresh read.
6. *Provenance.* `MEMORY_METRIC` is exported and asserted; the failure message
   names the quantity; `test_the_memory_failure_no_longer_claims_to_be_about_rss`
   fails if anyone restores the old wording.
7. *Known-bad calibration.* `test_the_footprint_tracks_dirty_anonymous_memory`
   allocates 192 MB of touched anonymous pages — the kind that get compressed and
   swapped — and requires the metric to move with them. The existing end-to-end
   breach tests now exercise the footprint path.
8. *Fix vs mute.* This changes the bounded quantity, not the reporting of it.
   The ceiling value did not move.

*Runtime cost:* `proc_pid_rusage` measures at **1.12 µs/call against psutil
`memory_info().rss` at 1.55 µs** — 0.73×, on 20000 calls each. The honest metric
is the cheaper one. At nine tracked members that is 10 µs per sample.

**Measured on the real workload, and it is not a small correction.** `v3k0-p2`
was re-run twice under the new monitor with a second sampler reading both
quantities over the same eight ranks:

| run | summed RSS | summed footprint | ratio |
| --- | --- | --- | --- |
| run-01 (2026-08-27, old metric) | 15.82 GB | — | — |
| run-02 | 15.41 GB (sampler) | 6.80 GB | 0.43 |
| run-03 | 15.41 GB (sampler) | 5.85 GB | 0.38 |

**Summed RSS over an MPI tree counts shared pages once per rank.** The shared
libraries, the memory-mapped 100 MB mesh, and MPI's shared segments are each
counted eight times over. Footprint attributes them once. So the old number was
not merely blind to swap — for a healthy run it was inflated by roughly 2.5×.

**That ratio does not extrapolate, and the ceiling is not too loose — measured.**
The obvious inference from a single 0.38 ratio is that 24 GiB has become ~2.5×
more permissive and should be lowered to about 9 GiB. That inference is wrong,
and re-running the upper rungs shows why. The shared-page overcount is roughly a
fixed quantity; the factorisation's private memory is what grows. So the ratio
climbs with the rung, and past the point where the machine starts compressing,
it crosses one:

| rung | tets | old peak RSS | new peak footprint | footprint / RSS |
| --- | --- | --- | --- | --- |
| `v3k0-p2` | 266994 | 15.82 GB | 6.80 GB | 0.43 |
| `v3k3-p2` | 581382 | 20.99 GB | 18.90 GB | 0.90 |
| `v3k4-p2` | 1002870 | 21.38 GB | **24.49 GB** | **1.15** |

**This is the §0.26 defect caught in the act.** Between 581k and 1.00M
tetrahedra the summed RSS goes essentially flat — 20.99 to 21.38 GB, a 2% rise
for a 72% larger problem — because the additional memory is being compressed
rather than kept resident. Footprint over the same step rises 30%. RSS was not
merely a different number; it had stopped responding to the workload.

**`v3k4-p2` is now rejected: 24.49 GB against the 24 GiB ceiling.** That rung is
in the four-rung ladder of §0.25. Its recorded result stands — the matrix was
produced by a direct factorisation that ran to completion, and swapping costs
time rather than accuracy — but the run is not reproducible on this host under
an honest ceiling, and `grade_p.py` now refuses to grade that ladder because the
newest run under the tag is a rejection. That refusal is the intended design and
it is telling the truth.

So the ceiling needs no recalibration. **24 GiB of footprint is approximately
this machine's real limit for this workload**, which is why the runs that
exceeded it are exactly the runs that drove the machine into swap. The number
was accidentally right and the metric was wrong.

**One link in the chain is argued, not measured.** That footprint counts pages
the compressor has written to the swap file — not merely pages it has compressed
in RAM — is the property that makes it catch the §0.26 condition, and verifying
it directly requires recreating the state that panicked the machine. The support
for it is that jetsam ranks victims on this counter and could not function
otherwise, and that the system OOM guard on this machine says so in as many
words. Treat it as strongly evidenced rather than as measured here.

**Run-to-run reproducibility, checked in passing.** The three `v3k0-p2` matrices
are not bit-identical — MPI reduction order is not fixed — but they agree to
`max|ΔC| = 1.6e-24 F`, which is `1e-9` fF, nine orders below the 1 fF gate. The
traces agree to all six printed digits at 23.446952 pF. Ladder gradings are
unaffected by which run is read.

**The consequence for this campaign.** Runs that previously passed the ceiling
while swapping can now be killed by it, which is the guard working. Results
already on disk were accepted under the old metric and are unaffected; what
changes is what can be run next — and, given the 2.5× measured above, more of it
than before rather than less, until the ceiling value is revisited.

### 0.30 Distance to the goal: one rung in convergence, one machine in memory

> **PARTIALLY RETRACTED (§0.31, 2026-08-27).** The convergence half of this
> section stands. The memory half — and the escape-route list it ends with —
> rested on §0.19's reading of a reduction factor, which was wrong. "One machine
> in memory" is the cost of a *direct* solve, and the direct solve is not the
> only one available. Read §0.31 with this.

Two separate distances, and they have different answers.

**Convergence: one rung, and it is projectable.** Of the 171 entries in the
four-rung combined ladder, 18 unique entries (37 counting both triangles) sit
outside 2% + 1 fF. Every one of them is contracting, ratios 0.254 to 0.348, and
**none has a ratio at or above 1**. Projecting each failing entry's next step as
`step x ratio` and comparing it to its own band:

| entry | C (fF) | step (fF) | x band now | ratio | x band projected |
| --- | --- | --- | --- | --- | --- |
| C[0][16] | -140.41 | 6.76 | 1.78 | 0.277 | 0.49 |
| C[6][9] | -55.30 | 3.34 | 1.59 | 0.347 | 0.55 |
| C[6][15] | -95.17 | 4.54 | 1.56 | 0.270 | 0.42 |
| C[6][8] | -54.72 | 3.19 | 1.52 | 0.348 | 0.53 |

The worst projected entry lands at **0.55 of its band** — roughly 2x margin. One
more halving of the vertical step closes the entrywise gate on every entry, and
the trace step goes from 1.63% to about 0.46%. This is the least uncertain thing
in this document: nothing is stalling, nothing is oscillating, and the required
rung is a single named computation.

**Memory: the ladder is already over budget, so the rung is not one step away.**
§0.29 measured `v3k4-p2` — the ladder's existing top rung — at 24.49 GB of
footprint against a 24 GiB ceiling. The fifth rung is 1.85x that mesh with
superlinear factorisation growth. The 38.5 GB of swap it drove (§0.28) is the
direct evidence of what it needs, and it is more than this machine has.

Every number in that paragraph is a property of the **full-system direct
factorisation**, not of the fifth rung. It is the factorisation's fill-in that
grows superlinearly and that no longer fits; the discretisation itself is a few
hundred MB. §0.31 measures what the same rung costs without one.

**The escape routes, and which are already closed:**

- *Iterative solve instead of a factorisation.* ~~**Measured closed.**~~
  **This entry was wrong on both counts and is retracted — see §0.31.** BoomerAMG
  at order 2 does not stall, and the p-multigrid hierarchy this list called
  untested was already running in the very measurement quoted against it. This
  is the open route, not a closed one.
- *A smaller refinement step for the fifth rung* (sqrt(2) rather than 2, ~1.3M
  tets). The gate's observed-order calculation uses the actual size ratio, so
  this is arithmetically legal — and it should be **refused**, because it shrinks
  the finest step without proving anything more about the solution. It is the
  gate-weakening §6 warns about, wearing a mesh parameter as a disguise.
- *A machine with more memory.* Open, and — until §0.31 — believed to be the
  only route that was neither closed by measurement nor a dodge. It is now the
  fallback rather than the plan.
- *A solver that does not factorise the whole system at order 2.* ~~Open and
  untested~~ — **open and, as of §0.31, partially measured.** The claim here
  that "Palace's p-multigrid hierarchy was disabled for these runs
  (`multigrid_max_levels=1`)" is false for the BoomerAMG run it was reasoning
  about: that config came from the older zladder writer, which emitted no `MG*`
  keys at all, so Palace applied its own default and built the hierarchy
  (`Level 0 (p = 1)`, `Level 1 (p = 2)`). `multigrid_max_levels=1` was pinned
  only on the *SuperLU* runs, where it is what makes the solve direct.

**Beyond the canary.** Fugu is 82 terminals against the canary's 18, on a larger
board, and §1 puts it strictly downstream of an entrywise canary pass. The solve
cost is per right-hand side — 215 s each at v3k5 size — so the terminal count
alone is a 4.5x multiplier on top of a larger mesh. Nothing about the canary's
resource picture makes Fugu look reachable on this host either. Treat the canary
gate as the near goal and Fugu as a separate procurement question.

### 0.31 BoomerAMG at order 2 never stalled — §0.19 misread an iteration cap

§0.19 recorded one line of Palace output, `avg. reduction factor: 9.489e-01`,
and concluded from it that "the preconditioner is doing essentially nothing at
order 2". §0.30 inherited that as a **closed** escape route and built its
"one machine in memory" verdict on top of it. Both are wrong. The run that
produced the number is still on disk
(`out/palace-qualification/simple-hb-zladder-v1/v3l0-boomeramg/run-02`), and it
says something quite different.

**First: the p-multigrid hierarchy was already running.** §0.30 listed "a proper
p-multigrid hierarchy" as the cheapest untested experiment left, on the grounds
that `multigrid_max_levels=1` had disabled it. That pin is applied only to
SuperLU runs — it is what makes the solve direct. The BoomerAMG config in
question was written by the older zladder writer, whose entire `Solver.Linear`
block is five keys:

```json
{"KSPType": "CG", "MaxIts": 500, "Tol": 1e-12,
 "Type": "BoomerAMG", "VerificationTol": 1e-10}
```

No `MG*` keys at all, so Palace applied its own defaults, and its own default
builds the hierarchy. From the run's stdout:

```
Assembling multigrid hierarchy:
 Level 0 (p = 1): 77794 unknowns
 Level 1 (p = 2): 568114 unknowns
```

Two levels, p=1 coarsening under p=2, with AMG at the bottom. That *is* the
experiment §0.30 proposed, and it had already been run — against itself.

**Second: it did not stall.** The residual histories, three of the eleven
solves, 500 iterations each:

| solve | it 0 | it 500 | total reduction | rate over the last 50 its |
| --- | --- | --- | --- | --- |
| 1 | 2.9056e+01 | 6.6270e-10 | 2.28e-11 | 0.9409/it |
| 2 | 9.2051e+01 | 1.0297e-09 | 1.12e-11 | 0.9265/it |
| 3 | 9.8829e+00 | 5.4244e-10 | 5.49e-11 | 0.9553/it |

That is 9.4 to 10.3 orders of magnitude of monotone reduction. A preconditioner
"doing essentially nothing" leaves the residual near its initial value; this one
converged, steadily, and was still converging when it hit the cap. Extrapolating
each solve's own late-stage rate, `Tol = 1e-12` was **32 to 88 further
iterations** away — a run that was between 6% and 18% short of its own stopping
criterion, not one that had given up.

**The decisive number was in the rejection record all along.** Palace checks the
*explicit* residual separately from the Krylov one, and the workflow's
acceptance threshold for it is `explicit_residual_tolerance = 1e-10`. Every one
of the ten capped solves reported:

```
Linear solver did not converge, norm(Ax-b)/norm(b) = 4.168e-12  (norm(b) = 1.590e+02)
                                                     6.693e-12
                                                     5.291e-12   ... 3.810e-12 to 8.049e-12
```

Between 3.8e-12 and 8.0e-12 — **more than an order of magnitude inside the
tolerance this pipeline demands**, on all ten. Those solutions were accurate.
They were rejected because CG's stopping test, in the preconditioned B-norm at a
stricter 1e-12, had not yet tripped.

**So the failure was three settings, all of them ours:** `Tol = 1e-12` in a norm
that is not the one we accept on; `MaxIts = 500`, a few dozen iterations short of
meeting it; and the 1800 s wall, which took the run at 10 of 18 right-hand sides.
Nothing about the physics, the order, or the preconditioner.

**The lesson, stated so it is not repeated.** `avg. reduction factor` is
`(r_N/r_0)^(1/N)`. It is an arithmetic identity over whatever interval the solve
happened to run, and **a healthy solve stopped at an iteration cap produces
exactly the same kind of number as a stalled one**. 0.949 per iteration sustained
for 500 iterations *is* 9.4 orders — the figure looks damning only if you read it
per-iteration and never multiply. A stall has to be diagnosed from the residual
history: a stalled solve goes flat, and this one did not. This is the §6 trap in
its general form — a summary statistic was promoted to a diagnosis without
checking the series it summarises.

**Cost, from the same run.** 1831.7 s covered 10 full 500-iteration solves plus
39% of an eleventh, so roughly **176 s per 500-iteration solve** at 383k tets on
8 ranks. Eighteen solves at the ~560 iterations they actually need projects to
**~3550 s**, inside the 7200 s `pcb_convergence` wall from §0.27. Peak was
5.96 GB under the old RSS metric, with Palace itself estimating 0.26 GB per rank
— against 8.4 GB for the SuperLU factorisation of the same system.

**Why this reopens the fifth rung.** §0.30's memory wall is a property of the
factorisation's fill-in, which grows superlinearly and is what does not fit in
24 GiB. A Krylov solve stores the operator and a handful of vectors; its memory
grows roughly with the unknowns. `v3k5` is 1845846 tets against `v3k0`'s 266994
— 6.9x — which a direct solve cannot hold and an AMG-preconditioned CG has no
particular reason to care about. That is a projection, not a measurement, and it
is stated here as one.

**What is being measured now.** The claim that has to hold before any of this is
usable is not "it converges" but "it converges to the same matrix". `v3k0-p2`
has three independent SuperLU runs agreeing entrywise to 1.6e-24 F, so it is the
reference. The validation run is:

```
probe.py v3k0 2 BoomerAMG 8 pcb_convergence 2000
```

`MaxIts` raised to 2000 and `Tol` left at 1e-12 — deliberately that way round.
The tolerance was never the problem; the budget for reaching it was. Relaxing an
acceptance criterion so that a run passes is precisely what §6 forbids, and it
would be no less a mute button here than anywhere else. `probe.py` now takes
`MAXITS` as a sixth argument and, unlike the resource class, puts it in the
output directory name: a solve stopped at its cap and a solve stopped at its
tolerance report different matrices, and the per-rung directories exist to keep
exactly that kind of pair from being conflated by a grader reading the newest
run under a tag.

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
  matches a SuperLU direct solve to 3e-15 and is ~2× faster. ~~The historical
  stalls were an order≥2 Chebyshev-smoother problem.~~ **Retracted (§0.31):**
  there were no stalls to explain. The one order-2 run cited as evidence was
  converging at 9.4 orders per 500 iterations when its iteration cap stopped it,
  and it set no smoother options at all.
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
