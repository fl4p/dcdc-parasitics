# Review brief: `probe_ports` — user-declared two-terminal ports

## Feature

`probe_ports` lets an extraction config declare extra FastHenry ports by
`REF.PAD`, so the loop inductance of a **mounting position** is extracted rather
than inferred:

```yaml
probe_ports:
  cap_at_d9:    [D9.2, D9.3]
  cap_q2_j3:    [J3.1, Q2.3]
  snubber_land: [R11.1, C8.2]
```

Each becomes one `.external` labelled `P_probe_<name>`, appended to the existing
`ports` list (every consumer is name-keyed, so appending is safe), plus a derived
`probe_ports` block in `parasitics.json` carrying `L`, `R`, `R_dc`, ring-band
`L_ring`/`R_ring`, the mutual to the commutation port, and the perturbation
verdict.

### Why

`verifications/ring-ident` fitted a 2.278 nF C0G from Q2.3 (BuckGND) to J3 (SW)
and measured a frequency shift 4.4× too small — only 0.430 nF of 2.278 nF
appeared in the tank (18.9 %). Inverting the ring leaves a degenerate `(Ls, Rs)`
family whose surviving branch is a mount loop of ~18–21 nH, putting the added
branch's own SRF at 23–25 MHz, on top of the ring being measured. That was a
hypothesis with **no independent check**, because `parasitics.json` had no port
anywhere near the capacitor. This replaces the inherited
`ESL_MIN/NOM/MAX = 0.6/0.9/1.2 nH` bound in
`verifications/ring-ident/predict_added_c.py` with a per-position measured value.

## Files changed

| File | Change |
|------|-------|
| `parasitics/lib/probe_ports.py` | **NEW.** Spec parsing (YAML mapping + wire form), label generation, and the guards that do not need pcbnew: label collision, node-pair collision, dropped-probe refusal, synthesized-short and device-closure refusal, production-Cin refusal, perturbation measurement, manifest |
| `parasitics/lib/kicad_geom.py` | `Model.existing_node()`, `Model.has_direct_link()`, `Model.ideal_link_component()`; `_probe_pad_node_stack()` (pad-by-number layer walk); `build_probe_terminals()`; `_device_closure_nodes()`; `--probe-ports` flag; `build(probe_ports=...)` refusal on the residual basis; port emission + guards; `topo.probe_ports` and sidecar |
| `parasitics/visualize_paths.py` | `IGNORED_STRUCTURED_KEYS` so the viewer stops rejecting mapping-valued extraction keys |
| `parasitics/lib/solve_reduce.py` | derived `probe_ports` block in `reduce_parasitics` |
| `parasitics/extract_parasitics.py` | `DEFAULTS` entry, `_validate_config` branch, `--probe-ports` CLI arg, `run_geom` wire serialization, post-merge validation, residual-leg stripping in `_run_reduce_basis` |
| `parasitics/examples/flu-D9-probe.yaml` | **NEW.** The config that answers the added-cap question |
| `parasitics/test/test_probe_ports.py` | **NEW.** 44 tests: one that watches each guard fire, plus its control |
| `parasitics/test/test_extract_config.py` | config shape/precedence/refusal + subprocess forwarding + residual-leg stripping |
| `parasitics/README.md` | port table row + `probe_ports` section |

## Guards, and the test that watches each one fire

The house failure mode is the **anti-monotone false PASS**: a check that, when it
cannot evaluate its input, returns the value meaning "fine". Nothing here does.
`extract_parasitics` today fails closed on structure/type but fails **OPEN** on
ref-set membership — a bogus `cin_loop_refs` entry is silently dropped
(`kicad_geom.py:1987-2039`). Probe ports deliberately do not inherit that.

| # | Guard | Behaviour | Fires in | Control |
|---|-------|-----------|----------|---------|
| 1 | refdes absent from the board | `ProbeError` → `SystemExit` | `test_guard1_unknown_refdes_is_a_hard_error_not_a_silent_skip` | `test_guard1_control_known_refdes_resolves` |
| 2 | pad number absent from that footprint | `SystemExit`, naming the pads that do exist | `test_guard2_unknown_pad_number_names_the_pads_that_do_exist` | same control as 1 |
| 3 | pad resolves to no copper contact | `SystemExit` (never the legacy bare pad-centre fallback) | `test_guard3_pad_with_no_copper_contact_is_a_hard_error` | `test_guard3_control_pad_over_a_pour_bonds_and_records_the_terminal`, `test_guard3_point_terminal_mode_is_accepted_and_labelled_as_such` |
| 4 | duplicate / colliding port label | `SystemExit` | `test_guard4_label_collision_with_a_generated_port_fires` | `test_guard4_control_distinct_labels_pass` |
| 5 | same node pair as an existing port | `SystemExit` quoting the singular-`Zc` reason and naming the port to read instead | `test_guard5_same_node_pair_as_an_existing_port_fires_and_says_what_to_read`, `test_guard5_two_probes_on_one_node_pair_also_fire` | `test_guard5_control_distinct_node_pairs_pass`, `test_guard5_ignores_a_pre_existing_duplicate_that_has_no_probe` |
| 6 | probe dropped as floating | `SystemExit` (the derived ports only warn) | `test_guard6_probe_dropped_as_floating_is_a_hard_error`, `test_guard6_also_fires_when_the_label_just_vanished_without_being_listed`, `test_guard6_fires_end_to_end_through_drop_floating_ports` | `test_guard6_control_surviving_probe_passes` |
| 7 | perturbation reported, never assumed | per-probe `pulled_new_copper` + `retained_nodes_added`, plus a run warning | `test_guard7_probe_on_isolated_copper_is_flagged`, `test_guard7_terminal_geometry_alone_counts_as_perturbation`, `test_guard7_no_baseline_ports_reports_perturbed_not_fine` | `test_guard7_probe_on_already_ported_copper_is_not_flagged` |
| 8 | `switch_residual` basis | refused explicitly, both in `build()` and in `parse_args` | `test_guard8_switch_residual_basis_refuses_probe_ports`, `test_probe_ports_refused_on_the_switch_residual_basis` | the `full_loop` control in the same config test |
| 9 | double validation (YAML bypasses argparse) | shape in `_validate_config`, full parse post-merge in `parse_args` | `test_probe_ports_yaml_endpoint_syntax_is_validated_post_merge`, `test_probe_ports_cli_string_is_validated_the_same_way` | `test_probe_ports_yaml_mapping_is_accepted` |
| **10** | **probe spans a synthesized short / a device's own closure** (added after coordinator review — see below) | `SystemExit`; never the near-zero | `test_probe_across_a_zero_lead_device_closure_is_refused`, `test_probe_across_device_terminals_is_refused_even_with_real_leads` | `test_probe_over_real_copper_is_not_mistaken_for_a_short`, `test_short_detection_does_not_fire_on_a_long_equiv_free_chain` |

### Guard 10 (added after coordinator review) — the probe must span COPPER

The canonical anti-monotone shape, and **none of guards 1–9 catch it**: the
terminals resolve, nothing floats, no label or node pair collides, no copper is
pulled — and the port still measures nothing, because the extractor itself
shorted its endpoints.

Measured on the full pipeline, Fugu2 @ `8424e85`, `lead_mm: 0`, probe across the
LS device tabs (`cap_at_d9` = D9.2 SW → D9.3 BuckGND):

```
P_probe_cap_at_d9   diag = 1.702011e-15 H   (0.0017 nH)
P_ls_D9             diag = 3.575232e-09 H
```

Reported with no warning, no null, no refusal — a non-measurement dressed as a
measurement, in exactly the position the feature exists to measure. The
node-pair collision guard cannot see it: the two nodes really are distinct, they
are merely shorted **through** the closure. Traced in the emitted deck, the path
is three hops:

```
N2797 (D9.2 pad) --seg 0.00100 mm--> N2801 (drain die)
                 --.equiv-->          N2802 (source die)
                 --seg 0.00100 mm--> N2799 (D9.3 pad)
```

Two independent refusals, because they fail on different axes:

* **`require_not_shorted(model, probes)`** — `Model.ideal_link_component()` walks
  only *impedance-free* links: `.equiv` (a FastHenry ideal short) and segments
  shorter than `SNAP`, the model's **own** node-identity grid. The threshold is
  therefore an existing constant that already defines "coincident" here, not a
  new tolerance, and emphatically not a threshold on the answer. Validated
  against the real 19-port deck: it flags `P_probe_cap_at_d9` and **nothing else**
  — not `cap_q2_j3`, not `snubber_land`, and none of the 16 derived ports
  (including `P_ls_D9`, which shares node N2797 with the probe).
* **`require_not_across_device_closure(probes, device_nodes)`** — structural, from
  the topo handles. With `lead_mm > 0` the closure is no longer a *short*, so the
  first guard correctly stops firing, but the port still reports synthesized
  package geometry under a name that promises board copper. Refusing under any
  `lead_mm` keeps the verdict from depending on an unrelated knob.

Calibrated on the real board: `cap_at_d9` fires at `lead_mm=0` **and** at
`lead_mm=3` (exit 1 both times, each guard verified to fire on its own with the
other disabled); `cap_q2_j3` + `snubber_land` pass (exit 0). Tests:
`test_probe_across_a_zero_lead_device_closure_is_refused`,
`test_probe_across_device_terminals_is_refused_even_with_real_leads`,
`test_probe_over_real_copper_is_not_mistaken_for_a_short`,
`test_short_detection_does_not_fire_on_a_long_equiv_free_chain`,
`test_device_closure_nodes_harvests_both_parallel_models`.

Two additional refusals, not in the original guard list but the same failure
class:

* **A probe name that would be altered by sanitizing is refused**, not sanitized.
  `cap-a` and `cap.a` both sanitize to `P_probe_cap_a`, so accepting them would
  silently emit **one** port for **two** declared probes
  (`test_probe_names_that_would_sanitize_are_refused_not_merged`).
* **Two endpoints that intern to the same node** (same net, layer and position
  within the `SNAP` grid) are refused — a port across one node measures nothing
  and makes `Zc` singular (`test_two_endpoints_that_intern_to_one_node_are_refused`).

### Guard 7 in detail — why it is a measurement, not a claim

`Model.prune()` retains only copper reachable from port endpoints, so a probe in a
previously-unported region **adds copper to the deck and can shift `L_loop`**. The
`P_out_hs` aux port dodges this deliberately (it snaps to an existing pour node
"so the FastHenry reduction is byte-for-byte unchanged"); a probe cannot always
dodge it, because its purpose is to reach a position no existing port covers.

`annotate_perturbation()` therefore computes, per probe:

* `terminal_segs_added` / `terminal_nodes_added` — geometry this probe's own
  pad-land terminals created, measured as a delta around their construction;
* `retained_nodes_added` — `component({a, b}) − component(all non-probe port
  endpoints ∪ keep_nodes)`, i.e. the nodes `prune()` now keeps only because this
  probe seeds them.

`pulled_new_copper` is the OR of those. **If no baseline exists** (no non-probe
ports at all) every probe reports `true` with
`perturbation_basis: "no_baseline_ports"` — absence of evidence never encodes
absence of the problem.

Related fix, found while building this: re-running the pad contact cascade on a
pad that is *already* a terminal would append a **second identical set of
pad→pour spokes** — real parallel copper, halving the terminal spreading R. So
`_probe_pad_node_stack` reuses an existing **pad-land terminal** (a node in
`Model.distributed_terminals`; a pour-mesh node that merely happens to coincide
with the pad centre does **not** qualify, since it was never bonded to the land)
and adds nothing. That is what makes "a probe on already-ported copper is
bit-identical" true rather than aspirational
(`test_probe_on_an_already_terminated_pad_adds_no_copper`).

Second, related fix — **two different questions had been conflated** in the layer
walk. "Did an earlier consumer already place a node here?" (`pre_existing`, which
decides whether the vertical THT barrel would be a duplicate) is **not** the same
as "is that node a real pad-land terminal?" (`is_reused`, which decides whether
the contact cascade may be skipped). On a 2-layer pad whose pour exists only on
F.Cu, the first consumer bonds on F.Cu and falls back to a bare pad-centre node on
B.Cu; keying the barrel on `is_reused` then added a **second barrel** every time.
Measured: 20 segments where 18 were correct — two duplicate barrels for a
two-endpoint probe. Fixed, and the regression test was calibrated against the
pre-fix code, where it fails with `assert 20 == 18`
(`test_partly_fallen_back_pad_does_not_get_a_duplicate_barrel`).

### Downstream

`solve_reduce.reduce_parasitics` raises if a probe declared in `topo.probe_ports`
is absent from the solved `ports` list — that combination is impossible given
guard 6, so seeing it means the sidecar does not describe the solve, and emitting
a `null` "measurement" would be exactly the failure the guards exist to prevent.
A manifest with no `pulled_new_copper` verdict yields `None` (not `False`) plus a
`reduce_warn` (`test_unknown_perturbation_verdict_is_reported_as_unknown_not_as_clean`).

## Independent Codex review (2026-08-17) — 7 findings, all addressed

An independent `codex exec` review was run against the working tree with the
physical stakes spelled out. It built the real Fugu2 geometry under KiCad's Python
and reproduced defects rather than speculating. Verdict: *"Do not ship this yet."*
All seven are fixed; each has a test calibrated against the pre-fix code.

| # | Sev | Finding | Disposition |
|---|-----|---------|-------------|
| 1 | High | THT barrel keyed on **node existence**, which is not proof a barrel exists — `single` mode re-added a barrel the legacy stack had placed (1→2 segs); pad-centre pour nodes on both layers suppressed a barrel that was never placed, leaving the stack **open** | Added `Model.has_direct_link()` (a pair index maintained at seg/equiv emission) and key the barrel on it. `test_single_mode_does_not_add_a_second_barrel_over_the_legacy_stack`, `test_pad_centre_pour_nodes_still_get_their_barrel` |
| 2 | High | (a) a pad on **no copper layer** silently became `cu[0]` — a fabricated land, bonded to whatever pour was there, 16 segs, guard 3 never fired; (b) **duplicate pad numbers** resolved by `next(...)`, silently choosing a land 10 mm away | Both refused. `test_pad_on_no_copper_layer_is_refused_not_invented`, `test_duplicate_pad_numbers_are_ambiguous_and_refused` |
| 3 | High | `pulled_new_copper` warned but did not **protect the production Cin model**: `cin_branches`/`cin_matrix` are reduced from the probed deck and consumed by the loss tool, which cannot know. Codex also showed stripping probes from all non-`full_loop` legs would be **worse** (it subtracts an unprobed `L_cap` from a probed `L_full`) | Hard refusal: `require_no_production_cin_network()`. Costs only the geometry build. Example config switched to `emit_cin_network: false` and labelled diagnostic-only. `test_production_cin_network_is_refused_on_a_perturbed_deck` |
| 4 | Med | `yaml.safe_load` accepts **duplicate keys** and keeps the last, silently deleting a declared probe before the duplicate-name guard sees it; `str(k)` also collapsed `1:` and `"1":` | Duplicate-key-rejecting `SafeLoader` subclass; non-`str` probe names refused. `test_duplicate_yaml_keys_are_refused_not_silently_last_wins`, `test_probe_names_must_be_strings_so_yaml_ints_cannot_collide` |
| 5 | Med | `pulled_new_copper` was **order-dependent**: two probes sharing a pad credited the cost to whichever ran first, so the other reported "harmless". Also: `retained_nodes_added` is **structurally zero** in any successful build | Added the order-free `endpoint_new_in_probe_phase` signal; documented the structurally-zero term so it is not read as corroboration. `test_guard7_endpoint_built_during_the_probe_phase_counts_even_with_zero_deltas`, `test_perturbation_attribution_is_order_independent` |
| 6 | Med | `M_to_loop` was `L[probe, cin_idx[0]]` — the mutual to the **first** Cin port only. Two equal-current caps with probe mutuals 1 nH and 5 nH: true effective mutual 3 nH, reported 1 nH | Now the weighted reduction the gate CSI already uses; raw row kept as `M_to_first_cin`, basis as `M_to_loop_basis`. `test_M_to_loop_is_the_effective_mutual_not_just_the_first_cin_row` |
| 7 | Low | `visualize_paths.py` rejected `probe_ports` as unknown despite advertising that extraction configs are accepted | `IGNORED_STRUCTURED_KEYS` (also fixes the same pre-existing break for `gate_net_override`). Verified under KiCad Python |

**Claims Codex tried to break and could not**, worth recording: stitch/weld ordering
is early enough; guard 5 sees the complete port list and its traversal is
deterministic (and the singular-`Zc` claim has prior real-FastHenry evidence at
`docs/FINDINGS.md:32`); `R`/`R_dc`/`L_ring`/`R_ring` are read at the frequencies
they report; raising on a declared-but-unsolved probe is right; illegal pad
strings are rejected rather than mangled; `None` inputs hard-fail rather than
false-pass; and J3/R11/C8 **are** inside the 8 mm ROI on this board.

Also fixed from its test critique: the guard tests called the helpers directly, so
deleting their production call sites would have left them green
(`test_every_guard_is_invoked_from_build`, which also pins that terminals are built
before `stitch_zones` and ports added before `drop_floating_ports`).

## Second independent Codex review — 3 further findings, all closed

A second review, run in parallel by the coordinator, found three defects the
first did not. Its verdict: *"I would not use the extracted value for publication
until findings 1–4 are closed."* All three are the same shape — **a plausible
number where there should be a refusal** — and all three were confirmed against
HEAD before being fixed.

| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| F1 | High | `solve_reduce` converted every probe entry with bare `float()` and validated nothing. A matrix with a valid `P_pwr` row and NaN **only** in the probe row emitted `L`, `R`, `R_dc`, `L_ring`, `R_ring` and `M_to_loop` all as `nan` with no warning; finite negative self-L/self-R passed too. The existing conditioning check covers only the Cin submatrix, never probe rows | `_validate_probe_numerics()`: refuses non-finite values (diagonal *and* the whole coupling row), non-positive self-L, materially negative self-R, non-reciprocal and ill-conditioned probe submatrices |
| F3b | High | Terminal reuse was keyed on the node alone — `(net, layer, snapped xy)` — not on pad identity. A **larger** pad at the same position reported `reused_existing_terminal`, added zero contacts and inherited a **smaller** pad's single-node contact region, materially changing spreading impedance while reporting a clean reuse | `Model.terminal_owner` records which pad built each terminal; reuse requires a `(ref, pad)` match, and a foreign terminal in the same SNAP cell is refused |
| F4 | Med | When no mesh node overlapped the pad, `_pad_land_terminal` fell through to `_pad_proximity_contacts` and the probe path accepted it as an ordinary `padland` terminal. A 0.1 mm pad with no overlapping copper bonded to a node 1 mm away and reported success; nothing in the probe entry said proximity had been used | Refused **by default** for probes, with `--probe-allow-proximity-bond` as an explicit opt-in; `bond` (`overlap` / `proximity` / `proximity_inherited` / `point_mode`) now reaches the probe entry and `parasitics.json` |

### The thresholds are measured, not guessed — and the obvious ones were wrong

F1 asked for reciprocity and conditioning checks. Implementing them naively would
have **refused every valid extraction**. Measured on the good Fugu2 solve:

| metric | naive form | on a VALID board | scaled/scoped form | on the same board |
|---|---|---|---|---|
| reciprocity | `|a−b| / max(|a|,|b|)` | **1.386** (fails) | `|a−b| / sqrt(|L_ii·L_jj|)` | **9.6e-4** (passes, 10× margin at a 1e-2 gate) |
| conditioning | `cond(full port matrix)` | **2.6e6** (fails a 1e6 gate) | `cond(Cin+probe submatrix)` | **49** (passes, 4+ orders of margin) |

The naive reciprocity metric blows up because near-zero off-diagonals dominate it
— the worst pair on the real board is `P_pwr`/`P_pwr1` at 2.79e-12 vs −1.08e-12,
both effectively zero. `test_F1_thresholds_do_not_false_fire_on_realistic_values`
pins this with the real numbers, so a future tightening cannot silently start
rejecting good boards.

### A judgement call worth flagging

F4 refuses a probe that **itself** fabricated proximity spokes, but only *warns*
when a probe **reuses** a terminal that some other port had already built that
way (`bond: proximity_inherited`). Refusing the inherited case would reject a
probe because of a decision the device/cap extraction made — the same
approximation that underlies that device's own port and `L_loop`, everywhere, by
validated default. Surfaced, not silent; refusing it would have been inconsistent
rather than safer.

**A claim of mine that the instrumentation falsified.** Before `bond` existed I
read the raw `terminal_regions` rows positionally and concluded that
`cap_q2_j3`'s Q2.3 end reused a proximity-bonded terminal. The instrumented run
says otherwise: `cap_q2_j3` is **`overlap` / `overlap`** at both ends. My
positional reading of which Q2 pad owned which row was simply wrong. The
falsification therefore rests on two genuine overlap bonds, not on an inherited
approximation — a stronger result than I claimed, and a reminder that the reason
to emit provenance is that reasoning about it is unreliable. `snubber_land`'s
R11.1 end is the only real proximity case on this board.

## Real-board verification (this is no longer a paper feature)

KiCad's Python is available here, so the geometry step was run for real against
`/Users/fab/dev/ee/hw/Fugu2/Fugu2.kicad_pcb` (FastHenry was **not** run — no
inductance has been solved yet).

| probe | terminal | segs added | verdict |
|---|---|---|---|
| `cap_at_d9` (D9.2→D9.3) | reused existing terminals both ends | 0 | `pulled_new_copper: false` |
| `cap_q2_j3` (J3.1→Q2.3) | padland + reused | 47 | `true` |
| `snubber_land` (R11.1→C8.2) | padland both ends | 7 | `true` |

Independently reproduced by Codex to the segment. Deck totals confirm the verdict
is calibrated, not decorative:

| run | nodes | segs |
|---|---|---|
| no probes | 2772 | 6564 |
| `cap_at_d9` | 2772 | 6564 |
| `cap_q2_j3` | 2773 | 6612 |

**Plan verification step 3 (non-perturbation A/B) is now DONE and passed.** With
`cap_at_d9`, the emitted `.inp` is **byte-identical across all 9384 non-`.external`
lines** to the same config without `probe_ports`; the sole difference is the one
added `.external N2797 N2799`. That is the terminal-reuse fix doing its job — the
claim "a probe on already-ported copper is bit-identical" is measured, not asserted.

The `emit_cin_network` refusal was also exercised end-to-end on the real board:
exit 1 with the explanatory message for `cap_q2_j3`, exit 0 for `cap_at_d9`.

## Test results

`python3 -m pytest parasitics/test -q` → **223 passed** (156 before this change).
`python3 -m pytest loss/test -q` → 679 passed, 31 skipped, 1 pre-existing failure
in `loss/test/test_model_spec.py::test_explicit_keys_lists_only_what_the_user_actually_wrote`
(`lm_tau_scale_ls` / `lm_tm_scale_ls` leaking into `explicit_keys`) which is
another session's work in `loss/` and is untouched by this change.

## The physics question — ANSWERED, and the hypothesis is REJECTED

Plan step 4 is done. Full pipeline (geometry + FastHenry) on
`ee/hw/Fugu2/Fugu2.kicad_pcb @ 8424e85`:

| probe | predicted | **extracted** | verdict |
|---|---|---|---|
| `cap_q2_j3` | ~18–21 nH | **L = 1.2055 nH**, `L_ring` = 1.1292 nH | **FALSIFIED** |
| `snubber_land` | between the two | **L = 1.7004 nH**, `L_ring` = 2.0517 nH | — |
| `cap_at_d9` | ~1–3 nH | **not measurable** — spans D9's own closure (guard 10) | refused |

**Re-run after F1/F3b/F4: every number is bit-identical.** Same config, same deck
(2774 nodes / 6618 segs / 18 ports), FastHenry re-solved:

| quantity | pre-fix | post-fix |
|---|---|---|
| `L_loop` | 1.222925 nH | **identical** |
| `cap_q2_j3` L / L_ring / M_to_loop | 1.205550 / 1.129237 / 0.208549 nH | **identical** |
| `snubber_land` L / L_ring / M_to_loop | 1.700354 / 2.051691 / 0.126487 nH | **identical** |

So **the falsification did not rest on any of the three defects** — they were
latent hazards on this board, not active contaminants. `bond` now records
`overlap`/`overlap` for `cap_q2_j3` and `proximity`/`overlap` for `snubber_land`.

Re-run **after** guard 10 and all seven Codex fixes, with the shipped
`examples/flu-D9-probe.yaml` (exit 0, 18 ports, `L_loop` 1.2229 nH). It reproduces
the coordinator's pre-fix values to four digits (1.206 / 1.700 nH), so the
falsification does not rest on the pre-fix code. The small `L_ring` differences
(1.1292 vs 1.113, 2.0517 vs 2.053) are the expected consequence of that run
carrying a third probe: `cap_at_d9` is refused now, so the deck differs slightly.

The plan's own falsification criterion — *"if `cap_q2_j3` comes back at ~1 nH, my
~20 nH explanation is wrong"* — **is met**. The detour-loop explanation of the
18.9 % coupling is out; ~1.2 nH puts the added branch's series resonance nowhere
near the 23–25 MHz that would have sat on the ring. This is the feature working
as intended: it was built to be able to falsify the belief that motivated it, and
it did.

Independently, `PLAN-added-c.md` now records that the bench comparison behind the
18.9 % figure was **cross-session** (stock 2026-08-16 20:23 UTC vs fitted
2026-08-17 07:19 UTC) against a +3.5 % non-averaging between-session systematic,
so the −4.42 % shift never separated the intervention from drift. **The
measurement the ~20 nH was invented to explain is itself not established.** Two
independent reasons to stop treating ~20 nH as an expectation.

`examples/flu-D9-probe.yaml` and this document no longer present ~20 nH as
something to confirm.

**Codex finding 6 was not theoretical — measured on this board.** With the 7-cap
Cin bank, the effective mutual and the raw first-Cin row differ by a factor of
3.5:

| probe | `M_to_loop` (effective, correct) | `M_to_first_cin` (what the pre-fix code reported) |
|---|---|---|
| `cap_q2_j3` | **0.2085 nH** | 0.7251 nH |
| `snubber_land` | **0.1265 nH** | 0.2014 nH |

Also confirmed on real data: `retained_nodes_added` is **0** for both probes while
both are correctly flagged `pulled_new_copper: true` — exactly the
structurally-zero behaviour documented under guard 7, with the terminal-geometry
signal doing the work.

**ROI reachability (was open):** resolved — J3, R11 and C8 are all **inside** the
8 mm ROI on this board (ROI ≈ `(10.955, 23.664)–(79.460, 108.748)` mm), confirmed
independently by Codex and by the real-board runs completing with no dropped ports.

## Correction to the plan's follow-on note

The plan states `examples/flu-D9-probe.yaml` wants `ls_ref: [D9]` and that
`allow_missing_gate_ports: true` is the workaround for the resulting
`gate_net_override` failure. **`allow_missing_gate_ports` does not unblock it.**
There are two independent blockers, and that flag reaches neither:

1. `gate_net_override.py:72` computes `sibling_refs = declared_fet_refs −
   overridden_refs`. With D9 both the only LS ref and the overridden one, no
   declared sibling has a pad 1 on `Net-(Q2-G)` → `SystemExit` at `:74`.
2. Dropping the override does not help: D9's pads are BuckGND / SW / BuckGND, so
   `fet_discovery.classify_gate_and_rail` picks `gate = BuckGND = source` and
   `validate_switch` refuses the FET as gate-source shorted.
   `allow_missing_gate_ports` downgrades a **missing gate port**, not an invalid
   FET topology.

`examples/flu-D9-probe.yaml` therefore uses the dual-LS declaration that is known
to run (`ls_ref: [Q2, D9]` + the override, as in
`fugu2-dualLS-perDev-noLeads.yaml`), with the reasoning written into the file
header. The probes are local pad-to-pad loops, so the extra Q2 lead/die copper
shifts the commutation loop rather than the mount loops — but this substitution
is a **deviation from the plan** and should be revisited if the D9-only path is
unblocked (letting an override anchor on a non-populated footprint's pad).

## Risks

* **R1 (closed) — matrix-Cin leg consistency.** Was a warning; now a hard refusal
  (Codex finding 3). A run that emits the Cin network and has any probe reporting
  `pulled_new_copper: true` fails at the geometry step. Codex additionally showed
  my proposed alternative — stripping probes from every non-`full_loop` leg —
  would have been **worse**: the combine subtracts `L_cap` from `L_full`, so an
  unprobed minuend against a probed subtrahend contaminates the difference
  directly instead of cancelling. It also corrected my diagnosis: changing the
  unprobed `L_sw_physical` alone does *not* move the emitted gauge, because the
  median regauging cancels that constant.
* **R5 — the viewer is still not usable with a real extraction config.**
  `visualize_paths.py` no longer rejects `probe_ports` (or `gate_net_override`),
  but it still rejects `cin_loop_refs`, `cin_network_model`, `lf_freq` and
  `zone_mesh` as unknown keys. That is pre-existing and out of scope here; the
  advertised "point both tools at the same config" is still not true.
* **R2 — partial-layer fallback.** A pad whose copper bonds on some layers and
  falls back on others is accepted (with a stderr warning and a
  `+point_fallback_on_N_layer(s)` marker in the terminal provenance). Only a pad
  that bonds on *no* layer is a hard error. A more aggressive rule would refuse
  legitimate THT pads whose inner layers carry no same-net pour.
* **R3 — `--terminal-mode point`.** Accepted, because there the pad-centre node
  is the run-wide terminal model rather than a silent substitution; recorded as
  `a_terminal: "point_mode"`.
* **R4 — probe ports cost solve time.** Each adds a row/column to `Zc` and shows
  up in `mesh_complexity.work_units` (`work ∝ ports`). Three probes on a Fugu2
  run take it from 13–15 to 16–18 ports.
