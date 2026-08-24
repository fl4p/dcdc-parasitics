# Palace workflow efficiency plan

## Scope

Improve Palace PCB campaigns without weakening topology, residual, raw-matrix, energy-matrix, reciprocity, passivity, warning, provenance, lifecycle, or containment gates.

The `simple-hb` p2 run is dispositioned as `rejected_diagnostic`: 15 of 18 strict RHS solves completed before the 1,800 s timeout, but no matrix was emitted. Its bound implementation is now frozen evidence; subsequent source changes require new evidence identities.

## Current status

- E1 byte-recounted topology, exact H1 hierarchy, runtime hierarchy equality, and complete workload identity are implemented.
- E2 reader-time stream/resource witnesses, causal post-exit limits, strict RHS ordering, native flushed setup/RHS/finalization milestones, and rejected-attempt timing validation are implemented. Rejected timing reconstructs its workload from current config, mesh, build, executable, runtime binaries, launcher, process count, host class, and implementation inputs; clean monitor-witnessed wall limits remain censored `wall_timeout` evidence.
- E3 selection is fail-closed: every modeled dimension needs a finite upper bound, every run needs a decision, and prelaunch requires a source-controlled projection hash. The production projection allowlist is empty pending reviewed evidence.
- Locally produced content-addressed snapshots are never executable evidence: chmod is not a same-UID boundary. Launch now additionally requires an independently owned, non-writable POSIX snapshot root, parent, binaries, config, and mesh. No materializer for that privilege boundary exists yet, so real execution fails closed before workload publication or monitor invocation. Portable monitoring remains diagnostic-only.
- E4 native per-RHS checkpoint/restart is implemented and independently accepted. `dcdc-palace-config-v3` now binds an absolute persistent checkpoint root plus the preregistered native campaign identity while retaining read support for frozen v2 manifests: exact reaction columns plus per-rank binary64 shards, marker-last durable publication, anchored no-follow containment, bounded nonblocking reads, serial/MPI crash injection, adversarial restart tests, residual/reaction replay, and byte-identical resumed matrices all pass. The native changes are committed as `d28dbfd5` and `0ca2de94`; the rebuilt serial, MPI, and 27.98 s integration checkpoint tests pass. Clean source identity `9a50e1ad…be94b` and build-v2 identity `34cb1f1f…f6b18` are independently reviewed and pass the refreshed 125-test build/source/runner set plus the complete 833-pass suite.
- E5 v2 stages 1–2 and their pure reconciliation/resource-charge boundary are implemented independently of the ledger: accepted/rejected run manifests produce a witness-derived attempt projection, and `lib/palace_checkpoint.py` descriptor-validates the exact native E4 root/RHS/shard/response/completion formats, binary64 payloads, content bindings, and retained bytes. A generated fixture has 12 adversarial tests, and the validator passed against a real two-RHS native Palace checkpoint. `lib/palace_head_authority.py` also provides a service-side HMAC-authenticated, locked compare-and-swap canonical head: its durable instance binds root inode, authority ID, key fingerprint, client UID, and nonce; the client UID cannot mutate the authority path; lock/root replacement, clone-fork, stale-CAS, tamper, duplicate-key, interrupted registration, identity/key substitution, and symlink tests pass. The authority/campaign-v2 identity slice is independently clear of P0/P1 findings and its ancestor-coverage P2 is tested. Campaign v2 now separates a preregistered random native checkpoint identity from the final ledger campaign digest, avoiding a config-hash/workload/campaign self-reference. Validation requires an externally trusted complete campaign digest, current source-controlled checkpoint-validator digest, exact execution roster/native digest, native inventory prefix/partition, and exact retained-byte agreement when finalization supplies it. Unknown resource uppers consume their full preregistered reservation. The v2 ledger now derives registration/finish state from trusted reconciliation, charges unknown bounds to preregistered reservations, replays every attempt, and publishes through a crash-recoverable pending → entry/next-head → external CAS → local-head protocol. Canonical clone forks, pre/post-CAS crashes, missing-entry recovery, stale publication temporaries, reservation overruns, completed final binding, and matrix/config/terminal tampering have focused coverage. Matrix capability creation now requires a fresh completed canonical replay and revalidates the authority head and artifact hashes on every accepted parse. Production registration now derives reservations from an authorized resource projection and a privilege-bound snapshot, accounts process-count input reads, and binds the same validated decision through execution reconciliation. `run_palace()` registers before launch and finishes only from the published run witness. Public pending-registration recovery rederives the same trusted reservation; public completion recovery replays the last trusted registration. Independent E5 review found no P0/P1/P2 blockers. Local execution-snapshot preparation is now retry-idempotent, collision-free, serialized, descriptor-validated, and fail-closed on retained output or unknown entries. Privileged execution-snapshot materialization and reviewed static-projection evidence remain open; native E4 source/build attestation is now complete.

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

## Slice E3 — resource decision before execution

1. Add pure `lib/palace_resources.py` schemas and selection logic; no subprocesses or artifact mutation.
2. Preserve an authority trust root equivalent to current source-controlled limits: immutable profile definitions, pinned policy and validator digests, trusted host/environment-class derivation, and a signed or source-controlled authorization scope. Content addressing alone does not establish authority.
3. Create a content-addressed `palace-resource-decision-v1` before `run_palace` with workload identity, revalidated raw observation/event-log identities, conservative lower and upper bounds for every enforced dimension, uncertainty reasons, trusted authorization identity, selected profile, deterministic profile ordering, preregistered minimum headroom, and validator identity.
4. Select a profile only when every enforced dimension has a finite conservative complete-workload upper bound produced by a trusted policy and that bound plus required headroom fits. Unknown upper bounds reject selection; extrapolation beyond observed order/DoF/host range requires an explicit conservative policy.
5. Model wall time, CPU, vector residency, hierarchy/operator/solver memory, terminal-dependent retained vectors, matrix output, logging, and checkpoint I/O separately. Sum simultaneously resident memory components; use a maximum only for mutually exclusive phase envelopes.
6. Change `run_palace` to require and revalidate the decision. An inadequate decision must not invoke the monitor or create the run directory.
7. A resource-cap failure with causally validated progress updates observations and triggers profile re-selection before unrelated solver-control tuning.

**Gate:** p-order hierarchy and terminal count affect memory and wall/output projections; censored evidence excludes inadequate profiles; unknown uppers and untrusted authorization reject; deterministic selection chooses the smallest adequate authorized profile regardless of caller preference.

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
5. Use fresh Palace output directories per attempt; checkpoint storage is separate but inside the same accounting and containment boundary.
6. Enforce cumulative caps for wall/CPU, iterations and solves, bytes written/read, stdout/stderr, checkpoints, retained output, and attempt count. Track retained unique storage separately from cumulative I/O; use max rather than sum for RSS.
7. Partial attempts remain `rejected_diagnostic`; matrix-like files are quarantined and inaccessible through the matrix API. Only a complete `1..N` ledger may invoke existing raw/energy matrix gates and downstream reconstruction.

**Gate:** every launched attempt appears exactly once in the exclusive ledger, each terminal is newly solved exactly once, loaded/new telemetry reconciles to `N`, and forked, omitted, tampered, or rebound campaigns fail before matrix access.

## Verification order

1. E1–E3 pure schemas, adversarial tests, and existing fixture regression suite.
2. `simple-hb` p1 resource-decision canary; no native checkpoint dependency.
3. Define and verify E5's pure campaign identity, exclusive-ledger, causal-timeout, accounting, and quarantine schemas.
4. Implement E4 against the frozen E5 identity, then run compiled single-rank and multi-rank two-terminal forced-timeout/resume fixtures.
5. `simple-hb` p2 only if the selected authorized profile has a finite conservative complete-workload upper bound with required headroom.
6. No Fugu retry until E1–E3 reject or select a resource-feasible profile from bound evidence.

## Completion limits

These improvements optimize execution and evidence reuse. They do not relax p2→p3, h, outer-domain, material, native-containment, independent-review, or measurement requirements, and they cannot promote diagnostic evidence to `physical_model_validated`.
