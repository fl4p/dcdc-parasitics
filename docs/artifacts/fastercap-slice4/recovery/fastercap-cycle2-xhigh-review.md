# Cycle 2 FasterCap review and final Cycle 3 selection

## Inherited decisions

- Frozen deck SHA-256: `3e639b84072ee71682a8c7414517ab252cbe524020ca71931b70cc60bfed6a33`.
- Preserve automatic `-a0.0125 -ap`, ≤1,000 Arnoldi steps/RHS, ≤500,000 panels, and strict unsymmetrized reciprocity/passivity.
- No symmetrization, thin/open sheets, larger limits, or tolerance changes.
- Cycle 1 ruled out CGS2, Galerkin, component expansion, mesh angles alone, fixed panel-size variants, and `-ps2048/4096`.
- Solver arithmetic changes require unchanged fixture requalification.
- Cycle 3 is the final authorized numerical-recovery experiment.

## Diagnosis

### Blocker — Cycle 2 fails panel and raw-reciprocity gates

The supplied artifacts are provenance-consistent:

- Source SHA-256 `3a520867...`
- Binary SHA-256 `cc7eaf24...`
- Deck SHA-256 `3e639b84...`
- Transcript SHA-256 `2100ffd8...`

Cycle 2 removed the GMRES-1000 failure (`max_gmres_iteration: 707`) but reached 938,216 panels; the first over-limit rung was 574,018 (`out/fastercap-cycle2-state-sync-auto/summary.json`, transcript `:338-360`).

The 938,216-panel automatic terminal has all explicit residuals below 0.00625 (`:382-386`) yet fails all three raw reciprocity pairs:

- SW–VIN: `8.027e-15 > 6.203e-16` — 12.9× tolerance.
- SW–PGND: `2.946e-14 > 1.772e-15` — 16.6×.
- VIN–PGND: `3.77e-15 > 2.064e-15` — 1.83×.

Sign and passivity pass, but that cannot override reciprocity.

### High — recurrence convergence is a real solver defect

Automatic mode sets `gmresTol = 0.0125/2 = 0.00625` at `/Users/fab/dev/vendor/FastFieldSolvers/FasterCap/Solver/SolveCapacitance.cpp:317`.

GMRES stops solely on the Givens/Hessenberg recurrence residual at `SolveCapacitance.cpp:2485-2505`. It reconstructs the solution at `:2526-2566`, computes the true residual at `:2569-2576`, but returns success unconditionally at `:2586`.

That defect is observed directly:

- 194,034-panel SW RHS: recurrence accepted after printed index 707, but true residual is `0.029323` (`transcript:267-268`).
- 330,878-panel SW RHS: true residual `0.00774094` (`:305-306`).

A logged true residual is therefore not currently a convergence gate.

### High — the recurrence defect cannot explain the complete failure

It plausibly explains the large SW reciprocity errors at 194k and 330k because each RHS populates one excitation row (`SolveCapacitance.cpp:1199-1203`, `:1309-1365`), and SW is the first conductor.

It does **not** explain:

1. **Terminal reciprocity:** all three 938k true residuals pass, yet all reciprocity pairs fail, including VIN–PGND.
2. **Refinement below 500k:** FasterCap uses the ordinary Frobenius norm (`/Users/fab/dev/vendor/FastFieldSolvers/LinAlgebra/Mtx.h:464-482`). Zeroing the complete SW-row contribution from the observed 194k→330k difference still leaves
   `||ΔC_VIN,PGND||F / ||C330||F = 0.015628391 > 0.0125`.
   Passing at 330k through SW correction alone would require at least 25.0% inflation of the denominator. The analogous unaffected-row ratio at 116k→194k is `0.02420587`.
3. **Later refinement behavior:** at 574k every true residual passes, while the automatic matrix difference remains `0.0450651` (`transcript:344-355`).

Thus residual recovery may repair individual rows, but it has a low chance of producing an automatic terminal below 500k.

### Medium — requested/effective telemetry remains incomplete

Cycle 2 prints one `Precond Type(s)` line per rung, sourced from `globalVars` (`SolveCapacitance.cpp:667-685`). It does not independently identify the requested automatic policy and the `m_clsGlobalVars` state actually consumed by each RHS at `:2305-2313`.

The source sync at `:346-349` and `:437-440` is correct, but Cycle 3 still needs independently correlated requested/effective telemetry.

## Drift / contradiction check

- Treating true-residual recovery as the final experiment would quietly assume the false SW solve caused automatic refinement. The unaffected VIN/PGND matrix delta and passing-residual 938k reciprocity failures contradict that assumption.
- Median-centroid hierarchy was explicitly retained as the distinct later hypothesis after Cycle 1. The current midpoint split remains unchanged at `/Users/fab/dev/vendor/FastFieldSolvers/FasterCap/Solver/Autorefine.cpp:6594-6609`, despite the prior depth-8 descendant imbalance reaching 837:1.
- Adding median hierarchy and residual restart together would stack unrelated recovery mechanisms. A hard true-residual **gate** is mandatory qualification infrastructure; residual replacement/restart is a separate recovery mechanism and must not be added.

## Recommendation

### Select Cycle 3: deterministic median-centroid 3D hierarchy

Change only `CAutoRefine::RecurBuild3DSuperHier()` at `Autorefine.cpp:6574-6655`:

1. Retain longest-bounding-box-axis selection at `:6594-6595`.
2. Replace midpoint-plane partitioning at `:6596-6609` with a deterministic centroid ordering along that axis.
3. Break ties lexicographically on the other centroid coordinates, preserving original order for exact ties.
4. Split by count at `firstPanel + panelNum/2`; both children are nonempty and differ by at most one panel.
5. Recompute child bounding boxes and recurse through the existing paths at `:6611-6632`.
6. Leave the 2D hierarchy, refinement schedule, automatic Frobenius criterion, GMRES arithmetic, and automatic preconditioner thresholds unchanged.

The shared hierarchy necessarily affects interaction approximation and two-level preconditioner construction; those effects are inseparable consequences of this one hierarchy mechanism, not stacked experiments.

This has the highest remaining chance because it directly targets the only evidence-backed defect capable of affecting operator symmetry, matrix stability, GMRES behavior, and panel refinement together.

### Residual alternatives

- Merely replacing the reported recurrence residual after reconstruction is not recovery.
- Continuing the old Arnoldi recurrence after substituting `b-Ax` is mathematically invalid.
- A one-shot correction is insufficient unless its result is explicitly reverified.
- The minimal sound general GMRES repair would be a verified restart from the current solution—or equivalently repeated correction solves `Aδ=r`, `x←x+δ`—with a fresh Arnoldi basis, explicit residual verification after every cycle, and one cumulative 1,000-step budget.

Do **not** include that recovery in Cycle 3. Instead, convert the existing explicit residual calculation into a fail-closed gate: if `||b-Ax||/||b|| > gmresTol`, return non-convergence without emitting or using that matrix.

### Required telemetry

For every rung and RHS, emit:

- rung panel count and hierarchy policy;
- RHS conductor/index;
- requested automatic preconditioner type/dimension;
- effective member-state type/dimension at GMRES entry;
- recurrence residual;
- explicit `||b-Ax||/||b||`;
- printed final iteration index and total Arnoldi steps.

Also summarize per-conductor hierarchy input count, maximum depth, and minimum/maximum descendant counts at representative depths without logging every node.

### Fixture requalification

Before the frozen deck:

1. Rebuild and record source/binary SHA-256.
2. Run `test/test_fastercap.py:719-746`.
3. Re-run the air, PCB-like, and εr=4.2 Slice 2 fixture ladders at `0.00125/0.0009/0.000625`.
4. Preserve all completion, resource, adjacent-matrix, raw reciprocity, sign, passivity, and branch-reconstruction gates in `docs/fastercap-slice2-reduction-study.md:54-86`.
5. Do not repin tolerances or goldens.

### Exact Cycle 3 stop criterion

Run exactly one monitored frozen-deck endpoint using `-a0.0125 -ap -b`. Fail and stop immediately on any of:

- deck, source, or binary hash mismatch;
- fixture failure;
- requested/effective preconditioner mismatch;
- non-finite or true residual `> 0.00625`;
- more than 1,000 cumulative Arnoldi steps for any RHS, or any 1,000-iteration non-convergence diagnostic;
- refined panels `> 500000`, before building/solving that rung;
- no automatic terminal satisfying `FNorm(Cn-Cn-1)/FNorm(Cn) <= 0.0125` at ≤500k;
- terminal raw-matrix failure under `lib/maxwell.py:140-205`.

Success requires all conditions simultaneously. If Cycle 3 fails, stop numerical-recovery cycling and report the blocker; do not launch Cycle 4.

## Risks

- Count-balanced median splits can increase bounding-box overlap or link count despite improving depth balance.
- The raw reciprocity tolerance is much tighter than the GMRES tolerance; balancing may still be insufficient.
- The hard true-residual gate may terminate Cycle 3 at an early rung. That is valid falsification, not a reason to add restart during the run.
- The supplied evidence contains no Cycle 2 fixture-requalification artifacts, so Cycle 3 must produce fresh fixture evidence.

## Need from main agent

None. The final-cycle authorization and immutable gates are sufficient.

## Suggested execution prompt

> Implement one final FasterCap experiment: replace only the 3D midpoint split in `CAutoRefine::RecurBuild3DSuperHier` with a deterministic longest-axis median-centroid count split, leaving 2D hierarchy, refinement, GMRES, and `-ap` policy unchanged. Preserve Cycle 2 state synchronization. Add per-RHS requested/effective preconditioner, iteration, recurrence-residual, and explicit true-residual telemetry; make true residual `<= gmresTol` a hard acceptance gate without restart or correction. Rebuild with hashes, requalify the real parallel-plate test and frozen Slice 2 fixture ladders under unchanged gates, then run one monitored frozen-deck `-a0.0125 -ap -b` endpoint. Stop on any telemetry mismatch, true residual above 0.00625, more than 1,000 Arnoldi steps/RHS, panel count above 500k, missing automatic terminal, or terminal raw reciprocity/passivity failure. Do not add another recovery mechanism, weaken gates, or run a fourth cycle.