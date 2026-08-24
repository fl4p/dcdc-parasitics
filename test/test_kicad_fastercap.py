#!/usr/bin/env python3
import hashlib
import json
import os
import stat
import subprocess
import sys

import numpy as np
import pytest
from shapely.geometry import Polygon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))

import extract_capacitance  # noqa: E402
from fastercap import extruded_triangulation_surface  # noqa: E402
from fastercap_reduction import load_geometry_manifest  # noqa: E402
from kicad_fastercap import (  # noqa: E402
    QUALITY_MESH_MIN_ANGLE_DEG,
    _condition_geometry,
    _minimum_triangle_angle_deg,
    _record_geometry,
    _snap_to_kicad_grid,
    copper_layer_bounds,
    parse_stackup,
    surfaces_from_filled_zone_dump,
    triangulate_polygonal,
)
from kicad_fastercap_schema import parse_group  # noqa: E402


STACKUP = """
(kicad_pcb
  (setup
    (stackup
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 1.51)
        (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
    )
  )
)
"""


def test_group_parser_preserves_multiple_kicad_nets():
    assert parse_group("RETURN=PGND,GND") == ("RETURN", ("PGND", "GND"))
    with pytest.raises(ValueError, match="NAME=NET"):
        parse_group("RETURN")


def test_parse_stackup_preserves_order_material_and_z(tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text(STACKUP)
    layers = parse_stackup(board_path)
    assert [layer.name for layer in layers] == [
        "F.Mask", "F.Cu", "dielectric 1", "B.Cu", "B.Mask"
    ]
    core = layers[2]
    assert core.kind == "core"
    assert core.epsilon_r == 4.5
    assert core.z_top_mm == pytest.approx(-0.045)
    assert core.z_bottom_mm == pytest.approx(-1.555)
    assert copper_layer_bounds(layers) == {
        "F.Cu": pytest.approx((-0.045, -0.01)),
        "B.Cu": pytest.approx((-1.59, -1.555)),
    }


def test_stackup_parser_ignores_parentheses_inside_strings(tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text(STACKUP.replace('"FR4"', '"FR4 (high-Tg)"'))
    layers = parse_stackup(board_path)
    assert len(layers) == 5


def test_polygon_triangulation_covers_concavity_and_hole():
    polygon = Polygon(
        ((0, 0), (5, 0), (5, 5), (0, 5)),
        holes=(((1, 1), (4, 1), (4, 4), (1, 4)),),
    )
    triangles = triangulate_polygonal(polygon)
    triangle_area = sum(Polygon(triangle).area for triangle in triangles)
    assert triangle_area == pytest.approx(polygon.area, rel=1e-12)
    assert min(map(_minimum_triangle_angle_deg, triangles)) >= (
        QUALITY_MESH_MIN_ANGLE_DEG
    )
    surface = extruded_triangulation_surface("ring", triangles, 0.0, 0.035)
    assert surface.panels


def test_empty_polygon_triangulation_is_empty():
    assert triangulate_polygonal(Polygon()) == ()


def test_dump_module_import_has_no_system_geometry_dependencies():
    code = f"""
import builtins, sys
sys.path.insert(0, {os.path.join(ROOT, 'lib')!r})
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name.split('.')[0] in {{'numpy', 'shapely', 'fastercap'}}:
        raise ImportError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
import kicad_fastercap_dump
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_zone_dump_builds_grouped_multilayer_surface_in_metres(tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text(STACKUP)
    dump = {
        "format": "dcdc-fastercap-zones-v1",
        "groups": ["SW"],
        "records": [
            {
                "group": "SW",
                "net": "SW",
                "layer": "F.Cu",
                "shell": ((0, 0), (2, 0), (2, 1), (0, 1)),
                "holes": (),
            },
            {
                "group": "SW",
                "net": "SW",
                "layer": "B.Cu",
                "shell": ((0, 0), (2, 0), (2, 1), (0, 1)),
                "holes": (),
            },
        ],
    }
    result = surfaces_from_filled_zone_dump(dump, parse_stackup(board_path))
    assert result.source_mode == "filled_zones_only"
    assert result.area_mm2 == {"SW": pytest.approx(4.0)}
    assert result.polygon_count == {"SW": 2}
    assert result.triangle_count == {"SW": 4}
    assert result.component_count == {"SW": 2}
    assert result.repaired_polygon_count == {"SW": 0}
    assert result.geometry_tolerance_mm == 1e-3
    assert result.simplification_area_error_mm2 == {"SW": pytest.approx(0.0)}
    assert result.minimum_boundary_segment_mm == {"SW": pytest.approx(1.0)}
    assert len(result.surfaces) == 1
    surface = result.surfaces[0]
    assert surface.name == "SW"
    assert len(surface.panels) == 16
    assert len(surface.parts) == 2
    coordinates = np.asarray([point for panel in surface.panels for point in panel])
    assert coordinates[:, 0].max() == pytest.approx(0.002)
    assert sorted(np.unique(coordinates[:, 2])) == pytest.approx([
        -0.00159, -0.001555, -0.000045, -0.00001
    ])


def test_invalid_contour_repair_preserves_area_and_is_counted():
    record = {
        "shell": (
            (0, 0), (1, 0), (1, 1), (0, 1), (0, 0),
            (2, 0), (3, 0), (3, 1), (2, 1), (2, 0), (0, 0),
        ),
        "holes": (),
    }
    geometry, repaired = _record_geometry(record)
    assert repaired == 1
    assert geometry.area == pytest.approx(2.0)


def test_grid_snap_rejects_area_loss():
    narrow = Polygon(((0, 0), (1, 0), (1, 0.0000004), (0, 0.0000004)))
    with pytest.raises(ValueError, match="KiCad grid changed its area"):
        _snap_to_kicad_grid(narrow)


def test_simplification_rejects_area_change_and_short_hole_edges():
    bumped = Polygon(((0, 0), (1, 0), (1, 1), (0.5, 1.001), (0, 1)))
    with pytest.raises(ValueError, match="area beyond its gate"):
        _condition_geometry(bumped, 0.01, 0.0)

    tiny_hole = Polygon(
        ((0, 0), (2, 0), (2, 2), (0, 2)),
        holes=(((0.9, 0.9), (0.9004, 0.9),
                (0.9004, 0.9004), (0.9, 0.9004)),),
    )
    with pytest.raises(ValueError, match="below half its tolerance"):
        _condition_geometry(tiny_hole, 0.001, 1e-4)


def test_cli_binds_dump_provenance_and_emits_si_deck(tmp_path, monkeypatch):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text(STACKUP)
    output = tmp_path / "out"
    fake_kicad = tmp_path / "fake-kicad-python"
    dump = {
        "format": "dcdc-fastercap-zones-v1",
        "groups": ["SW"],
        "records": [{
            "group": "SW", "net": "SW", "layer": "F.Cu",
            "shell": ((0, 0), (2, 0), (2, 1), (0, 1)), "holes": (),
        }],
        "kicad_version": "test-kicad",
        "python_executable": "/test/kicad/python",
    }
    fake_kicad.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"dump={dump!r}\n"
        "path=sys.argv[sys.argv.index('--output')+1]\n"
        "json.dump(dump,open(path,'w'))\n"
        "print('fake dump complete')\n"
    )
    fake_kicad.chmod(fake_kicad.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(extract_capacitance, "KICAD_PY", str(fake_kicad))

    assert extract_capacitance.main([
        str(board_path), "--group", "SW=SW", "-o", str(output)
    ]) == 0
    metadata_paths = list(output.glob(
        "filled_zones_physical_air_only_diagnostic.geometry.*.json"
    ))
    assert len(metadata_paths) == 1
    metadata = json.loads(metadata_paths[0].read_text())
    content_id = metadata.pop("content_id_sha256")
    canonical_metadata = json.dumps(
        metadata, sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(canonical_metadata).hexdigest() == content_id
    assert content_id in metadata_paths[0].name
    assert load_geometry_manifest(metadata_paths[0])["content_id_sha256"] == (
        content_id
    )
    assert metadata["filled_zone_record_count"] == 1
    assert metadata["filled_zone_dump_sha256"]
    assert metadata["kicad_version"] == "test-kicad"
    assert metadata["kicad_python"] == "/test/kicad/python"
    deck = output / "filled_zones_physical_air_only_diagnostic.lst"
    panel = next(
        line for line in deck.read_text().splitlines()
        if line.startswith(("T ", "Q "))
    )
    assert max(abs(float(value)) for value in panel.split()[2:]) <= 0.002
    assert metadata["reduction"]["representation"] == "physical"
    assert metadata["reduction"]["physical_validation_authorized"] is False
    assert metadata["reduction"]["artifact_class"] == {
        "source": "filled_zones_only",
        "material_scope": "air_only",
        "lifecycle": "diagnostic",
        "physical_model_validated": False,
        "conductor_policy": "grouped",
    }
    manifest = json.loads((output / f"{deck.name}.manifest.json").read_text())
    provenance = manifest["provenance"]
    canonical = json.dumps(
        provenance, sort_keys=True, separators=(",", ":")
    ).encode()
    assert manifest["provenance_sha256"] == hashlib.sha256(canonical).hexdigest()
    deck.write_text(deck.read_text() + "* tampered\n")
    with pytest.raises(ValueError, match="deck hash mismatch"):
        load_geometry_manifest(metadata_paths[0])

    reduced_output = tmp_path / "reduced"
    assert extract_capacitance.main([
        str(board_path), "--group", "SW=SW", "-o", str(reduced_output),
        "--representation", "effective-thickness",
        "--effective-thickness-um", "34", "--anchor", "midplane",
        "--separate-components",
    ]) == 0
    reduced_deck = (
        reduced_output /
        "filled_zones_effective_34um_midplane_base_components_"
        "air_only_diagnostic.lst"
    )
    coordinates = []
    for line in reduced_deck.read_text().splitlines():
        if line.startswith(("T ", "Q ")):
            values = [float(value) for value in line.split()[2:]]
            coordinates.extend(values[index] for index in range(2, len(values), 3))
    assert sorted(set(coordinates)) == pytest.approx((-44.5e-6, -10.5e-6))
    reduced_metadata_path, = reduced_output.glob(
        "filled_zones_effective_34um_midplane_base_components_"
        "air_only_diagnostic.geometry.*.json"
    )
    reduced_metadata = json.loads(reduced_metadata_path.read_text())
    assert reduced_metadata["reduction"]["qualification_state"] == (
        "outside_fixture_envelope"
    )
    assert reduced_metadata["reduction"]["source_thickness_m"]["SW"] == (
        pytest.approx([35e-6])
    )
    assert reduced_metadata["reduction"]["effective_thickness_m"] == (
        pytest.approx(34e-6)
    )
    assert reduced_metadata["reduction"]["lifecycle_ceiling"] == (
        "numerically_converged_diagnostic"
    )
    assert reduced_metadata["reduction"]["source_mode"] == "filled_zones_only"
    assert reduced_metadata["reduction"]["material_scope"] == "air_only"
    assembly = reduced_metadata["reduction"]["electrical_assembly"]
    assert assembly["solver_policy"] == "base_components"
    assert assembly["assignments"] == {"SW__component_001": "SW"}
    assert reduced_metadata["solver_conductor_names"] == [
        "SW__component_001"
    ]

    tampered = json.loads(reduced_metadata_path.read_text())
    tampered["reduction"]["qualification_state"] = "fixture_qualified"
    reduced_metadata_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="does not match its hash"):
        load_geometry_manifest(reduced_metadata_path)


def test_cli_reduction_arguments_fail_closed(tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text(STACKUP)
    base = [str(board_path), "--group", "SW=SW", "-o", str(tmp_path / "out")]
    with pytest.raises(SystemExit):
        extract_capacitance.main(
            base + ["--representation", "effective-thickness"]
        )
    with pytest.raises(SystemExit):
        extract_capacitance.main(base + ["--effective-thickness-um", "34"])
    with pytest.raises(SystemExit):
        extract_capacitance.main([
            *base, "--representation", "effective-thickness",
            "--effective-thickness-um", "nan",
        ])
    with pytest.raises(SystemExit):
        extract_capacitance.main([
            *base, "--representation", "effective-thickness",
            "--effective-thickness-um", "34", "--anchor", "centre",
        ])


def test_nonfinite_geometry_tolerances_are_rejected(tmp_path):
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text(STACKUP)
    dump = {
        "format": "dcdc-fastercap-zones-v1",
        "groups": ["SW"],
        "records": [],
    }
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            surfaces_from_filled_zone_dump(
                dump, parse_stackup(board_path), geometry_tolerance_mm=value
            )
        with pytest.raises(ValueError, match="finite"):
            surfaces_from_filled_zone_dump(
                dump, parse_stackup(board_path),
                simplification_area_rtol=value,
            )
