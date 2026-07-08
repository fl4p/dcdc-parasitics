# Cin Matrix Reduction — Implementation Spec

**Status:** Identity-matrix slice implemented and reviewed (channel `cinL`, 2026-07-08).
The pure additive-fit core is implemented; cap-only extraction orchestration and
`matrix_with_sw_coupling` emission remain future work.
**Owners:** codex-ee-cinL (producer-side), ee-fable (consumer-side), codex-ee-83ak (gate/validation section)

## Implementation Record (2026-07-08)

Implemented now:

- Producer emits `cin_model.mode = "matrix"`, `cin_model.basis = "identity"` and a
  full `cin_matrix` when `lead_mm=0` with pad-ideal FET closure. This is the exact
  full Cin port submatrix; no trunk or gauge-fixing port is needed.
- Producer has the pure additive-fit helper for the future non-identity path:
  it fits `delta_ij = L_sw + m_i + m_j` with `L_sw` fixed by the explicit port
  gauge, emits physical-gauge metadata plus modeling-gauge element values, and
  reports the separability residual. It is not wired into extraction orchestration yet.
- Producer can build the future decomposed matrix payload from full/cap-only/switch
  bases: cap-only `L/R`, `L_sw_element`, physical-gauge metadata, modeling-gauge
  `m_i`, cap-to-switch `K`, `matrix` vs `matrix_with_sw_coupling` resolved mode, and
  the strict `K < 0.95` realizability rail. Additive residual above the floor resolves
  to `mode="none"` / `full_multiport_required=true` rather than emitting a decomposed
  matrix. It is still not emitted by the CLI.
- Reducer-side handoff from three reduced runs is implemented: full-loop + cap-only +
  switch-residual runs are aligned by `cin_net` ref order, require exactly one switch
  gauge port in the current helper, and produce the same decomposed payload. CLI
  orchestration of those runs is still future work.
- Consumer accepts only the explicit identity contract: `mode=matrix`, `basis=identity`,
  `cin_model_valid=true`, `gauge_fix_status=structurally_not_required`,
  `switch_board_copper=in_matrix`, and `spice_realizable=true`.
- Consumer realizes the matrix as coupled branch inductors, validates finite/symmetric
  numeric matrices, positive self-L/R, PSD/passivity, `abs(K)<0.95`, and preserves `K`
  statements through Cin flattening.
- For `basis=identity`, the full matrix carries the switch-side board-copper L. The deck
  adds zero switch-side board L and no trunk; FET package L comes from the `_L0` device
  model. This avoids both double-count and double-drop.
- Identity R handling is explicit: the matrix diagonal `R_100k` includes shared switch
  resistance, so the consumer subtracts `r_hs_switch+r_ls_switch` from each branch
  diagonal for the coupled Cin subckt, clamps only with a warning if numerical tolerance
  forces it, and emits comments showing the reconstruction residual. Symmetric off-diagonal
  R is not realized in v1; the consumer emits a `cin_matrix-r-offdiag-omitted` comment with
  the largest omitted term.
- Contract selection is driven by explicit `cin_mode`/`cin_basis`, including provenance
  headers for generated `.lib` files. It is no longer inferred from whether the network was
  assembled in-memory or loaded from a file.
- Provenance freshness is path-scoped: auto-discovered stale generated `cin_network.lib`
  files refuse; an explicitly supplied `--cin-network` with a stale header warns because the
  user intentionally selected that file.

Acceptance on Fugu2 7-cap identity (`fugu2-perDev-noLeads.yaml`, `_L0`, `lead_mm=0`):

| Check | Result |
|-------|--------|
| `cin_model` | `mode=matrix`, `basis=identity`, `cin_model_valid=true` |
| Gauge | `gauge_fix_status=structurally_not_required`, `L_sw_element=0` |
| Matrix realizability | `K_max=0.945157` (passes the 0.95 refusal rail, close enough to keep the rail mandatory) |
| Ideal current-share closure | extractor and identity matrix agree: C27 +28.8%, C18 +28.5%, C17 +22.1%, C9 +29.4%, C16 -12.6%, C21 +2.2%, C22 +1.7% |
| Scalar-clamped comparison | C27 +4.6%, C16 +14.6%; RMS share error vs identity = 15.46 percentage points |

Loaded loss A/B, identity minus scalar-clamped, same operating point:

| Metric | Delta |
|--------|-------|
| Total loss | -0.455 W |
| Cin loss | -0.128 W |
| Cin copper | -0.225 W |
| MLCC ESR | +0.086 W |
| Bulk ESR | +0.012 W |
| Loop R | unchanged |

C27 remains starved by the scalar deck in the loaded run: identity `0.087 W` vs scalar
`0.017 W` for that cap. The scalar-only fake trunk bucket (`0.316 W`) disappears in the
identity deck.

## Problem Statement

The scalar single-trunk Cin reducer (`solve_reduce.py:_cin_branch_decomp`) is invalid for heterogeneous/multi-region Cin cap banks. On the Fugu2 7-cap run (incl C27), it produces a negative switch-side residual (`L_loop_switch_raw = -0.469 nH`) that is silently clamped to 0.

### Root Cause

Two inconsistent bases:
- **`L_loop`** from `_eff_commutation` (line 79): solves the full Cin port matrix with the circulating-mode current split. The effective parallel L drops BELOW the mean off-diagonal mutual for a heterogeneous bank.
- **`cin_L_shared`** from `_cin_branch_decomp` (line 131): `min(mean(off-diag L), min(diag L))` — assumes homogeneous `L[i,j] = L_shared`.

For a heterogeneous 2-region bank (near-bank mutuals 3.2–4.6 nH, C27 ~0.56 nH), `L_loop - L_shared < 0` is **physically correct** — the model is faithfully reporting its single-node assumption is violated. The silent clamp to 0 (`L_loop_switch = max(0.0, L_loop_switch)`, line 524) hides this.

### Evidence (Fugu2)

**7-cap run (incl C27) — the motivating failure case:**

| Metric | Value |
|--------|-------|
| L_loop | 2.665 nH |
| cin_L_shared_raw | 3.135 nH |
| L_loop_switch_raw | -0.469 nH (clamped to 0) |
| Cin submatrix eig | 0.167, 0.234, 0.496, 0.675, 1.414, 7.702, 22.883 nH (all positive = passive) |
| Off-diag std/mean | 1.43 / 2.79 nH (51% — highly heterogeneous) |
| Current split (C27) | C27 29%, C18 28%, C17 22%, C9 30%, **C16 -13%**, C21 2%, C22 2% |
| Ring freq (7-cap) | 65.0 MHz |

**Refreshed 6-cap run (excl C27) — the near-bank homogeneous baseline:**

| Metric | Value |
|--------|-------|
| L_loop | 3.24 nH |
| L_loop_switch | 0.278 nH (~9% of loop — small but above FastHenry noise) |
| Ring freq (6-cap) | 62.5 MHz (+4% vs 7-cap — C27 is first-class, not noise) |

## Design

### Gate Architecture — Three Separate Fields

Do NOT collapse to one boolean. Three independent diagnostics:

#### 1. `switch_separability` (pass/fail)

Tests whether opening FET copper from the full-loop deck shifts the cap-cap mutuals. If the switch path is separable from the cap mutual structure, the decomposition (cap-only matrix + scalar switch residual) is valid.

**Method:** Run two extractions from the SAME fixture (parametric FET-filament-drop flag — same `.inc`/YAML config, same terminal slices, same grid, same nwinc; only FET-net filament groups removed from the solve):

- **Full-loop matrix:** `L_full` — existing `P_pwr_i` ports (cap_i Vin→GND loop, FETs shorted)
- **Cap-only matrix:** `L_cap` — same ports, FET copper replaced by pad-land-level equiv short (see Demarcation Plane section)

**Exact additive model:** Under the separability assumption, opening FET copper adds only a shared switch segment with self-inductance `L_sw` and per-cap mutual `m_i` (cap branch i ↔ switch segment). The difference matrix is:

```
delta_ij = L_full_ij - L_cap_ij = L_sw + m_i + m_j   (for ALL i,j, including i==j)
```

This is an **exact** additive model (not first-order): `delta_ii = L_sw + 2*m_i`, `delta_ij = L_sw + m_i + m_j` (i≠j). Both are the same bilinear form.

**Implementation:**
1. Compute `delta_ij = L_full_ij - L_cap_ij` for all (i,j) pairs
2. Fit the additive model `delta_ij = L_sw + m_i + m_j` via rank-structured least-squares (2-way ANOVA on the symmetric delta matrix)
3. Separability metric = Frobenius norm of fit residual (`||delta - (L_sw + m_i + m_j)||_F`), gated at the null-perturbation floor
4. The fitted `m_i` ARE the cap↔switch mutuals → `K(cap_i, L_sw) = m_i / sqrt(L_cap_ii * L_sw)` comes FREE from the same fit, no separate switch-coupling extraction needed

**Gauge freedom (CRITICAL):** The additive model has a one-parameter gauge freedom: `L_sw → L_sw + 2c`, `m_i → m_i - c` leaves every `delta_ij` invariant. `L_sw` and the `m_i` are NOT individually identifiable from delta alone. The explicit switch-residual port measurement is **REQUIRED** — not a nice sanity check, but the gauge-fixing constraint. Its agreement with the fitted `L_sw` validates the port geometry. If they disagree beyond the null-perturbation floor, the port geometry is wrong → fail the gate.

**Decision tree:**

| Condition | Verdict | Mode |
|-----------|---------|------|
| Fit residual within floor, all modeling-gauge `m_i` within floor of zero | Separable, no significant cap↔switch coupling | `matrix` (or `scalar_trunk` if also homogeneous) |
| Fit residual within floor, any modeling-gauge `m_i` significant ( \|m_i\| > floor ) | Separable with cap↔switch coupling | `matrix_with_sw_coupling` (emit K for each significant m_i), `gauge_fix_status="fixed"` |
| Fit residual above floor (random scatter) | Non-separable | `none`, `full_multiport_required=true` (no decomposed matrix emission) |

**Gauge-fix-aware m_i rule (modeling gauge):** The floor/mode test is evaluated in the MODELING gauge (post-regauge, `c = median(m_i)`). In the physical gauge near-bank `m_i` are nonzero, so "any `|m_i| > floor`" would make every board `matrix_with_sw_coupling` — plain `matrix` would never be selected. In the modeling gauge, uniform coupling is absorbed into `L_sw_element` (near-bank `m_i → 0` → plain `matrix`) and only a genuine anomaly (C27) survives → `matrix_with_sw_coupling`.

**Emission default — modeling-gauge elements + physical-gauge metadata (footgun warning):** DEFAULT emission is modeling-gauge: `L_sw_element` = effective `L_sw` (`L_sw_phys + 2*median(m)`), K only for post-regauge outliers. Physical gauge (`L_sw_physical`, `m_i_physical`, `regauge_c`) is carried as metadata for the refusal/geometry cross-check only — NEVER wired as an element. The footgun: pairing physical `L_sw` with outliers-only K silently drops `2*m_near` from every near-bank loop — a double-DROP, the mirror image of the double-count bug. `assemble_cin_network` asserts it never wires `L_sw_physical` as an element; the field name prevents the footgun by construction.

**Mode taxonomy consequence:** `matrix` vs `matrix_with_sw_coupling` is decided purely by "any modeling-gauge `|m_i| > floor`?" — outlier/region detection stays a DIAGNOSTIC (cap-cap axis), not an emission trigger. Cleaner.

**One K per outlier is sufficient:** In the coupled-L netlist, the simulator derives every loop-to-loop mutual from the K matrix. Setting `K(cap_i, L_sw) = m_i / sqrt(L_cap_ii * L_sw)` reproduces both cap_i's self-row offset AND all cap_i↔near cross-mutuals (both mutuals already present as K's). Per-pair `K(cap_i, near_j)` terms would double-count.

**Gauge reconciliation (physical vs modeling gauge):** The fit is gauge-FREE — only the combos `L_sw + m_i + m_j` are identifiable from delta. Two valid gauges, both giving identical SPICE loop L:

- **Physical gauge:** Port measures physical `L_sw` (P→die self) → pins `c` → physical `m_i` (near-bank ~equal, C27 outlier). Use THIS for geometry validation (fitted-vs-port agreement). The consumer refuses un-gauge-fixed matrix payloads — an un-gauged `L_sw` is not a physical element value.
- **Modeling gauge:** Re-gauge `c = median(m_i)` so near-bank `m_i → 0` (absorbed into an effective `L_sw`) and only outlier caps keep explicit `K(cap_i, L_sw)`. Use THIS for the netlist — near-bank K's are NOT required.

"Port fixes the gauge" (physical, for validation) and "one K per outlier" (modeling gauge, for emission) are the SAME fit in two gauges — not a conflict. SPICE loop L is identical either way.

**Payload carries both gauges:** The JSON must include physical-gauge values (port-measured `L_sw`, physical `m_i`, or equivalently the re-gauge constant `c`) as metadata alongside the modeling-gauge element values. Emission gauge for elements, physical gauge for provenance. The consumer refusal check ("un-gauged `L_sw` is not a physical element value") and any downstream geometry validation need the physical gauge available, not just the emitted elements.

**Acceptance identity is gauge-invariant:** `L_sw + 2*m_i = delta_ii` is invariant under the re-gauge, so the netlist-level check works identically in either gauge — no consistency trap.

**Min-caps assumption:** The additive-model fit is robust when one region dominates (6 near + 1 far → outlier m_i is clearly C27). On a near-even split between two regions, the fit may degrade → region-cluster first, then per-region fit. Edge case; state in spec, not a blocker.

#### 2. `region_assignment` (list of regions)

From cap-only off-diagonal structure. C27 falls out as its own region by default (not behind a clustering threshold that might merge it).

**Method:** Cluster the `L_cap` off-diagonal matrix by mutual coupling strength. Caps within a region have high mutuals; inter-region mutuals are weak.

C27 is its own region by default given the 0.56 nH vs 3.2–4.6 nH gap.

#### 3. `cin_model_valid` (composite)

```
scalar_valid     = switch_separability AND homogeneous (n_regions == 1, no neg shares)
matrix_valid     = switch_separability ALONE
multiport_valid  = NOT switch_separability AND SPICE realizability (K < 0.95 port-merge)
```

The circulating mode (negative current shares) is a **scalar-model killer, not a matrix-model killer** — coupled-L with K reproduces the circulating mode exactly by construction.

**Homogeneous = n_regions == 1:** Define homogeneity via `region_assignment.n_regions == 1`, NOT a separate spread threshold. This couples the two diagnostics and removes an independent spread-threshold knob to calibrate. A bank can have low global spread but still be 2-region. (The interim heuristic spread-ratio is fine until region_assignment on the cap-only basis lands; then retire the standalone threshold.)

**K-merge applies to all coupled-L modes:** The `max(K_ij) >= 0.95` port-merge/refusal rail applies to `matrix`, `matrix_with_sw_coupling`, AND `none` (full multiport) — the multiport is also a coupled-L realization, so tightly-coupled near-bank caps (K→1) can make even the multiport non-convergent in SPICE. `multiport_valid` is therefore NOT unconditionally true: it requires matrix passivity (guaranteed by solver) AND SPICE realizability (K < 0.95 port-merge).

**Partial-basis qualifier:** Each validity field carries a `basis` qualifier indicating which tests were actually evaluated. Before the cap-only extraction is implemented, `switch_separability.status = 'not_evaluated'` and `scalar_valid` is computed from homogeneity alone — emit `basis: 'homogeneity_only'` so downstream consumers know the gate is partial. When the full separability test lands, flip to `basis: 'full'` and the full conjunction applies. A bare `scalar_valid=true` without the basis field overstates confidence during the interim period.

**Basis=identity (lead_mm=0 with leads-internal _L0 models):** When FET device models carry internal package leads (`leads_internal=True`, e.g. IPP024 _L0) and `lead_mm=0`, the extracted package excursion is ~zero — the packages live in the SPICE models, not the copper. The cap-only and full-loop extractions are degenerate (identical topology at pad-land level), so there is nothing to decompose on the package axis: no fit, no delta, no gauge freedom, no gauge port required.

The correct emission is the **full Cin submatrix** with `L_sw_element = 0` (no separate trunk element), `basis = 'identity'`, `gauge_fix_status = 'structurally_not_required'`. The consumer dispatch knows the gauge-fix requirement is structurally absent, not missing.

**CRITICAL contract clarification (prevents double-DROP):** `L_sw_element = 0` means "no separate TRUNK element," NOT "the switch-side copper has zero inductance." The SW pour + switch-cell board-copper inductance is IN the coupled matrix (that is precisely why `K_max` rides high — pour-in-matrix). Consumer contract for `basis=identity`:
- The full-matrix `cin_network` carries the ENTIRE commutation-loop board copper (caps + SW pour)
- Deck switch-side BOARD L = 0 (it's in the matrix)
- FET PACKAGE L comes from the `_L0` model
- The deck must NOT additionally zero or drop the matrix's shared switch coupling — reading `L_sw_element=0` as "no switch inductance" and suppressing matrix switch content = double-DROP in a new hat

**Payload must state affirmatively:** `basis=identity` => switch board-copper L lives in the matrix, `gauge_fix_status=structurally_not_required`, deck adds ZERO switch-side board L and ZERO trunk.

**K_max near the 0.95 rail:** With pour-in-matrix, `K_max` can ride dangerously close to 0.95 (measured 0.9451 on Fugu2). The `K >= 0.95` port-merge/refusal safety MUST still run on `basis=identity` emissions for tighter banks. This is the price of exactness when the shared pour stays in the coupled matrix.

**`fet_closure` label:** When `lead_mm<=0` uses pad-land ideal closure (instead of zero-length lead filaments), record `fet_closure='pad_ideal'` in topo — it silently makes `full_loop` and `per_fet cap_only` degenerate, and the label prevents the misread (today's vacuous per_fet "win" is the proof).

**Closure contest deferred to lead_mm>0:** The A(per_fet) vs B(cell_bridge) closure contest is real but only for `lead_mm>0` fixtures (die-only device models, packages in copper). At `lead_mm=0` with `_L0`, per_fet is degenerate (no gauge port, fits nothing) and cannot emit a gauge-fixed payload. The closure parametrization is NOT wasted — it's the tool for the `lead_mm>0` contest when a die-only device model is used. Do not extrapolate `lead_mm=0` numbers to `lead_mm>0`.

**Measured verdict (Fugu2, _L0, lead_mm=0):** B-open residual Fro 0.231nH, SVD rank-1 (sigma2/sigma1=0.003), C27-dominant leading vector (+0.92). The C27-dominance discriminates the mechanism: a group-equipotential artifact would be near-dominant (it merges Q1/Q3 HS drain pads); a far-cap-dominant rank-1 direction is the signature of "C27 couples to a different part of the spatially-extended cell" — the lumped-trunk MODEL-FORM limit, not the near-side equipotential. Honesty caveat: at lead_mm=0 we can't fully isolate model-form from residual fixture effects because per_fet (the no-group-merge control) is degenerate; a future lead_mm>0 board with non-degenerate per_fet would confirm. Verdict: `basis=identity` (rule (ii) — residual decisively above floor on both magnitude and structure grounds).

**Future escape hatch (record, do NOT build now):** `delta(B-open) = L_sw + m_i + m_j + v_i·v_j` exactly — the rank-1 correction is precisely what ONE additional coupled inductor provides. A two-segment cell model (near-bank segment + C27-path segment) would fit B-open to the noise floor and make the trunk split valid. Irrelevant today (`basis=identity` is exact and simpler, `K_max` 0.9451 passes), but if a future tighter bank pushes `K_max` over 0.95 where the trunk's K-margin matters, the augmented two-segment trunk is the documented way out.

`matrix_valid` uses `null` (not `false`) to distinguish "not yet evaluated" from "evaluated and failed." `null` = unknown, `false` = tested and failed.

### Threshold Calibration — Null-Perturbation Floor

The separability threshold is a **same-fixture numerical-repeatability floor**, NOT a physics-magnitude knob. It is defensible per-fixture as "below solver noise."

**Critical:** Do NOT calibrate the floor from the FET-filament drop itself — that perturbation IS the signal the test detects. Calibrating on the signal inflates the tol by exactly the signal, so the gate can never fire (self-defeating tol).

**Calibration recipe:**
1. **Run-to-run:** FastHenry is deterministic, so identical-deck repeatability ≈ GMRES tolerance, near zero. Do NOT pad the floor with a phantom run-to-run budget.
2. **Null perturbation:** Drop an equal-size filament group that is OUTSIDE every commutation path — a gate-loop or bulk-side stub — matched to the FET drop in (a) filament/cell count and (b) local mesh density and connectivity. This measures filament-removal meshing/solve sensitivity without touching the physics under test.
3. **Floor = max(null_scatter, GMRES_tol)**

**Null group selection:** Meshing sensitivity is LOCAL, not global. Match the null to the FET drop in cell count AND mesh regime. Best null = a group adjacent to but off the commutation path (gate-loop copper or bulk-side stub) with comparable cell count and mesh density. A size-matched-but-mesh-mismatched null gives a wrong floor in either direction.

### cin_mode Enum

Replace `cin_has_trunk: bool` with:

```python
cin_mode: Literal["scalar_trunk", "matrix", "matrix_with_sw_coupling", "none"]
```

- `scalar_trunk`: existing Model B (asserts `scalar_valid == true`)
- `matrix`: coupled-L network from cap-only matrix + scalar switch L (asserts `switch_separability == true`)
- `matrix_with_sw_coupling`: coupled-L + explicit K(cap_i, L_sw) for caps with significant modeling-gauge m_i (asserts `switch_separability == true`)
- `none`: full multiport, no decomposition (fallback when `switch_separability == false`)

**Requested vs resolved:** The CLI `--cin-network-model {scalar_trunk|matrix}` is the user's *request* (a family). The emitted `cin_model.mode` is the *resolved* outcome — the decision tree picks `matrix` vs `matrix_with_sw_coupling` vs `none` producer-side from the additive-fit modeling-gauge m_i + Frobenius fit residual. `matrix_with_sw_coupling` and `none` are resolved-only (never CLI-requestable). Document this in help text + field docs so `mode` isn't read back as the request.

**`cin_model_valid` is mode-dependent:** Define it as `valid_for(resolved_mode)`:
- `scalar_trunk` → `scalar_valid`
- `matrix` / `matrix_with_sw_coupling` → `matrix_valid`
- `none` → `multiport_valid` (NOT switch_separability AND SPICE realizability — K < 0.95 port-merge; NOT unconditionally true)

Do NOT hardwire `cin_model_valid = scalar_valid` — it will silently misreport once matrix mode lands.

### SPICE Matrix Realization

For `matrix` and `matrix_with_sw_coupling` modes:

- Self-L per port: `L_i = L_cap_ii`
- Coupling: `K_ij = M_ij / sqrt(L_cap_ii * L_cap_jj)` (from cap-only matrix)
- Switch inductor: `L_sw` (scalar, gauge-fixed by explicit port measurement — see switch_separability section)
- Outlier cap↔switch coupling: `K(cap_i, L_sw) = m_i / sqrt(L_cap_ii * L_sw)` (m_i comes FREE from the additive-model fit, no separate extraction)
- R: diagonal per-port `R_100k` for the coupled network (ring damping at HF). For
  `basis=identity`, subtract shared switch resistance from each diagonal before wiring the
  per-cap branch R, because `r_hs_switch`/`r_ls_switch` remain in the loop paths for
  attribution/conduction.
- Resistive off-diagonal coupling: not realized in v1. It is usually much smaller than the
  inductive coupling, but the consumer must not drop it silently; emit an omitted-term
  diagnostic/comment with the largest symmetric off-diagonal R.
- K convergence check: if `max(K_ij) >= 0.95`, refuse now (future implementation may merge
  those ports into one region). Applies to ALL coupled-L modes (matrix,
  matrix_with_sw_coupling, and none/multiport).
- Matrix passivity check: validate PSD/eigenvalues of the full L matrix, not only pairwise
  `K`; pairwise `abs(K)<1` is necessary but not sufficient for a realizable coupled-L bank.

**Gauge-fix cross-check (REQUIRED, not optional):** The explicit switch-residual port measurement fixes the gauge freedom in the additive model (`L_sw → L_sw + 2c`, `m_i → m_i - c`). Fitted `L_sw` and port-measured `L_sw` must agree within the null-perturbation floor. Disagreement ⇒ port geometry is wrong ⇒ fail the gate.

### Dual R Basis

Emit two R fields per port:
- `R_100k`: per-port diagonal R at 100 kHz (lowest FastHenry sweep freq) — inflated ~60% by inductive proximity. Use for the coupled-L+R network (ring damping at HF).
- `R_dc`: true DC resistance. Use for conduction-loss path in the loss deck.

Currently `R` is read at 100 kHz only. Matrix mode with per-port diagonal R inherits the inflation. Document which basis each path uses in the copper contract.

**R_dc obtainability:** 100 kHz is the current FastHenry sweep floor — there is no DC point. The producer must either extend the FastHenry frequency sweep with a ~DC/1 kHz point or run a separate DC solve to obtain true `R_dc`. Without this, `R_dc` is fiction. Add to producer-side implementation tasks.

### Demarcation Plane

The L_cap / L_sw split requires a single shared **demarcation plane P** — the boundary between cap-branch copper and switch-side copper. Plane P is defined once and binds to **three roles**:

1. **Cap-only loop closure:** pad-land-level equiv short (drain pad-land node = source pad-land node at P) that closes every `P_pwr_i` loop
2. **Additive-fit split:** `L_sw` = self-inductance of everything above P (the switch segment); `L_cap` = everything below P (the cap branches)
3. **Gauge-fixing port:** the explicit switch-residual port measures P → die junction, fixing the additive model's gauge freedom

If any of the three references a different plane, the additive fit residual inflates and the separability gate false-fires. A copper sliver is double-counted or dropped.

**The short does NOT collapse cap spatial separation:** P is the COMMON switch node every cap loop already shares. The caps stay distinguished by their own Vin_i → P branch copper (that's where C27's distance lives). The short is at the shared node, not across the bank — cap-cap structure is preserved.

**Implementation:** The demarcation plane P is the board copper surface at the FET pad lands — where the FET drain/source pads meet the board copper. In the full-loop run, `build_fet()` adds vertical lead stubs from pad-land nodes up to a die plane at `z = +lead_mm`, then shorts drain↔source at the die plane (`model.equiv(dref, sref)`). For the cap-only run:

1. **Remove** the vertical lead stubs (`model.seg(dn, dref, ...)`, `model.seg(sn, sref, ...)`) and the die-plane equiv (`model.equiv(dref, sref)`)
2. **Add** a pad-land-level equiv short: `model.equiv(dn, sn)` — short drain pad-land to source pad-land at plane P
3. This closes every `P_pwr_i` loop at plane P without any FET copper in the path

The switch-residual port is then measured as a standalone port from plane P (drain/source pad-land nodes) to the die junction — the same copper that was removed in step 1.

### Parametric FET-Filament-Drop Flag

Add a flag to `extract_parasitics.py` (and the YAML config) to produce the cap-only extraction:

```
# YAML
drop_fet_filaments: true   # cap-only extraction (switch copper excluded, pad-land equiv short added)
```

**Critical:** The operation is REPLACE, not DROP. Simply skipping `build_fet()` removes the only Vin→GND conductive path (the die-plane equiv closes every `P_pwr_i` loop), leaving FastHenry with no closed loop for the cap ports — the solve is ill-posed. See the Demarcation Plane section above: remove lead stubs + die-plane equiv, ADD pad-land-level equiv short.

**Implementation notes (from code exploration):**
- FET filaments are the vertical lead stubs in `kicad_geom.py:build_fet` (lines 1028–1074): `model.seg(dn, dref, ...)`, `model.seg(sn, sref, ...)`, `model.seg(gn, gref, ...)`, plus `model.equiv(dref, sref)` (channel short at die plane).
- `dn`/`sn` are the pad-land nodes (board surface); `dref`/`sref` are the die-plane nodes at `z = +lead_mm`.
- Cap-only run: skip `model.seg(dn, dref, ...)` + `model.seg(sn, sref, ...)` + `model.equiv(dref, sref)`, add `model.equiv(dn, sn)`.
- Gate lead stubs (`model.seg(gn, gref, ...)`) can be dropped entirely — they're not in any Cin commutation path.
- The switch-node copper that remains (pad land where caps connect) stays in the filament set — correct.
- Must use the **same** `.inc`/YAML config, terminal definitions, grid, and nwinc as the full-loop run. This guarantees injection-style consistency by construction and kills the rectangular-zone vs polygon meshing confound (mesh is literally identical between runs).
- Terminal injection must use the same pad-land terminal-slice injection as the full-loop basis (on simple-hb, injection style alone moved L 9.08→6.86 nH, ~25%).

### Interim Gate (before matrix extraction is built)

Instead of silently accepting a negative residual, **flag it**:
- Emit `cin_model_valid = false` with diagnostics: `residual_value`, `offdiag_std/mean ratio`, `max negative current share`
- Loss deck refuses scalar `cin_network` unless `--allow-scalar-cin` override
- Preserve the raw negative residual in diagnostics; keep the legacy clamped scalar fields
  only for explicit override/backward-compatible deck emission
- This gates the broken path immediately while the matrix extraction is being built

**Current code location:** `solve_reduce.py` preserves `L_loop_switch_raw` for diagnostics,
marks `cin_model_valid=false`, and still computes the legacy clamped `L_loop_switch` field
for `--allow-scalar-cin` compatibility. Consumers must key off `cin_model_valid`, not the
presence of a nonnegative clamped scalar field.

**Interim scalar_valid heuristic:** The current scalar_valid proxy uses three triggers: high offdiag spread ratio, negative ideal current shares, or `L_loop_switch_raw < -0.05nH`. This is an *interim heuristic*, NOT the principled `switch_separability` test (which is `not_evaluated` until the cap-only basis lands). Document it as interim. The `-0.05nH` residual constant is a loop-residual sign-test band — it is a DIFFERENT quantity from the future null-perturbation floor (cap-only separability scatter). Do NOT unify the two constants later.

**Breaking change:** Default `scalar_trunk` + Fugu2 example restored to 7-cap C27 → CLI hard-exits by default. This is the agreed loud-fail. Existing Fugu2 loss runs break until matrix mode lands or `--allow-scalar-cin` is passed. The loss consumer must surface WHICH trigger fired (negative residual / offdiag spread / negative shares), not just a nonzero exit.

**Override flags (dual-stage defense-in-depth):**
- Producer (`extract_parasitics.py`): `--allow-scalar-cin` — emit scalar despite invalid
- Consumer (`loss.py`): `--allow-scalar-cin` — consume scalar despite `cin_model_valid=false`
- Old JSON without `cin_model` field = invalid-by-default on the consumer side
- Both flags use the same name for consistency; each gates its own stage

## Validation Controls

Two checks, two positive controls, two negatives. Do NOT cross-assign controls.

| Check | Positive control (must fail) | Negative control (must pass) |
|-------|------------------------------|------------------------------|
| **Region detection** | 7-cap C27 matrix (MUST flag C27 as own region) | 6-cap near-bank (must pass, no multi-region) |
| **Switch separability** | Injection-style mismatch on simple-hb (point vs padland, 9.08 vs 6.86 nH) AND/OR long-SW synthetic deck (must fire on basis inconsistency / entangled switch path) | Matched injection style (must pass) |

**C27 is NOT a positive control for switch-separability.** C27 is switch-separable (heterogeneity on cap-cap axis, not switch axis). If the switch-separability check fires on C27, that's a false positive meaning FET-copper removal disturbed the near-bank mutuals (fixture failure), not detection of C27.

## Consumer-Side Changes (deck.py / models.py)

### Current state

- `models.py:assemble_cin_network` (line 98): builds one shared `Ltrunk` (VIN→vtr) + per-cap `Lb_<ref>` branches + cap elements. Consumes scalar `l_shared`/`r_shared`.
- `deck.py` (line 98): `cin_has_trunk: bool` — when True (Model B), uses `L_loop_switch` (trunk-excluded residual) for `Lloop_hs`/`Lloop_ls` instead of full `L_loop`. The two together reconstruct the full ring L without double-counting.

### Required changes

1. Replace `cin_has_trunk: bool` with `cin_mode: Literal["scalar_trunk", "matrix", "matrix_with_sw_coupling", "none"]`

   **CRITICAL LANDMINE (line 843):** `loss.py:843` currently sets `cin_has_trunk = cin_src.startswith("assembled")` — routing the copper contract off assembled-vs-file. Today this is correct ({assembled → Model B, .lib → Model A legacy}). But the moment matrix mode emits a Model-C `.lib`: it loads via `--cin-network`/sibling → `cin_src='file'` → `cin_has_trunk=False` → Model A semantics → deck adds full `L_loop` AND the `.lib` carries `L_sw_element` → **SILENT DOUBLE-COUNT** of the switch path. The exact class this spec exists to kill, reintroduced through the contract-selection heuristic. **Fix:** contract selection must move to `cin_model.mode` from the JSON (this Required Change #1), NOT assembled-vs-file. Add a refusal: a `.lib` consumed on a build that still uses the 843 heuristic with no `cin_mode` dispatch must REFUSE. This elevates the `.lib` provenance header from freshness/validity nicety to **CORRECTNESS REQUIREMENT** — the header must carry mode (A/B/C) as first-class so the consumer can pick the right copper contract for a `.lib` input. Delete line 843 when matrix `.lib` emission lands. Code comment at 843 NOW: `TODO(matrix): contract selection must move to cin_model.mode; assembled-vs-file heuristic double-counts a Model-C .lib.`
2. `scalar_trunk` path: unchanged from current Model B (asserts `cin_model_valid == true`)
3. `matrix` path: `assemble_cin_network` accepts cap-only L matrix → emits coupled-L subckt (self-L + K) + scalar `L_sw` as switch inductor. Deck uses `L_sw` directly (no subtraction needed — decomposition is explicit).
4. `matrix_with_sw_coupling` path: same as matrix + explicit `K(cap_i, L_sw)` for caps with significant modeling-gauge m_i.
5. `none` path: full multiport Cin network inlined, no decomposition, no switch subtraction.
6. Dual R: `assemble_cin_network` accepts both `R_100k` (for coupled network) and `R_dc` (for conduction-loss reporting).
7. Double-count contract: the copper contract documentation must be updated — matrix mode is a third contract alongside Model B (trunk-in-cin_network-at-ripple, switch residual in Lloop) and Model A (no trunk, full L_loop in Lloop).
8. **Null contract:** `matrix_valid: null` / `full_multiport_required: null` must be consumed as "unavailable/unknown," never as falsy-OK. Deck.py must explicitly check for `null` and refuse, not fall through to a default. Pin this in the consumer-side spec.

### Model C Copper Contract (matrix / matrix_with_sw_coupling)

Matrix mode is a THIRD copper contract. State it as explicitly as Model B was stated, or the
double-count bug returns wearing a new hat.

**Existing contracts, for contrast:**
- **Model A** (no trunk): `cin_network` = per-cap branches only; the deck places the FULL
  `L_loop` in the loop paths.
- **Model B** (`scalar_trunk`): `cin_network` = shared trunk (`cin_L_shared`) + per-cap
  branches; the deck places the trunk-excluded residual `L_loop_switch` (+ `r_hs_switch`/
  `r_ls_switch`) in the loop paths. Trunk + residual reconstruct the ring L.

**Model C** (`matrix`, `matrix_with_sw_coupling`):

- **All switch-side board-copper L lives INSIDE `cin_network`.** The subckt contains: per-cap
  branch inductors `Lb_i = L_cap_ii` with couplings `K_ij` (from the cap-only matrix), AND the
  switch residual `L_sw` in trunk position (`VIN → vtr`). The internal node `vtr` IS the
  demarcation plane P from the producer-side extraction: the cap-only matrix is referenced to
  P and `L_sw` spans P → die junction, so this placement reproduces the extracted split
  exactly — no subtraction, no reconstruction arithmetic in the deck.
- **Why inside, not in the deck's loop paths:** `matrix_with_sw_coupling` emits
  `K(Lb_i, L_sw)` statements from the fitted `m_i`, and SPICE `K` requires both inductor
  elements in the same subckt scope — cross-subckt K is fragile/nonportable. Since the
  sw-coupling mode needs `L_sw` inside, plain `matrix` mode places it inside too: one
  placement rule, not two.
- **The deck sets its switch-side board-copper L to ZERO** in Model C (`Lloop_hs`/`Lloop_ls`
  board-copper contribution = 0; no `L_loop_switch` subtraction — that quantity does not
  exist in this contract). Device package/lead L is unaffected: it belongs to the device
  models per the leads_internal contract, is never part of `L_sw`, and stays where it is.
- **R placement stays split in the loop paths:** `r_hs_switch`/`r_ls_switch` remain in the
  hs/ls loop branches (R does not couple; per-device R placement drives per-device loss
  attribution). `cin_network` carries only the per-cap `R_100k` diagonals. `R_dc` is a
  reporting field for the conduction-loss path, not a netlist element in the ring network.
- **Gauge-fixed inputs only — field-naming discipline** (see the Gauge reconciliation
  subsection under switch_separability): the payload carries `L_sw_element` (modeling gauge —
  the ONLY value `assemble_cin_network` wires as an inductor, together with the K-set in the
  SAME gauge) and `L_sw_physical` + `m_i_physical` + `regauge_c` (provenance metadata, read
  ONLY by the refusal/geometry check, never wired as an element). `assemble_cin_network`
  refuses a `matrix*` payload whose gauge-fix cross-check failed or whose physical-gauge
  metadata is absent — an un-gauged `L_sw` is not a physical element value — and asserts it
  never wires `L_sw_physical`. Mixing gauges (physical `L_sw` with modeling-gauge outlier-only
  K's) silently drops `2*m_near` from every near-bank loop — the double-DROP footgun, mirror
  image of the double-count bug.
- **Known limitation (same as Model B):** a single scalar `L_sw` lumps hs/ls switch-side
  copper into one element, exactly as Model B's single `L_loop_switch` did. Asymmetric hs/ls
  copper beyond plane P is not represented in v1; do not "fix" this by adding L back into the
  deck's loop paths without redefining the contract.
- **Acceptance identity (netlist level):** for each cap i,
  `L_cap_ii + L_sw + 2*m_i = L_full_ii` within the null-perturbation floor (this is
  `delta_ii = L_sw + 2*m_i` re-checked on the emitted element values). End-to-end check: a
  single-cap deck's ring frequency must reproduce the full-loop extraction's ring — the same
  style of ring smoke used for the Model B acceptance.

### basis='identity' Contract (leads-internal + lead_mm=0 — the measured Fugu2 verdict)

Established by the 2026-07-08 tie-breaker measurement: at `lead_mm=0` with leads-internal
(`_L0`) device models there is NO board-copper package excursion to decompose — `full_loop`
and per-FET cap-only closure are the identical operation (per_fet delta ≈ 0 = solver noise,
0.0008 nH Fro, which doubles as the empirical same-deck noise floor), and the cell-bridge
trunk split paid a 0.326 nH structured fixture residual (400x above that floor). Verdict:
emit the FULL Cin port submatrix directly, with no decomposition.

- **`L_sw_element = 0` means NO SEPARATE TRUNK ELEMENT — NOT zero switch-side inductance.**
  The SW pour + switch-cell board copper is IN the coupled matrix (that is exactly why
  `K_max` rides at 0.9451 on Fugu2 — pour-in-matrix). A consumer that reads
  `L_sw_element=0` as "no switch inductance" and also suppresses the matrix's shared switch
  content commits the double-DROP in a new hat. State it affirmatively in the payload:
  `basis='identity'` ⇒ switch board-copper L lives in the matrix; deck adds ZERO switch-side
  board L and ZERO trunk; FET package L comes from the leads-internal device model.
- **No gauge port required — structurally:** nothing was decomposed, so the additive-model
  gauge freedom never arises. `gauge_fix_status='structurally_not_required'` (distinct from
  `'missing'`, which the dispatch class refuses). The producer must set this only when
  `basis='identity'` is genuinely in effect (pad-ideal closure, leads-internal), never as a
  default.
- **K-merge safety still applies:** identity emission is a coupled-L realization, so the
  `max(K_ij) >= 0.95` port-merge/refusal rail applies unchanged. Fugu2 measures `K_max = 0.9451` —
  passing but 0.0009 from the rail; tighter cap banks may require the merge.
- **R handling:** identity `R_100k` diagonals include the shared switch resistance because the
  full port includes the switch pour. The loss consumer subtracts `r_hs_switch+r_ls_switch`
  from each matrix diagonal for the branch R it wires, leaves those shared R fields in the
  loop paths, and comments both the reconstruction residual and any omitted off-diagonal R.
- **Acceptance observable:** ideal current-share closure is the hard observable. The identity
  matrix reproduces the extractor split including the circulating C16 sign, while the scalar
  trunk flips C16 positive and starves C27. Ring frequency on this fixture is comparatively
  insensitive and is not the acceptance criterion for the identity slice.
- **Scope:** this contract is specific to leads-internal + `lead_mm=0` fixtures. For
  `lead_mm>0` / die-only device models the package excursion is real board-side content, the
  A-vs-B closure contest is live again, and the Model C trunk contract above applies — rerun
  the tie-breaker (with B-open, die shorts dropped) before choosing. Today's numbers do not
  extrapolate.

## File Inventory

| File | Path | Changes |
|------|------|---------|
| solve_reduce.py | `dcdc-tools/parasitics/lib/solve_reduce.py` | Implemented: scalar loud-fail diagnostics; identity `cin_matrix` emission with `cin_model.mode/basis`, matrix validity, full-matrix payload, strict `K < 0.95` producer rail, pure additive-fit helper, decomposed matrix payload builder, and reducer-side full/cap/switch handoff combiner. Future: CLI orchestration, switch_separability gate wiring into emitted `cin_model`, region assignment. |
| extract_parasitics.py | `dcdc-tools/parasitics/extract_parasitics.py` | Implemented: `--cin-network-model matrix`, scalar refusal/override, unsupported `matrix_with_sw_coupling` refusal. Future: `--drop-fet-filaments`, DC/1kHz R_dc solve. |
| kicad_geom.py | `dcdc-tools/parasitics/lib/kicad_geom.py` | Implemented: pad-ideal closure for `lead_mm<=0`, topology metadata, equivalent-node residual-port guard. Future: non-degenerate cap-only FET-filament replacement for `lead_mm>0`. |
| models.py | `dcdc-tools/loss/lib/models.py` | Implemented: identity matrix coupled-L assembly, PSD/K validation, R diagonal switch-R subtraction, offdiag-R omission diagnostics, K-preserving flatten. Future: cap-only matrix + `L_sw_element` + cap↔switch K. |
| deck.py | `dcdc-tools/loss/lib/deck.py` | Implemented: explicit `cin_mode`/`cin_basis`, matrix identity board-L-zero contract, no-cin-ceramics K stripping. |
| loss.py | `dcdc-tools/loss/loss.py` | Implemented: identity matrix gate, matrix/scalar dispatch, provenance source-sha policy, stale generated-lib refusal vs explicit-lib warning. |
| fugu2-perDev-noLeads.yaml | `dcdc-tools/parasitics/examples/fugu2-perDev-noLeads.yaml` | Implemented: identity matrix fixture (`lead_mm: 0`, `cin_network_model: matrix`). |

## Implementation Order

1. **Done:** Interim scalar gate — flag negative residual/offdiag spread/negative shares, preserve raw diagnostics, and refuse scalar by default while retaining clamped legacy fields only behind override.
2. **Done:** Identity matrix mode — full Cin submatrix producer payload and consumer coupled-L realization.
3. **Done:** Explicit consumer contract — `cin_mode`/`cin_basis`, identity board-L-zero deck contract, generated-lib provenance checks.
4. **Next:** FET-filament replacement flag for non-degenerate `lead_mm>0` cap-only extraction.
5. **Partly done:** Switch-separability additive fit core (`delta_ij = L_sw + m_i + m_j`), decomposed payload builder, and reducer-side three-run combiner are implemented and unit-tested; remaining work is CLI orchestration plus null-perturbation floor calibration.
6. **Next:** Region assignment from cap-only off-diag clustering (C27 own region by default).
7. **Next:** `matrix_with_sw_coupling` — emit `L_sw_element` plus K(cap_i, L_sw) in modeling gauge.
8. **Next:** Dual R basis — true `R_dc` requires FastHenry sweep extension or a separate DC solve.
9. **Later:** Full multiport fallback and port-merge implementation when switch_separability fails or K hits the rail.
