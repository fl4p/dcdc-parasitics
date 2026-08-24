# Cycle 1 FasterCap review

## Inherited decisions

- Preserve automatic `-a0.0125 -ap`, ≤1,000 GMRES iterations/RHS, ≤500k panels, and strict unsymmetrized reciprocity/passivity.
- Frozen deck SHA: `3e639b84072ee71682a8c7414517ab252cbe524020ca71931b70cc60bfed6a33`.
- No larger limits, symmetrization, thin/open sheets, or previously falsified approaches.
- Solver arithmetic changes require fixture requalification.

## Diagnosis

### Blocker — automatic preconditioner state lags its printed state by one rung

`m_clsGlobalVars` is copied **before** `AutoSetPrecondType()` mutates `globalVars`; FasterCap then prints the new `globalVars` but solves using stale `m_clsGlobalVars`:

- Initial rung: `Solver/SolveCapacitance.cpp:329-349`
- Subsequent rungs: `Solver/SolveCapacitance.cpp:399-438`
- Preconditioner selection mutates only its argument: `Solver/SolveCapacitance.cpp:580-607`
- The solve and preconditioner construction consume `m_clsGlobalVars`: `Solver/SolveCapacitance.cpp:1043-1055`, `Solver/SolveCapacitance.cpp:1070-1112`, `Solver/SolveCapacitance.cpp:2302-2314`

Consequently, the 194,034-panel automatic transcript prints 1,024 but actually uses the previous rung’s 512 state. Its 1,000/314/343 traces and matrix exactly match fixed-512 (`out/fastercap-cycle1-cgs2-auto/cgs2-auto.stdout.072844372cc157a9064674da47c7b3fac43aff4a6c2d7fb4e3c9dd5c4213d599.bin:266-281`; fixed-512 `...bc2f4a0f...bin:245-261`). At the next rung, 1,024 finally becomes effective and iterations fall to 310/53/60 (`cgs2-auto...bin:302-319`).

Therefore the inherited claim that “the stall occurs when automatic preconditioning becomes 1,024” is false: it occurs while stale 512 remains effective. Fixed-1,024’s 707/161/164 result independently confirms this (`fixed1024...5709b374...bin:245-259`).

### High — no cross-rung warm start

Automatic mode rebuilds and deallocates each rung (`Solver/SolveCapacitance.cpp:368-390`, `:468-476`); GMRES vectors and preconditioners are destroyed at `Solver/SolveCapacitance.cpp:1769-1829`. Each RHS starts from `P*b`, not the preceding mesh solution (`Solver/SolveCapacitance.cpp:2302-2324`). The `X0` routine is uncalled. Warm-start discontinuity is not the cause.

### High — reported residual and auto estimator remain unqualified

GMRES stops on the Givens/Hessenberg recurrence residual (`Solver/SolveCapacitance.cpp:2493-2517`); the explicit `||b-Ax||` check is commented out (`Solver/SolveCapacitance.cpp:2577-2583`). Actual residual is therefore unknown.

Automatic convergence considers only consecutive matrix Frobenius difference (`Solver/SolveCapacitance.cpp:443-462`) while refinement follows a geometric `m_dMaxMeshEps/√2` schedule (`Solver/SolveCapacitance.cpp:383-426`). It never evaluates raw reciprocity.

All observed matrices pass raw sign/passivity but fail reciprocity. Even the 938,216-panel matrix has a worst reciprocity error 16.6× its fixed tolerance, despite FasterCap reporting `0.00330837` automatic convergence (`fixed1024...bin:350-364`; gate policy `lib/maxwell.py:140-205`).

## Drift / contradiction check

- Printed preconditioner telemetry was incorrectly treated as effective state.
- CGS2 corrections near `1e-13` and its unchanged 194,034-panel failure falsify orthogonality loss as primary.
- Fixed 1,024 resolves the GMRES limit but does not resolve automatic refinement: its 330,878-panel norm is `0.0214828`, followed by 574,018 panels (`fixed1024...bin:272-334`).
- Thus state synchronization is a correctness prerequisite, not evidence that Cycle 2 will necessarily pass the complete panel/reciprocity gates.

## Recommendation

### Cycle 2: synchronize automatic preconditioner state

Minimal source experiment:

1. Keep the Cycle 1 CGS2 code otherwise unchanged so the experimental delta is isolated.
2. Immediately after both `AutoSetPrecondType()` calls at `Solver/SolveCapacitance.cpp:346` and `:435`, copy the updated preconditioner state into `m_clsGlobalVars`—preferably the complete `globalVars` object.
3. Emit requested/effective preconditioner type and dimension before each RHS.
4. Re-enable an explicit relative residual `||b-Ax||/||b||` after solution reconstruction, logging it alongside recurrence residual and total iterations. Do not alter stopping tolerance during this cycle.

This is the highest-confidence next move because it repairs a proven mismatch between advertised `-ap` policy and executed arithmetic. Expect the 194,034-panel cliff to improve; do not assume full qualification.

### Fixture requalification

Before Fugu:

- Rebuild and record source/binary SHA-256.
- Run the real parallel-plate test at `test/test_fastercap.py:719-746`.
- Re-run the frozen Slice 2 air, PCB-like, and εr=4.2 fixture ladders at `0.00125/0.0009/0.000625`; require their existing completion, resource, raw reciprocity/passivity and matrix-difference gates unchanged (`docs/fastercap-slice2-reduction-study.md:54-86`).
- Do not repin tolerances or goldens to accommodate the patch.

Then run exactly one monitored frozen-deck `-a0.0125 -ap -b` endpoint.

### Exact falsification criterion

Cycle 2 is falsified immediately if any of the following occurs:

- requested and effective preconditioners differ;
- any RHS reaches the 1,000-iteration failure;
- explicit relative residual exceeds `0.00625`;
- no automatic terminal matrix exists at ≤500,000 panels;
- the terminal raw matrix fails any existing reciprocity/passivity gate.

In particular, reaching the known 574,018-panel rung is sufficient failure; stop without trying tighter tolerance, larger limits, or symmetrization.

## Risks

- Fixed-1,024 evidence predicts corrected state may still produce norm `0.0214828` at 330,878 panels and exceed 500k next.
- True residual may disagree with the recurrence residual, requiring a later residual-replacement/restart experiment.
- The frozen input hierarchy is extremely imbalanced under midpoint partitioning—depth-8 aggregate descendant counts span up to 837:1—so a median-centroid hierarchy remains a plausible later experiment, but should not be stacked into Cycle 2 (`Solver/Autorefine.cpp:6574-6645`).

## Need from main agent

None; the existing recovery-cycle authorization covers this bounded source experiment.

## Suggested execution prompt

> Patch FasterCap automatic mode so the post-`AutoSetPrecondType` state at `SolveCapacitance.cpp:346` and `:435` is copied into the `m_clsGlobalVars` actually consumed by `SolveForCapacitance`. Add requested/effective preconditioner telemetry and explicit `||b-Ax||/||b||` telemetry without changing convergence logic. Rebuild with source/binary hashes, requalify the real parallel-plate test and frozen Slice 2 ladders under unchanged gates, then run one monitored frozen-deck `-a0.0125 -ap -b` endpoint. Stop as failed on any 1,000-iteration diagnostic, true residual >0.00625, raw gate failure, or refinement above 500k; do not weaken gates or stack another numerical mechanism.