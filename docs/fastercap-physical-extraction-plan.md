# FasterCap PCB extraction plan

## Objective and present evidence

Produce a reusable KiCad-to-FasterCap electrostatic extraction path that can
return a numerically converged, passive, reciprocal Maxwell matrix for Fugu2 and
other PCB designs. Reusable geometry, solver, validation, lifecycle, and matrix
code stays in `dcdc-parasitics`; Fugu2 conductor policy, excitation, assembly
models, and EMI analysis stay in `dcdc-tools`.

The existing filled-zone adapter is an accepted geometry-development baseline,
not a physical PCB capacitance model. Its 1 µm Fugu2 deck has 7,925 input panels
and valid SI coordinates, but the air-only solve did not converge: a 1% run
reached 626,785 refined panels, repeatedly exhausted 1,000 GMRES iterations, and
timed out after 30 minutes. No matrix from that run is a valid extraction.

## Status

This FasterCap plan is closed as `BLOCK`; no qualifying Fugu matrix was
produced. Palace is now the authorized maintained replacement under
`docs/palace-electrostatic-qualification-plan.md`. The evidence below remains
the historical FasterCap record and cannot qualify Palace or a physical model.

- Slice 1 complete; interleaved Codex xhigh review returned `ACCEPT`.
- Slice 2 complete; raw-gated report
  `65df6f9f6fcb579fd4b5bb4aa33fd616dd49ef1054da3c9f3324ef506ce6a608`
  and interleaved Codex xhigh re-review returned `ACCEPT`.
- Slice 3 complete; interleaved Codex xhigh re-review returned `ACCEPT`.
- Slice 4 final frozen report `7dd866efe04a21d82970c0340e52752e1e5745363c81a1cfea15731e50bd0d5e`
  is `rejected_diagnostic`: the live monitor terminated the shared endpoint at
  the 1,000-GMRES gate. Two mandatory xhigh reviews found delayed stream reads
  a shared stdout/stderr partial-record tail, and early return with a detached
  descendant, escaped-descendant identity/RSS tracking, PID reuse, and Windows
  leader-only waiting. The sixth review remains `BLOCK`: polling cannot contain
  descendants that replace their environment, immediately enforce arbitrarily
  short RSS peaks, or close every late-spawn kill race. A native containment
  boundary is required before re-review. Three subsequent source-level numerical
  recovery cycles also ended `BLOCK`: CGS2 was falsified, automatic
  preconditioner state synchronization removed the 1,000-step cliff but not the
  938,216-panel/reciprocity failures, and median-centroid hierarchy plus a hard
  true-residual gate failed frozen mixed-dielectric and PCB-like fixture
  requalification before any Fugu endpoint. Evidence is retained under
  `docs/artifacts/fastercap-slice4/recovery/`. Slices 5–6 have not started.

## Lifecycle states

Artifacts use exactly one qualified state; the unqualified word “accepted” is
not a lifecycle state.

1. `rejected_diagnostic`: incomplete, failed, partial, or gate-failing run. It
   may retain diagnostic matrices but cannot expose `accepted_matrix` or create
   a downstream-consumable matrix artifact.
2. `numerically_converged_diagnostic`: solver and numeric gates pass, but the
   geometry or material model is intentionally incomplete. Slice 4 ends here.
3. `geometry_complete`: every in-scope object and region has a disposition and
   the post-union geometric integrity gates pass. This is not a matrix verdict.
4. `physical_model_validated`: complete geometry, materials, reduction
   qualification, and all numerical gates pass. Only this Slice 5 state can
   enter Slice 6.
5. `predictive_calibrated`: separate bench evidence validates the source and
   assembly/cable transfer observables for a declared band. Until then, Slice 6
   results remain comparative even if the electrostatic model is validated.

State transitions are monotonic and recorded in manifests. A hash alone never
confers a state.

## Gate policy `fastercap-pcb-gates-v1`

These gates are fixed before implementation. Changes require a new policy
version, rationale, rerunning all affected ladders, and a fresh independent
review; they cannot be changed in place after observing a result.

### Identity and run-completion gates

- Deck conductors receive stable unique solver labels from a deck manifest.
  Parsed labels must match the manifest exactly in count, spelling, uniqueness,
  and order. User-facing names are mapped only after that bijection passes.
- Automatic runs must exit normally, contain FasterCap’s normal terminal
  completion, have a final reported weighted Frobenius auto norm no greater
  than requested `-a`, and contain no later refinement iteration.
- Every conductor right-hand side must meet FasterCap’s reported effective
  GMRES tolerance. Any iteration-limit, non-convergence, singularity, memory,
  malformed-panel, or incomplete-output diagnostic rejects the run even when a
  complete matrix was printed.
- Requested and effective settings are both recorded. Automatic experiments
  vary only `-a` and `-ap`; manual diagnostics use fixed `-m` and `-t` and vary
  preconditioner settings separately because automatic mode overrides them.
- Partial or rejected output may be parsed only into diagnostic iterations. It
  never populates an accepted/final matrix field.

### Matrix gates

All gates apply to the unsymmetrized raw matrix in farads.

- Every value must be finite; every diagonal must be positive.
- Reciprocity for each `i != j` requires
  `abs(Cij-Cji) <= 1e-18 F + 1e-3*max(abs(Cij),abs(Cji))`.
- Off-diagonal sign requires `Cij <= 1e-18 F`. Row sums require
  `sum_j(Cij) >= -1e-18 F`.
- The minimum eigenvalue of the raw matrix’s symmetric part must be at least
  `-max(1e-18 F, 1e-6*max(diag(C)))`.
- No nearest-passive, clipping, or other matrix projection is allowed.
  Reciprocity averaging is permitted only after the raw reciprocity gate
  passes; the elementwise averaging displacement is recorded and cannot exceed
  half the reciprocity allowance above. The averaged downstream matrix is then
  gated again and must have strictly non-positive off-diagonals, non-negative
  row sums, and a positive-semidefinite spectrum. Any positive off-diagonal,
  negative row sum, or negative eigenvalue rejects the matrix, regardless of
  the raw tolerance that admitted averaging. Branch reconstruction must exactly
  reproduce this gated downstream matrix; emitters must refuse rather than clip
  a violation.
- Nested discretization convergence uses at least three levels. For both
  adjacent pairs, every entry must satisfy
  `abs(Cij[fine]-Cij[coarse]) <= 1e-15 F + 0.02*max(abs(Cij[fine]),abs(Cij[coarse]))`.
  Near-zero entries therefore use the 1 fF absolute gate rather than an
  unstable percentage.
- Entrywise numerical uncertainty is
  `u_num,ij=max(abs(Cij[L3]-Cij[L2]), abs(Cij[L2]-Cij[L1]))`; no assumed
  convergence order is used. Capacitance-domain intervals add numerical,
  representation, and material-property contributions in farads,
  conservatively rather than in quadrature. Source-waveform and circuit/assembly
  uncertainties are never added to capacitance values; each remains in its
  native physical domain and is propagated through the circuit model into the
  final cable-current interval.

### Reduction and geometry gates

- A reduced representation must agree with a converged physical-thickness
  reference on every fixture matrix entry within
  `2e-15 F + 0.01*max(abs(Cij,reduced),abs(Cij,reference))`.
- Existing contour conditioning gates remain: 1 nm KiCad-grid snapping,
  simplification area relative error at most `1e-4`, and no retained boundary
  segment shorter than half the requested geometry tolerance.
- Effective-thickness changes anchor the copper midplane unless a fixture
  explicitly requires a material boundary face to remain fixed. They must not
  create intersections, remove clearances, cross a dielectric boundary, or
  change any gap by more than `min(1 µm, 1% of the source gap)`.
- Physical-model validation requalifies reduction over Fugu2’s observed
  width/gap/thickness envelope, dielectric contacts, barrels, pad/via/zone
  junctions, and every topology transition. Air-only qualification cannot be
  promoted to a material model.

### Resource classes

The harness terminates a rung when any limit is exceeded and records
`rejected_diagnostic`:

| class | wall time | peak RSS | output disk | refined panels | GMRES iterations/RHS |
|---|---:|---:|---:|---:|---:|
| synthetic | 120 s | 4 GiB | 1 GiB | 250,000 | 1,000 |
| Fugu diagnostic | 20 min | 8 GiB | 5 GiB | 500,000 | 1,000 |
| Fugu physical | 30 min | 12 GiB | 10 GiB | 1,000,000 | 1,000 |

A resource failure is evidence about feasibility, not permission to loosen a
numeric gate. A policy revision may choose a different algorithm or documented
resource class before rerunning.

### Pinned Fugu2 geometry benchmark

- PCB: `/Users/fab/dev/ee/hw/Fugu2/Fugu2.kicad_pcb`
- SHA-256: `32906787c3c5bfaf9b0efc0156b6ae40c0d2b29dd69c354e75a8ee8a8b0d3665`
- At 0.001 mm conditioning tolerance: 10 filled-zone records.
- SW/VIN/PGND areas: 246.7410616 / 1027.4381104 / 1556.0629464 mm².
- Triangles: 353 / 689 / 1597; panels: 1069 / 2067 / 4789.
- Repairs: 0 / 2 / 2; minimum edge at least 0.001155 mm; maximum area
  error 0.00136 mm².

A board-hash change requires a new benchmark; it cannot silently update this
one.

### Ranking and calibration gates

- Variant A ranks above B only when its propagated cable-current-magnitude
  interval is strictly above B’s interval at every predeclared decision
  frequency. The propagation spans capacitance, material, source-spectrum
  magnitude/phase, measured or lumped assembly models, cable/LISN parameters,
  and termination uncertainty in their native domains. Any overlap, or any
  load-bearing term without a finite evidence-backed bound, is reported as
  tied/unresolved and is never broken by nominal values.
- The known-added-C experiment validates only the switching-source tank model,
  not the full source-waveform spectrum. It must follow
  `dcdc-tools/verifications/ring-ident/PLAN-added-c.md`: same-session stock/+1/+2
  captures, measured added capacitance, six-capture means, its §6 uncertainty
  budget, and §7 pair-consistency refusal rules at 28/45/55/68 V. Harmonic
  source magnitude and phase used for cable-current prediction require their own
  scope-derived bounds; absent bounds make the affected ranking unresolved.
- That experiment does not calibrate cable, enclosure, or LISN transfer. A
  `predictive_calibrated` EMI claim additionally requires measured cable-port
  current magnitude at all ranking frequencies for baseline and at least one
  perturbation, with the measured interval overlapping the predicted interval.
  Until such a protocol is separately preregistered and executed, all EMI
  rankings are explicitly comparative.

## Content-addressed run manifest

Every run writes a diagnostic manifest atomically after execution. It binds:

- gate-policy and lifecycle-state versions;
- PCB, contour dump, object census, deck, stdout, stderr, raw matrix, and final
  matrix hashes;
- parser/library revision, Python environment, solver version, binary hash, and
  exact command;
- deck-manifest labels plus requested and effective solver settings;
- wall time, peak RSS, disk usage, initial/refined panels, GMRES observations,
  and every gate result;
- unsymmetrized diagnostic matrices and, only when qualified, the downstream
  matrix hash and uncertainty.

Failed runs use a diagnostic-manifest filename and cannot create the qualified
manifest filename expected by downstream tooling. Manifest publication uses a
temporary file plus atomic rename only after every gate for the requested state
passes.

## Execution and review protocol

Implement the six slices in order. Each slice must satisfy its gates before the
next begins. After each slice, run an independent Codex review at xhigh effort.
Address every blocker and repeat that slice’s review until clean. Reviewers are
read-only; the primary agent remains the sole writer in the working tree.

Do not weaken identity, completion, reciprocity, passivity, geometry, material,
resource, or convergence gates to make a run pass. Do not publish capacitance
values from partial, non-converged, diagnostic-only, or incomplete models.

## Slice 1 — Fail-closed solver diagnostics

Implement one mandatory result classifier used by every later slice. Preserve
raw FasterCap output and parse per-iteration panel counts, effective settings,
GMRES outcomes, matrices, auto norms, runtime, memory, and diagnostics. Separate
geometry acceptance, linear-solver convergence, refinement convergence, label
identity, and raw-matrix validation.

Use captured failed Fugu output and small synthetic fixtures. Run automatic
`-a`/`-ap` experiments separately from manual fixed-`-m`/fixed-`-t`
preconditioner diagnostics. Vary one factor at a time and enforce the resource
class before launch.

Acceptance gates:

- Complete matrices followed by a failure or missing terminal convergence are
  classified `rejected_diagnostic` and expose no downstream matrix.
- Exact solver-label bijection is enforced, including adversarial swapped,
  missing, duplicate, and same-dimension replacement cases.
- A machine-readable diagnostic manifest records every `v1` identity,
  completion, resource, and matrix gate.
- Existing FasterCap, Maxwell, and KiCad tests remain green.

## Slice 2 — Qualify candidate reduced representations

Evaluate documented FasterCap representations: physical-thickness reference,
supported thin-sheet semantics if confirmed from source/documentation,
controlled effective thickness, and boundary simplification. This slice grants
only fixture-scoped qualification; it does not authorize use in a physical PCB
model.

Compare candidates on converged parallel-plate and PCB-like air fixtures, then
on simple two-medium boundary fixtures. Measure per-entry matrix error,
reciprocity, passivity, panel growth, runtime, clearance changes, and anchored
surface displacement. Reject candidates that only improve runtime.

Acceptance gates:

- Selected and rejected candidates have quantitative evidence under the fixed
  reduction budget.
- Approximation knobs have units, finite bounds, explicit defaults, and anchoring
  semantics.
- Qualification scope lists geometry/material/topology envelopes explicitly.
- No Fugu2-specific assumptions enter reusable APIs.

## Slice 3 — Implement reusable reduction and provenance

Add fixture-qualified representations as explicit opt-in paths. Preserve
physical extrusion for references. Record source thickness, effective
representation, anchored surface, simplification, area/gap errors, panel counts,
and all solver settings in the content-addressed manifest.

Generated filenames and metadata distinguish physical, reduced, air-only,
full-stackup, diagnostic, and validated artifacts. The implementation must not
provide an API that promotes fixture qualification to physical validation.

Acceptance gates:

- Unit and CLI tests pin units, defaults, invalid input, labels, anchoring,
  clearance/intersection gates, and manifest hashes.
- KiCad Python remains stdlib plus `pcbnew`; system geometry dependencies stay
  outside that interpreter.
- Full pytest, Ruff, system-Python compile, and KiCad-Python compile pass.

## Slice 4 — Bounded Fugu2 diagnostic convergence

Generate reduced filled-zone Fugu2 air decks and run two independent nested
ladders: at least three geometry-tolerance rungs with solver refinement held at
its final/tightest setting, and at least three automatic FasterCap-refinement
rungs with geometry tolerance held at its final/tightest setting. The ladders
share the same production endpoint. Preserve every hash, command, raw stream,
resource observation, and diagnostic manifest. Raw reciprocity is checked before
symmetrization.

This slice can produce only `numerically_converged_diagnostic`. Its results are
not physical inputs to Slice 6.

Acceptance gates:

- Every rung in both three-or-more-level ladders independently passes identity,
  completion, resource, reciprocity, sign, passivity, and solver gates.
- Both adjacent matrix pairs in each axis pass the fixed per-entry delta gate;
  uncertainty is the conservative maximum of all adjacent deltas from both
  ladders.
- Geometry areas and provenance match the pinned benchmark.
- Any failed rung prevents creation of a numerically-converged matrix artifact.

## Slice 5 — Complete and validate the physical PCB model

### Conductor and dielectric representation

Extract tracks, pads, vias, zones, copper graphics, footprint copper, plated
holes/barrels, board outline, cutouts, and material regions. Build an object-level
census by source object, primitive class, layer, and net. Every copper object and
dielectric region is `solved`, `bonded`, `grounded`, `floating`, or
`intentionally_omitted`; intentional omissions require a measured sensitivity
bound under the reduction budget.

Keep the base extraction at separate physical-conductor/net granularity.
Implement ideal-short aggregation and floating-conductor Schur reduction as
separate tested transforms with provenance. Never irreversibly group different
nets before extraction merely because a variant may bond them.

Partition conductor faces by adjacent medium. Collate multiple `C ... +` parts
into one electrical conductor where top, bottom, or side faces touch different
media. Segment dielectric interfaces around conductor contacts; do not emit
ambiguous coincident internal panels. Support per-panel dielectric reference
points or a proven convex partition rather than one global point for non-convex
outlines or cutouts.

After same-conductor solid union, require watertight/manifold surfaces,
connectivity agreement, no overlaps, no coincident internal faces, and minimum
clearance. Test top/bottom copper at a dielectric boundary, plated barrels,
pad/via/zone junctions, non-convex outlines, and cutouts.

### Physical requalification

Requalify every reduced representation against converged physical-thickness
references over the observed Fugu2 width/gap/thickness envelope, material
contacts, and topology transitions. Carry the measured representation error into
the matrix uncertainty.

Acceptance gates:

- The object census is complete and content-addressed; no load-bearing object or
  floating shield is silently absent.
- Analytic fixtures verify dielectric orientation/permittivity and conductor
  collation at mixed-medium boundaries.
- The complete Fugu2 model reaches `geometry_complete`, then repeats both
  independent three-or-more-rung ladders—geometry with solver held tight and
  solver with geometry held tight, sharing one production endpoint—and passes
  both ladders plus the reduction budget to reach `physical_model_validated`.
- A physical-model manifest binds all geometry, material, solver, matrix, and
  uncertainty evidence under `v1`.

## Slice 6 — Fugu2 assembly, excitation, and comparative ranking

Consume only a `physical_model_validated` Slice 5 manifest. Add Fugu2 policy in
`dcdc-tools`, not `dcdc-parasitics`. Preserve separate base conductors and derive
bonded/open/floating variants using explicit ideal-short aggregation,
floating-conductor reduction, or a clearly identified geometry re-extraction.

Define the assembly boundary before calculation:

- enumerate heatsink, chassis, enclosure, PE, cable, and LISN conductors;
- state which are geometrically extracted and which use measured/lumped models;
- always preserve electrostatic infinity as a distinct port in the base branch
  model, including when an enclosing conductor is extracted; never silently
  drop it or map it to PE or SPICE node `0`;
- permit grounding or reference selection only as an explicit tested network
  transformation that states how every enclosure-to-infinity branch is handled
  and exactly reconstructs the matrix before and after transformation;
- declare cable/LISN topology, termination, decision frequencies, valid
  quasi-static/lumped band, and double-count boundaries.

Convert the validated Maxwell matrix into branches/SPICE, drive it from the
existing switching waveform, and compute cable-port common-mode current.
Evaluate bond, Y-cap, heatsink, chassis/PE, and cable variants. KCL and branch
reconstruction are necessary but do not substitute for the boundary definition.

Acceptance gates:

- Every result references the physical-model manifest and assembly manifest.
- Infinity/reference, ideal-short, floating reduction, KCL, passivity,
  reciprocity, and exact branch reconstruction tests pass on the base network
  and again after every explicit grounding/reference transformation.
- Rankings obey the fixed interval non-overlap rule; overlaps are unresolved.
- Source uncertainty follows the preregistered known-added-C protocol.
- Results remain comparative unless a separate cable-port validation protocol
  satisfies the `predictive_calibrated` gate.

## Final deliverables

- Reusable fail-closed diagnostics, extraction, reduction, manifests, and
  validation in `dcdc-parasitics`.
- Reviewed Fugu2 diagnostic and physical-model artifacts with qualified states.
- Fugu2-specific assembly, excitation, and comparative variant analysis in
  `dcdc-tools`.
- Six clean interleaved Codex xhigh reviews plus a final cross-slice review.
