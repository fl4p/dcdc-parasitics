# KiPEX pcbnew Adapter — Headless Cross-Check

## Location

`dcdc-tools/parasitics/kipex_crosscheck/`

## What it is

A headless adapter that feeds KiCad boards to KiPEX's `Translator` (polygon quad
meshing) without the normal kipy IPC path (which requires launching from the
KiCad GUI). It wraps `pcbnew.LoadBoard()` into KiPEX's expected Board interface
(footprints, pads, tracks, vias, zones, stackup) and runs FastHenry on the
resulting `.inp`.

## Files

- `kipex_pcbnew_adapter.py` — the pcbnew→KiPEX Board adapter (shared by all
  cross-check scripts). Exports `Board`, `BoardLayer`, `FASTHENRY`, `kx`
  (the KiPEX translator module).
- `kipex_src/` — KiPEX's own source tree (translator.py, filaments.py, quad.py,
  etc.), vendored from the KiPEX plugin. Not modified except for the 2-layer
  via limitation.
- `kipex_simple_hb.py` — cross-check for the simple-hb test board.
- `kipex_reboostv2.py` — cross-check for ReboostV2 (GaN half-bridge).

## Dependencies

- KiCad's bundled Python (`pcbnew` module) — must run under
  `/Applications/KiCad/.../python3`
- KiPEX Python deps: `kipy`, `cffi`, `engineering_notation` — installed at
  `/private/tmp/kipex-pydeps06` (set `KIPEX_PYDEPS` env to override)
- FastHenry binary at `/Users/fab/dev/vendor/FastHenry2/bin/fasthenry`

## Usage

```bash
KICAD_PY="/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3"
REBOOST_PCB=/path/to/reboost_partial_conv.kicad_pcb \
$KICAD_PY kipex_crosscheck/kipex_reboostv2.py
```

Env vars: `REBOOST_PCB` (board path), `KIPEX_QUAD_MAX`/`KIPEX_QUAD_MIN` (mesh
density, default 3.0/1.0 mm), `KIPEX_OUT` (output dir), `KIPEX_PYDEPS`.

## Limitations

1. **2-layer only**: KiPEX's `translator.py` raises "Only two layers with most
   basic vias implemented" for >2 copper layers. The adapter reports only
   F.Cu + B.Cu in the stackup (inner layers ignored; through-vias still
   connect F.Cu↔B.Cu). This is a reasonable approximation for commutation loop
   L where current is primarily on outer layers.

2. **Altium-imported zone fills produce degenerate FastHenry filaments**: the
   KiCad Altium importer's zone fills contain sliver/overlap polygons that
   KiPEX's quad mesh turns into overlapping non-orthogonal filaments. FastHenry
   crashes with "mutual inductance = infinity" + "filament not expected length".
   This blocks KiPEX cross-check on Altium-imported boards until the zone fill
   geometry is cleaned up. Native KiCad boards (Fugu2, simple-hb) work fine.

## Validated results

| Board | KiPEX L_loop | dcdc L_loop | Notes |
|-------|-------------|------------|-------|
| Fugu2 | 4.51 nH | 4.13 nH (lead_mm=0.1) | Good agreement (8%) |
| simple-hb | 6.64 nH | 9.08 nH | dcdc over-extracts (rectangular zone mesh) |
| ReboostV2 | BLOCKED | ~9 nH (inflated) | Altium zone fill degenerate filaments |

See `docs/cross-check-findings.md` for the full Fugu2/simple-hb analysis.
