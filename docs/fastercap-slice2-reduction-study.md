# FasterCap Slice 2 representation qualification

## Scope

This study can qualify a representation only on the declared synthetic fixture
envelope. It cannot produce a physical PCB matrix, authorize Fugu2 use, or
replace Slice 5 requalification over complete geometry, materials, and topology.
All dimensions are SI metres and all capacitances are farads.

The executable study is `experiments/fastercap_reduction_study.py`. Every solve
uses the Slice 1 fail-closed runner, a three-rung automatic-refinement ladder,
and resource class `synthetic`. Each run retains its content-addressed deck,
streams, matrix, manifest, solver hash, effective settings, and resource
observations.

## Authoritative FasterCap semantics

The reviewed public source is FasterCap 6.0.7 commit
`b42179a8fdd25ab42fe45527282b4a738d7e7f87`, which was also upstream `master`
when checked on 2026-08-22.

- The 3D input language represents conductor and dielectric boundary surfaces
  with `T` and `Q` panels. A `C` statement assigns one contacting-medium
  permittivity to its panel set. Consecutive `C ... +` parts can collate face
  sets with different contacting media into one electrical conductor.
  [3D conductor help](https://github.com/ediloren/FasterCap/blob/b42179a8fdd25ab42fe45527282b4a738d7e7f87/hlp/InputFile/3D/InputFile_3D_ch03.htm#L18-L49)
- A `D` item is a two-medium interface. Its reference point assigns sides
  panel-by-panel; FasterCap does not infer a global inside/outside topology.
  [3D dielectric help](https://github.com/ediloren/FasterCap/blob/b42179a8fdd25ab42fe45527282b4a738d7e7f87/hlp/InputFile/3D/InputFile_3D_ch04.htm#L18-L30)
- FasterCap's author explicitly states that a zero-thickness conductor at an
  interface between unlike permittivities is unsupported. The supported model
  is finite thickness with face panel sets assigned to their contacting media
  and collated with `+`.
  [Official support forum topic 1300](https://www.fastfieldsolvers.com/forum/topic.asp?TOPIC_ID=1300)
- The complete public parser/help corpus has no thin-sheet, `B`, or 2.5D
  conductor primitive. Native 2D mode is a separate invariant cross-section
  solver whose result is F/m, not a finite planar patch in the 3D engine.
  [2D input structure](https://github.com/ediloren/FasterCap/blob/b42179a8fdd25ab42fe45527282b4a738d7e7f87/hlp/InputFile/2D/InputFile_2D_ch02.htm#L18-L29)

Homogeneous-medium open-sheet semantics remain undocumented. Successful parsing
would not prove physical validity. Slice 2 therefore excludes thin sheets
rather than providing an override.

## Fixed candidates and fixtures

The physical reference is 35 µm closed copper. Candidate knobs are fixed before
examining results:

- 34 µm closed effective thickness, copper midplane fixed;
- 1 µm closed effective thickness, copper midplane fixed;
- 1 µm topology-preserving removal of redundant collinear boundary vertices,
  with physical thickness unchanged.

The fixtures are:

1. overlapping 1 mm square parallel plates in air with 100 µm physical face
   clearance;
2. a coplanar PCB-like L-shape and rectangle in air, with 400 µm minimum
   lateral clearance and redundant collinear vertices at 0.5–1 mm spacing;
3. 10 mm parallel plates wholly enclosed by a convex εr=4.2 region; the nearest
   air/material interface is 982.5 µm from physical copper.

The third fixture exercises a simple two-medium interface without claiming the
mixed-medium conductor-contact semantics that belong to Slice 5.

## Gates and selection rule

Every representation first passes finite-knob, area, intersection, dielectric-
crossing, anchoring, and clearance gates. A changed gap must differ by no more
than `min(1 µm, 1% of source gap)`. Invalid geometry is rejected before launch.

Each valid geometry is solved at automatic relative-error rungs 0.00125,
0.0009, and 0.000625. Both adjacent unsymmetrized raw-matrix pairs must satisfy
`1e-15 F + 2%*max(abs(entries))`. Every individual rung must also pass all
Slice 1 identity, completion, resource, reciprocity, sign, passivity, and exact
branch-reconstruction gates.

The tightest unsymmetrized raw candidate matrix is compared entrywise with the
tightest raw physical reference under
`2e-15 F + 1%*max(abs(entries))`. Averaged downstream matrices are retained for
diagnostics but never decide qualification. Passing runtime or reducing
input vertices alone is not a selection benefit: a candidate must reduce the
tight-rung refined panel count. Reports preserve input/refined panel counts,
panel growth, solver and
wall time, RSS, matrix gates, entry differences and allowances, clearance
change, area error, anchor displacement, and maximum surface displacement.

## Preliminary fixture rejection

The first report, content SHA-256
`628a9f8f75575073c4ac2f717e729e184b44f5378dd7fc279be377d1b1ab0d97`,
was retained rather than overwritten. It rejected the 10 mm/35 µm parallel-air
reference at the fixed 120 s resource limit. The PCB-like micro-jog geometry
also triggered FasterCap's forbidden thin-triangle warning. These are fixture
design failures, not permission to loosen solver or diagnostic gates.

The second report, content SHA-256
`3ec3b550da1065b0a00c0edfbda123cb4c91df96b1c739410355d517982c3cc3`,
qualified the 34 µm effective-thickness candidate only on the 1 mm parallel-air
fixture. It also showed that constrained triangulation around a geometrically
valid 20 µm notch still creates panels below FasterCap's 5° warning threshold;
that PCB fixture is retained as rejected evidence. The scaled 1 mm material
fixture exceeded the resource class, whereas the original 10 mm material
fixture had already converged.

The third report, content SHA-256
`13e4076a37c16c75e0a71b37af319d4a68241c70bc730a4e1a3003a883c29b2b`,
validated the revised fixtures but showed that the 0.0003125 air endpoints
exceeded the fixed 120 s resource class. The fourth report, content SHA-256
`50fd94718dc2cbc240d96d75bff5cb0ae3beba9f94091b701aca34b9cc22a211`,
showed non-monotonic exact-branch rejection at 0.0015/0.001 for two PCB
representations. The 0.00125 and 0.000625 physical rungs passed, and an explicit
0.0009 probe passed physical, effective-thickness, and simplified PCB decks.
The final ladder is frozen to those three demonstrated settings; no resource or
matrix gate changes.

The final fixture keeps the bounded 1 mm air plate, restores the convergent
10 mm material fixture, and narrows boundary simplification to exact removal of
redundant collinear vertices. This is intentionally a narrow geometry envelope
with zero area, clearance, and Hausdorff displacement; it is not evidence for
smoothing real curved or jagged PCB boundaries. Solver rungs remain tighter
than the coarse PCB run that failed raw reciprocity.

## Quantitative result

The final raw-gated report is
`docs/artifacts/fastercap-slice2/slice2-reduction-study.65df6f9f6fcb579fd4b5bb4aa33fd616dd49ef1054da3c9f3324ef506ce6a608.json`.
Its canonical pre-ID content SHA-256 is
`65df6f9f6fcb579fd4b5bb4aa33fd616dd49ef1054da3c9f3324ef506ce6a608`;
the pretty-printed artifact file SHA-256 is
`db2ee02755e396ac8e11f84591838bba7815c67c193a50e6cd708482cfc0036b`.
The retained `afb03f44...` artifact is superseded because its Slice 2 comparison
logic used downstream averaged matrices. It is rejection-history evidence, not
a qualification report.

Every physical reference passed all three raw-matrix rungs. The largest
adjacent-delta to allowance ratio was 0.001629 for the air plate, 0.010905 for
the material fixture, and 0.004071 for the PCB-like fixture.

Only `effective_34um` on `parallel_plate_air` is fixture-qualified. It preserves
the midplane, moves each copper face by 0.5 µm, changes the 100 µm physical gap
by exactly the permitted 1 µm, and passes every raw matrix entry with maximum
error 1.081 fF and maximum error/allowance ratio 0.340982. Tight-rung refined panels
fall from 81,680 to 81,472. Tight-rung solver time does not improve
(16.80 s reference versus 16.99 s candidate), so the verdict relies on measured
panel reduction and accuracy, not runtime.

The same 34 µm representation is explicitly not qualified elsewhere:

- on the PCB-like air fixture it passes the matrix budget (maximum
  error/allowance ratio 0.0529) but increases tight-rung panels from 27,586 to
  28,729;
- in the two-medium fixture its maximum raw matrix error is 2.71176 pF, 36.897 times
  the allowed entry budget, and panels increase from 1,072 to 1,104.

The 1 µm effective thickness is rejected before launch on both parallel-plate
fixtures because it changes the physical gap by 34 µm; on the coplanar fixture
it preserves clearance but fails the numerical/resource ladder. Removing
collinear PCB vertices reduces emitted input panels from 46 to 22 and stays
inside the matrix budget, but tight-rung refined panels increase from 27,586 to
28,263, so it is rejected. Simplification is also rejected on both rectangular
fixtures because it changes no geometry or refined-panel count.

The resulting qualification envelope is therefore deliberately narrow: closed
34 µm copper, midplane anchored, two overlapping 1 mm square conductors in air,
35 µm source thickness, and 100 µm source face gap. It does not cover coplanar
coupling, dielectric interfaces, conductor/material contact, arbitrary contour
simplification, vias, barrels, junctions, or any Fugu2 geometry. Those cases
remain rejected or unqualified and require Slice 5 requalification.
