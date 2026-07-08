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
