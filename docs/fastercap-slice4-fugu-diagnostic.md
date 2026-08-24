# FasterCap Slice 4 Fugu2 diagnostic convergence

## Scope and fixed study

This slice is a filled-zones-only, air-only diagnostic. It cannot become a
physical PCB model or enter Slice 6. The authoritative PCB hash is
`32906787c3c5bfaf9b0efc0156b6ae40c0d2b29dd69c354e75a8ee8a8b0d3665`.
Groups are SW=`SW`, VIN=`Solar+`, and PGND=`BuckGND`; the pinned dump contains 10
zone records.

The study is preregistered in
`experiments/fastercap_fugu_diagnostic_study.py` before the first valid solver
launch. It uses the explicit 34 µm midplane-anchored closed-thickness path, which
is correctly marked `outside_fixture_envelope` for Fugu2.

Two independent ladders share one production endpoint:

- geometry tolerances 0.002, 0.001, and 0.0005 mm, with automatic FasterCap
  relative error fixed at 0.0125;
- automatic FasterCap relative errors 0.05, 0.025, and 0.0125, with geometry
  tolerance fixed at 0.0005 mm.

The shared endpoint is run first. A rejected endpoint stops the study because no
complete ladder or final matrix can then qualify. The fixed
`fugu_diagnostic` resource class permits 20 minutes, 8 GiB process-tree RSS,
5 GiB output, 500,000 refined panels, and 1,000 GMRES iterations per RHS.

Every rung must independently pass the Slice 1 identity, transcript, automatic
convergence, resource, raw reciprocity, sign, passivity, and downstream matrix
gates. Adjacent unsymmetrized raw matrices must pass
`1e-15 F + 2%*max(abs(entries))` entrywise. If both ladders pass, uncertainty is
the entrywise maximum of all four adjacent differences. Any failed or missing
rung prevents creation of a diagnostic matrix artifact.

## Geometry and provenance gates

Every geometry manifest is consumed through the Slice 3 fail-closed loader. The
study additionally pins the PCB hash, groups, 10-record census, 34 µm effective
thickness, two 35 µm source extrusions per conductor, air-only filled-zone
context, diagnostic lifecycle ceiling, and benchmark areas within `1e-4`
relative error. Disconnected solids are retained as explicit `C ... +` parts of
their SW, VIN, or PGND conductor. Top and bottom faces use exact-boundary MeshPy
constrained-quality triangulation with a 20° minimum input angle; sidewalls
remain quadrilaterals. The manifest pins the triangulation engine, angle, grouped
conductor identity, component census, and panel census.

Extraction commands and byte-exact stdout/stderr are hashed; every solver
command, stream, matrix, resource observation, and deck identity is preserved by
its run manifest. The overall report is content-addressed.

## Result

The final frozen study is retained at
`docs/artifacts/fastercap-slice4/fugu-diagnostic-study.7dd866efe04a21d82970c0340e52752e1e5745363c81a1cfea15731e50bd0d5e.json`.
Its canonical content ID is `7dd866ef...`; the pretty-file SHA-256 is
`c5f04048ff8754f5182a12cf77e4276a8fa91a22bb39be02537bf0dfc55927d7`.
The deterministic complete-evidence bundle is
`docs/artifacts/fastercap-slice4/fastercap-slice4-final.7dd866efe04a21d82970c0340e52752e1e5745363c81a1cfea15731e50bd0d5e.tar.gz`,
SHA-256
`158530f1f82e1f9fb560c19d44d4f987c0bc5e5e39e75452c60a851b62c8ce78`.
It contains all three geometry directories, decks, manifests, extraction
streams, solver streams, run manifest, and study report with normalized archive
metadata.

All three quality-meshed geometry manifests passed the pinned gates. Their
content IDs at 0.002, 0.001, and 0.0005 mm are `15e16cc8...`, `f6129d3a...`, and
`30b73d5f...`; their grouped input panel counts are 18,003, 21,808, and 23,744.
The shared 0.0005 mm, `-a0.0125` endpoint refined from 35,334 to 194,034 panels.
The live stream monitor then terminated the process at the first 1,000-iteration
GMRES breach: the SW RHS residual was 0.008 against a 0.00625 target. The killed
iteration had no complete matrix. The endpoint is therefore
`rejected_diagnostic`; fail-fast execution omitted the remaining rungs, both
ladders are incomplete, and no matrix artifact exists.

Bounded diagnostics established that component separation, larger fixed
preconditioners, Galerkin, and 10°/15°/20° quality meshes do not close all gates.
The 20° grouped mesh removes the original thin-triangle warning and converges
comfortably at `-a0.05`, but the raw matrix remains nonreciprocal; the tight rung
then violates the GMRES limit.

Three source-level recovery cycles were attempted under unchanged gates. Cycle
1 showed that fixed 1024 preconditioning avoids the 1,000-step cliff but still
requires 938,216 panels; CGS2 corrections near `1e-13` ruled out Arnoldi basis
orthogonality as the primary cause. Cycle 2 fixed a real one-rung stale automatic
preconditioner-state defect and exposed explicit SW residuals of 0.029323 and
0.00774094 at 194,034 and 330,878 panels, but still required 938,216 panels and
failed terminal raw reciprocity. Cycle 3 added deterministic median-centroid
count balancing and a hard explicit-residual gate. The live parallel-plate test
passed, but frozen fixture requalification failed before any Fugu launch: the
median hierarchy regressed the enclosed-FR4 operator, while the hard gate exposed
pre-existing false recurrence convergence on PCB-like fixtures. The canonical
fixture report is
`docs/artifacts/fastercap-slice4/recovery/slice2-reduction-study.804e462ee81109b99c1f189fa56b71677712bcbb5b08588f682815b1bf06a09a.json`;
the three independent reviews and cycle summaries are retained beside it.

The sixth mandatory implementation review also remains `BLOCK`: portable
polling does not provide the required native containment for environment-clearing
descendants, transient RSS peaks, and late-spawn termination races. No Fugu2
diagnostic capacitance matrix is available, and Slice 5 cannot start until both
the numerical-solver and native-containment blockers are resolved and
independently reviewed.
