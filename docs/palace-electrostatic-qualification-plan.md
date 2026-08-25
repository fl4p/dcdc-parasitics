# Palace electrostatic qualification plan

## Objective

Replace the blocked FasterCap path with a maintained Palace finite-element
workflow that produces a numerically converged, passive Maxwell capacitance
matrix for complete PCB geometry and materials. FasterCap remains diagnostic
only. No FasterCap artifact, tolerance, or fixture verdict transfers to Palace.

This plan defines `palace-electrostatic-pcb-gates-v1`. A Palace result cannot
enter Fugu integration until the frozen air, enclosed-FR4, and PCB-like fixtures
pass this policy and receive an independent xhigh review. A fixture result never
authorizes a physical PCB model.

## Solver and local qualification instrumentation

The pinned upstream source is `awslabs/palace` commit
`86810b1909f3e05e0bf1fd78f485e59d55582b6d` from 2026-08-21. Palace is built
from source on macOS arm64 with its dependency revisions pinned by that tree.
Source, build options, dependencies, wrapper, MPI launcher, and solver binary are
hashed in every run manifest.

Upstream Palace's electrostatic solver computes terminal capacitance from field
energy. It evaluates only one triangle of the matrix and copies it to the other
triangle. Its linear wrapper also warns rather than aborting when a Krylov solve
does not converge. The qualification build therefore carries two narrow,
reviewable instrumentation changes:

1. compute and print `||b-Ax||/||b||` from the assembled operator after every
   terminal solve, and abort if it exceeds the declared linear tolerance;
2. preserve each terminal's unsolved Dirichlet boundary vector, evaluate the
   uneliminated stiffness-operator reaction after each independently solved
   excitation, and emit the ordered charge-response matrix as
   `terminal-Craw.csv` at 17-digit precision before Palace creates its standard
   symmetric energy matrix.

`terminal-Craw.csv` is the only matrix used for reciprocity and raw passivity
qualification. Each entry is `W_iᵀ K_uneliminated V_j`, where `W_i` selects the
Dirichlet degrees of freedom of response terminal `i` and `V_j` is the separately
solved excitation `j`; unlike swapping a symmetric energy-product multiplication,
this can expose inconsistent ordered solves. `terminal-C.csv` may be used
downstream only after the raw matrix passes, is exactly symmetric as emitted,
and agrees entrywise with the independent reaction-charge matrix within
`1e-18 F + 1e-3*max(abs(entries))`. This is not evidence from two solvers; both
matrices come from the same finite-element discretization through independent
postprocessing identities.

## Geometry and reference semantics

Palace solves a volumetric finite domain. Conductors are finite closed holes in
a conformal tetrahedral air/dielectric mesh. Each conductor terminal is a
closed boundary attribute; multiple disconnected solids may share one terminal
attribute only through an explicit ideal-short grouping manifest. Thin or open
sheets remain unsupported.

The electrostatic reference is an outer Dirichlet boundary approximating
infinity. It is named `electrostatic_infinity_boundary` in mesh and manifests.
It is not protective earth, chassis, a PCB net, or SPICE node `0`. Qualification
requires an outer-domain expansion ladder; no single finite-box result may be
called an infinity-referenced matrix.

Gmsh generation is single-threaded and content-addressed. Manifests pin its
Python module and native library, all source polygon rings and z extents,
material volumes and permittivities, physical attributes, mesh controls, node
and tetrahedron counts, the finite-reference box, and mesh bytes. A Palace
configuration is rejected unless its terminal, material, and ground attributes
exactly match that mesh manifest.

## Fixed gates

### Identity and completion

- The configuration, mesh, mesh manifest, source and binary, launcher, exact
  command, environment-relevant build options, stdout, stderr, metadata, and
  matrix files are hashed.
- Terminal indices are contiguous from one and map bijectively to stable names
  and mesh boundary attributes. CSV row and column indices must match exactly.
- The output directory must not exist before launch. Exit must be zero, every
  expected output must exist, and any warning, error, abort, or non-convergence
  diagnostic rejects the run.
- Every terminal must produce exactly one explicit residual record. Its target
  must equal the configuration value and its residual must not exceed it.

### Raw Maxwell matrix

All tests use `terminal-Craw.csv` without averaging, clipping, projection, or
sign repair.

- Every entry is finite; every diagonal is positive.
- Every off-diagonal is at most `1e-18 F`.
- Every row sum is at least `-1e-18 F`.
- The minimum eigenvalue of the symmetric part is at least
  `-max(1e-18 F, 1e-6*max(diag(C)))`.
- For every ordered coupling,
  `abs(Cij-Cji) <= 1e-18 F + 1e-3*max(abs(Cij),abs(Cji))`.

After those gates, Palace's standard energy matrix must be exactly symmetric and
agree entrywise with the ordered reaction-charge matrix within
`1e-18 F + 1e-3*max(abs(entries))`. It must pass strict finite, sign, row-sum,
positive-semidefinite, and exact capacitor-branch reconstruction gates. The
explicit infinity reference remains named through conversion and is never
silently mapped to SPICE `0`.

### Mesh, polynomial order, and finite reference

Each fixture and physical model must pass three independent convergence axes:

1. an `h` ladder using successively finer conformal tetrahedral meshes;
2. a finite-element `p` cross-check at orders 2 and 3 on a declared mesh;
3. an outer-domain expansion ladder with fixed local conductor/interface mesh
   controls.

Adjacent raw matrices must pass the preregistered entrywise allowance
`1e-15 F + 2%*max(abs(entries))`. The outer-domain ladder additionally requires
monotone or bounded convergence of each capacitance-to-reference row sum. A
smaller linear residual does not substitute for any mesh or domain ladder.

The parallel-plate air fixture also receives a dimensional analytic cross-check.
The enclosed-FR4 fixture tests conformal material interfaces and complete
material coverage. The PCB-like coplanar fixture tests nonconvex geometry,
redundant segmentation, and lateral fields. Analytic agreement is a separate
witness from mesh convergence and is not double-counted.

### Resource limits

The launcher records process-tree wall time, peak RSS, and output growth and
terminates runs that exceed the selected local resource class. These are
engineering safeguards, not a security boundary and not a separate condition
for accepting numerically and physically valid results.

## Execution sequence

### P0 — build and upstream smoke test

1. Pin source and dependency revisions and record all build options.
2. Build the qualification instrumentation.
3. Run Palace's upstream electrostatic regression and compare its output to the
   pinned regression data.
4. Run the local parallel-plate mesh once to validate Gmsh attributes, config
   schema, explicit residual telemetry, ordered raw output, and manifests.
5. Obtain xhigh review of the instrumentation and parser before using its
   matrices as qualification evidence.

### P1 — frozen fixture qualification

Run the complete `h`, `p`, and outer-domain ladders for
`parallel_plate_air`, `parallel_plate_enclosed_fr4`, and
`pcb_like_coplanar_air`. Preserve every rejected rung and create one
content-addressed report. Any missing rung, failed raw gate, or incomplete
ladder fails P1. A fresh xhigh `ACCEPT` is required.

The initial P1 report ID `63cc9997...` is `rejected_diagnostic`; its xhigh
review rejects the first recovery proposal. The prospective recovery ladder is
therefore frozen as follows before any new matrix is inspected:

- `h`: mesh scales `0.85`, `0.70`, and `0.60`, with near and far controls scaled
  together, order 1, outer scale 6, and the `synthetic` resource profile;
- `p`: orders 2 and 3 on the same content-addressed mesh, near scale `0.50`, far
  scale `1.00`, outer scale 6, and the `pcb_diagnostic` profile; the p2-to-p3
  choice supersedes p1-to-p2 after direct runs showed p1 convergence too slow
  for the 24 GiB budget while p2-to-p3 passed by nearly an order of magnitude;
- outer domain: scales 4, 6, and 8 with near/far mesh scale 1, order 1, and the
  `synthetic` profile.

Only the two high-order `p` rungs may use `pcb_diagnostic` (10 million nodes,
50 million tetrahedra, 24 GiB peak RSS, and 30 minutes). The 24 GiB p-profile
ceiling was explicitly authorized after the 8 GiB ladder cleared all memory
limits but exposed the need for a finer shared p mesh. Every other fixture
rung remains `synthetic` (1 million nodes, 5 million tetrahedra, 8 GiB, and five
minutes). The 8 GiB limit supersedes the historical 4 GiB ceiling by explicit
user authorization after P1 evidence `5f96a460...`; it is prospective and does
not change that rejected run. The assigned class and exact limits are
content-bound in every rung.
Exceeding either profile rejects the rung; no further limit increase is
authorized. Before a `p` solve, mesh-only evidence must establish that the
shared near-refined/far-coarse mesh and a conservative resource projection fit
the declared profile. Each rung report must content-bind the mesh bytes, full
mesh manifest and canonical provenance ID, Gmsh module/library/version identity,
all geometry/material/reference/meshing controls and counts, and the run manifest
with its exact resource class and numeric limits. Before any p-axis matrix is
inspected, the aggregate must prove exact equality of the p=2/p=3 mesh bytes,
manifest bytes/content ID, Gmsh identity, and every shared mesh/control field;
order is the sole intentional difference. Missing, stale, tampered, or unequal
identity rejects P1 with null uncertainty. Equality never substitutes for
presence: every report, mesh/config/run identity, and nested provenance field
has a required schema. Aggregate rung controls must equal the stored study
controls; the live config, config-manifest provenance, report, and rung must
prove actual orders 2 and 3 respectively. The nested finite-reference objects
have an exact schema and cross-bind outer scale and three-coordinate bounds.
Run artifacts must cross-bind that same config, mesh, and manifest and must
include hash-validated resolved config, Palace runtime metadata, raw/standard
CSVs, stdout, and stderr. The resolved config is the execution-order witness;
its order, mesh, refinement, materials, boundaries, problem, and linear solver
must match the input/config provenance/report/rung. Runtime counts and CSV bytes
must match the report and run manifest. Mesh/config validation reconstructs safe
finite numeric geometry, positive unique attributes/materials, and fixed reference
meaning rather than trusting equal JSON. The persisted run is revalidated through
the same production config, warning, completion, residual, matrix, resource,
implementation, binary, launcher, and build gates used at execution. Malformed
containers become recorded rejections. These checks precede all p-matrix access.
Because these branch, resource, and identity gates change acceptance code, a
fresh P0 run, review, and acceptance binding are mandatory before this recovery
ladder may execute.

### P2 — complete KiCad volume and material adapter

Reuse the accepted KiCad geometry dump but replace surface-only FasterCap
emission with conformal Palace volume construction. Census and dispose every
track, pad, via barrel, zone, graphic copper object, dielectric layer, solder
mask region included by policy, and external air/reference boundary. Prove
closed conductor topology, shared material interfaces, positive tetrahedral
Jacobians, stable net identity, and complete attribute coverage. Filled zones
alone remain diagnostic.

### P3 — Fugu physical extraction

Run the approved mesh/order/domain ladders over the complete Fugu model. The
first feasibility rung is v13 at outer scale 2, unrefined arrangement mesh,
order 2, and provisional mask `epsilon_r=3.3`. If it remains inside the PCB
profile, the 3-D h pair is that mesh versus `max_planar_area_m2=1e-7` and
`max_vertical_step_m=0.02` at the same outer scale and order. The p pair is
order 2 to 3 on the byte-identical fine mesh. The outer ladder is scale 2 to 3
to 4 on fine order-2 meshes, followed by the mask envelope 1.0/3.3/5.0 on
byte-identical topology. Apply raw gates at every rung and
preserve uncertainty as the maximum adjacent raw matrix difference across all
accepted axes. Aggregate disconnected copper only through explicit ideal
shorts after base-conductor qualification. A passing result may reach
`physical_model_validated` only after independent physical review.

### P4 — EMI integration and calibration

Use the physical matrix in the project-specific Fugu assembly and cable-current
model. Keep infinity, chassis, PE, and circuit ground distinct. Comparative EMC
simulation remains pre-compliance evidence. `predictive_calibrated` still
requires the declared known-added-capacitance and cable-port measurements;
formal EMC acceptance remains measurement-based.

## Current status

- Palace source is pinned. Superseded P0 evidence ID
  `1be70b46d871da41c457db24fa6fb5babd13dd166257d2f1bd06b40bbc0eec09`
  is retained under `docs/artifacts/palace-p0/` as `rejected_diagnostic` evidence.
- Independent xhigh review rejected that smoke because native Palace warnings
  bypassed classification, the first ordered matrix was a symmetric-bilinear-form
  tautology rather than an independent response, CSV units/identity were not
  exact, and normal completion metadata was not required. Revocation ID
  `fa26f504297c06015481639ca58bae402a579e62bb2c1785c81c5f5484bdcb6b`
  records its effective `rejected_diagnostic` lifecycle.
- A second xhigh review rejected reaction-only evidence ID `aed3892e...`
  because `terminal-C.csv` had been populated by copying `C_raw`, removing the
  promised independent energy-form cross-check. Revocation ID `de575dda...`
  records its effective `rejected_diagnostic` lifecycle.
- Independent energy-plus-reaction P0 evidence ID
  `d342b53b84f2c01ddb9ba9e70afb0a25fcc56a816155b6bedc30c97966649457`
  received xhigh `ACCEPT`; acceptance binding ID
  `d13d0b52ef220913c3526480d9ec4554349e7cc7ebf2621d546fdf5ea9355ded`
  authorizes only P1 frozen-fixture ladders. Its local smoke has explicit residuals
  `1.918e-11`/`1.755e-11`, reaction reciprocity error `7.77e-24 F`, maximum
  reaction-versus-energy difference `9.55e-24 F`, complete metadata/build/runtime
  binding including effective `libceed.dylib`, exact CSV identity/units, and no
  Palace warning. The authorized initial P1 attempt rejected with report ID
  `63cc9997...`; its review SHA-256 is `2ddb293c...` and rejects the first recovery
  proposal. Recovery evidence `13ba908d...` was also rejected under binding ID
  `12306858...` because it did not prove p=1/p=2 shared mesh identity. Evidence
  `5b0fc628...` was rejected under binding `adf3810f...` because equal identity
  omissions and an order-1 run relabeled as order 2 remained possible. Evidence
  `a201a028...` was rejected under binding `35a0d2c8...` because nested reference
  omissions and a contradiction in the runtime-resolved order remained possible.
  Evidence `62191232...` was rejected under binding `3860f2d0...` because unsafe
  geometry/reference values, malformed containers, prohibited resolved settings,
  and incomplete run semantics remained accepted. Evidence `334a1712...` was
  rejected under binding `64b86c3d...` because boolean geometry, runtime-resolved
  defaults, negative residuals, and boolean runtime metadata remained accepted.
  Evidence `82f66e04...` was rejected under binding `229c40f3...` because Python
  equality erased boolean/integer distinctions in mesh/reference controls, the
  requested config, and a residual terminal identity witness. Evidence
  `7d96d88b...` was rejected under binding `dc2e991c...` because non-p ladder
  controls were not compared to frozen `RUNGS`, outer permittivity was not tied
  to the Palace outer material, and execution/runtime telemetry allowed scalar
  substitutions. Evidence `e154a6c0...` was rejected under binding `40bdf5ce...`
  because named fixture semantics were not reconstructed, runtime ordering and
  cross-witness checks were incomplete, and a malformed outer reports container
  escaped fail-closed evaluation. Replacement evidence `5a8bfee0...` received
  xhigh `ACCEPT` under binding `bcf78920...` and authorized one complete P1
  attempt. That authorization was consumed by aggregate `3c210ac8...`, which
  correctly returned `rejected_diagnostic`; rejection evidence `7afd3add...`
  received independent xhigh `ACCEPT`. Every in-memory rung had tuple-valued
  finite-reference bounds incompatible with the exact list schema, and all
  order-2 runs exposed pinned resolved-default mismatches (`AMSMaxIts=2`,
  `PartialAssemblyOrder=1`). No ladder axis or uncertainty was accepted.
  Those acceptance-bound paths were corrected and dual-order P0 evidence
  `6568223a...` received xhigh `ACCEPT` under binding `af4b154e...`. Its sole
  P1 authorization was consumed by aggregate `b8f707a7...`, which correctly
  returned `rejected_diagnostic`. Enclosed-FR4 `h_fine` exceeded the frozen
  4 GiB synthetic RSS limit and was terminated; the coplanar p1-to-p2 maximum
  entry difference `5.127286755641463e-15 F` exceeded the unchanged maximum
  allowance `3.002467771150061e-15 F`. Rejection evidence `5f96a460...`
  received independent xhigh `ACCEPT` under binding `e94fb320...`.
  That frozen-policy output remains rejected and was not retried. By explicit
  user direction, subsequent direct qualification raised synthetic RSS to 8 GiB,
  raised the p diagnostic ceiling to 24 GiB, and replaced the slowly converging
  p1-to-p2 pair with p2-to-p3 on the same mesh. Complete aggregate `483eb06d...`
  then passed all 24 h/p/outer rungs with no failures. Maximum RSS was 8.97 GiB;
  the largest p difference was `1.2496715557102749e-14 F` against allowance
  `1.7591304744691002e-13 F`. P1 is numerically converged. This does not itself
  authorize a Fugu matrix or claim native process containment.
- Solver-neutral frozen fixture geometry, deterministic Gmsh volume emission,
  content-addressed mesh/config manifests, explicit-reference checks, Palace CSV
  parsing, raw/downstream matrix gates, and initial unit tests are implemented.
- Complete Palace fixture aggregate `483eb06d...` passes all h/p/outer axes.
- P2 now has a complete two-interpreter KiCad adapter. The stdlib+`pcbnew`
  boundary dumps and censuses tracks, vias, pads, zones, plated and NPTH drills,
  board outlines, unassigned copper, unsupported features, grouping policy, and
  stable item/net identity. System Python reconstructs grouped copper prisms,
  exact polygonal annular barrels, stackup dielectrics, drill voids, and finite
  outer bounds. `all_nets_and_isolated_items` preserves each named net and each
  flashed no-net item separately; NPTH-only pads remain voids, not terminals.
- Gmsh OCC remains unsuitable for plated pad/barrel intersections, but no
  approximation is used. The replacement PLC backend globally nodes every 2D
  material/conductor boundary with MeshPy, reuses that exact arrangement at all
  geometry z-planes, performs a deterministic conformal three-tetrahedron prism
  split, removes conductor cells, and emits Gmsh 2.2 directly. It rejects open
  conductor/material shells, non-manifold faces, incomplete attributes, and
  nonpositive Jacobians. A source-bound backend-specific manifest hashes the
  PCB, KiCad dump, stackup/material policy, geometry controls, MeshPy package and
  native extension, generated mesh, and complete reconstructed geometry.
- The final hardened synthetic plated-via board passed Palace end to end at
  order 2 with 864 nodes and 2,664 tetrahedra. Explicit residuals were
  `4.5073395185890536e-12` and `9.632560627384526e-12` against a `1e-11`
  target. The raw matrix was
  `[[1.1632761325927474e-13, -3.2662883641374494e-14],`
  `[-3.2662883640993026e-14, 7.282584357088305e-14]] F` and passed reciprocity,
  passivity, downstream reconstruction, and independent energy-form gates.
  Runtime was 9.12 s with 176 MB peak RSS. Run content SHA-256 is
  `7ed9fa121756481ca7fa8f39596d6139ff1742575538680ac85b5024792740ce`;
  mesh SHA-256 is
  `8da182f0b6f76508e6701bbd937716219f62d871960257d361766d59e9ee1105`.
  This validates the exact via topology and solver path, not a physical Fugu
  result.
- Complete Fugu source conversion closes over 82 conductor groups, 381
  conductor solids, 229 drills, and three dielectric layers with zero unassigned
  or unsupported copper. Topology attempts v1 through v12 remain stale/rejected;
  each stronger gate preserved its failure rather than weakening acceptance.
  Source-bound v13 independently revalidates and passed final P0/P1 review. It
  passes reconstruction, exact noded-segment coverage, planar
  interface-crossing rejection, all-node adjacent-z-plane checks, material
  attribution, positive canonical Jacobians, physical-group bounds, manifold
  face incidence, marked-boundary closure, and independent manifest revalidation.
  It contains 540,828 nodes and 2,135,874 tetrahedra. Its provenance SHA-256 is
  `bab7d73b784454cf2854e5e82d7941a5cc344bb199c51a6451df0c0a00afd2ff` and
  mesh SHA-256 is
  `6155f0e9c7b9e77a13d1edf4f0d3cbae0bd904b727fe133419616fc81ad849c0`;
  the bound PCB SHA-256 is
  `32906787c3c5bfaf9b0efc0156b6ae40c0d2b29dd69c354e75a8ee8a8b0d3665`.
  P2 topology is therefore `geometry_complete`. Solder-mask `epsilon_r=3.3`
  remains explicitly provisional and cannot support a physical-model claim
  until the actual material is identified or sensitivity bounds it.
- P3 diagnostic execution has started, but no Palace Fugu matrix exists. The
  frozen v13 order-2 baseline was rejected after the 1,800 s PCB wall-time gate
  while still solving right-hand side 1 of 82. PCG reached iteration 311 with
  preconditioned residual norm `2.581542e-3`; the mesh reported geometric
  condition estimate `1.0431e9`. Peak monitored RSS was 11,368,644,608 bytes.
  Rejected run content SHA-256 is
  `fae577c91ad42a71bb4343edafb932ca3117fbe8cc30b3bf9d454e00aabb9d1a`.
  No residual threshold or resource limit was weakened. The preregistered full
  3-D h-refined mesh remained inside the PCB element caps at 5,224,064 nodes and
  30,479,388 total elements, but was rejected because 180 reconstructed source
  segments lacked exact mesh-edge-chain coverage. The subsequent byte-identical
  v13 p1 feasibility run converged its first PCG solve in 18 iterations and
  56.13 s with 9,942,024,192 bytes peak RSS, but the independent explicit
  residual `1.38159534528792132e-10` failed the `1e-10` gate. Its rejected run
  content SHA-256 is
  `49ea68ba4e59c60717da9c093d0ab3e6447882902a605c931afbacde17569441`.
  Following the qualified synthetic recovery policy, the active transition is
  byte-identical p1 with a stricter `1e-11` solver target, not a weakened gate.
  v13 remains topology evidence only, not `physical_model_validated` or
  predictive evidence.
- Darwin's process-tree monitor records and enforces practical wall-time, RSS,
  and output limits. Platform-specific containment is not an acceptance gate.
- No source-bound fabrication record identifies the actual Fugu solder-mask
  dielectric. P3 therefore uses an explicit diagnostic envelope of `epsilon_r`
  1.0, 3.3, and 5.0 for both mask layers. The endpoints are bounding policies,
  not claims about the manufactured material. Each rung must regenerate and
  source-bind its geometry manifest; config-only permittivity substitution is
  forbidden. The three meshes must be byte-identical before matrix comparison.
