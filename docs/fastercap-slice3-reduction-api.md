# FasterCap Slice 3 reduction API

## Scope

Slice 3 promotes only the Slice 2 fixture-qualified closed-thickness transform
into reusable code. It does not promote fixture qualification to PCB or physical
validation. Thin/open sheets remain unsupported.

The implementation is `lib/fastercap_reduction.py`. The explicit request has two
representations:

- `physical`, the default, which preserves every extrusion face;
- `effective_thickness`, which requires a finite positive SI thickness smaller
  than every source extrusion and an explicit `midplane`, `top`, or `bottom`
  anchor.

The same `thickness_bounds()` implementation is used by the retained Slice 2
study and the reusable surface transform. No separate production formula exists.

## Fail-closed geometry and qualification

The transform operates on closed finite-thickness `ConductorSurface` objects.
Every undirected panel edge must have exactly two opposite-oriented uses before
any dimensional qualification is considered; top/bottom-only open surfaces fail.
Each source extrusion is then identified by paired z faces, so one grouped
conductor may retain multiple PCB copper layers. X/Y coordinates and panel
topology are unchanged. Exact projected-prism checks reject source or candidate
conductor intersection/contact and enforce the Slice 2 clearance-change gate
`min(1 um, 1% source clearance)`.

The only `fixture_qualified` envelope is:

- two overlapping 1 mm square conductors in air;
- 35 µm physical copper and 100 µm source face gap;
- 34 µm effective closed thickness;
- midplane anchor.

Qualification also requires an explicit `synthetic_fixture` source context, the
pinned fixture ID, and `air_only` material scope. A dimensionally identical
`filled_zones_only` PCB or `full_stackup` model remains
`outside_fixture_envelope`. Every other geometry, medium, thickness, topology,
or anchor is likewise outside. Provenance always records
`physical_validation_authorized: false` and
`lifecycle_ceiling: numerically_converged_diagnostic`; the API provides no
validated-artifact path.

## Provenance and artifact identity

`apply_thickness_reduction()` records source/effective thickness, anchor,
simplification (`none` for this transform), area and planar displacement (both
zero), source/candidate clearance and error allowance, maximum surface
displacement, per-conductor source/candidate panel counts, qualification ID and
envelope, and lifecycle ceiling.

`write_fastercap_input(..., provenance=...)` binds that record into the deck
manifest with a canonical SHA-256. Manifest loading verifies the provenance hash
before solver launch. `extract_capacitance.py` adds KiCad conditioning metrics and
artifact class, then emits and consumes a canonical content-addressed geometry
manifest. `load_geometry_manifest()` verifies its content ID and filename, the
referenced deck and deck-manifest hashes, the full deck schema, and exact
reduction-provenance equality. Artifact paths are absolute. Filenames distinguish
physical/effective, air-only/full-stackup, and diagnostic status. Solver run
manifests already bind the complete deck manifest, solver settings, binary,
streams, resources, and matrices.

## Real Fugu2 API exercise

The authoritative PCB was emitted with SW=`SW`, VIN=`Solar+`, and
PGND=`BuckGND`, 34 µm effective thickness, and a midplane anchor. The result is a
filled-zones-only air diagnostic under
`out/fastercap-slice3-fugu-api/`:

- geometry content ID:
  `1fc5f486b7aedd400b13dbf16a7165871b4450ec24746e0ccbf34bf8d7f5d925`;
- deck SHA-256:
  `2bf4439c77de538d22f7529d8d7d09074ec19f07bbf80435841f70297cd717a3`;
- deck-manifest SHA-256:
  `2d8b460faea2e6ead5db3cdcbdff73c82b232080955da4d2a4a95a8b42468d80`;
- reduction-provenance SHA-256:
  `e9bc51f7c462c8d51cfaa0abdfc66b34538dfc4f6dc608e26897d5bfa518ea49`.

The three groups each retain two 35 µm source extrusions and unchanged panel
counts (SW 1,069; VIN 2,067; PGND 4,789). The minimum inter-conductor clearance
is unchanged at 200.347 µm. The manifest correctly says
`outside_fixture_envelope`, `air_only`, `diagnostic`, and
`physical_model_validated: false`. No capacitance value or numerical-convergence
claim is made here; those belong to Slice 4.
