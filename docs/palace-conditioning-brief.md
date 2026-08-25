# Brief: Palace electrostatic solver conditioning struggle (2026-08-25)

Audience: an agent joining cold. Repo root for all relative paths:
`/Users/fab/dev/pv/ee/dcdc-tools/parasitics`.

## Mission context

We extract physical PCB Maxwell capacitance matrices from KiCad boards with
AWS Palace (electrostatic FEM), as the maintained successor to a rejected
FasterCap diagnostic track. The end goal is a converged, passive, reciprocal
matrix for the Fugu2 power stage (82 terminals), then EMC pre-compliance
analysis. Everything is gated fail-closed: any Palace warning, missing
residual, metadata mismatch, or positive off-diagonal mutual capacitance
rejects a run (`lib/palace.py`, `lib/palace_matrix_gates.py`). Lifecycle
states run `rejected_diagnostic` → `numerically_converged_diagnostic` →
`geometry_complete` → `physical_model_validated`. Nothing below a complete,
warning-free, convergence-laddered matrix may be called physical capacitance.

## The struggle in one paragraph

Small models solve; the real board does not. Every refined Fugu mesh stalls
in BoomerAMG-preconditioned CG at an average residual reduction factor of
~0.974–0.982 per iteration, independent of MPI rank count, refinement
direction, aggressive-coarsening setting, or Krylov method (CG vs restarted
GMRES). Memory is *not* the limit (peaks 10–14.6 GB inside a 24 GiB
profile). This is a preconditioner quality problem on thin-layer PCB
extrusion meshes, and we have exhausted the cheap knobs.

## Chronology of evidence (all under `out/palace-qualification/`)

1. **Fixture ladder (24 rungs)**: all pass. The pipeline, gates, and build
   (`/Users/fab/dev/vendor/palace-build-qualification-make/bin/palace`,
   native per-RHS checkpointing added in vendor commits `d28dbfd5`,
   `0ca2de94`) are sound.
2. **simple-hb canary** (18 terminals, borrowed Fugu2 stackup, mesh
   `d0b1ed39…`, 22,526 nodes): p1 passes in ~7 s. p2 completed 18/18 RHS
   via checkpoint+resume (`simple-hb-user-space-p2-checkpoint-v2`). The p2
   matrix is reciprocal, positive definite, sole defect +0.298 aF symmetric
   positive mutual (numerical noise; gate correctly rejects, we did NOT
   weaken it). But p1→p2 convergence FAILS: 224/324 entries move more than
   2% + 1 fF. So p-convergence is real and p3 is mandatory.
3. **simple-hb p3 probe** (`simple-hb-p3-mpi4-probe-v1`): zero RHS
   checkpoints in 300 s at 4 ranks — slow, but PCG was still reducing, not
   stalled.
   **Update 2026-08-25**: a checkpointed 4-rank p3 campaign
   (`simple-hb-p3-v1`) completed 18/18 RHS in ~1.5 h and was ACCEPTED
   (`numerically_converged_diagnostic`, zero positive off-diagonals — the
   +0.298 aF p2 defect vanished). But p2→p3 convergence fails 182/324
   entries: ALL entries shrink ~65% p1→p2 and ~44% p2→p3, rung deltas
   decaying geometrically at ratio ≈ 0.2 — monotone convergence from above
   dominated by thin-copper edge singularities. Meeting 2%/rung would need
   ~p5–p6. The p-ladder as specified cannot pass on this mesh family; see
   ask 3 below, which is now the live question, plus edge-targeted
   h-refinement as the mesh-side alternative (bounded by the conditioning
   ceiling above). A `PeakNodeMemoryMegabytes` validation bug (per-node vs
   per-rank aggregation) that falsely rejected all multi-rank completions
   is fixed in `lib/palace.py`.
4. **Fugu full board** (mesh v13, 540,828 nodes / 2.14 M tets / 82
   terminals, `geometry_complete`): coarse p1 completes numerically but the
   matrix is unphysical — 71 positive off-diagonals, worst +35.8 pF
   (`Bat+` ↔ `Net-(D11-A)`). Treated as a discretization defect needing
   refinement.
5. **Refined Fugu attempts — ALL fail RHS 1 at the iteration cap**:
   - planar midpoint mesh (2.11 M nodes / 11.06 M tets), 1 and 4 ranks:
     reduction factor 0.974, residual ~1.8e-6 after 500 its
     (`fugu2-p3-ladder/mid-p1-outer2-er3p3-v1`).
   - vertical-only refinement (1.01 M nodes / 4.96 M tets): same signature
     (`fugu2-p3-ladder/z20-p1-outer2-er3p3-v1`).
   - `AMGAggressiveCoarsening=False`: 0.972. No cure.
   - restarted GMRES(100): worse (4.5e-5 after 500).
   - two-terminal principal-block probes (others grounded): p2 coarse and
     p1 midpoint both fail RHS 1 even with 750–1250 iterations
     (`fugu2-p2-principal-bat-d11-v1`, `fugu2-hmid-p1-principal-bat-d11-*`).
6. **Reduced Fugu patch** (new, diagnostic-only,
   `fugu2-q3-q2-c27-geometry-v1` + hash-bound derivation receipt in
   `fugu2-q3-q2-c27-source-v1`): one HS FET (Q3), one LS FET (Q2), one
   input cap (C27); 5 nets clipped to a 2 mm-margin ROI; 49,011 nodes /
   190,014 tets / 5 terminals. p1 passes all gates in 6.5 s. p2 at 4 ranks
   reached explicit residuals ~1e-12–1e-8 but PCG missed its internal
   1e-12 criterion within 1250 its (reduction factor ~0.978) → warning →
   rejected. The quarantined p2 matrix is reciprocal (5.6e-22 F), positive
   definite, ZERO positive off-diagonals; p1→p2 moves 19/25 entries.
   A serial retry with `Tol=1e-11` (keeping `VerificationTol=1e-10`) was
   killed by the 1800-s wall while sharing cores with the p3 campaign —
   inconclusive, parked.

## Key readings of the evidence

- The stall reduction factor is ~0.974–0.982 across two orders of magnitude
  of problem size (49 k → 11 M tets). BoomerAMG's coarse grids do not
  capture the near-kernel of these thin, high-aspect extruded PCB layers
  (25 µm copper / mask layers vs mm lateral extents → highly anisotropic
  tets and jumpy ε).
- The reduced patch shows the conditioning penalty exists even at tiny
  scale but is *surmountable* there (residual reaches 1e-12 territory);
  the full board is the same disease at lethal dose.
- Positive off-diagonals on coarse Fugu p1 are a discretization artifact;
  the reduced patch at p2 has none, supporting the refinement hypothesis —
  but refinement is exactly what the solver cannot currently deliver.

## Decision state

- **Gate ordering (user-corrected)**: simple-hb must pass p2→p3 BEFORE any
  further Fugu qualification. Resuming Fugu earlier was a mistake; the
  reduced patch is explicitly diagnostic-only and advances no lifecycle
  state (see memory `simple_hb_palace_intermediate_only`).
- **Currently running**: bg job "simple-hb p3 checkpointed campaign"
  (`out/palace-qualification/simple-hb-p3-v1/campaign.py`): order 3,
  `Tol=1e-12`, `VerificationTol=1e-10`, max 1250 its, 4 ranks, 1800-s
  attempts with native per-RHS checkpoint/resume, fail-closed on
  no-forward-progress attempts, ≤12 attempts.
- **Explicitly rejected next steps** (no new hypothesis → no more compute):
  more CG iterations on refined Fugu, MPI rank variation,
  aggressive-coarsening toggles, restarted GMRES.

## What we want from a fresh look

1. A **mesh-hierarchy-aware preconditioner** path: geometric multigrid via
   MFEM/Palace, or AMG parameter families known to work for thin-layer
   extrusions (e.g., BoomerAMG strength thresholds, HMIS/PMIS coarsening,
   aggressive levels tuned for anisotropy, or line/plane smoothers).
   Palace's ksp/AMG surface is in `/Users/fab/dev/vendor/palace`
   (`palace/linalg/ksp.cpp`, config schema
   `scripts/schema/config-schema.json`).
2. Alternatively a **materially different meshing strategy** that removes
   the anisotropy at acceptable cost: z-graded but laterally coarsened
   meshes, boundary-layer meshing, or higher-order geometry with fewer,
   better-shaped tets. Mesher lives in `lib/palace_plc_mesh.py` (TetGen via
   MeshPy on an extruded PLC; per-layer z-planes are exact and validated).
3. Sanity-check the p-ladder economics: p2→p3 on simple-hb costs hours;
   if there is a principled cheaper convergence-evidence design (e.g.,
   Richardson-style extrapolation across h at fixed p with the existing
   gates), name it — but do NOT propose weakening tolerances or gates.

## Addendum 2026-08-25: direct-solver escape hatch (fresh-look findings)

1. **The qualification build had every sparse direct solver disabled except
   SuperLU_DIST**, which is compiled in and working
   (`PALACE_WITH_SUPERLU=ON` in the build cache; `PALACE_WITH_MUMPS` and
   `PALACE_WITH_STRUMPACK` were OFF). "Exhausted the cheap knobs" was
   premature: `"Type": "SuperLU"` was available all along.
2. A second build with **MUMPS + STRUMPACK + SuperLU** all enabled now
   exists at `/Users/fab/dev/vendor/palace-build-qualification-direct-make`
   (same vendor commits `d28dbfd5`/`0ca2de94`; SLEPc/SUNDIALS/GSLIB OFF to
   match; configure needs `OPENBLAS_DIR=/opt/homebrew/opt/openblas`). It is
   NOT yet qualified (no build-v2 manifest, no fixture ladder).
3. `lib/palace.py` + `lib/palace_workflow.py` now thread a
   `linear_solver_type` knob (`BoomerAMG|SuperLU|STRUMPACK|MUMPS`,
   default unchanged) through config, provenance, and the resolved-config
   gate. Legacy manifests load as BoomerAMG. 9 new tests; 221 pass.
   `MGMaxLevels` is untouched, so a direct type = exact coarse solve inside
   the existing p-multigrid; at p1 it is the whole solve.
4. Progress-pattern bug fixed: MFEM prints "converged in 1 iteration"
   (singular); the `rhs_converged`/`rhs_not_converged` regexes required the
   plural and fail-closed-rejected an otherwise perfect run. AMG never
   converged in 1 iteration, so this was unreachable until now.
5. **simple-hb p1 SuperLU probe** (existing qualification binary,
   `simple-hb-p1-superlu-probe-v1`): all gates green in 5.0 s, 1 CG
   iteration per RHS, avg reduction factor 5.6e-15, matrix agrees with the
   AMG p1 matrix to 4.1e-13 relative.
6. **simple-hb p3 is NOT preconditioner-bound**: the AMG checkpoint
   campaign completed 18/18 on 2026-08-25 08:20-10:01 (~5.6 min/RHS;
   attempt-06 assembled `numerically_converged_diagnostic`), and a SuperLU
   p3 campaign (`simple-hb-p3-superlu-v1`) sustains the same ~6 min/RHS.
   Fine-level Chebyshev/assembly work dominates the canary; the AMG coarse
   solve was already adequate THERE. The 0.974-stall disease is
   Fugu-specific, where the direct coarse solve replaces a preconditioner
   that cannot converge at all.
7. **p2→p3 ladder verdict (diagnostic)**: FAILS 182/324 entries at
   2% + 1 fF (p2 quarantined matrix from `simple-hb-user-space-p2-v2-resume1`
   vs p3 attempt-06). Convergence evidence needs higher rungs. Projected
   DOF on the frozen canary mesh: p4 = 1.16 M, p5 = 2.22 M — affordable
   with direct-coarse multigrid.
8. **The stalling component is the fine-level smoother, not the coarse
   solve.** Reduced-Fugu p2 with SuperLU as the MG coarse solver STILL
   stalled at reduction factor 0.976 (verification abort at 3.4e-9
   explicit residual). Chebyshev smoothing cannot damp the anisotropic
   error components on these meshes; an exact coarse solve does not help.
9. **`multigrid_max_levels=1` + direct factorization kills the stall.**
   New fail-closed knob (None or 1 only; hierarchy gate expects the
   single-level `MultigridDegreesOfFreedom` accordingly). Reduced-Fugu p2
   (189 k DOF, 4 ranks, SuperLU): ALL GATES GREEN in 16 s, 1 CG iteration
   per RHS (reduction ~1e-13), 5.1 GB peak. Matrix agrees with the
   quarantined stalled-AMG p2 to 3.4e-11 relative, zero positive
   off-diagonals, reciprocity 7e-25 F. The parked serial Tol=1e-11 probe
   is retired — its question is answered with a gate-grade run.
   At order 1 no knob is needed: `Type=SuperLU` already IS the full
   direct solve (single-level hierarchy).
10. **Refined-Fugu z20 p1 direct solved 82/82 RHS in 574 s, 16.5 GB**
    (SuperLU, order 1 = full direct, 4 ranks). Prior iteration-cap stall is
    dead. BUT: the matrix gate fires on positive off-diagonals — the worst
    is Bat+ ↔ Net-(D11-A) at **+35.8 pF, mesh-independent** (coarse p1 and
    z20 p1 give identical values to 4 significant figures; the coarse
    `+3.5794e-11` was incorrectly dismissed as discretization in an earlier
    run). The gate was correct and the solver was right all along.
11. **Root cause is a geometry extraction defect, not discretization.**
    The +35.8 pF mutual is 250× larger than the next positive entry (0.14 pF).
    Bat+ and Net-(D11-A) are disjoint bottom-layer polygons 1.2 mm apart,
    do not touch domain boundaries, and have normal diagonal entries +
    perfect reciprocity → the matrix is numerically sound. D11 is an
    SS310 Schottky diode on the PowerSupply sheet. The extraction
    incorrectly isolates its anode pad as a separate conductor from Bat+
    (which the physical circuit connects), producing a converged but
    unphysical capacitance entry. The midpoint p1 probe is UNNECESSARY —
    the z20 vertical-only refinement already proves mesh independence;
    further XY refinement will not change the sign. Fix the extraction,
    not the mesh.
12. **simple-hb p3 SuperLU completed 18/18 RHS** (3 attempts, last 927 s,
    3.3 GB, 641-687 CG its/RHS, reduction ~0.960, numerically_converged).
    Zero positive off-diagonals, reciprocity 5.6e-23 F. Matrix agrees with
    p3 AMG (attempt-06) to 3.2e-12 relative — both solvers produce the
    same discrete answer.
13. **p2→p3 ladder FAILS 182/324 entries** (18 diagonal + 164 off-diagonal).
    Diagonals are the dominant failure mode: p2/p3 ≈ 1.6-1.8× across all
    18 terminals (e.g., terminal 1: 7.23→4.19 pF). This is consistent with
    slow p-convergence on thin-layer extrusions — the field singularities
    at conductor edges need high polynomial degree. P4 (1.16 M DOF)
    campaign launched; p5 (2.22 M DOF) pending if p4 shows approach to
    convergence.
14. The refined-Fugu solver path is proven but the matrix defect (+35.8 pF
    Bat+↔Net-(D11-A), mesh-independent) needs geometry-side root cause
    before further Fugu qualification.
15. **`multigrid_max_levels=1` scales to the canary at p4.** simple-hb p4
    full-direct (8 ranks, SuperLU): ALL GATES GREEN in 94.5 s, 11.34 GB,
    18/18 RHS at **1 CG iteration each**, zero positive off-diagonals,
    reciprocity 2.5e-25 F. The iterative path at p4 was dying: reduction
    factor degraded 0.960 (p3) → 0.978 (p4), one RHS already exhausted the
    1250-iteration cap, and the checkpointed campaign was pacing ~1 RHS per
    1800-s attempt (13+ h projected, ending in a gate failure). Direct
    factorization is ~500× faster here and converges unconditionally.
16. **Gate defect found and fixed: `_validate_execution_runtime_binding`
    compared against a quantity that is not a lower bound.** Palace's
    `PeakNodeMemoryMegabytes` equals `PeakMemoryMegabytes["Total"]` in every
    observed run — it is the sum over ranks of each rank's peak-over-time.
    The monitor samples the simultaneous tree total. Since
    `sum_r max_t >= max_t sum_r` always, the gate demanded the measurement
    exceed an upper bound of itself; it passed only because RSS
    double-counts shared pages across ranks (p1 probe: 829 MB observed vs
    498 MB reported, 1.66x inflation). At 8 ranks SuperLU's fill imbalance
    staggers the per-rank peaks and the inflation stopped covering the gap,
    producing a false rejection of a clean p4 solve. The check now uses
    `PeakMemoryMegabytes["Max"]`, the largest single-rank peak, which IS a
    valid lower bound on the observed node peak. Three regression tests
    pin the staggered-peak accept and both reject paths. Separately,
    `experiments/palace_fixture_ladder_study.py` carried its own hardcoded
    provenance key set and had been rejecting every config written since
    the `linear_solver_type` knob landed; it now mirrors the loader's
    optional-key contract. 407 tests pass.
17. **THE P-LADDER CANNOT PRODUCE CONVERGENCE EVIDENCE ON THIS MESH.**
    Trace of the raw matrix: p1 103.155, p2 36.792, p3 21.755, p4 15.744 pF
    (ratios 2.80, 1.69, 1.38). Successive differences 15.037 then 6.011,
    ratio 2.50. Aitken extrapolation from (p2,p3,p4) puts the limit at
    ~11.71 pF, so **p4 is still 34.5% high** (per-terminal 19-46%).
    Extrapolating the 2.50 difference ratio, the 2% gate needs ~p7-p8.
    Both routes there are closed:
      - direct: p5 ~26.9 GB (over the 24 GiB profile), p7 ~100 GB;
      - iterative: the smoother is already failing at p4.
    Root cause is **mesh conditioning, not the solver**: the frozen canary
    mesh reports `h` from 1.77e-5 to 0.578 m (4.5 decades) and element
    `kappa` up to **1.36e7**. Sliver elements over-stiffen the H1 energy,
    which is why p1 sits ~9x above the extrapolated limit and why the error
    decays so slowly under p-refinement. A p-ladder on a degenerate mesh
    measures mesh quality, not discretization convergence.
18. **Recommended redesign**: get convergence evidence from h-refinement
    with element-quality control (bounded `kappa`) at fixed low order,
    where each rung is cheap and full-direct solvable, instead of p-refining
    a mesh with 1e7 aspect ratios. This requires mesher work in
    `lib/palace_plc_mesh.py` (quality-driven TetGen constraints), and it
    supersedes the p4/p5 plan in item 12. Do not weaken the 2% + 1 fF gate
    to make the existing ladder pass.

## Hard constraints for any successor

- Never present non-converged or partial results as physical capacitance;
  never weaken the sign/warning/residual gates.
- Ordinary user-space execution only; no privileged machinery.
- Byte-identical meshes across a p-ladder; content-addressed provenance
  everywhere; fresh output directories per attempt; checkpoint roots are
  the only shared mutable state.
- Do not commit without explicit instruction. The working tree carries
  unrelated concurrent-agent edits — stage nothing you did not write.
- Machine is shared: check running background jobs before launching
  multi-rank solves (the last serial probe died by core contention).
