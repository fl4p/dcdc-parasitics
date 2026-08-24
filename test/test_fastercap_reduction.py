import hashlib
import json

import numpy as np
import pytest

from lib.fastercap import (
    ConductorSurface,
    box_surface,
    write_fastercap_input,
)
from lib.fastercap_diagnostics import validate_deck_manifest
from lib.fastercap_reduction import (
    QUALIFICATION_ID,
    ReductionContext,
    ThicknessReductionRequest,
    apply_thickness_reduction,
    diagnostic_artifact_stem,
    expand_conductor_components,
    thickness_bounds,
    write_content_addressed_manifest,
)


FIXTURE_CONTEXT = ReductionContext(
    "synthetic_fixture", "air_only", QUALIFICATION_ID
)
PCB_CONTEXT = ReductionContext("filled_zones_only", "air_only")


def parallel_plate_fixture():
    lower = box_surface(
        "lower", (0.0, 0.0, -17.5e-6), (1e-3, 1e-3, 17.5e-6)
    )
    upper = box_surface(
        "upper", (0.0, 0.0, 117.5e-6), (1e-3, 1e-3, 152.5e-6)
    )
    return lower, upper


def z_levels(surface):
    return np.unique([
        point[2] for panel in surface.panels for point in panel
    ])


def test_request_rejects_unsupported_and_invalid_inputs():
    with pytest.raises(ValueError, match="thin sheets are unsupported"):
        ThicknessReductionRequest("thin_sheet", 1e-6)
    with pytest.raises(ValueError, match="cannot set effective thickness"):
        ThicknessReductionRequest("physical", 34e-6)
    for value in (None, 0.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite positive"):
            ThicknessReductionRequest("effective_thickness", value)
    with pytest.raises(ValueError, match="anchor"):
        ThicknessReductionRequest("effective_thickness", 34e-6, "centre")


def test_thickness_bounds_pin_si_units_and_anchors():
    midplane = 1e-3
    request = ThicknessReductionRequest("effective_thickness", 34e-6)
    assert thickness_bounds(midplane, 35e-6, request) == pytest.approx(
        (0.983e-3, 1.017e-3)
    )
    top = ThicknessReductionRequest("effective_thickness", 34e-6, "top")
    assert thickness_bounds(midplane, 35e-6, top) == pytest.approx(
        (0.9835e-3, 1.0175e-3)
    )
    bottom = ThicknessReductionRequest("effective_thickness", 34e-6, "bottom")
    assert thickness_bounds(midplane, 35e-6, bottom) == pytest.approx(
        (0.9825e-3, 1.0165e-3)
    )
    with pytest.raises(ValueError, match="smaller than source"):
        thickness_bounds(
            midplane, 35e-6,
            ThicknessReductionRequest("effective_thickness", 35e-6),
        )


def test_qualified_transform_is_exact_and_cannot_authorize_physical_use():
    request = ThicknessReductionRequest("effective_thickness", 34e-6)
    result = apply_thickness_reduction(
        parallel_plate_fixture(), request, FIXTURE_CONTEXT
    )
    lower, upper = result.conductors
    assert z_levels(lower) == pytest.approx((-17e-6, 17e-6))
    assert z_levels(upper) == pytest.approx((118e-6, 152e-6))
    provenance = result.provenance
    assert provenance["qualification_id"] == QUALIFICATION_ID
    assert provenance["qualification_state"] == "fixture_qualified"
    assert provenance["source_minimum_clearance_m"] == pytest.approx(100e-6)
    assert provenance["candidate_minimum_clearance_m"] == pytest.approx(101e-6)
    assert provenance["maximum_clearance_error_m"] == pytest.approx(1e-6)
    assert provenance["allowed_clearance_error_m"] == pytest.approx(1e-6)
    assert provenance["maximum_planar_boundary_displacement_m"] == 0.0
    assert provenance["maximum_surface_displacement_m"] == pytest.approx(0.5e-6)
    assert provenance["maximum_area_relative_error"] == 0.0
    assert provenance["source_panel_count"] == {"lower": 6, "upper": 6}
    assert provenance["candidate_panel_count"] == {"lower": 6, "upper": 6}
    assert provenance["physical_validation_authorized"] is False
    assert provenance["lifecycle_ceiling"] == "numerically_converged_diagnostic"


def test_open_or_untrusted_fixture_geometry_cannot_qualify():
    request = ThicknessReductionRequest("effective_thickness", 34e-6)
    open_surfaces = []
    for surface in parallel_plate_fixture():
        horizontal = tuple(
            panel for panel in surface.panels
            if len({point[2] for point in panel}) == 1
        )
        open_surfaces.append(ConductorSurface(surface.name, horizontal))
    with pytest.raises(ValueError, match="not a closed orientable surface"):
        apply_thickness_reduction(
            tuple(open_surfaces), request, FIXTURE_CONTEXT
        )

    pcb_result = apply_thickness_reduction(
        parallel_plate_fixture(), request, PCB_CONTEXT
    )
    assert pcb_result.provenance["qualification_state"] == (
        "outside_fixture_envelope"
    )
    material_context = ReductionContext(
        "synthetic_fixture", "full_stackup", QUALIFICATION_ID
    )
    material_result = apply_thickness_reduction(
        parallel_plate_fixture(), request, material_context
    )
    assert material_result.provenance["qualification_state"] == (
        "outside_fixture_envelope"
    )


def test_clearance_and_intersection_gates_fail_closed():
    with pytest.raises(ValueError, match="clearance change"):
        apply_thickness_reduction(
            parallel_plate_fixture(),
            ThicknessReductionRequest("effective_thickness", 1e-6),
            FIXTURE_CONTEXT,
        )
    lower, _ = parallel_plate_fixture()
    touching = box_surface(
        "touching", (1e-3, 0.0, -17.5e-6),
        (2e-3, 1e-3, 17.5e-6),
    )
    with pytest.raises(ValueError, match="intersect or touch"):
        apply_thickness_reduction(
            (lower, touching), ThicknessReductionRequest("physical"),
            FIXTURE_CONTEXT,
        )


def test_nonfixture_geometry_and_anchor_cannot_inherit_qualification():
    lower, upper = parallel_plate_fixture()
    shifted = box_surface(
        "upper", (0.1e-3, 0.0, 117.5e-6),
        (1.1e-3, 1e-3, 152.5e-6),
    )
    request = ThicknessReductionRequest("effective_thickness", 34e-6)
    result = apply_thickness_reduction(
        (lower, shifted), request, FIXTURE_CONTEXT
    )
    assert result.provenance["qualification_state"] == "outside_fixture_envelope"
    top = apply_thickness_reduction(
        (lower, upper),
        ThicknessReductionRequest("effective_thickness", 34e-6, "top"),
        FIXTURE_CONTEXT,
    )
    assert top.provenance["qualification_state"] == "outside_fixture_envelope"


def test_physical_representation_preserves_multilayer_surface():
    first = box_surface("net", (0, 0, 0), (1e-3, 1e-3, 35e-6))
    second = box_surface("net", (0, 0, 1e-3), (1e-3, 1e-3, 1.035e-3))
    combined = type(first)(
        "net", first.panels + second.panels,
        parts=(first.panels, second.panels),
    )
    result = apply_thickness_reduction(
        (combined,), ThicknessReductionRequest("physical"), PCB_CONTEXT
    )
    assert z_levels(result.conductors[0]) == pytest.approx(
        (0.0, 35e-6, 1e-3, 1.035e-3)
    )
    assert result.provenance["qualification_state"] == "outside_fixture_envelope"
    expansion = expand_conductor_components(result.conductors)
    assert tuple(surface.name for surface in expansion.conductors) == (
        "net__component_001", "net__component_002"
    )
    assert expansion.assignments == {
        "net__component_001": "net",
        "net__component_002": "net",
    }
    assert expansion.group_order == ("net",)


def test_artifact_labels_distinguish_representation_material_and_lifecycle():
    physical = ThicknessReductionRequest("physical")
    effective = ThicknessReductionRequest("effective_thickness", 34e-6)
    assert diagnostic_artifact_stem(
        PCB_CONTEXT, physical
    ) == "filled_zones_physical_air_only_diagnostic"
    full_stackup = ReductionContext("filled_zones_only", "full_stackup")
    assert diagnostic_artifact_stem(
        full_stackup, effective
    ) == "filled_zones_effective_34um_midplane_full_stackup_diagnostic"
    assert diagnostic_artifact_stem(
        PCB_CONTEXT, effective, "base_components"
    ) == (
        "filled_zones_effective_34um_midplane_base_components_"
        "air_only_diagnostic"
    )
    with pytest.raises(ValueError, match="material scope"):
        ReductionContext("filled_zones_only", "fr4ish")


def test_deck_provenance_hash_fails_closed_on_tampering(tmp_path):
    deck = tmp_path / "model.lst"
    provenance = {"representation": "effective_thickness", "value": 34e-6}
    write_fastercap_input(
        deck, parallel_plate_fixture(), provenance=provenance
    )
    manifest_path = tmp_path / "model.lst.manifest.json"
    deck_hash = hashlib.sha256(deck.read_bytes()).hexdigest()
    validated = validate_deck_manifest(
        manifest_path, deck_sha256=deck_hash, deck_path=deck
    )
    assert validated.raw["provenance"] == provenance
    manifest = json.loads(manifest_path.read_text())
    manifest["provenance"]["value"] = 1e-6
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="provenance does not match"):
        validate_deck_manifest(
            manifest_path, deck_sha256=deck_hash, deck_path=deck
        )


def test_content_addressed_manifest_hashes_canonical_payload(tmp_path):
    payload = {"format": "test", "value": [1, 2, 3]}
    path = write_content_addressed_manifest(tmp_path, "geometry", payload)
    document = json.loads(path.read_text())
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    assert document["content_id_sha256"] == digest
    assert digest in path.name
