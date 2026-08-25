# Palace workflow efficiency plan

## Scope

Improve Palace PCB campaigns without weakening topology, residual, raw-matrix, energy-matrix, reciprocity, passivity, warning, provenance, or lifecycle gates.

The current-schema `simple-hb` p2 checkpoint campaign completed all 18 strict RHS solves across one 1,800 s attempt plus a 114 s resume. The matrix remains `rejected_diagnostic`: p1→p2 changes exceed the convergence tolerance, and the only p2 sign violation is symmetric +0.298 aF numerical noise between two unconnected pads. The result is diagnostic borrowed-stackup evidence, not a physical matrix.

Fugu h-refinement is currently blocked by linear-solver conditioning rather than memory. A 2.11 M-node / 11.06 M-tetrahedron planar midpoint used 10.65–12.56 GB RSS but stalled on RHS 1 with the same approximately 0.974 PCG reduction factor at one and four MPI ranks. Disabling aggressive BoomerAMG coarsening and using restarted GMRES did not resolve the stall. A 1.01 M-node / 4.96 M-tetrahedron vertical-only rung failed similarly. Targeted two-terminal principal-block probes remain diagnostic-only: coarse-mesh p2 stalled at residual 3.05e-5 after 750 iterations, and midpoint p1 stalled at residual 2.74e-6 after 1,250 PCG iterations. No refined Fugu matrix was emitted or promoted.

## Current status

- E1 byte-recounted topology, exact H1 hierarchy, runtime hierarchy equality, and complete workload identity are implemented.
- E2 reader-time stream/resource witnesses, causal post-exit limits, strict RHS ordering, native flushed setup/RHS/finalization milestones, and rejected-attempt timing validation are implemented. Rejected timing reconstructs its workload from current config, mesh, build, executable, runtime binaries, launcher, process count, host class, and implementation inputs; clean monitor-witnessed wall limits remain censored `wall_timeout` evidence.
- E3 uses two practical resource classes with wall-time, RSS, output, node, and tetrahedron caps. Ordinary runs select the smallest fitting class automatically; checkpointed campaigns may additionally bind a finite resource decision for cumulative accounting.
- Execution uses an ordinary user-space content-addressed snapshot. Inputs are hash-checked before and after the solve; no elevated privileges or separate ownership boundary are required.
- E4 native per-RHS checkpoint/restart is implemented and independently accepted. `dcdc-palace-config-v3` now binds an absolute persistent checkpoint root plus the preregistered native campaign identity while retaining read support for frozen v2 manifests: exact reaction columns plus per-rank binary64 shards, marker-last durable publication, anchored no-follow containment, bounded nonblocking reads, serial/MPI crash injection, adversarial restart tests, residual/reaction replay, and byte-identical resumed matrices all pass. The native changes are committed as `d28dbfd5` and `0ca2de94`; the rebuilt serial, MPI, and 27.98 s integration checkpoint tests pass. Clean source identity `9a50e1ad…be94b` and build-v2 identity `34cb1f1f…f6b18` are independently reviewed and pass the refreshed 125-test build/source/runner set plus the complete 833-pass suite.
- E5 preserves validated checkpoint prefixes and cumulative attempt accounting for interrupted p2/p3 solves. It is used only when checkpoint/restart materially saves solver time; ordinary runs do not require campaign authority infrastructure.

### Native E4 source identity

`lib/palace_source_identity.py` emits a review-neutral `palace-source-identity-v1` artifact without weakening build-v1's untracked-source rejection. After the reviewed native changes entered git as `d28dbfd5` and `0ca2de94`, the current clean-tree candidate became `docs/artifacts/palace-e4-source-v5/palace-source-identity.9a50e1ad35a4b6b644afdff791f312c75b8d58ff7bc01cc5669fe7faa16be94b.json`. It binds commit `0ca2de94`, an empty tracked patch and untracked roster, recursive submodule status, and repository/effective-global Git exclude policy. All source, manifest, patch, policy, and blob reads reject symlinked path components without resolving them first; the filename equals the canonical digest. Earlier pre-commit candidates are superseded and inadmissible.

`lib/palace_source_policy.py` authorizes only clean-tree digest `9a50e1ad35a4b6b644afdff791f312c75b8d58ff7bc01cc5669fe7faa16be94b` and its exact rebuilt wrapper/native binaries, build root, and MPI launcher. Build-v2 candidate `docs/artifacts/palace-e4-build-v3/palace-build-v2.34cb1f1fe5b785e639e31cef23d00516988d9346e20893c0482671c69f8f6b18.json` binds that policy to the complete current CMake-cache roster, resolved linked libraries, and host class. Build-v1 remains unchanged and continues to reject untracked sources.

## Slice E1 — exact workload identity

1. Extend PLC mesh provenance with exact unique edge and face counts computed by the existing byte validator.
2. Derive conforming tetrahedral H1 unknowns for every p-multigrid level `q = 1..p` from unique volumetric connectivity:

   `D_q = V + (q - 1)E + C(q - 1, 2)F + C(q - 1, 3)T`.
3. Record the exact hierarchy vector, finest count, hierarchy sum, and hierarchy peak. Model terminal-retained solution and boundary vectors explicitly; their residency scales approximately with `16 * terminal_count * finest_true_dofs` bytes before operator and solver overhead.
4. Add a strict `palace-workload-v1` record binding mesh/config/build hashes, nodes, edges, faces, tetrahedra, order, H1 hierarchy, terminal count, process count, solver controls, host class, and implementation identity.
5. Reject booleans, nonfinite values, inconsistent counts, unknown fields, stale hashes, unsupported orders, and counts derived from planar rather than volumetric connectivity.

**Gate:** every hierarchy level matches both the combinatorial formula and Palace's successful runtime metadata; malformed or rebound workload records fail before output-directory creation.

## Slice E2 — structured progress evidence

1. Add a Palace-specific streaming parser to the generic process monitor.
2. Record a pre-launch monotonic timestamp, then timestamp source, byte offset, and chunk at reader receipt time for stdout and stderr as separate partial orders. Record explicit limit detection, kill initiation, and process termination events.
3. Add flushed native Palace milestones for setup start/end, each RHS start, each explicit-residual completion, and finalization start/end. Parser absence proves only time to an unobserved milestone; it cannot be relabeled as internal setup or RHS duration.
4. Preserve events in rejected and successful run manifests with exact stdout/stderr hashes and rederive observations from the raw event log during validation.
5. Treat incomplete observations as censored bounds, never point estimates. Completed phase intervals require both native boundary events and matching build, controls, order, process count, mesh identity, host/environment class, containment backend, and power policy.

**Gate:** split/interleaved lines, duplicated or reordered RHS milestones, parser loss, missing native boundaries, and forged observations fail closed. Rejected attempts quarantine any matrix files and expose timing evidence only through the validated observation API.

## Slice E3 — practical resource limits

1. Keep pure workload and resource schemas for repeatability and checkpoint accounting.
2. Select the smallest resource class whose node and tetrahedron caps fit the mesh; callers may request a larger known class.
3. Enforce wall-time, RSS, and output caps with the existing process monitor.
4. Require finite complete-workload projections only for checkpointed multi-attempt campaigns, where cumulative reservation accounting is useful.

**Gate:** every run stays within its selected class and records observed wall time, RSS, output, and solver progress. Resource bookkeeping must not block an otherwise valid ordinary solve.

## Slice E4 — native per-RHS checkpoint

1. Version a Palace checkpoint format bound to one complete campaign identity: config/mesh/build, ordered terminals, solver controls, binaries/libraries, launcher, MPI count and partition, resource policy, numeric ABI, and implementation identity.
2. Precompute every response-terminal boundary selector before the first excitation so a complete ordered reaction-charge column is available after each accepted solve.
3. After explicit residual verification and terminal-specific postprocessing succeed, persist the complete reaction column and one binary64 solution-vector shard per MPI rank with global/local true-DOF, ownership, partition, and numeric-ABI metadata.
4. Use an exclusive campaign writer and same-filesystem temporary files. Every rank flushes and fsyncs its shard; the root verifies collective completion, fsyncs response and parent directory, and atomically publishes the completion marker last.
5. Resume contiguous prefixes only. Reject holes, duplicates, reordering, unknown files, path escape, symlinks, truncation, nonfinite values, wrong partition, or identity changes.
6. On load, rebuild the RHS and operator, rerun explicit residual verification, recompute the reaction column, require round-trip-exact agreement, and replay terminal postprocessing and metadata before skipping KSP.
7. Reconstruct the full in-memory solution array so the existing independent energy matrix remains unchanged; resource selection must account for final all-terminal rehydration. Bitwise identity across MPI restarts remains a demonstrated fixture gate, not an assumption.

**Gate:** uninterrupted and forced-timeout/resume single-rank and multi-rank fixture runs emit identical `terminal-Craw.csv` and `terminal-C.csv`; kill points before shard, response, parent fsync, and marker publication cannot create an accepted prefix.

## Slice E5 — attested attempt chain

1. Define a pure campaign identity and an exclusive monotonic ledger/head registered before launch. Forbid forks, omitted siblings, deletion, replacement, or multiple writers; each attempt appends atomically to the one trusted head.
2. Add immutable per-attempt manifests with prefix-before/after, checkpoint inventory, raw streams/events, telemetry, resource enforcement, and previous-head hash.
3. Introduce versioned causal timeout classification. Monitor-initiated wall termination and its expected missing suffixes are censored consequences; any warning, residual, solver, memory, output, or identity diagnostic observed before kill remains terminal.
4. Permit resume only after a validated wall-time rejection with strict prefix advancement and identical campaign identity. Keep the existing strict single-attempt completion gate; a separate chain validator reconciles newly solved plus checkpoint-loaded RHSs to `N`.
5. Use fresh Palace output directories per attempt; checkpoint storage is separate but included in the same accounting.
6. Enforce cumulative caps for wall/CPU, iterations and solves, bytes written/read, stdout/stderr, checkpoints, retained output, and attempt count. Track retained unique storage separately from cumulative I/O; use max rather than sum for RSS.
7. Partial attempts remain `rejected_diagnostic`; matrix-like files are quarantined and inaccessible through the matrix API. Only a complete `1..N` ledger may invoke existing raw/energy matrix gates and downstream reconstruction.

**Gate:** every launched attempt appears exactly once in the exclusive ledger, each terminal is newly solved exactly once, loaded/new telemetry reconciles to `N`, and forked, omitted, tampered, or rebound campaigns fail before matrix access.

## Verification order

1. E1–E3 pure schemas, adversarial tests, and existing fixture regression suite.
2. `simple-hb` p1 ordinary user-space canary; no checkpoint dependency.
3. Define and verify E5's pure campaign identity, exclusive-ledger, causal-timeout, accounting, and quarantine schemas.
4. Implement E4 against the frozen E5 identity, then run compiled single-rank and multi-rank two-terminal forced-timeout/resume fixtures.
5. Run `simple-hb` p2 with checkpoint/restart and a conservative local cap.
6. Retry Fugu only after the cheap fixture identifies a numerically useful, resource-feasible setting.

## Completion limits

These improvements optimize execution and evidence reuse. They do not relax p2→p3, h, outer-domain, material, independent-review, or measurement requirements, and they cannot promote diagnostic evidence to `physical_model_validated`.

## 2026-08-25 addendum — simple-hb p3 disposition

The checkpointed p3 campaign (`out/palace-qualification/simple-hb-p3-v1`,
native identity in `native-campaign-identity.json`) completed 18/18 RHS in
four 4-rank attempts (~1.5 h wall) plus a 19 s replay attempt accepted as
`numerically_converged_diagnostic`
(`attempt-06/config.json.run.numerically_converged_diagnostic.052a4583….json`).
The accepted p3 matrix is reciprocal (5.3e-23 F), positive definite, and has
zero positive off-diagonals — the +0.298 aF p2 sign defect vanished at p3.
Acceptance also required fixing `PeakNodeMemoryMegabytes` validation in
`lib/palace.py`: Palace aggregates that field per shared-memory *node*
(vendor `palace/utils/memoryreporting.cpp`), so `Total == Average ×
node_count` with `node_count ∈ [1, ranks]` and node total equal to the
per-rank total; the old `Average × ranks` expectation falsely rejected every
multi-rank completion.

p2→p3 convergence FAILS: 182/324 entries exceed 2% + 1 fF. The failure is
systematic, not localized: every matrix entry, including all 18 diagonals,
shrinks ~65% p1→p2 and ~44% p2→p3, with absolute rung deltas decaying
geometrically at ratio ≈ 0.2. This is monotone convergence from above
consistent with under-resolved thin-copper edge singularities dominating the
electrostatic energy. Extrapolating the observed decay, p3 remains ~20–25%
above the limit and the 2% successive-rung criterion would not be met before
roughly p5–p6, which is not resource-feasible. Conclusion: the current mesh
family cannot pass the p-ladder as specified; the next admissible moves are
edge-targeted h-refinement (bounded by the known conditioning ceiling), a
principled revision of the convergence-evidence design, or both. No gate was
weakened; p3 remains diagnostic evidence under the borrowed-stackup ceiling.
