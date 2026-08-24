# FasterCap Slice 1 diagnostic result

## Verdict

Slice 1 establishes fail-closed run classification; it does not produce a
qualified Fugu2 capacitance matrix. The existing Fugu2 filled-zone air deck is
`rejected_diagnostic` for two independently measured reasons:

1. adaptive refinement exceeds the `fugu_diagnostic` 500,000-panel resource
   limit, reaching 626,785 panels in the incomplete ninth iteration; and
2. GMRES repeatedly reaches 1,000 iterations without its 0.005 residual target.

The archived full partial transcript is
`test/fixtures/fastercap_fugu_1pct_full.out` (SHA-256
`9e2b8ffa781610ca3a712e620eb4be8d9af46d9390c6903eb6c5457908db4d92`).
The smaller regression excerpt is
`test/fixtures/fastercap_fugu_complete_matrix_failure.out`. It deliberately
contains a complete matrix followed by a later incomplete iteration, proving
that matrix completeness is not solver convergence.

## Measured evidence

The 1% automatic run began from 7,925 input panels. Completed refinement
matrices were non-monotonic, and 16 right-hand-side solves reported failure at
1,000 GMRES iterations. Their residuals span 0.006–0.048 with median 0.015,
against a 0.005 target. The
last completed matrix had 6.65% maximum pairwise reciprocity error. Refinement
then grew from 368,928 to 626,785 panels. The 30-minute duration came from the
external background supervisor notification and is not encoded in FasterCap's
partial transcript; it is timing context, not an archived solver measurement.
No capacitance from that run is valid.

These observations separate two demonstrated blockers:

- **linear solve:** explicit per-RHS non-convergence;
- **adaptive geometry:** superlinear panel growth beyond the fixed resource
  class.

They do not yet identify whether physical copper aspect ratio, contour density,
or a particular FasterCap preconditioner dominates those blockers. Slice 2
must vary those factors independently on bounded fixtures; changing them all in
one Fugu2 rerun would not identify a cause.

## Installed-solver diagnostic

The initial FasterCap 6.0.7 macOS build emitted
`Error: cannot retrieve the information about the free memory quantity` while
continuing in-core. Gate policy `fastercap-pcb-gates-v1` correctly rejected that
explicit diagnostic. The local solver now obtains available memory from Mach VM
statistics on macOS before making its in-core/out-of-core decision; the rebuilt
binary passes the real parallel-plate smoke test without an error. Unpatched
binaries remain rejected. The independent process monitor also measures
process-tree RSS, but it does not authorize ignoring solver-reported errors.

## Harness behavior

- Automatic `-a`/`-ap` runs can return a matrix only after every identity,
  completion, RHS, resource, raw-matrix, downstream-matrix, and exact branch
  gate passes.
- Manual fixed-`-m`/fixed-`-t` preconditioner probes always remain
  diagnostic-only, even when their individual gates pass.
- Failed runs preserve byte-exact streams, all diagnostic matrices, effective
  settings, per-RHS evidence, live resource observations, and content-addressed
  manifests. They expose no qualified matrix.
