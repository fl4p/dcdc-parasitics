# parasitics — findings

Numbered findings from extracting Fugu2 power-stage parasitics. Each records the
symptom, root cause, evidence, and the fix commit. Newest first within a section.

## Hardware-validation run (Fugu2, `--emit-cin-network`, KiCad pcbnew 9.0.4 + FastHenry)

Running the real pipeline on `Fugu2.kicad_pcb` surfaced three bugs that the synthetic
`test/test_reduce.py` suite **and** an adversarial code review both missed — each only
appears once real board geometry drives the FastHenry solve.

### 1. C27: a single floating port NaNs the entire solve

- **Symptom.** `--emit-cin-network` at `--pitch 2.0` → FastHenry returns **all-NaN** for
  every port at every frequency; the reduce dies in `np.linalg.cond()` ("SVD did not
  converge").
- **Root cause.** `--emit-cin-network` ports every input cap individually. **C27** — a
  0805 MLCC only 4.6 mm from the FETs — has F.Cu pads that never bond into the meshed
  pour at the 2.0 mm pitch (its identical twin **C9** does bond). A single disconnected
  port makes FastHenry's whole multiport solution NaN. Distance is a red herring: the
  *far* 36.6 mm electrolytic C12 connects fine through the pour; the *close* C27 sits on
  copper the coarse mesh doesn't reach.
- **Evidence.** `.external` list showed C27's node pair isolated; `drop_floating_ports`
  identified it by union-find from `P_pwr`. At `--pitch 1.0` C27 bonds (branch
  `Lb 5.99 nH / Rb 6.34 mΩ` — the high values correctly flag it as a weakly-bonded cap).
- **Fix.** `Model.drop_floating_ports()` removes any port not in `P_pwr`'s connected
  component, with a stderr warning + `topo['cin_dropped_ports']`, so an unbonded cap is
  dropped explicitly instead of poisoning every result. (`47c93c2`)
- **Takeaway.** For a *complete* full-bank run, use a pitch fine enough to bond every
  cap; a dropped port in the manifest means the mesh is too coarse for that cap.

### 2. Duplicate conduction-anchor port → singular Zc

- **Symptom.** FastHenry "Error on factor: 3" (matrix factorization fails).
- **Root cause.** The LF conduction anchor is ported as `P_bulk` across the nearest bulk
  cap (C11); `--emit-cin-network` then *also* ported C11 as `P_cin_C11` across the same
  Vin/GND pads → two identical `.external` → singular Zc. `cin_network_ports` dedup'd
  against the HF set but not the conduction anchor.
- **Fix.** Fold the anchor cap into `cin_net` under its existing `P_bulk` label
  (`anchors` arg); never re-port it. (`47c93c2`)

### 3. Negative branch Lb with a heterogeneous (bulk + MLCC) bank

- **Symptom.** Real full-bank `cin_branches` had small **negative** `Lb` on the nearest
  MLCCs (C18 −0.23 nH, C17 −0.05 nH) — non-physical for the SPICE `cin_network` inductor.
- **Root cause.** The shared trunk was the *mean* off-diagonal L. Bulk caps (self-L
  10–19 nH) have large mutuals that pull that mean **above** the MLCC diagonals, so
  `Lb_i = L[i,i] − L_shared` goes negative.
- **Fix.** Clamp the trunk to `min(mean off-diagonal, min diagonal)` and floor `Lb`/`Rb`
  at 0. The nearest cap gets `Lb → 0` (it *is* essentially the trunk); all others
  positive. Homogeneous banks (mean off-diag < min diag) are unchanged. (`4b5597d`)

## Reduction findings

### 4. Negative cap current-share in a tightly-coupled bank (not a polarity bug)

- **Symptom.** Warning `negative current share on ['C16','C22']` on the 6-cap parallel
  reduction.
- **Root cause.** The Fugu2 MLCC bank is *extremely* tightly coupled — off-diagonal
  mutuals are ~98% of the self-L (shared trunk ~8.4 nH; private branches sub-nH). In the
  ideal-cap (copper-only) limit the split is decided by those sub-nH differences, so the
  highest-branch-L caps pick up small **negative circulating** shares. Not a polarity or
  code error.
- **Evidence.** On the extracted port matrix, adding realistic cap ESL/ESR (0.5 nH /
  3 mΩ) regularizes the split to **all-positive** (C16 −6.6 → +7.3%, C22 −0.2 → +2.0%)
  and drops `cond(Zc)` 293 → 76.
- **Fix.** The warning now names the real cause/fix — pass `--cin-esl/--cin-esr` for the
  physical split — instead of only "review polarity/geometry". (`100b258`)
