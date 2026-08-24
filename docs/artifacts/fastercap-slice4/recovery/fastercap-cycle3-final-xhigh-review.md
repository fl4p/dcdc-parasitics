# BLOCK — Cycle 3 FasterCap recovery failure

## Inherited decisions

- Cycle 3 was the third and final authorized numerical-recovery experiment. No Cycle 4 is authorized.
- Immutable qualification gates remain: automatic `-a0.0125 -ap`, ≤1,000 Arnoldi steps/RHS, ≤500,000 panels, explicit residual acceptance, and strict raw reciprocity/passivity without symmetrization.
- Solver changes require complete fixture requalification before any frozen Fugu endpoint.
- Attested source/binary hashes match:
  - `Autorefine.cpp`: `730c76d8f0c9c787dcd5acb84907775ac0c6f0e0d4698e72ba5ff63c1366a069`
  - `SolveCapacitance.cpp`: `9a5bb458de14af2eeca3908fb17d706a1b70e8e33c978dc49a8ae88fdb35a039`
  - Binary: `f75b2610ff4c7261e31ab70d1cfffc7237d12844e9496d89335dc2e3f12190ee`

## Diagnosis

### BLOCKER — fixture requalification failed before Fugu

The canonical fixture result is:

`out/fastercap-cycle3-fixture-requalification/slice2-reduction-study.804e462ee81109b99c1f189fa56b71677712bcbb5b08588f682815b1bf06a09a.json`

It is explicitly fixture-only and does not authorize Fugu use (`:74-75`). The decisive failure is the enclosed-FR4 physical reference at its first `a=0.00125` rung:

- At 7,428 panels, automatic mode changed from Jacobi to requested/effective two-level type `4/4`, dimension `128/128`.
- Hierarchy telemetry was `(12, depth 4)`, `(460, depth 9)`, `(460, depth 9)`.
- The first RHS exhausted exactly 1,000 Arnoldi steps with recurrence residual `1.64837` and explicit residual `44.6349`, versus the actual `0.000625` target.
- The run returned 1 with `matrix: null` and lifecycle `rejected_diagnostic`.

Locators:

- Transcript: `out/fastercap-cycle3-fixture-requalification/parallel_plate_enclosed_fr4/physical_35um/a_0.00125/model.lst.stdout.8103d161ef9c62e749a452150828d08e7e382053a1c5948d3f284d92b7b27dad.bin:444-463`
- Manifest: `out/fastercap-cycle3-fixture-requalification/parallel_plate_enclosed_fr4/physical_35um/a_0.00125/model.lst.run.rejected_diagnostic.e9a9de29143be9f4a24d65806e7315b8fe70c702d30fe1b7a3e1947afb9a7e5f.json:424-436,850-851,909-950`

The printed generic termination is only the wrapper. The exact cause is GMRES non-convergence followed by the hard explicit-residual rejection in `SolveCapacitance.cpp:2584-2595`.

The enclosed-FR4 `effective_34um` candidate independently fails at 7,460 panels and 1,000 steps: recurrence `0.341409`, explicit `10.4234`, target `0.000625`, with hierarchy `(12,4)/(476,9)/(476,9)` and requested/effective `4/4, 128/128`:

`out/fastercap-cycle3-fixture-requalification/parallel_plate_enclosed_fr4/effective_34um/a_0.00125/model.lst.stdout.e69d69d656bdd4707e2b5c77dd90859b9ec6416b85367093aa2eb236f5cb02b4.bin:444-463`

### HIGH — Cycle 3 shows two different failure mechanisms

**Enclosed FR4 is a hierarchy/operator regression, not merely newly exposed false convergence.** At the physical-reference initial rung, recurrence and explicit residual agree and pass (`0.000553792/0.000553793` and `0.000529273/0.000529273`), yet the resulting matrix already contains a positive off-diagonal and negative diagonal:

`out/fastercap-cycle3-fixture-requalification/parallel_plate_enclosed_fr4/physical_35um/a_0.00125/model.lst.stdout.8103d161ef9c62e749a452150828d08e7e382053a1c5948d3f284d92b7b27dad.bin:30-45`

Subsequent accepted Jacobi solves produce violently unstable matrix differences, including `1.51934` and `15.4981`, before the two-level transition fails. Thus accurate solution of the Cycle 3 approximate operator does not produce a valid physical matrix. The median hierarchy/current operator path regressed mixed-dielectric behavior; a GMRES restart alone cannot repair that.

**PCB-like `effective_1um` is genuine pre-existing recurrence false convergence exposed by the new gate.** At 16,192 panels under matched Jacobi `1/1, 128/128`, the recurrence residual is `0.000624993`, just below `0.000625`, after 486 Arnoldi steps, while the explicit residual is `0.0121497`—19.44× over target:

`out/fastercap-cycle3-fixture-requalification/pcb_like_coplanar_air/effective_1um/a_0.00125/model.lst.stdout.1c909417d8d18e0743193d7cf3843a4c28114cb9594557d6aa1c7d2471e3fe57.bin:211-227`

The hard gate correctly prevented matrix emission.

Two other PCB candidate rungs completed numerically but were rejected by the unchanged exact branch-reconstruction gate:

- `effective_34um`, `a=0.000625`: 83,007 panels, final norm `0.000389264`, process exit 0 — `.../model.lst.run.rejected_diagnostic.41c0b0010fe3688470d305591dfde633cfeb149d68f2fdec294d5e518ad0ff8f.json:742,752,1288`
- `simplify_1um`, `a=0.00125`: 28,259 panels, final norm `0.000690576`, process exit 0 — `.../model.lst.run.rejected_diagnostic.16026f7d74a3872b0a90551a70e0204d6f08f7b9629a5ec6ac73ed5f83e70d7f.json:613,623,1066`

### MEDIUM — telemetry confirms state synchronization but cannot fully explain hierarchy quality

Across 263 requested/effective telemetry records, there are zero mismatches. The Cycle 2 state-sync defect did not recur.

However, `Autorefine.cpp:6479-6487` logs only input count and the depth implied by count balancing. It does not record observed descendant ranges, bounding-box overlap, aspect ratio, or link inflation. Therefore it proves count balance, not that the median hierarchy remains a good spatial interaction hierarchy.

## Drift / contradiction check

- Treating every rejection as “the true-residual gate exposed old false convergence” would be incorrect. That explanation fits PCB `effective_1um`, but not FR4 matrices whose explicit residuals passed while their physical structure was already invalid.
- Treating restarted GMRES as a complete remedy would also conflict with the evidence: it addresses recurrence drift, not the FR4 operator/hierarchy regression.
- Historical midpoint results support a hierarchy regression, but exact attribution to median alone is not fully controlled: the older FR4 physical deck SHA was `428ac808...`, while Cycle 3 used `b04e5592...`; facet ordering changed. A same-byte midpoint/median comparison was not run.
- Upstream `ediloren/FasterCap` issue #8 corroborates the broader automatic-mode asymmetry/crash class, and FasterCAP_v2 retaining it removes confidence that a version upgrade alone is an architecture fix. It is corroboration, not proof of the Cycle 3 median mechanism.

## Recommendation

**Stop the recovery loop as BLOCKED. Do not run or propose Cycle 4.**

There is **no Fugu matrix**: repository inspection found only `out/fastercap-cycle3-fixture-requalification/` for Cycle 3 and no Cycle 3 Fugu endpoint artifact. The endpoint was correctly skipped. **Slice 5 may not start.**

Preserve the following only as a future, separately authorized architecture record:

1. **Diagnostic isolation, not qualification:** use the identical byte-for-byte failing decks at fixed manual mesh rungs to compare midpoint versus median/hybrid spatial trees and Jacobi versus two-level preconditioning. Manual matrices may identify whether the fault enters interaction approximation or preconditioning, but cannot satisfy the automatic-mode qualification gate.
2. **If FasterCap remains:** implement true-residual restarted GMRES or verified correction solves with a fresh Arnoldi basis, explicit residual after every cycle, and one cumulative 1,000-step budget. Pair it with a spatial hierarchy that controls both population balance and bounding-box overlap; count balance alone is insufficient.
3. **Independent BEM architecture:** evaluate a solver with finite closed conductors, dielectric interfaces, explicit linear residuals, mesh convergence, and raw Maxwell reciprocity. Under the current literal FasterCap gate it can only be diagnostic; qualifying a replacement solver requires separate authorization and an explicit equivalence contract.
4. **Qualification remains separate:** no diagnostic/manual/alternative-solver matrix may enter Fugu. Any future qualifying implementation must first pass the complete frozen fixtures and all resource, residual, raw reciprocity, sign, passivity, and reconstruction gates.

## Risks

- Median-only causation remains unproven without a same-deck/current-source midpoint control.
- The missing spatial-quality telemetry leaves bounding-box overlap and hierarchy-induced operator error unmeasured.
- The exact branch-reconstruction rejections may be a separate parser/rounding contract issue, but they remain valid qualification failures until isolated.
- Restarted GMRES may repair PCB false convergence yet leave FR4 and terminal reciprocity failures unchanged.
- The upstream defect lineage makes further in-place FasterCap changes high-risk without independent reference results.

## Need from main agent

None under current authorization. Retain the BLOCK and require fresh authorization before any diagnostic or architectural continuation.

## Suggested execution prompt

No executor handoff is warranted; this task is read-only and no further recovery cycle is authorized.