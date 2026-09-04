# PyPEEC vs FastHenry — measured cross-check and scaling

**Date:** 2026-09-04 · **Machine:** macOS ARM, 11 cores, 36 GB RAM · **Status:** exploratory,
not a qualification. No result here is a validated extractor path.

**Question:** is there a multithreaded/GPU/faster alternative to FastHenry for the loop-L work
in `parasitics/`, and does it agree?

**Candidate:** [PyPEEC](https://pypeec.otvam.ch/) 5.8.0 — 3D quasi-magnetostatic PEEC solver
(Dartmouth PMIC), FFT-accelerated on a voxel grid, Python, MPL-2.0, `pip install pypeec`.
Installed here in a throwaway venv; nothing was added to `requirements.txt`.

Fixtures and generators: `experiments/pypeec-bench/`.

---

## 1. Fixture A — trace over trace (each solver's native element)

40 mm long, 2 mm wide, 80 µm copper bars; 1.68 mm centre-to-centre; shorted at the far end by a
2 mm × 0.5 mm link; port across the near end; ρ = 1.7241e-8 Ω·m; 1 MHz.
FastHenry: 3 segments. PyPEEC: 250 µm in-plane voxels, one voxel through the 80 µm copper
(2880 conductor voxels).

| | L | R | wall |
|---|---|---|---|
| FastHenry `nwinc=8 nhinc=3` | 20.844 nH | 11.386 mΩ | 0.02 s |
| FastHenry convergence band (`nwinc` 8–16, `nhinc` 1–6) | 20.829–20.857 nH | 11.19–11.50 mΩ | ≤0.1 s |
| PyPEEC | 20.871 nH | 10.286 mΩ | 1.8 s solve (+9.5 s mesher, import-dominated) |

**ΔL = +0.13 %.** That is a genuine independent confirmation of the inductance: two different
discretizations (filament bars vs voxel PEEC), two different codes, same answer.

**ΔR = −8 … −11 %.** Expected and explained: PyPEEC has a single voxel through the 80 µm copper,
so it cannot develop a thickness-direction skin profile (δ = 65 µm at 1 MHz), while FastHenry
subdivides with `nhinc`. Dropping FastHenry to `nhinc=1` moves it only to 11.19 mΩ, so the voxel
port model accounts for part of the residual too. **Do not use a single-voxel-thick PyPEEC model
for the conduction/ring resistances.**

## 2. Fixture B — trace over a return *plane* (they disagree)

Same trace, 40 × 20 mm return plane, 1.68 mm below, 1 MHz. FastHenry uses its native `g1`
uniform ground-plane element; PyPEEC voxelizes the plane.

| pitch | FastHenry L | FastHenry wall | PyPEEC L | PyPEEC solve |
|---|---|---|---|---|
| 2.0 mm | 16.198 nH | 0.2 s | 15.413 nH | 0.5 s |
| 1.0 mm | 16.340 nH | 0.2 s | 15.863 nH | 0.5 s |
| 0.5 mm | 16.467 nH | 0.7 s | 15.693 nH | 0.8 s |
| 0.25 mm | 16.504 nH | 5.9 s | 15.641 nH | 2.3 s |
| 0.125 mm | 16.633 nH | 113.1 s | 15.648 nH | 9.5 s |

**A ~5 % gap that does not close with refinement.** Measured contribution of the contact model:
spreading the FastHenry port and short from a single plane node to the full 2 mm width via
`.equiv` moves L from 16.501 → 16.304 nH, i.e. **1.2 %**. The remaining ~4 % is unexplained and
was **not** run to ground — the prime suspect is FastHenry's `g1` element (a fixed orthogonal
filament grid with its own approximations) against voxel PEEC.

Caveat on scope: the production extractor does **not** use `g1`; it meshes zones into segments
(`add_zones_polygon` / grid). So this 5 % is a statement about *this fixture*, not about
`extract_parasitics.py`. It does mean **PyPEEC is not yet usable as a numeric cross-check on
plane-return loops** until that residual is understood.

## 3. Fixture C — what PyPEEC actually costs

PyPEEC's cost scales with the **bounding-box voxel grid**, not with the copper in it. 0.25 mm
in-plane, 35 µm z, 48 z-layers (a 4-layer-board-like stack):

| box | grid | `n_total` | `n_used` | solve | peak RSS |
|---|---|---|---|---|---|
| 40 × 30 mm | 160 × 120 × 48 | 0.92 M | 20.8 k | 6.1 s | 0.89 GB |
| 80 × 60 mm | 320 × 240 × 48 | 3.69 M | 79.7 k | 29.2 s | 2.74 GB |

GMRES converged in **2 iterations** at both sizes — the FFT operator and preconditioner are doing
their job; the cost is the grid, not the iteration count.

Extrapolated to the whole ReboostV2 board (195.8 × 145.3 mm, 4 layers) at 0.25 mm:
grid ≈ 784 × 580 × 48 ≈ **21.8 M**, ≈ 3 min and ≈ 16 GB. Feasible on this 36 GB machine, at the
edge, and pointless — only the power stage matters, so any real use would crop, exactly as
`--margin` already does for FastHenry.

### Scaling, and where the crossover is

Same fixture, same copper, wall time only:

| pitch | FastHenry | PyPEEC solve | winner |
|---|---|---|---|
| 0.5 mm | 0.7 s | 0.8 s | tie |
| 0.25 mm | 5.9 s | 2.3 s | PyPEEC 2.6x |
| 0.125 mm | 113.1 s | 9.5 s | **PyPEEC 12x** |

Per 4x increase in cells, FastHenry costs 8.4x then 19x (superlinear); PyPEEC costs 2.9x then
4.1x (roughly linear in the grid, as an FFT method should be). **The crossover sits near 0.5 mm
pitch.** Above it FastHenry wins outright; below it PyPEEC pulls away fast.

The production extractor runs ReboostV2 at **1.0 mm** pitch — on the FastHenry side of the
crossover.

## 4. The real workload, for reference

`extract_parasitics.py --config examples/ReboostV2-polygon.yaml` re-run from scratch:

- 4856 nodes, 12842 segments, **6 ports, 11 frequencies**
- **1165 s (19.4 min)** wall
- `L_loop = 4.02 nH`, `CSI_hs = 0.07 nH`, `CSI_ls = 0.05 nH`, `R_ring = 5.35 mΩ`
  — reproduces `out/reboostv2-polygon/report.md` exactly.

The 19 minutes is **6 ports × 11 frequencies**, not one hard solve. PyPEEC pays the same
multiplier: it solves per frequency and would need one solve per port excitation too. Nothing
measured here says PyPEEC would finish this job faster.

## 5. Verdict

- **Numerically, PyPEEC is a credible independent check on loop inductance** — 0.13 % on bar
  geometry, from a completely different discretization. That is worth having for the Tier-2
  loop-L guard.
- **It is not a FastHenry replacement for this repo today.** Resistance is wrong at single-voxel
  copper thickness; the plane-return fixture disagrees by 5 %; and the port/terminal machinery
  (`padland`/`finite`/`point` terminal modes, CSI source-lead splitting, the Cin bases) has no
  equivalent in PyPEEC's voxel world and would have to be rebuilt.
- **Faster? Only above ~0.5 mm mesh pitch.** At the 1.0 mm pitch the extractor actually uses,
  FastHenry is at least as fast. PyPEEC's advantage is a scaling advantage, and it only pays off
  if you want meshes finer than the extractor currently runs.
- **The "GPU" claim is untested here.** PyPEEC's GPU path is CuPy, i.e. NVIDIA only. On this
  machine you get FFT plus threaded BLAS, nothing more. Do not repeat the GPU claim as if it
  applied to this hardware.
- **Its Gerber import needs `gerbv` and `mogrify`,** neither of which is installed here, so the
  advertised KiCad→Gerber→voxel path was not exercised.

Suggested next step if this is pursued: port **one** extractor fixture end-to-end (a single
commutation loop, cropped, with real terminals) and check `L_loop` against FastHenry on the same
copper — not to replace the extractor, but to give the loop-L guard a second opinion.

## 6. What about Palace?

Not benchmarked — but its formulation settles the question without a run.

Palace **does** have a `"Magnetostatic"` problem type that extracts an inductance matrix
(`~/dev/vendor/palace/docs/src/guide/problem.md:141`, `reference.md:619`). It is MPI-parallel
with real multi-GPU support, so unlike FastHenry it would actually use the machine.

But the formulation is `∇×(μ_r⁻¹ ∇×A_i) = 0` with **conductors as PEC** and unit **surface**
currents on the ports. That means:

- **External inductance only.** No field inside the copper, so no internal inductance, no skin
  effect, and no frequency dependence. For a PCB commutation loop the external term dominates,
  so it would be a reasonable *L* cross-check.
- **No resistance at all.** PEC conductors dissipate nothing. Every R this extractor produces —
  ring R, per-switch conduction R, SW spreading R, the whole I²R budget — is outside what Palace
  magnetostatic can compute.

Getting L *and* R out of Palace means the full-wave `"Driven"` mode with lumped ports and
surface-impedance boundaries, which requires meshing the air and dielectric volume with a
truncating outer boundary. This repo already knows what that costs: the electrostatic path
needed `lib/palace_plc_mesh.py`, an outer-domain expansion ladder, and a 24-rung fixture
qualification (`docs/palace-electrostatic-qualification-plan.md`).

So Palace is the *accurate and genuinely parallel* option and the mesher groundwork is partly
laid, but it is a much larger undertaking than PyPEEC and, in magnetostatic mode, answers only
half the question. **Not the fast drop-in.**

## Open, not closed

1. The ~4 % plane-return residual after the contact-model correction is unexplained.
2. Everything above is a single machine, single run each; no repeat timings, no error bars.
3. No PyPEEC run here modelled a real board — only synthetic fixtures.
4. Palace was assessed from its documented formulation only; no Palace magnetostatic run was
   made, so its wall time on this geometry is unmeasured.
