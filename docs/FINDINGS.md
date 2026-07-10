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

## Altium `.PcbDoc` conversion (ReboostV2, GaN half-bridge)

Consolidated Altium→KiCad converter at `lib/altium_import.py`, auto-invoked by
`extract_parasitics.py` when the input ends in `.PcbDoc`. Board: ReboostV2.1
(GaN half-bridge, HS=Q1/LS=Q2 GS61008T, SW=`HSS`, Vin=`Vb`, GND=`GND`, 4-layer).

### 5. KiCad's programmatic Altium importer inverts the board and drops a copper pour

- **Symptom.** A straight `PCB_IO_MGR.Load(ALTIUM_DESIGNER)` + `Save(KICAD_SEXP)` yields a
  board that meshes to **0 segments** (empty commutation loop); the `remapUnsureLayers()`
  asserts during Load are not cosmetic.
- **Root cause.** The importer flips ~the whole board onto B.Cu (491/507 SMD pads land
  B.Cu, `GetLayer()` reports F.Cu but `IsOnLayer(F_Cu)` is False), and **silently drops**
  the FET-area `Vb` copper — it was a *ShapeBasedRegion* on the top layer, the primitive
  KiCad's Altium reader loses. Zones also import as outlines only (no fill).
- **Fix.** `altium_import.py` 3-tier repair: (a) swap B.Cu-only pad layer sets → F.Cu
  (+mask/paste); (b) relayer power-net tracks only B.Cu→F.Cu (moving all 1184 tracks makes
  a degenerate all-NaN mesh); (c) `AddLayer(F.Cu)` to power zones — this KEEPS B.Cu so the
  4-layer via-stitched GND return survives ({F.Cu,B.Cu,In2}, 166 vias — NOT a 2D squash);
  synthesize the dropped `Vb` pour as a minimal bridge (bbox from FET-drain + Cin-Vin pads
  within 8 mm, 56.56 mm²) via S-expression text insert (the `pcbnew.ZONE()` ctor hangs);
  `ZONE_FILLER.Fill()` only after a Save+native-reopen (fill on the raw Altium board
  SIGSEGVs). Provenance to `OUT.altium.json` sidecar (stdout is polluted by KiCad noise).
- **Takeaway.** GUI import is the gold path when available; the programmatic pipeline is
  the headless fallback and every fix it applies is flagged PROVISIONAL in `report.md`.

### 6. Auto-discovery mis-includes a BJT and contaminates L_loop

- **Symptom.** Auto-discovery gave `L_loop = 2.61 nH`. (A mid-session transient state also
  reported 4.05 nH — **both were wrong**; the reproducible committed-code value is ~8.3 nH,
  see finding 7.)
- **Root cause.** On this GaN board auto-discovery pulled in **Q3 (MMBT5401, a gate-drive
  BJT)** as a second HS device, spawning 15 ports that kept the Zc matrix non-degenerate
  *while `P_ls` was silently dropped*. The 2.61 nH solve never had a real LS
  common-source port — a bogus BJT port propped up a degenerate matrix.
- **Fix / rule.** **Always pass explicit `--hs-ref Q1 --ls-ref Q2` for GaN/SiC boards.**
  Never trust an auto-discovery loop-L on a board with non-power transistors.

### 7. Pad-in-void POINT injection inflates loop L (the real ReboostV2 blocker)

- **Symptom.** With explicit refs, `L_loop` was **not reproducible** (a mid-session state
  reported 4.05 nH; committed code gives ~9 nH) and every run warned
  `N pad-land terminal fallback(s) used point-style pad nodes`.
- **Root cause.** 6 FET/Cin terminals sit in pour **clearance voids** (or are smaller than
  the mesh pitch), so no zone node falls strictly inside the pad land. `_pad_land_terminal`
  then dropped to a single welded pad-centre node = **POINT injection**, which concentrates
  current and inflates local loop L, and is sensitive to *which* node welds — hence the
  irreproducibility. `--weld-tol 3.0` and `--terminal-mode finite` do **not** clear it.
- **Fix.** `_pad_land_terminal` proximity fallback: when no node is strictly inside the pad,
  bond to the nearest same-net pour nodes within `radius = pad_half_diag + 1.5·pitch`
  (nearest-first, cap 8) as a *distributed contact patch* (`_pad_proximity_contacts`,
  `terminal_regions[].proximity=True`). Strictly additive — inert where pads already bond.
- **Evidence.** ReboostV2 `L_loop`: grid no-fix **8.98 nH** (6 point-fallbacks) → grid+fix
  **8.27 nH** (2 fallbacks) → **converges with the independent polygon mesher's 8.31 nH**.
  So the trustworthy ReboostV2 loop L is **~8.3 nH** — not the contaminated 2.61 nor the
  transient 4.05. Fugu2 impact is 1 pad (geom-only 5→4 fallbacks) → negligible. 88 tests pass.
- **Notes.** The synthesized Vb bridge pour is a non-issue (56.56 vs 550 mm² → Δ0.01 nH),
  and the *real* dropped pour recovered from the `.PcbDoc` (ShapeBasedRegions6[55], via
  `altium_monkey`, `x+=65.494, y=195.004−y` transform) gives the same ~8.3 nH. CSI = 0
  (gate routing dropped by the importer — expected, not a real zero). Separately, `fugu2-cu`
  can hit a pre-existing degenerate-filament FastHenry crash at fine pitch (unrelated).

### 8. ReboostV2 GaN commutation loop: ground-truth rebuild → L_loop = 2.6 nH (leadless), stackup R correction modest not ~8×

- **Symptom.** The 8.3 nH from finding 7 came from a board whose 4-layer stack is a *mixed*
  approximation. The `--relayer partial` pipeline flips pads/tracks/power-zones B.Cu→F.Cu but
  then (a) forces Altium-**TOP** pours (notably the **HSS** switch-node pour) onto F.Cu, (b)
  double-faces Vb/GND as `{F.Cu,B.Cu}` via `AddLayer`, and (c) leaves inner layers MID1/MID2
  un-flipped. Outer layers flipped, inner not = a physically inconsistent stackup.
- **Root cause (verified from OLE ground truth via `altium_monkey`).** ReboostV2's FETs (Q1/Q2)
  and local Cin are **BOTTOM-mounted** (Altium layer 32); the under-FET Vb pour is on that same
  layer, so Q1-D→Vb is a real *same-layer* bond (the earlier "synth bridge shortcuts vias" alarm
  was wrong — the 2 Vb vias are 23 mm away in the bulk area, never in that path). The genuine
  multilayer element is **HSS**: bottom-mounted FET pads → the Altium-TOP HSS pour via **14 vias**.
  The partial relayer collapses that via path onto F.Cu, and the double-faced GND shorts the return.
- **Fix.** Rebuild power copper from `altium_monkey` on a **consistent global flip**
  (Altium BOTTOM(32)→F.Cu, TOP(1)→B.Cu, MID1(2)→In2, MID2(3)→In1 — matches pads-on-F.Cu).
  ShapeBasedRegions *are* the poured fill, so each region is emitted **directly as a zone whose
  `filled_polygon` == its (deduped) outline** — no `ZONE_FILLER`, no synth pour, no `AddLayer`
  double-facing. Redundant power tracks inside same-net pours are dropped (loop is pour+via
  dominated); vias kept as-is. Region net resolved via **parent polygon** (poured regions carry
  `net_index=65535`). Transform **self-calibrated** per board: fix M to a Y-mirror (0-residual on
  a 177-component origin fit), solve translation by median over matched pads → `tx=65.5011,
  ty=195.0036`, 0 µm on 195/526 pads, Q1-D exact. Board: `reboost_groundtruth.kicad_pcb`.
- **Evidence — INDUCTANCE (RESOLVED).** The earlier 6.98 (opus-altium) vs 8.48 nH (oc-ee)
  "dispute" was **not** a plateau-frequency issue (that hypothesis is retracted). Two separate
  bugs, then one modelling choice, explained everything — run to ground cross-agent:
  1. *Point-injection weld* at the Q1/Q2 SW pads (pads on F.Cu, HSS pour on B.Cu via 14 vias):
     the bonding fell back to a single arbitrary welded node, so 3 runs on identical board+code
     gave 3 different `.inp` (differing in exactly the 4 weld segments), identical R, different Zc.
     Fixed by cross-layer via-top bonding + deterministic node selection (`5a891b9`).
  2. *`.equiv` teleports*: strict-inside **and** proximity terminal contacts, plus the `lead_mm=0`
     FET channel die-short, bonded across a gap with zero-impedance `.equiv`, teleporting current
     past pour/via copper. Fixed by replacing those `.equiv` bridges with finite-impedance
     `model.seg` spokes (`f0ef514`, `f2dae41`) — keeping the drain/source and terminal nodes
     **distinct** is what preserves the copper R. A 1 µm die-plane epsilon (`ecb5240`) only stops
     the `lead_mm=0` die plane sitting coincident with the pad plane; the epsilon *magnitude* is
     electrically negligible — it is the distinct nodes, not the 1 µm segment, that carry the R.
  3. *The dominant effect — `--lead-mm`.* GS61008T is a **leadless GaN LGA** part. The default
     `lead_mm=3` bolts fictitious 3 mm TO-220 lead risers onto it. `lead_mm=0` (correct for GaN)
     removes them. Full **measured** sweep on the fixed code (both agents, `ecb5240`+WT):
     `lead_mm` 0 / 0.5 / 1.0 / 2.0 / 3.0 → `L_loop` **2.64 / 3.05 / 3.74 / 5.42 / 7.25 nH**
     (R_HS **0.37 / 0.86 / 1.35 / 2.34 / 3.32 mΩ**, R_LS similar). L is monotonic and **convex** in
     lead length — ΔL/mm *accelerates* 0.82 → 1.38 → 1.68 → 1.83 (partial-self-L of a lengthening
     riser), so it is **not** linear; R does rise ~linearly (~0.98 mΩ/mm/side). The measured
     `lead_mm=3` endpoint is **7.25 nH** = 2.64 + ~4.6 nH of fictitious risers, nothing physical.
     (Earlier one-off `lead_mm=3` runs read 6.88 / 6.98 nH on pre-`ecb5240` code states; 7.25 is
     the current-code measured endpoint — the old 6.88 was never a sweep point.)
  **FINAL: `L_loop = 2.6 nH`, `r_hs/r_ls = 0.37/0.65 mΩ`** at `lead_mm=0` (leadless, pour+via
  copper only). Cross-agent confirmed: opus-altium 2.64 vs oc-ee 2.62 (~1%). **Always extract
  GaN/SiC leadless boards with `--lead-mm 0`.**
- **Evidence — RESISTANCE (earlier "~8×" claim CORRECTED — now with a controlled re-run).** The
  prior claim — ground-truth `r_hs=3.20`/`r_ls=3.46` vs partial-relayer 0.39/1.35, "~8× higher,
  the material correction" — was **confounded by `lead_mm`**: that 3.20/3.46 was a `lead_mm=3` run,
  ~90% fictitious lead resistance (~3 mΩ/side). Re-running **both** boards at the physically-correct
  `lead_mm=0` (the clean same-`lead_mm` comparison) removes the confound:

  | board @ `lead_mm=0` | `L_loop` | `r_hs` | `r_ls` |
  |---|---|---|---|
  | ground-truth rebuild  | 2.64 nH | 0.37 mΩ  | 0.65 mΩ |
  | Altium-import partial  | 3.89 nH | 0.365 mΩ | 0.80 mΩ |

  HS R is **essentially identical** (−1%); the ground-truth rebuild reads **lower** on both LS R
  (0.65 vs 0.80, ~19%) and loop L (2.64 vs 3.89, ~32%). So the stackup/relayer geometry is a
  **modest** correction (tens of %, on the LS return and loop-L), **not ~8×** — do NOT cite "8×".
  Note the two boards differ by more than GND facing (the import also collapses the 14-via HSS
  path onto F.Cu and has a different pour tessellation), so no single-mechanism "un-shorting the
  return raises R" direction argument holds — the honest statement is just the measured tens-of-%
  gap, with the ground-truth rebuild the physically-consistent number to cite.
- **Consumer warning.** Use the ground-truth stackup **and `--lead-mm 0`** for this GaN board:
  `r_hs`/`r_ls`/`L_loop` from `--relayer partial` *or* from any `lead_mm>0` run are unreliable
  (fictitious lead risers dominate both R and L). Clean ground-truth zones also **unblock the
  KiPEX cross-check** (the degenerate-filament crash on sliver Altium fills is gone).
- **FastHenry gotchas hit building this.** Hand-injecting raw power tracks over the zone fills →
  coincident/non-orthogonal filaments → `Error on factor: 3` (singular); drop them. At pitch 2.0
  the reduced Zc is near-degenerate (`SVD did not converge`) — use the shipped default pitch 1.0.
