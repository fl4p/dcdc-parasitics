#!/usr/bin/env python3
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest
from shapely.geometry import Polygon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
pytest.importorskip("gmsh")

import kicad_fastercap  # noqa: E402
from kicad_fastercap import StackupLayer  # noqa: E402
from kicad_palace import (  # noqa: E402
    load_pcb_volume_dump,
    load_pcb_volumes,
    pcb_volume_source_identity,
    volumes_from_pcb_dump,
)
from kicad_palace_dump import (  # noqa: E402
    _all_copper_groups,
    _drill_record,
    _mapping,
)
from palace_mesh import (  # noqa: E402
    generate_palace_mesh,
    validate_palace_mesh_content,
    validate_palace_mesh_manifest,
)
from palace_plc_mesh import (  # noqa: E402
    generate_palace_plc_mesh,
    validate_palace_plc_mesh_manifest,
)
from provenance import canonical_sha256, file_sha256  # noqa: E402


def _stackup(*, core_epsilon: float | None = 4.2):
    return (
        StackupLayer("F.Cu", "copper", 0.035, None, 0.0, -0.035),
        StackupLayer(
            "dielectric 1", "core", 0.93, core_epsilon, -0.035, -0.965
        ),
        StackupLayer("B.Cu", "copper", 0.035, None, -0.965, -1.0),
    )


def _polygon(group, layer, x_min, x_max, *, source_uuid):
    return {
        "group": group,
        "net": group,
        "layer": layer,
        "source_kind": "pad",
        "source_uuid": source_uuid,
        "shell": [[x_min, -0.2], [x_max, -0.2],
                  [x_max, 0.2], [x_min, 0.2]],
        "holes": [],
    }


def _dump():
    return {
        "format": "dcdc-kicad-palace-volumes-v1",
        "groups": ["A", "B"],
        "grouping_policy": "explicit",
        "copper_layers": ["F.Cu", "B.Cu"],
        "records": [
            _polygon("A", "F.Cu", -0.8, -0.4, source_uuid="a-top"),
            _polygon("A", "B.Cu", -0.8, -0.4, source_uuid="a-bottom"),
            _polygon("B", "F.Cu", 0.4, 0.8, source_uuid="b-top"),
        ],
        "drills": [{
            "group": "A",
            "net": "A",
            "source_kind": "pad",
            "source_uuid": "a-hole",
            "center_mm": [-0.6, 0.0],
            "size_mm": [0.2, 0.2],
            "shape": "circle",
            "start_layer": "F.Cu",
            "end_layer": "B.Cu",
            "plated": True,
            "unsupported_features": [],
        }],
        "board_outlines": [{
            "shell": [[-1.0, -0.5], [1.0, -0.5], [1.0, 0.5], [-1.0, 0.5]],
            "holes": [],
        }],
        "census": {
            "included": {"track": 0, "via": 0, "pad": 3, "zone": 0},
            "unassigned": [],
            "unsupported": [],
        },
        "polygon_error_mm": 0.001,
        "kicad_version": "test",
        "python_executable": "/test/kicad/python",
        "source_pcb_path": "/test/board.kicad_pcb",
        "source_pcb_sha256": "0" * 64,
    }


def test_complete_dump_builds_layer_copper_barrel_and_dielectric_volumes():
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6
    )
    assert geometry.groups == ("A", "B")
    assert {item.name for item in geometry.conductors} == {"A", "B"}
    assert len([item for item in geometry.conductors if item.name == "A"]) == 3
    barrel = next(
        item for item in geometry.conductors
        if item.name == "A" and item.z_max - item.z_min > 0.5e-3
    )
    assert barrel.z_min == pytest.approx(-0.965e-3)
    assert barrel.z_max == pytest.approx(-0.035e-3)
    assert len(geometry.dielectrics) == 1
    assert geometry.dielectrics[0].name == "dielectric 1"
    assert len(geometry.dielectrics[0].rings) == 2
    assert geometry.outer_bounds.minimum[0] < -1e-3
    assert geometry.outer_bounds.maximum[0] > 1e-3


def _plated_via_plc_mesh(tmp_path):
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6
    )
    return generate_palace_plc_mesh(
        tmp_path / "fixture.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
    )


def test_plated_via_volumes_generate_exact_conformal_plc_mesh(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    stored = validate_palace_plc_mesh_manifest(result.manifest_path)
    dispatched = validate_palace_mesh_manifest(result.manifest_path)
    assert stored == dispatched
    assert result.terminal_attributes == (("A", 101), ("B", 102))
    assert result.material_attributes == (
        ("outer", 1, 1.0), ("dielectric 1", 2, 4.2)
    )
    assert result.node_count > 0
    assert result.tetrahedron_count > 0


def test_refined_plc_preserves_source_segments_and_planar_area(tmp_path):
    geometry = volumes_from_pcb_dump(
        _dump(), _stackup(), plating_thickness_m=25e-6
    )
    max_area = 1e-8
    result = generate_palace_plc_mesh(
        tmp_path / "refined.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
        max_planar_area_m2=max_area,
        max_vertical_step_m=2e-4,
    )
    stored = validate_palace_plc_mesh_manifest(result.manifest_path)
    parameters = stored["provenance"]["mesh_parameters"]
    assert parameters["max_planar_area_m2"] == max_area
    assert parameters["allow_volume_steiner"] is False
    assert parameters["source_segment_max_length_m"] == math.sqrt(2 * max_area)
    assert result.node_count > _plated_via_plc_mesh(tmp_path / "coarse").node_count


def test_plc_manifest_rejects_rehashed_semantic_and_identity_tampering(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    original = json.loads(result.manifest_path.read_text())
    mutations = (
        (lambda value: value["provenance"]["mesh_parameters"].update(
            max_vertical_step_m=True
        ), "mesh parameters"),
        (lambda value: value["provenance"]["mesh_parameters"].update(
            allow_boundary_steiner=True
        ), "mesh parameters"),
        (lambda value: value["provenance"]["mesh_parameters"].update(
            allow_volume_steiner=True
        ), "mesh parameters"),
        (lambda value: value["provenance"]["mesh_parameters"].update(
            planar_quantum_m=2 * value["provenance"]["mesh_parameters"][
                "planar_quantum_m"
            ]
        ), "mesh parameters"),
        (lambda value: value["provenance"]["mesh_parameters"].update(
            source_segment_max_length_m=1e-3
        ), "mesh parameters"),
        (lambda value: value["provenance"]["terminal_attributes"][0].__setitem__(
            0, "substituted"
        ), "terminal attributes"),
        (lambda value: value["provenance"]["mesher"].update(
            native_extension_sha256="0" * 64
        ), "native_extension hash mismatch"),
        (lambda value: value["provenance"].update(
            minimum_tetrahedron_determinant_m3=True
        ), "Jacobian witness"),
        (lambda value: value["provenance"]["source_identity"].update(
            unbound=True
        ), "source identity has unknown fields"),
    )
    for mutate, message in mutations:
        value = json.loads(json.dumps(original))
        mutate(value)
        value["provenance_sha256"] = canonical_sha256(value["provenance"])
        result.manifest_path.write_text(json.dumps(value))
        with pytest.raises(ValueError, match=message):
            validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_manifest_binds_kicad_dump_pcb_stackup_and_controls(
        tmp_path, monkeypatch):
    dump_path = tmp_path / "dump.json"
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("synthetic pcb")
    dump = _dump()
    dump["source_pcb_path"] = str(pcb_path.resolve())
    dump["source_pcb_sha256"] = file_sha256(pcb_path)
    dump_path.write_text(json.dumps(dump))
    stackup = _stackup()
    monkeypatch.setattr(kicad_fastercap, "parse_stackup", lambda _: stackup)
    geometry = load_pcb_volumes(
        dump_path, stackup, plating_thickness_m=25e-6
    )
    source = pcb_volume_source_identity(
        geometry, dump_path, pcb_path, stackup
    )
    result = generate_palace_plc_mesh(
        tmp_path / "fixture.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
        source_identity=source,
    )
    stored = validate_palace_plc_mesh_manifest(result.manifest_path)
    assert stored["provenance"]["source_identity"]["kind"] == "kicad_volume_dump"
    assert stored["provenance"]["mesh_parameters"]["planar_quantum_m"] == 5e-7
    quantum = stored["provenance"]["mesh_parameters"]["planar_quantum_m"]
    outer = stored["provenance"]["outer_bounds"]
    planar_values = [
        value
        for prism in (
            *stored["provenance"]["conductors"],
            *stored["provenance"]["dielectrics"],
        )
        for ring in prism["rings"]
        for point in ring
        for value in point
    ]
    assert all(
        abs(value / quantum - round(value / quantum)) < 1e-9
        for value in (
            *outer["minimum"][:2], *outer["maximum"][:2], *planar_values
        )
    )
    substituted = json.loads(json.dumps(stored))
    substituted["provenance"]["source_identity"]["geometry_tolerance_mm"] *= 2
    substituted["provenance_sha256"] = canonical_sha256(
        substituted["provenance"]
    )
    result.manifest_path.write_text(json.dumps(substituted))
    with pytest.raises(ValueError, match="source reconstruction"):
        validate_palace_plc_mesh_manifest(result.manifest_path)
    substituted = json.loads(json.dumps(stored))
    substituted["provenance"]["source_identity"]["coordinate_grid_mm"] *= 2
    substituted["provenance"]["mesh_parameters"]["planar_quantum_m"] *= 2
    substituted["provenance_sha256"] = canonical_sha256(
        substituted["provenance"]
    )
    result.manifest_path.write_text(json.dumps(substituted))
    with pytest.raises(ValueError, match="coordinate grid differs"):
        validate_palace_plc_mesh_manifest(result.manifest_path)
    result.manifest_path.write_text(json.dumps(stored))
    pcb_path.write_text("tampered pcb")
    with pytest.raises(ValueError, match="source file identity mismatch"):
        validate_palace_plc_mesh_manifest(result.manifest_path)



def _rebind_plc_mesh(result, lines, mutate_provenance=None):
    result.mesh_path.write_text("\n".join(lines) + "\n")
    stored = json.loads(result.manifest_path.read_text())
    provenance = stored["provenance"]
    provenance["mesh_sha256"] = file_sha256(result.mesh_path)
    if mutate_provenance is not None:
        mutate_provenance(provenance)
    stored["provenance_sha256"] = canonical_sha256(provenance)
    result.manifest_path.write_text(json.dumps(stored))


def _element_line_indices(lines, element_type):
    start = lines.index("$Elements")
    count = int(lines[start + 1])
    return [
        index for index in range(start + 2, start + 2 + count)
        if lines[index].split()[1] == str(element_type)
    ]


def test_plc_validator_rejects_rebound_inverted_tetrahedron(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    index = _element_line_indices(lines, 4)[0]
    fields = lines[index].split()
    fields[-1], fields[-2] = fields[-2], fields[-1]
    lines[index] = " ".join(fields)
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="nonpositive tetrahedron"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_material_misclassification(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    node_start = lines.index("$Nodes")
    node_count = int(lines[node_start + 1])
    coordinates = {
        int(fields[0]): tuple(float(value) for value in fields[1:])
        for fields in (
            lines[index].split()
            for index in range(node_start + 2, node_start + 2 + node_count)
        )
    }
    indices = _element_line_indices(lines, 4)
    index = next(
        index for index in indices
        if lines[index].split()[3] == "2"
        and all(
            -0.9e-3 < coordinates[node][0] < 0.9e-3
            and -0.4e-3 < coordinates[node][1] < 0.4e-3
            for node in map(int, lines[index].split()[-4:])
        )
    )
    fields = lines[index].split()
    fields[3] = fields[4] = "1"
    lines[index] = " ".join(fields)
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="material is misclassified"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_missing_boundary_face(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    start = lines.index("$Elements")
    index = _element_line_indices(lines, 2)[0]
    del lines[index]
    lines[start + 1] = str(int(lines[start + 1]) - 1)
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="boundaries do not close"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_false_jacobian_witness(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    _rebind_plc_mesh(
        result,
        lines,
        lambda provenance: provenance.update(
            minimum_tetrahedron_determinant_m3=(
                2 * provenance["minimum_tetrahedron_determinant_m3"]
            )
        ),
    )
    with pytest.raises(ValueError, match="witness differs from mesh bytes"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_sub_grid_planar_displacement(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    node_start = lines.index("$Nodes")
    node_count = int(lines[node_start + 1])
    index = next(
        index
        for index in range(node_start + 2, node_start + 2 + node_count)
        if abs(float(lines[index].split()[1])) < 0.9e-3
        and abs(float(lines[index].split()[2])) < 0.4e-3
    )
    selected = tuple(float(value) for value in lines[index].split()[1:3])
    changed = 0
    for index in range(node_start + 2, node_start + 2 + node_count):
        fields = lines[index].split()
        if tuple(float(value) for value in fields[1:3]) == selected:
            fields[1] = f"{float(fields[1]) + 1e-12:.17g}"
            lines[index] = " ".join(fields)
            changed += 1
    assert changed > 1
    _rebind_plc_mesh(result, lines)
    with pytest.raises(
        ValueError,
        match="omits a noded source boundary|crosses a noded source boundary",
    ):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_sub_tolerance_z_interface(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    node_start = lines.index("$Nodes")
    node_count = int(lines[node_start + 1])
    changed = 0
    for index in range(node_start + 2, node_start + 2 + node_count):
        fields = lines[index].split()
        if float(fields[3]) == -0.000965:
            fields[3] = f"{float(fields[3]) + 1e-15:.17g}"
            lines[index] = " ".join(fields)
            changed += 1
    assert changed > 0
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="unknown z-plane"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_validator_rejects_rebound_partial_z_interface_warp(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    lines = result.mesh_path.read_text().splitlines()
    node_start = lines.index("$Nodes")
    node_count = int(lines[node_start + 1])
    index = next(
        index
        for index in range(node_start + 2, node_start + 2 + node_count)
        if float(lines[index].split()[3]) == -0.000965
    )
    fields = lines[index].split()
    fields[3] = f"{float(fields[3]) + 1e-15:.17g}"
    lines[index] = " ".join(fields)
    _rebind_plc_mesh(result, lines)
    with pytest.raises(ValueError, match="unknown z-plane"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_plc_manifest_rejects_mesh_byte_tampering(tmp_path):
    result = _plated_via_plc_mesh(tmp_path)
    result.mesh_path.write_text(result.mesh_path.read_text() + "tamper\n")
    with pytest.raises(ValueError, match="mesh hash mismatch"):
        validate_palace_plc_mesh_manifest(result.manifest_path)


def test_planar_board_volumes_generate_grouped_conformal_palace_mesh(tmp_path):
    dump = _dump()
    dump["drills"] = []
    geometry = volumes_from_pcb_dump(
        dump, _stackup(), plating_thickness_m=25e-6
    )
    result = generate_palace_mesh(
        tmp_path / "fixture.msh",
        outer_bounds=geometry.outer_bounds,
        conductors=geometry.conductors,
        dielectrics=geometry.dielectrics,
        mesh_size_near_m=0.1e-3,
        mesh_size_far_m=0.5e-3,
        transition_distance_m=1e-3,
    )
    assert result.terminal_attributes == (("A", 101), ("B", 102))
    assert result.material_attributes == (
        ("outer", 1, 1.0), ("dielectric 1", 2, 4.2)
    )
    stored = validate_palace_mesh_manifest(result.manifest_path)
    validate_palace_mesh_content(result.mesh_path, stored["provenance"])


class _Uuid:
    def __init__(self, value):
        self.value = value

    def AsString(self):
        return self.value


class _Pad:
    def __init__(self, uuid, net, *, flashed=True):
        self.m_Uuid = _Uuid(uuid)
        self.net = net
        self.flashed = flashed

    def GetNetname(self):
        return self.net

    def IsOnLayer(self, layer):
        return layer == 0

    def FlashLayer(self, layer):
        return self.flashed and layer == 0


class _Zone:
    def GetIsRuleArea(self):
        return False


class _Layers:
    def CuStack(self):
        return (0,)


class _Board:
    def __init__(self, pads):
        self.pads = pads

    def GetEnabledLayers(self):
        return _Layers()

    def GetTracks(self):
        return ()

    def GetPads(self):
        return self.pads

    def Zones(self):
        return ()


class _Pcbnew:
    PAD = _Pad
    ZONE = _Zone
    PAD_DRILL_SHAPE_CIRCLE = 1
    PAD_DRILL_SHAPE_OBLONG = 2
    PAD_ATTRIB_NPTH = 1

    @staticmethod
    def ToMM(value):
        return value / 1e6


class _Size:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _DrilledPad(_Pad):
    def HasDrilledHole(self):
        return True

    def GetPrimaryDrillSize(self):
        return _Size(6_000_000, 1_000_000)

    def GetPrimaryDrillStartLayer(self):
        return 0

    def GetPrimaryDrillEndLayer(self):
        return 1

    def GetAttribute(self):
        return 0

    def GetPrimaryDrillShape(self):
        return _Pcbnew.PAD_DRILL_SHAPE_OBLONG

    def GetPosition(self):
        return _Size(2_000_000, 3_000_000)

    def IsBackdrilledOrPostMachined(self, _):
        return False

    def GetPrimaryDrillCappedFlag(self):
        return False

    def GetPrimaryDrillFilledFlag(self):
        return False


class _DrillBoard:
    @staticmethod
    def GetLayerName(layer):
        return ("F.Cu", "B.Cu")[layer]


def test_all_copper_discovery_separates_flashed_isolated_items():
    groups, item_groups = _all_copper_groups(
        _Board((_Pad("named", "N"), _Pad("isolated", ""),
                _Pad("npth", "", flashed=False))),
        _Pcbnew,
    )
    assert groups == {"N": ("N",), "isolated:isolated": ()}
    assert item_groups == {"isolated:isolated": ("isolated",)}


def test_pcbnew_boundary_records_oblong_drill_shape():
    record = _drill_record(
        _DrilledPad("slot", "N"), "pad", _DrillBoard(), "N", _Pcbnew
    )
    assert record["shape"] == "oblong"
    assert record["size_mm"] == (6.0, 1.0)
    assert record["unsupported_features"] == []


def test_all_copper_mapping_keeps_isolated_items_out_of_net_groups():
    names, net_to_group, item_to_group = _mapping(
        {"N": ("N",), "isolated:item-1": ()},
        {"isolated:item-1": ("item-1",)},
    )
    assert names == ("N", "isolated:item-1")
    assert net_to_group == {"N": "N"}
    assert item_to_group == {"item-1": "isolated:item-1"}


def test_oblong_plated_drill_builds_capsule_barrel_not_ellipse():
    dump = _dump()
    dump["drills"][0]["size_mm"] = [0.6, 0.2]
    dump["drills"][0]["shape"] = "oblong"
    geometry = volumes_from_pcb_dump(
        dump, _stackup(), plating_thickness_m=25e-6
    )
    barrel = next(
        item for item in geometry.conductors
        if item.name == "A" and item.z_max - item.z_min > 0.5e-3
    )
    bounds = Polygon(barrel.rings[0], barrel.rings[1:]).bounds
    assert bounds[2] - bounds[0] == pytest.approx(0.65e-3)
    assert bounds[3] - bounds[1] == pytest.approx(0.25e-3)
    barrel_area = Polygon(barrel.rings[0], barrel.rings[1:]).area
    expected_area = (
        (0.65 - 0.25) * 0.25 + math.pi * 0.125**2
        - (0.6 - 0.2) * 0.2 - math.pi * 0.1**2
    ) * 1e-6
    assert barrel_area == pytest.approx(expected_area, rel=2e-3)


def test_npth_drill_without_group_remains_a_void_not_a_terminal():
    dump = _dump()
    dump["drills"][0]["group"] = None
    dump["drills"][0]["plated"] = False
    geometry = volumes_from_pcb_dump(
        dump, _stackup(), plating_thickness_m=25e-6
    )
    assert len([item for item in geometry.conductors if item.name == "A"]) == 2
    assert len(geometry.dielectrics[0].rings) == 2


def test_incomplete_census_rejects_before_geometry_access():
    dump = _dump()
    dump["census"]["unassigned"].append({
        "source_kind": "track", "source_uuid": "missing", "net": "C"
    })
    dump["records"] = "must not be inspected"
    with pytest.raises(ValueError, match="unassigned copper"):
        volumes_from_pcb_dump(
            dump, _stackup(), plating_thickness_m=25e-6
        )


def test_missing_dielectric_permittivity_rejects():
    with pytest.raises(ValueError, match="no positive permittivity"):
        volumes_from_pcb_dump(
            _dump(), _stackup(core_epsilon=None), plating_thickness_m=25e-6
        )


def test_dump_loader_requires_exact_schema(tmp_path):
    path = tmp_path / "dump.json"
    value = _dump()
    value["unexpected"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="schema mismatch"):
        load_pcb_volume_dump(path)


def test_kicad_dump_import_uses_only_stdlib_and_pcbnew_boundary():
    code = f"""
import builtins, sys, types
sys.path.insert(0, {str(Path(ROOT) / 'lib')!r})
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name.split('.')[0] in {{'numpy', 'shapely', 'gmsh'}}:
        raise ImportError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
import kicad_palace_dump
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
