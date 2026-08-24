#!/usr/bin/env python3
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
gmsh = pytest.importorskip("gmsh")

from palace_mesh import (  # noqa: E402
    BoxBounds,
    ConductorPrism,
    DielectricBox,
    DielectricPrism,
    generate_palace_mesh,
    validate_palace_mesh_content,
    validate_palace_mesh_manifest,
)
from provenance import canonical_sha256, file_sha256  # noqa: E402


def _square_prism(name, z_min, z_max):
    return ConductorPrism(
        name,
        (((-0.1e-3, -0.1e-3), (0.1e-3, -0.1e-3),
          (0.1e-3, 0.1e-3), (-0.1e-3, 0.1e-3)),),
        z_min,
        z_max,
    )


def _offset_square_prism(name, x_center, z_min, z_max):
    half = 0.1e-3
    return ConductorPrism(
        name,
        (((x_center - half, -half), (x_center + half, -half),
          (x_center + half, half), (x_center - half, half)),),
        z_min,
        z_max,
    )


def _generate(path, *, dielectrics=()):
    return generate_palace_mesh(
        path,
        outer_bounds=BoxBounds((-1e-3, -1e-3, -1e-3), (1e-3, 1e-3, 1e-3)),
        conductors=(
            _square_prism("plate_low", -60e-6, -40e-6),
            _square_prism("plate_high", 40e-6, 60e-6),
        ),
        dielectrics=dielectrics,
        mesh_size_near_m=50e-6,
        mesh_size_far_m=300e-6,
        transition_distance_m=400e-6,
    )


def test_air_fixture_mesh_has_explicit_terminals_material_and_reference(tmp_path):
    result = _generate(tmp_path / "fixture.msh")
    assert result.terminal_attributes == (("plate_low", 101), ("plate_high", 102))
    assert result.material_attributes == (("outer", 1, 1.0),)
    assert result.ground_attribute == 9999
    assert result.node_count > 0
    assert result.tetrahedron_count > 0
    stored = json.loads(result.manifest_path.read_text())
    provenance = stored["provenance"]
    assert stored["provenance_sha256"] == canonical_sha256(provenance)
    assert provenance["mesh_sha256"] == file_sha256(result.mesh_path)
    assert provenance["reference_semantics"] == {
        "convergence_ladder_required": True,
        "kind": "finite_outer_dirichlet_approximation",
        "not_a_circuit_node": True,
    }


def test_mesh_bytes_match_manifest_geometry_groups_and_counts(tmp_path):
    result = _generate(tmp_path / "fixture.msh")
    provenance = json.loads(result.manifest_path.read_text())["provenance"]
    validate_palace_mesh_content(result.mesh_path, provenance)
    provenance["outer_bounds"]["maximum"][0] += 1e-6
    with pytest.raises(ValueError, match="bounds differ"):
        validate_palace_mesh_content(result.mesh_path, provenance)


def test_disconnected_prisms_share_one_terminal_group(tmp_path):
    result = generate_palace_mesh(
        tmp_path / "fixture.msh",
        outer_bounds=BoxBounds((-2e-3, -1e-3, -1e-3), (2e-3, 1e-3, 1e-3)),
        conductors=(
            _offset_square_prism("terminal_a", -0.5e-3, -60e-6, -40e-6),
            _offset_square_prism("terminal_a", 0.0, -60e-6, -40e-6),
            _offset_square_prism("terminal_b", 0.5e-3, 40e-6, 60e-6),
        ),
        mesh_size_near_m=50e-6,
        mesh_size_far_m=300e-6,
        transition_distance_m=400e-6,
    )
    assert result.terminal_attributes == (("terminal_a", 101), ("terminal_b", 102))
    stored = validate_palace_mesh_manifest(result.manifest_path)
    assert len(stored["provenance"]["conductors"]) == 3
    validate_palace_mesh_content(result.mesh_path, stored["provenance"])


def test_polygonal_dielectric_parts_share_one_material_group(tmp_path):
    rings = (((-0.8e-3, -0.5e-3), (0.8e-3, -0.5e-3),
              (0.8e-3, 0.5e-3), (-0.8e-3, 0.5e-3)),)
    dielectric = DielectricPrism("fr4", rings, -0.2e-3, 0.2e-3, 4.2)
    result = _generate(tmp_path / "fixture.msh", dielectrics=(dielectric,))
    assert result.material_attributes == (("outer", 1, 1.0), ("fr4", 2, 4.2))
    stored = validate_palace_mesh_manifest(result.manifest_path)
    assert stored["provenance"]["dielectrics"][0]["rings"]
    validate_palace_mesh_content(result.mesh_path, stored["provenance"])


def test_same_name_dielectric_parts_require_one_permittivity(tmp_path):
    bounds = BoxBounds((-0.8e-3, -0.5e-3, -0.2e-3),
                       (0.8e-3, 0.5e-3, 0.0))
    with pytest.raises(ValueError, match="share permittivity"):
        _generate(tmp_path / "fixture.msh", dielectrics=(
            DielectricBox("fr4", bounds, 4.2),
            DielectricBox(
                "fr4",
                BoxBounds((-0.8e-3, -0.5e-3, 0.0),
                          (0.8e-3, 0.5e-3, 0.2e-3)),
                4.3,
            ),
        ))


def test_dielectric_fixture_retains_conformal_material_volume(tmp_path):
    dielectric = DielectricBox(
        "fr4",
        BoxBounds((-0.5e-3, -0.5e-3, -0.2e-3), (0.5e-3, 0.5e-3, 0.2e-3)),
        4.2,
    )
    result = _generate(tmp_path / "fixture.msh", dielectrics=(dielectric,))
    assert result.material_attributes == (("outer", 1, 1.0), ("fr4", 2, 4.2))
    gmsh.initialize()
    try:
        gmsh.open(str(result.mesh_path))
        groups = {
            (dimension, attribute): gmsh.model.getPhysicalName(dimension, attribute)
            for dimension, attribute in gmsh.model.getPhysicalGroups()
        }
    finally:
        gmsh.finalize()
    assert groups[(3, 1)] == "outer"
    assert groups[(3, 2)] == "fr4"
    assert groups[(2, 101)] == "plate_low"
    assert groups[(2, 102)] == "plate_high"
    assert groups[(2, 9999)] == "electrostatic_infinity_boundary"


@pytest.mark.parametrize("mutation", [
    "circuit_reference", "numeric_reference", "boolean_threads",
    "outer_material_mismatch", "nonnumeric_bound", "boolean_bound",
    "nonnumeric_conductor", "boolean_conductor", "fabricated_edge_count",
])
def test_rebound_unsafe_mesh_provenance_is_rejected(tmp_path, mutation):
    result = _generate(tmp_path / "fixture.msh")
    stored = json.loads(result.manifest_path.read_text())
    provenance = stored["provenance"]
    if mutation == "circuit_reference":
        provenance["reference_semantics"] = {
            "kind": "circuit_node",
            "convergence_ladder_required": False,
            "not_a_circuit_node": False,
        }
    elif mutation == "numeric_reference":
        provenance["reference_semantics"]["convergence_ladder_required"] = 1
        provenance["reference_semantics"]["not_a_circuit_node"] = 1
    elif mutation == "boolean_threads":
        provenance["mesh_parameters"]["threads"] = True
    elif mutation == "outer_material_mismatch":
        provenance["outer_permittivity"] = 2
    elif mutation == "nonnumeric_bound":
        provenance["outer_bounds"]["minimum"][0] = None
    elif mutation == "boolean_bound":
        provenance["outer_bounds"]["maximum"][0] = True
    elif mutation == "nonnumeric_conductor":
        provenance["conductors"][0]["z_min"] = "bad"
    elif mutation == "boolean_conductor":
        provenance["conductors"][0]["rings"][0][1][0] = True
    else:
        provenance["edge_count"] -= 1
    stored["provenance_sha256"] = canonical_sha256(provenance)
    result.manifest_path.write_text(json.dumps(stored))
    with pytest.raises(ValueError):
        validate_palace_mesh_manifest(
            result.manifest_path, mesh_path=result.mesh_path
        )


def test_boolean_material_and_geometry_scalars_are_rejected():
    with pytest.raises(ValueError, match="numeric scalar"):
        BoxBounds((-1.0, -1.0, -1.0), (True, 1.0, 1.0))
    with pytest.raises(ValueError, match="numeric scalar"):
        _square_prism("sheet", False, 1.0)
    with pytest.raises(ValueError, match="numeric scalar"):
        DielectricBox(
            "material", BoxBounds((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)), True
        )


def test_open_or_zero_thickness_conductors_are_rejected():
    with pytest.raises(ValueError, match="positive thickness"):
        _square_prism("sheet", 0.0, 0.0)
